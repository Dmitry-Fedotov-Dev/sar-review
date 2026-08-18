"""Состояние платформы: проверки, метрики Prometheus, уровни тревог.

Чистый модуль без побочных эффектов -- считает факты из базы и файловой
системы и возвращает их. Кто с этими фактами что делает (отдаёт по HTTP,
шлёт в телеграм, рисует в Grafana) решают вызывающие.

Набор проверок продиктован тем, что реально ломалось на боевой системе, а
не общим списком "что принято мониторить":

  * диск за сутки уполз с 18 ГБ до 3.4 ГБ, потому что отчёты хранили все
    полные кадры, -- и никто этого не видел, пока не начали искать вручную;
  * отчёт умеет залипнуть в статусе processing навсегда, если процесс,
    следивший за детектором, убили: на диске всё готово, а в базе вечное
    "обрабатывается";
  * воркер может умереть тихо -- очередь при этом выглядит ровно так же,
    как когда обрабатывать нечего;
  * резервные копии появились только что, и если они перестанут сниматься,
    узнать об этом будет неоткуда.

Отдельно про возраст бэкапа. Метрика возвращает None, когда копий нет
вообще, и вызывающий обязан отдать её как отсутствующую, а не как ноль:
"time() - 0" превращается в пятьдесят с лишним лет и выглядит как данные,
хотя данных нет. Эти грабли описаны в мониторинге wildflight, откуда взята
структура дашборда, -- повторять их незачем.
"""
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta

import sar_common

# Пороги. Подобраны под реальную машину: диск 250 ГБ, видео по 1-2 ГБ,
# одно видео при обработке пишет отчёт на сотни мегабайт.
DISK_WARN_GB = 10.0
DISK_CRIT_GB = 3.0
WORKER_WARN_SEC = 180          # цикл воркера -- секунды, 3 минуты это уже странно
WORKER_CRIT_SEC = 600
STUCK_REPORT_HOURS = 3.0       # дольше всякой разумной обработки одного файла
# то же окно, что у сервера (ONLINE_WINDOW_SEC): heartbeat раз в 15 с,
# 40 секунд прощают одну пропущенную отправку
ONLINE_WINDOW_SEC = 40
BACKUP_WARN_HOURS = 48.0
BACKUP_CRIT_HOURS = 24.0 * 7

OK, WARN, CRIT = "ok", "warn", "crit"
_ORDER = {OK: 0, WARN: 1, CRIT: 2}


def worst(levels):
    return max(levels, key=lambda l: _ORDER.get(l, 0), default=OK)


# psutil -- единственный кросс-платформенный способ получить загрузку CPU и
# память: в стандартной библиотеке os.getloadavg() есть только на Unix, а
# памяти нет вообще. Пакет под BSD-3, с MIT совместим. Зависимость мягкая:
# без него метрики железа просто отсутствуют, всё остальное работает.
try:
    import psutil
except Exception:                        # noqa: BLE001 -- отсутствие не авария
    psutil = None


# Загрузка процессора считается ФОНОВЫМ замером, а не в обработчике запроса.
#
# Причина -- поведение psutil, на котором первая версия и сломалась.
# cpu_percent(interval=None) возвращает долю времени С ПРОШЛОГО ВЫЗОВА В ЭТОМ
# ПРОЦЕССЕ, а вызывающих у нас несколько: Prometheus раз в 30 секунд, ручные
# запросы к /metrics, /healthz. Кто пришёл вторым в пределах секунды, получал
# почти нулевое окно и честный ноль. На боевом сервере метрика показывала
# 0.0 всегда, притом что psutil в отдельном процессе давал 7-15%.
#
# Блокирующий замер (interval=1) эту беду чинит, но добавляет секунду
# ожидания на каждый запрос -- в такие грабли здесь уже наступали с обходом
# папки отчётов. Поэтому единственный потребитель psutil -- фоновый поток с
# фиксированным шагом, а запросы читают готовое значение.
CPU_SAMPLE_SEC = 5.0
_HW_LOCK = threading.Lock()
_HW_SAMPLE = {}
_HW_THREAD = None


