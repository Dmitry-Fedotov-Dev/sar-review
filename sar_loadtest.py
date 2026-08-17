"""Нагрузочное тестирование SAR Review.

Зачем это в проекте. Производительность здесь однажды чинили по рассуждению:
"6 одновременных юзеров и платформа ощутимо виснет" -> подняли число потоков
waitress, зарезервировали ядра под веб, поставили потолки на объём детекций.
Стало лучше, но НАСКОЛЬКО -- никто не измерял. Этот модуль закрывает пробел:
он даёт воспроизводимое число вместо ощущения, и показывает, ЧТО именно
медленное, а не "платформа тормозит".

Прозрачность -- главное требование к нему, поэтому:
  * перед запуском печатается полный план: адрес, сценарий по шагам,
    сколько пользователей, сколько длится. Ничего не делается втихую;
  * результат разбит ПО ЭНДПОИНТАМ, а не одним средним числом -- среднее по
    больнице скрывает, что список файлов тормозит, а превью летают;
  * показываются перцентили, а не среднее: p95 -- это то, что реально
    чувствует человек, среднее же красиво выглядит при любых провалах;
  * ошибки печатаются с кодами и примерами, а не прячутся в проценты;
  * сырые замеры сохраняются в JSON -- результат можно перепроверить.

ТОЛЬКО ЧТЕНИЕ. Модуль не пишет в базу: не ставит пометок, не комментирует,
не отправляет heartbeat. Гонять можно по работающей системе, ничего не
испортив. Единственная запись -- сессия в куках на стороне клиента.

Чего он НЕ меряет, чтобы не создавать ложной уверенности:
  * отдачу самого видео (Range-запросы гигабайтных файлов) -- это упирается
    в сеть и диск, меряется отдельно и другими инструментами;
  * скорость отрисовки в браузере -- здесь только время ответа сервера;
  * работу воркера/детектора -- он отдельный процесс, его нагрузка меряется
    не через HTTP.

Запуск:
    python sar_loadtest.py --users 6 --duration 60
    python sar_loadtest.py --users 20 --duration 120 --ramp 20
"""
import argparse
import http.cookiejar
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

# Шаги сценария: (имя, метод построения пути). Порядок повторяет то, что
# реально делает человек: открыл список -> открыл отчёт -> ушёл в плеер.
SCENARIO = [
    ("список файлов (страница)", "page_index"),
    ("список файлов (данные)", "api_tree"),
    ("превью", "api_thumbnail"),
    ("отчёт со сценами", "page_report"),
    ("сцены ИИ (API)", "api_ai_scenes"),
    ("плеер (страница)", "page_player"),
    ("покрытие просмотра", "api_coverage"),
    ("рамки ИИ (опрос плеера)", "api_ai_detections"),
]

DEFAULT_LOCAL = ("127.0.0.1", "localhost", "::1")


class Sample:
    __slots__ = ("step", "ms", "status", "err", "bytes")

    def __init__(self, step, ms, status, err=None, nbytes=0):
        self.step = step
        self.ms = ms
        self.status = status
        self.err = err
        self.bytes = nbytes


class VirtualUser(threading.Thread):
    """Один виртуальный зритель со своей сессией (своя кука, как у человека)."""

    def __init__(self, idx, base, password, targets, stop_at, out, start_delay=0.0):
        super().__init__(daemon=True)
        self.idx = idx
        self.base = base.rstrip("/")
        self.password = password
        self.targets = targets
        self.stop_at = stop_at
        self.out = out
        self.start_delay = start_delay
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False
        self.loops = 0

    # --- транспорт ---
    def _get(self, path, step):
        url = self.base + path
        t0 = time.perf_counter()
        try:
            with self.op.open(url, timeout=120) as r:
                body = r.read()
            ms = (time.perf_counter() - t0) * 1000
            return Sample(step, ms, r.status, None, len(body))
        except urllib.error.HTTPError as e:
            ms = (time.perf_counter() - t0) * 1000
            try:
                e.read()
            except Exception:
                pass
            return Sample(step, ms, e.code, f"HTTP {e.code}")
        except Exception as e:
            ms = (time.perf_counter() - t0) * 1000
            return Sample(step, ms, 0, f"{type(e).__name__}: {e}"[:120])

    def login(self):
        data = urllib.parse.urlencode(
            {"name": f"loadtest-{self.idx}", "password": self.password}).encode()
        req = urllib.request.Request(self.base + "/login", data=data)
        t0 = time.perf_counter()
        try:
            with self.op.open(req, timeout=60) as r:
                r.read()
            self.logged_in = True
            return Sample("вход", (time.perf_counter() - t0) * 1000, r.status)
        except Exception as e:
            return Sample("вход", (time.perf_counter() - t0) * 1000, 0,
                          f"{type(e).__name__}: {e}"[:120])

    # --- сценарий ---
    def run(self):
        if self.start_delay:
            time.sleep(self.start_delay)
        s = self.login()
        self.out.append(s)
        if not self.logged_in:
            return
        n = 0
        while time.time() < self.stop_at:
            # разные пользователи смотрят разные файлы -- иначе всё уляжется
            # в кеш ОС и мы измерим не систему, а один горячий путь
            rep = self.targets["reports"][(self.idx + n) % len(self.targets["reports"])]
            thumb = self.targets["thumbs"][(self.idx + n) % len(self.targets["thumbs"])]
            rid = urllib.parse.quote(rep["report_id"])
            paths = {
                "page_index": "/",
                "api_tree": "/api/tree",
                "api_thumbnail": "/api/thumbnail/" + urllib.parse.quote(thumb),
                "page_report": f"/report/{rid}/",
                "api_ai_scenes": f"/api/report/{rid}/ai_scenes",
                "page_player": f"/report/{rid}/player/",
                "api_coverage": f"/api/report/{rid}/coverage",
                "api_ai_detections": f"/api/report/{rid}/ai_detections",
            }
            for label, key in SCENARIO:
                if time.time() >= self.stop_at:
                    break
                self.out.append(self._get(paths[key], label))
            n += 1
            self.loops += 1


