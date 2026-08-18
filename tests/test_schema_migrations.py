"""Тесты миграций схемы.

Поводом стала реальная поломка на боевой базе. Столбец notified_level
добавили в CREATE TABLE для alert_state -- но таблица там УЖЕ существовала
с прошлого обновления, а `CREATE TABLE IF NOT EXISTS` существующую таблицу
не трогает. Миграцию написать забыли, и тревоги начали падать на каждой
проверке с "no such column: notified_level": в логе бота, молча, при внешне
работающем мониторинге. Обнаружилось случайно, при ручной проверке.

Тесты на ЧИСТОЙ базе этого поймать не могут в принципе -- там CREATE TABLE
создаёт таблицу сразу правильной. Поэтому здесь база сначала создаётся по
СТАРОЙ схеме, а потом обновляется, как это происходит у пользователя после
git pull.
"""
import sqlite3

import pytest

import sar_alerts as al
import sar_common


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_alert_state_gets_notified_level_on_upgrade(tmp_path):
    """Регрессия: база со старой alert_state обязана обновиться."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE alert_state (
        check_name TEXT PRIMARY KEY,
        level TEXT NOT NULL,
        since TEXT NOT NULL,
        notified_at TEXT
    )""")
    conn.execute("INSERT INTO alert_state VALUES ('service','ok','2026-01-01',NULL)")
    conn.commit()
    conn.close()
    assert "notified_level" not in _columns(sqlite3.connect(db), "alert_state")

    sar_common.init_db(db)               # как при обновлении у пользователя

    c = sar_common.get_db_connection(db)
    assert "notified_level" in _columns(c, "alert_state")
    # и главное -- код, который на неё опирается, действительно работает
    assert al.load_state(c, "service") is not None
    c.close()


def test_existing_rows_survive_the_migration(tmp_path):
    """Миграция не должна терять уже накопленное состояние тревог."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE alert_state (
        check_name TEXT PRIMARY KEY, level TEXT NOT NULL,
        since TEXT NOT NULL, notified_at TEXT)""")
    conn.execute("INSERT INTO alert_state VALUES ('service','down','2026-01-01','2026-01-02')")
    conn.commit()
    conn.close()

    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    row = al.load_state(c, "service")
    assert row["level"] == "down"
    assert row["notified_at"] == "2026-01-02"
    c.close()


def test_alerting_works_end_to_end_on_an_upgraded_db(tmp_path):
    """Сквозная проверка: на обновлённой базе тревога проходит весь путь --
    сохранение, чтение, решение, мьют."""
    from datetime import datetime, timedelta

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE alert_state (
        check_name TEXT PRIMARY KEY, level TEXT NOT NULL,
        since TEXT NOT NULL, notified_at TEXT)""")
    conn.commit()
    conn.close()

    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)

    t0 = datetime(2026, 8, 18, 12, 0, 0)
    prev, _, _ = al.decide(None, al.OK, t0)
    al.save_state(c, "service", prev["level"], prev["since"],
                   prev["notified_at"], prev["notified_level"])
    for i in range(1, 6):
        prev = al.load_state(c, "service")
        state, notify, held = al.decide(prev, al.DOWN, t0 + timedelta(seconds=60 * i))
        al.save_state(c, "service", state["level"], state["since"],
                       state["notified_at"], state["notified_level"])
        if notify:
            break
    assert notify is True, "на обновлённой базе тревога не сработала"

    al.set_mute(c, hours=1, now=t0)
    assert al.mute_until(c, now=t0) is not None
    c.close()


# --- общая проверка: код и схема не разошлись -----------------------------

@pytest.mark.parametrize("table,needed", [
    ("alert_state", {"check_name", "level", "since", "notified_at", "notified_level"}),
    ("service_heartbeat", {"service", "last_seen", "pid", "note"}),
    ("telegram_access_requests", {"chat_id", "username", "status", "access_token", "role"}),
    ("manual_observations", {"report_id", "viewer_name", "timestamp_sec", "bbox",
                              "lat", "lon", "est_lat", "est_lon"}),
    ("presence", {"viewer_name", "last_seen"}),
])
def test_schema_has_columns_the_code_uses(tmp_path, table, needed):
    """Ловит расхождение схемы и кода на чистой базе.

    Не заменяет тесты выше -- те про ОБНОВЛЕНИЕ, а этот про то, что список
    столбцов вообще не разъехался с тем, что запрашивает код."""
    db = str(tmp_path / "fresh.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    have = _columns(c, table)
    c.close()
    missing = needed - have
    assert not missing, f"в {table} нет столбцов: {sorted(missing)}"


def test_init_db_is_idempotent(tmp_path):
    """Вызывается из обоих процессов при старте в любом порядке."""
    db = str(tmp_path / "x.db")
    for _ in range(3):
        sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    assert "notified_level" in _columns(c, "alert_state")
    c.close()