def _hw_sampler():
    while True:
        try:
            pct = float(psutil.cpu_percent(interval=CPU_SAMPLE_SEC))
            m = psutil.virtual_memory()
            with _HW_LOCK:
                _HW_SAMPLE.update({
                    "cpu_percent": pct,
                    "cpu_cores": int(psutil.cpu_count(logical=True) or 0),
                    "mem_total_bytes": int(m.total),
                    "mem_used_bytes": int(m.total - m.available),
                    "mem_percent": float(m.percent),
                })
        except Exception:                # noqa: BLE001 -- сбор метрик не может ронять сервис
            time.sleep(CPU_SAMPLE_SEC)


def _ensure_sampler():
    global _HW_THREAD
    if psutil is None or _HW_THREAD is not None:
        return
    _HW_THREAD = threading.Thread(target=_hw_sampler, daemon=True)
    _HW_THREAD.start()


def hardware():
    """Загрузка процессора и памяти из фонового замера.

    Пустой dict, если psutil недоступен или первый замер ещё не готов --
    отсутствующее значение честнее нуля, на который его подменяли раньше.
    """
    if psutil is None:
        return {}
    _ensure_sampler()
    with _HW_LOCK:
        return dict(_HW_SAMPLE)


def _disk_free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def backup_age_hours(backup_dir):
    """Часы с самой свежей копии, либо None если копий нет ни одной.

    None -- это НЕ ноль и не бесконечность: это "неизвестно". Вызывающий
    обязан не выдавать её за число, иначе на графике возникнет ложная
    уверенность там, где данных нет.
    """
    if not os.path.isdir(backup_dir):
        return None
    newest = None
    for f in os.listdir(backup_dir):
        if not (f.startswith("sar_backup_") and f.endswith(".zip")):
            continue
        m = re.search(r"(\d{8}_\d{6})", f)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        if newest is None or ts > newest:
            newest = ts
    if newest is None:
        return None
    return (datetime.now() - newest).total_seconds() / 3600.0


def dir_size_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# Обход папки отчётов -- дорогая операция: на боевой машине там 573 тысячи
# файлов (кропы и кадры), полный проход занимает десятки секунд. Считать его
# в обработчике HTTP-запроса нельзя по двум причинам, и обе выяснились на
# живом сервере: во-первых, /healthz просто зависал; во-вторых, эндпоинт
# открыт БЕЗ пароля, то есть любой желающий мог заставить сервер молотить
# диск, повторяя запрос. Поэтому размер считается в фоне и отдаётся из кеша,
# а пока значения нет -- метрика отсутствует, что честнее нуля.
_SIZE_CACHE = {}          # path -> (значение, время расчёта)
_SIZE_INFLIGHT = set()
_SIZE_LOCK = threading.Lock()
DIR_SIZE_TTL_SEC = 900


def dir_size_cached(path, ttl=DIR_SIZE_TTL_SEC, now=None):
    """Размер папки из кеша. Никогда не блокирует вызывающего.

    Возвращает None, если значения ещё нет -- и запускает расчёт в фоне.
    """
    now = now if now is not None else time.time()
    with _SIZE_LOCK:
        cached = _SIZE_CACHE.get(path)
        fresh = cached is not None and (now - cached[1]) < ttl
        need_refresh = not fresh and path not in _SIZE_INFLIGHT
        if need_refresh:
            _SIZE_INFLIGHT.add(path)

    if need_refresh:
        def _worker():
            try:
                size = dir_size_bytes(path)
                with _SIZE_LOCK:
                    _SIZE_CACHE[path] = (size, time.time())
            except Exception:
                pass
            finally:
                with _SIZE_LOCK:
                    _SIZE_INFLIGHT.discard(path)

        threading.Thread(target=_worker, daemon=True).start()

    return cached[0] if cached is not None else None


