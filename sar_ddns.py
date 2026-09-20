# -*- coding: utf-8 -*-
"""Сторож имени: держит запись deSEC наведённой на текущий адрес.

Зачем. Мегалайн выдаёт динамический адрес -- он меняется сам по себе, без
предупреждения. Пока платформа жила за туннелем, это было неважно: адрес
туннеля и так менялся при каждом перезапуске, поэтому постоянные ссылки на
находки пришлось пустить через бота. Прямое подключение эту подпорку
убирает, но только если имя действительно следует за адресом.

Что делает: раз в несколько минут узнаёт свой публичный адрес, сверяет с
тем, что сейчас в DNS, и обновляет запись, если они разошлись.

ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ И ПОЧЕМУ:

* Один источник адреса. Если сервис соврёт или его подменят, имя платформы
  уедет на чужую машину -- и заметить это будет некому. Спрашиваем ДВА
  независимых и обновляем, только когда они согласны.

* Обновление «на всякий случай». Неизвестность -- не изменение. Не сумели
  узнать адрес -- ничего не трогаем. Та же логика, что в probe() у сторожа
  туннеля: None означает «проверить не удалось», а не «сломалось».

* Системный резолвер при сверке. Он отвечает из кеша и может говорить про
  запись то, чего в DNS уже нет. Спрашиваем публичный (DoH) -- ровно тот
  урок, из-за которого сторож туннеля однажды начал перезапускать живой
  туннель по кругу.

Запуск:  python sar_ddns.py watch      (сторожем, рядом с остальными)
         python sar_ddns.py once       (один проход, для проверки)
         python sar_ddns.py show       (что вижу сейчас, ничего не меняя)
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import sar_common

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "sar_config.json")

UPDATE_URL = "https://update.dedyn.io/"
DOH_URL = "https://cloudflare-dns.com/dns-query"

HTTP_TIMEOUT = 15
WATCH_INTERVAL = int(os.environ.get("SAR_DDNS_INTERVAL", "300"))

# Источники публичного адреса. Именно РАЗНЫЕ организации, а не два адреса
# одного сервиса: смысл в том, чтобы совпадение что-то значило.
IP_SOURCES = [
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
]

UA = {"User-Agent": "sar-review-ddns/1.0"}


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def read_cfg():
    if not os.path.exists(CFG_PATH):
        return {}
    return json.load(io.open(CFG_PATH, encoding="utf-8"))


def ddns_cfg(cfg=None):
    cfg = cfg if cfg is not None else read_cfg()
    return (cfg.get("ddns") or {})


# --- адрес ----------------------------------------------------------------

def _valid_ipv4(text):
    parts = (text or "").strip().split(".")
    if len(parts) != 4:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 or n > 255 for n in nums):
        return None
    if not all(p.isdigit() for p in parts):
        return None
    return ".".join(str(n) for n in nums)


def _ask(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return _valid_ipv4(r.read().decode("utf-8", "replace"))
    except Exception as e:                              # noqa: BLE001
        # Отказ одного источника -- обычное дело, для того их и несколько.
        # Молчать всё равно нельзя: если отваливаются все, это надо видеть.
        log("  источник %s не ответил: %s" % (urllib.parse.urlsplit(url).hostname,
                                               str(e)[:60]))
        return None


def public_ip(sources=None):
    """Публичный адрес, подтверждённый ДВУМЯ независимыми источниками.

    Возвращает None, если подтверждения нет. None -- это «не знаю», и
    вызывающий обязан в этом случае ничего не менять: имя, уехавшее на
    чужой адрес, хуже имени, отставшего на один проход.
    """
    seen = []
    for url in (sources if sources is not None else IP_SOURCES):
        ip = _ask(url)
        if ip:
            seen.append(ip)
            if seen.count(ip) >= 2:
                return ip
    if seen:
        log("  источники не согласны между собой: %s" % ", ".join(sorted(set(seen))))
    return None


def dns_ip(host):
    """Что сейчас в DNS по мнению ПУБЛИЧНОГО резолвера.

    Возвращает None, если спросить не удалось. Пустой ответ (записи нет)
    отдаётся как "" -- это осмысленное «записи нет», а не «не знаю».
    """
    url = "%s?name=%s&type=A" % (DOH_URL, urllib.parse.quote(host))
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json",
                                               **UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            data = json.load(r)
    except Exception as e:                              # noqa: BLE001
        log("  не удалось спросить DNS про %s: %s" % (host, str(e)[:60]))
        return None
    answers = [a.get("data") for a in data.get("Answer", [])
               if a.get("type") == 1]
    return answers[0] if answers else ""


# --- обновление -----------------------------------------------------------

def update(host, token, ip):
    """Наводит запись на ip. Возвращает True при успехе.

    myipv6=preserve обязателен: без него обновление только по IPv4 стирает
    запись AAAA, если она была. Тихо и незаметно -- пока кто-нибудь с
    IPv6-подключением не перестанет открывать платформу.
    """
    qs = urllib.parse.urlencode({"hostname": host, "myipv4": ip,
                                 "myipv6": "preserve"})
    req = urllib.request.Request(UPDATE_URL + "?" + qs,
                                 headers={"Authorization": "Token %s" % token,
                                          **UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        # Код и тело -- но НИКОГДА не заголовки: там токен.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace").strip()[:120]
        except Exception:                               # noqa: BLE001
            detail = ""
        if e.code in (401, 403):
            log("  deSEC отказал (код %d): токен не принят. Проверьте его в "
                "sar_config.json" % e.code)
        elif e.code == 429:
            log("  deSEC просит подождать (код 429)")
        else:
            log("  deSEC вернул код %d %s" % (e.code, detail))
        return False
    except Exception as e:                              # noqa: BLE001
        log("  обновление не дошло: %s" % str(e)[:80])
        return False

    if body.lower().startswith("good") or body.lower().startswith("nochg"):
        return True
    log("  deSEC ответил неожиданно: %r" % body[:120])
    return False


# --- пульс ----------------------------------------------------------------

def heartbeat(note=None):
    """Отметка «сторож жив» в общую базу.

    Без неё зависший сторож неотличим от работающего: процесс есть,
    мониторинг зелёный, имя показывает на адрес недельной давности.
    """
    try:
        cfg = read_cfg()
        watch = os.path.abspath(os.path.join(
            ROOT, (cfg.get("server") or {}).get("watch_dir") or "."))
        _, _, db, _ = sar_common.resolve_paths(
            watch, (cfg.get("server") or {}).get("data_dir"))
        if not os.path.exists(db):
            return
        conn = sar_common.get_db_connection(db)
        try:
            sar_common.touch_heartbeat(conn, "ddns", note)
        finally:
            conn.close()
    except Exception:                                   # noqa: BLE001
        # Пульс -- вспомогательная вещь. Уронить из-за него сторож нельзя.
        pass


# --- проходы --------------------------------------------------------------

def once(cfg=None):
    """Один проход. Возвращает краткий итог строкой."""
    d = ddns_cfg(cfg)
    host, token = d.get("hostname") or "", d.get("token") or ""
    if not host or not token:
        return "не настроено: нужны ddns.hostname и ddns.token в sar_config.json"

    ip = public_ip()
    if not ip:
        # Ровно тот случай, ради которого None отличается от False.
        return "адрес не подтверждён -- ничего не меняем"

    have = dns_ip(host)
    if have is None:
        return "адрес %s, но DNS не отвечает -- ничего не меняем" % ip
    if have == ip:
        return "адрес %s, запись совпадает" % ip

    was = have or "записи не было"
    if update(host, token, ip):
        return "адрес сменился: %s -> %s, запись обновлена" % (was, ip)
    return "адрес сменился: %s -> %s, но обновить НЕ УДАЛОСЬ" % (was, ip)


def watch():
    d = ddns_cfg()
    log("сторож имени: %s, проверка раз в %d с"
        % (d.get("hostname") or "(имя не задано)", WATCH_INTERVAL))
    last = None
    while True:
        try:
            note = once()
        except Exception as e:                          # noqa: BLE001
            # Сторож обязан пережить любой сбой прохода: упавший сторож
            # молчит так же, как исправный, и это уже случалось.
            note = "проход сорвался: %s" % str(e)[:100]
        # Повторяющееся «всё совпадает» не печатаем: журнал должен
        # оставаться читаемым, иначе в нём потеряется настоящая беда.
        if note != last:
            log(note)
            last = note
        heartbeat(note)
        time.sleep(WATCH_INTERVAL)


def show():
    d = ddns_cfg()
    host = d.get("hostname") or ""
    print("имя:     %s" % (host or "(не задано в sar_config.json)"))
    print("токен:   %s" % ("задан" if d.get("token") else "НЕ ЗАДАН"))
    ip = public_ip()
    print("адрес:   %s" % (ip or "не подтверждён двумя источниками"))
    if host:
        have = dns_ip(host)
        print("в DNS:   %s" % ("не спросить" if have is None
                               else (have or "записи нет")))
        if ip and have == ip:
            print("итог:    совпадает, обновлять нечего")
        elif ip and have is not None:
            print("итог:    РАСХОДЯТСЯ, нужен проход")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if cmd == "watch":
        watch()
    elif cmd == "once":
        log(once())
    elif cmd == "show":
        show()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