def discover(base, password):
    """Находит реальные файлы для сценария через API -- без доступа к БД."""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"name": "loadtest-scout", "password": password}).encode()
    with op.open(urllib.request.Request(base + "/login", data=data), timeout=60) as r:
        r.read()
    with op.open(base + "/api/tree", timeout=180) as r:
        tree = json.loads(r.read().decode("utf-8"))
    items = tree.get("items", [])
    reports = [i for i in items if i.get("status") == "done"] or items
    thumbs = [i["name"] for i in items] or ["none"]
    return {"reports": reports[:12], "thumbs": thumbs[:12], "total": len(items)}


def percentile(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def report(samples, wall, users, out_json=None):
    by = defaultdict(list)
    for s in samples:
        by[s.step].append(s)

    print()
    print("=" * 78)
    print("РЕЗУЛЬТАТ")
    print("=" * 78)
    hdr = f"{'шаг':<28}{'N':>5}{'p50':>8}{'p95':>9}{'p99':>9}{'макс':>9}{'ошиб':>7}"
    print(hdr)
    print("-" * 78)

    worst = None
    for label, _ in [("вход", None)] + SCENARIO:
        ss = by.get(label)
        if not ss:
            continue
        ok = [s.ms for s in ss if s.err is None]
        bad = [s for s in ss if s.err is not None]
        p50, p95, p99 = percentile(ok, 50), percentile(ok, 95), percentile(ok, 99)
        mx = max(ok) if ok else 0.0
        print(f"{label:<28}{len(ss):>5}{p50:>8.0f}{p95:>9.0f}{p99:>9.0f}{mx:>9.0f}"
              f"{len(bad):>7}")
        if ok and (worst is None or p95 > worst[1]):
            worst = (label, p95)

    ok_all = [s.ms for s in samples if s.err is None]
    errs = [s for s in samples if s.err is not None]
    print("-" * 78)
    print(f"запросов: {len(samples)}   успешных: {len(ok_all)}   ошибок: {len(errs)}"
          f"   за {wall:.0f} с")
    if ok_all:
        print(f"пропускная способность: {len(ok_all)/wall:.1f} запросов/с "
              f"при {users} одновременных пользователях")
        print(f"общий p95: {percentile(ok_all, 95):.0f} мс")
    if worst:
        print(f"узкое место: «{worst[0]}» -- p95 {worst[1]:.0f} мс")

    if errs:
        print("\nОШИБКИ (первые 8):")
        seen = defaultdict(int)
        for e in errs:
            seen[e.err] += 1
        for msg, n in list(sorted(seen.items(), key=lambda kv: -kv[1]))[:8]:
            print(f"   x{n:<5} {msg}")

    # Порог осмысленности: 1 с -- граница, за которой интерфейс перестаёт
    # ощущаться отзывчивым. Не выдумка: ниже неё человек воспринимает отклик
    # как мгновенный, выше -- как ожидание.
    print()
    if ok_all:
        p95all = percentile(ok_all, 95)
        if p95all < 1000:
            print(f"ВЫВОД: при {users} пользователях платформа отвечает в пределах "
                  f"секунды (p95 {p95all:.0f} мс). Запас есть.")
        elif p95all < 3000:
            print(f"ВЫВОД: при {users} пользователях заметны задержки "
                  f"(p95 {p95all:.0f} мс). Работать можно, но это потолок.")
        else:
            print(f"ВЫВОД: при {users} пользователях платформа не тянет "
                  f"(p95 {p95all:.0f} мс). Нужен запас по железу или оптимизация.")

    if out_json:
        raw = [{"step": s.step, "ms": round(s.ms, 1), "status": s.status,
                "err": s.err, "bytes": s.bytes} for s in samples]
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({"users": users, "wall_sec": wall, "samples": raw},
                      f, ensure_ascii=False, indent=1)
        print(f"\nсырые замеры: {out_json}")


def main():
    ap = argparse.ArgumentParser(description="Нагрузочное тестирование SAR Review")
    ap.add_argument("--url", default=None, help="адрес сервера (по умолчанию из конфига)")
    ap.add_argument("--password", default=None, help="общий пароль (по умолчанию из конфига)")
    ap.add_argument("--users", type=int, default=6)
    ap.add_argument("--duration", type=int, default=60, help="секунд под нагрузкой")
    ap.add_argument("--ramp", type=int, default=0,
                    help="секунд на плавный набор пользователей")
    ap.add_argument("--json", default=None, help="куда сохранить сырые замеры")
    ap.add_argument("--allow-remote", action="store_true",
                    help="разрешить бить по НЕлокальному адресу")
    args = ap.parse_args()

    url, password = args.url, args.password
    if url is None or password is None:
        import sar_common
        cfg, _ = sar_common.load_server_config(os.path.dirname(os.path.abspath(__file__)))
        if url is None:
            host = cfg["host"]
            host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            url = f"http://{host}:{cfg['port']}"
        if password is None:
            password = cfg["shared_password"]

    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in DEFAULT_LOCAL and not args.allow_remote:
        print(f"ОТКАЗ: {parsed.hostname} -- не локальный адрес.\n"
              "Нагрузочный тест по публичному адресу пойдёт через туннель и\n"
              "положит его для реальных пользователей. Если это осознанно --\n"
              "добавьте --allow-remote.")
        return 2

    print("=" * 78)
    print("НАГРУЗОЧНОЕ ТЕСТИРОВАНИЕ SAR REVIEW")
    print("=" * 78)
    print(f"адрес           : {url}")
    print(f"пользователей   : {args.users}" +
          (f" (набор за {args.ramp} с)" if args.ramp else " (все сразу)"))
    print(f"длительность    : {args.duration} с")
    print("режим           : только чтение, база не изменяется")
    print("\nсценарий одного пользователя, по кругу:")
    for i, (label, _) in enumerate(SCENARIO, 1):
        print(f"   {i}. {label}")
    print("\nне измеряется   : отдача видео, отрисовка в браузере, работа воркера")

    print("\nищу реальные файлы для сценария...", flush=True)
    try:
        targets = discover(url, password)
    except Exception as e:
        print(f"ОТКАЗ: не удалось получить список файлов: {type(e).__name__}: {e}")
        print("Сервер запущен? Пароль верный?")
        return 2
    if not targets["reports"]:
        print("ОТКАЗ: на платформе нет ни одного файла -- нагружать нечего.")
        return 2
    print(f"файлов на платформе: {targets['total']}, "
          f"в сценарии участвуют: {len(targets['reports'])}")

    samples = []
    lock = threading.Lock()

    class Sink(list):
        def append(self, x):
            with lock:
                list.append(self, x)

    sink = Sink()
    stop_at = time.time() + args.ramp + args.duration
    users = [VirtualUser(i, url, password, targets, stop_at, sink,
                         start_delay=(args.ramp * i / max(args.users, 1)) if args.ramp else 0.0)
             for i in range(args.users)]

    print(f"\nстарт, {args.users} пользователей...\n", flush=True)
    t0 = time.time()
    for u in users:
        u.start()

    last = 0
    while any(u.is_alive() for u in users):
        time.sleep(1.0)
        with lock:
            n = len(sink)
            recent = [s.ms for s in sink[last:] if s.err is None]
        el = time.time() - t0
        cur = percentile(recent, 95) if recent else 0.0
        print(f"  [{el:5.0f} с] запросов {n:6}  (+{n-last:4}/с)  p95 за секунду {cur:7.0f} мс",
              flush=True)
        last = n
        if el > args.ramp + args.duration + 180:
            print("  превышено время ожидания, останавливаю")
            break

    for u in users:
        u.join(timeout=10)
    wall = time.time() - t0
    samples = list(sink)
    loops = sum(u.loops for u in users)
    print(f"\nзавершено: {loops} полных проходов сценария")
    report(samples, wall, args.users, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
