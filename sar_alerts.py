"""Тревоги: решение о том, когда беспокоить человека.

Пока проверка ровно одна -- доступна ли платформа. Узкий алерт, на который
реагируют, полезнее широкого, который через неделю замьютят.

Три правила, из которых состоит вся ценность модуля:

1. СООБЩАЕМ О СМЕНЕ СОСТОЯНИЯ, а не о факте. "Сервис лежит" раз в минуту
   превращается в фон, который перестают читать ровно к тому моменту, когда
   он важен.

2. ЖДЁМ ПОДТВЕРЖДЕНИЯ. Одна неудачная проверка -- это не авария, а сетевой
   чих: перезапуск сервера, коротко занятый порт, моргнувший туннель.
   Тревога уходит, только если состояние держится заданное время.

3. ПОМНИМ, О ЧЁМ УЖЕ СКАЗАЛИ. Состояние лежит в базе, поэтому перезапуск
   бота не рождает повторную тревогу о том же самом, а восстановление
   сообщается только тем, кому сообщили о падении.

Модуль ничего не шлёт и никуда не ходит: он получает наблюдение и прошлое
состояние, а возвращает решение. Сеть и телеграм -- снаружи, поэтому логику
можно проверить тестами без единого сокета.

ЧЕГО ЭТОТ МЕХАНИЗМ НЕ УМЕЕТ, и это важно понимать: проверку выполняет
телеграм-бот, то есть процесс на той же машине. Если машина выключится
целиком, бот умрёт вместе с платформой и не скажет НИЧЕГО. Прикрывается это
только внешним пингом /healthz откуда-то ещё -- см. monitoring/README.md.
"""
from datetime import datetime, timedelta

OK, DOWN = "ok", "down"

# Сколько держаться состоянию, прежде чем о нём сообщить. Полторы-две
# проверки подряд при опросе раз в минуту.
CONFIRM_SEC = 150


def load_state(conn, check):
    row = conn.execute(
        "SELECT level, since, notified_at, notified_level FROM alert_state WHERE check_name=?",
        (check,)).fetchone()
    return dict(row) if row is not None else None


def save_state(conn, check, level, since, notified_at, notified_level):
    conn.execute(
        "INSERT INTO alert_state (check_name, level, since, notified_at, notified_level) "
        "VALUES (?,?,?,?,?) ON CONFLICT(check_name) DO UPDATE SET "
        "level=excluded.level, since=excluded.since, notified_at=excluded.notified_at, "
        "notified_level=excluded.notified_level",
        (check, level, since, notified_at, notified_level))
    conn.commit()


# Мьют живёт в той же таблице под зарезервированным именем: это тоже
# состояние тревог, отдельная таблица ради одной строки была бы лишней.
# level='muted', notified_at = до какого момента молчим.
MUTE_KEY = "__mute__"
MAX_MUTE_HOURS = 24


def set_mute(conn, hours, now=None):
    """Замолчать на заданное время. Возвращает момент окончания.

    Срок ОБЯЗАТЕЛЕН и ограничен сверху: мьют без срока однажды поставят и
    забудут, а молчащий навсегда мониторинг хуже отсутствующего -- он ещё и
    создаёт ложное ощущение, что за системой следят.
    """
    now = now or datetime.now()
    hours = max(0.1, min(float(hours), MAX_MUTE_HOURS))
    until = now + timedelta(hours=hours)
    save_state(conn, MUTE_KEY, "muted", now.isoformat(), until.isoformat(), "muted")
    return until


def clear_mute(conn):
    conn.execute("DELETE FROM alert_state WHERE check_name=?", (MUTE_KEY,))
    conn.commit()


def mute_until(conn, now=None):
    """До какого момента молчим, либо None если тревоги включены."""
    now = now or datetime.now()
    row = load_state(conn, MUTE_KEY)
    if not row:
        return None
    until = _parse(row.get("notified_at"))
    if until is None or until <= now:
        return None                 # срок вышел -- мьют снялся сам
    return until


def _parse(ts):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def decide(prev, level, now, confirm_sec=CONFIRM_SEC):
    """Что делать с наблюдением. Чистая функция, без обращений к базе.

    prev -- прошлое состояние (dict из load_state) либо None.
    Возвращает (новое_состояние, надо_ли_слать, длительность_прошлого_сек).
    """
    now_iso = now.isoformat()

    if prev is None:
        # Первый запуск. Молчим: сообщать "сервис работает" в ответ на
        # включение мониторинга незачем, а сообщать "лежит" рано -- мы ещё
        # ничего не подтверждали.
        return ({"level": level, "since": now_iso,
                  "notified_at": None, "notified_level": level}, False, 0.0)

    since = _parse(prev.get("since")) or now
    if prev.get("level") != level:
        # состояние сменилось -- отсчёт подтверждения начинается заново,
        # но память о том, что человеку уже сообщили, сохраняется
        return ({"level": level, "since": now_iso,
                  "notified_at": prev.get("notified_at"),
                  "notified_level": prev.get("notified_level")}, False, 0.0)

    held = (now - since).total_seconds()
    state = {"level": level, "since": prev.get("since"),
             "notified_at": prev.get("notified_at"),
             "notified_level": prev.get("notified_level")}

    if level != prev.get("notified_level") and held >= confirm_sec:
        state["notified_at"] = now_iso
        state["notified_level"] = level
        return (state, True, held)

    return (state, False, held)


def humanize(seconds):
    if seconds is None:
        return "неизвестно сколько"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    h, m = divmod(seconds // 60, 60)
    return f"{h} ч {m:02d} мин" if m else f"{h} ч"


def message(level, held_sec, url, detail=None):
    """Текст тревоги. Пишем то, что нужно для действия, а не для отчёта."""
    if level == DOWN:
        txt = (f"\U0001F534 Платформа не отвечает\n\n"
               f"Адрес: {url}\n"
               f"Не отвечает уже: {humanize(held_sec)}")
        if detail:
            txt += f"\nПричина: {detail}"
        txt += ("\n\nЧто проверить:\n"
                "1. Запущен ли процесс sar_server.py\n"
                "2. Жив ли туннель (cloudflared)\n"
                "3. Свободно ли место на диске\n\n"
                "Обработка видео при этом продолжается: воркер работает "
                "отдельным процессом и от веб-слоя не зависит.")
        return txt
    return (f"\U0001F7E2 Платформа снова отвечает\n\n"
            f"Была недоступна: {humanize(held_sec)}\n"
            f"Адрес: {url}")


def check_service(url, timeout=10):
    """Одна проверка доступности. Возвращает (уровень, пояснение).

    Код 503 -- это ЖИВОЙ сервер, который честно сообщает о своей беде
    (см. /healthz). Считать его недоступностью нельзя: тревога о падении
    там, где сервис отвечает, обесценивает саму тревогу.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read(2048)
            return OK, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 503:
            return OK, "HTTP 503 — сервер отвечает, но сообщает о проблеме"
        return DOWN, f"HTTP {e.code}"
    except Exception as e:
        return DOWN, f"{type(e).__name__}: {e}"[:120]