def collect(conn, watch_dir, backup_dir=None, reports_dir=None):
    """Снимок состояния. Возвращает dict фактов -- без оценок и порогов."""
    now = datetime.now()
    facts = {"ts": now.isoformat()}

    by_status = {}
    for r in conn.execute("SELECT status, COUNT(*) n FROM reports GROUP BY status"):
        by_status[r["status"]] = r["n"]
    facts["reports_by_status"] = by_status
    facts["queue_depth"] = by_status.get("queued", 0) + by_status.get("processing", 0)

    cutoff = (now - timedelta(hours=STUCK_REPORT_HOURS)).isoformat()
    facts["stuck_reports"] = conn.execute(
        "SELECT COUNT(*) n FROM reports WHERE status='processing' AND updated_at < ?",
        (cutoff,)).fetchone()["n"]

    facts["worker_heartbeat_sec"] = sar_common.heartbeat_age_sec(conn, "worker")
    facts["bot_heartbeat_sec"] = sar_common.heartbeat_age_sec(conn, "bot")

    facts["disk_free_gb"] = _disk_free_gb(watch_dir)
    facts["backup_age_hours"] = backup_age_hours(
        backup_dir or os.path.join(os.path.dirname(watch_dir.rstrip(os.sep)), "sar_backups"))

    # человеческая работа -- это и есть основная ценность в базе
    facts["manual_observations"] = conn.execute(
        "SELECT COUNT(*) n FROM manual_observations").fetchone()["n"]
    facts["triage_marks"] = conn.execute(
        "SELECT COUNT(*) n FROM detection_priorities").fetchone()["n"]
    try:
        facts["comments"] = conn.execute(
            "SELECT COUNT(*) n FROM detection_comments").fetchone()["n"]
    except Exception:
        facts["comments"] = 0
    facts["people_approved"] = conn.execute(
        "SELECT COUNT(*) n FROM telegram_access_requests WHERE status='approved'"
    ).fetchone()["n"]

    # last_seen в presence хранится ЧИСЛОМ (time.time()), а не строкой ISO --
    # см. api_heartbeat в sar_server.py. Сравнение числового столбца со
    # строкой в SQLite всегда ложно (число сортируется раньше текста), и
    # первая версия этой метрики из-за такой подмены типов показывала ноль
    # при любом числе зрителей, ничем не жалуясь.
    fresh = time.time() - ONLINE_WINDOW_SEC
    facts["viewers_online"] = conn.execute(
        "SELECT COUNT(DISTINCT viewer_name) n FROM presence WHERE last_seen > ?",
        (fresh,)).fetchone()["n"]

    row = conn.execute(
        "SELECT COALESCE(SUM(end_sec-start_sec),0) s FROM watch_segments").fetchone()
    facts["watched_seconds"] = float(row["s"] or 0.0)

    try:
        facts["streams_active"] = conn.execute(
            "SELECT COUNT(*) n FROM streams WHERE status='live'").fetchone()["n"]
    except Exception:
        facts["streams_active"] = None        # ветка стримов ещё не влита

    if reports_dir and os.path.isdir(reports_dir):
        facts["reports_bytes"] = dir_size_cached(reports_dir)

    facts.update(hardware())
    return facts


def evaluate(facts):
    """Оценка фактов по порогам. Возвращает {имя: (уровень, текст)}."""
    checks = {}

    hb = facts.get("worker_heartbeat_sec")
    if hb is None:
        checks["worker"] = (WARN, "воркер ни разу не отмечался — старая версия или не запущен")
    elif hb > WORKER_CRIT_SEC:
        checks["worker"] = (CRIT, f"воркер молчит {hb/60:.0f} мин — очередь стоит")
    elif hb > WORKER_WARN_SEC:
        checks["worker"] = (WARN, f"воркер не отмечался {hb:.0f} с")
    else:
        checks["worker"] = (OK, f"воркер жив ({hb:.0f} с назад)")

    free = facts.get("disk_free_gb")
    if free is None:
        checks["disk"] = (WARN, "не удалось узнать свободное место")
    elif free < DISK_CRIT_GB:
        checks["disk"] = (CRIT, f"на диске {free:.1f} ГБ — обработка встанет")
    elif free < DISK_WARN_GB:
        checks["disk"] = (WARN, f"на диске {free:.1f} ГБ")
    else:
        checks["disk"] = (OK, f"свободно {free:.1f} ГБ")

    stuck = facts.get("stuck_reports", 0)
    checks["stuck"] = ((WARN, f"залипло в обработке: {stuck} — файлы готовы, статус нет")
                        if stuck else (OK, "залипших нет"))

    err = facts.get("reports_by_status", {}).get("error", 0)
    checks["errors"] = ((WARN, f"файлов с ошибкой: {err}") if err else (OK, "ошибок нет"))

    age = facts.get("backup_age_hours")
    if age is None:
        checks["backup"] = (CRIT, "резервных копий нет ни одной")
    elif age > BACKUP_CRIT_HOURS:
        checks["backup"] = (CRIT, f"последняя копия {age/24:.0f} суток назад")
    elif age > BACKUP_WARN_HOURS:
        checks["backup"] = (WARN, f"последняя копия {age:.0f} ч назад")
    else:
        checks["backup"] = (OK, f"копия {age:.0f} ч назад")

    return checks


def overall(checks):
    return worst([lvl for lvl, _ in checks.values()])


# --------------------------------------------------------------------------
# Prometheus. Формат текстовый и простой, поэтому рендерим сами -- тащить
# зависимость ради конкатенации строк незачем, а лишний пакет в проекте,
# который должен ставиться двумя командами в поле, стоит дороже.
# --------------------------------------------------------------------------

_METRIC_RE = re.compile(r"[^a-zA-Z0-9_]")


def _esc(v):
    return _METRIC_RE.sub("_", str(v))


def render_prometheus(facts, checks):
    out = []

    def add(name, value, help_text, mtype="gauge", labels=None):
        if value is None:
            return               # отсутствующее не выдаём за ноль
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {mtype}")
        lbl = ("{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}") if labels else ""
        out.append(f"{name}{lbl} {value}")

    add("sar_up", 1, "Платформа отвечает")
    add("sar_worker_heartbeat_age_seconds", facts.get("worker_heartbeat_sec"),
        "Секунд с последней отметки воркера")
    add("sar_disk_free_bytes", (facts["disk_free_gb"] * 1e9)
        if facts.get("disk_free_gb") is not None else None, "Свободно на диске с видео")
    add("sar_queue_depth", facts.get("queue_depth"), "Файлов в очереди и в работе")
    add("sar_stuck_reports", facts.get("stuck_reports"), "Отчётов, залипших в обработке")
    add("sar_viewers_online", facts.get("viewers_online"), "Зрителей онлайн")
    add("sar_manual_observations_total", facts.get("manual_observations"),
        "Ручных пометок всего", "counter")
    add("sar_triage_marks_total", facts.get("triage_marks"),
        "Триаж-статусов всего", "counter")
    add("sar_people_approved", facts.get("people_approved"), "Людей с доступом")
    add("sar_watched_seconds_total", round(facts.get("watched_seconds", 0.0), 1),
        "Секунд отсмотрено людьми", "counter")
    add("sar_streams_active", facts.get("streams_active"), "Активных трансляций")
    add("sar_reports_bytes", facts.get("reports_bytes"), "Размер готовых отчётов")

    # Железо. Отдаём и текущую загрузку, и потолок -- без потолка проценты
    # не читаются: 80% на двух ядрах и на двадцати это разные новости.
    add("sar_cpu_cores", facts.get("cpu_cores"), "Логических ядер всего")
    add("sar_cpu_percent", facts.get("cpu_percent"), "Загрузка процессора, %")
    add("sar_memory_total_bytes", facts.get("mem_total_bytes"), "Оперативной памяти всего")
    add("sar_memory_used_bytes", facts.get("mem_used_bytes"), "Занято оперативной памяти")
    add("sar_memory_percent", facts.get("mem_percent"), "Занято памяти, %")

    age = facts.get("backup_age_hours")
    add("sar_backup_age_seconds", (age * 3600.0) if age is not None else None,
        "Секунд с последней резервной копии; метрика отсутствует, если копий нет")

    for status, n in (facts.get("reports_by_status") or {}).items():
        out.append(f'sar_reports{{status="{_esc(status)}"}} {n}')

    for name, (lvl, _) in checks.items():
        out.append(f'sar_check_level{{check="{_esc(name)}"}} {_ORDER.get(lvl, 0)}')

    return "\n".join(out) + "\n"
