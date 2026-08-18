"""Тесты мониторинга платформы.

Проверки писались не по общему списку "что принято мониторить", а по тому,
что реально ломалось на боевой системе. Тесты держат именно эти случаи:

  * диск незаметно уполз до 3.4 ГБ и чуть не остановил операцию;
  * отчёт умеет залипнуть в processing навсегда -- на диске готово, в базе
    вечное "обрабатывается";
  * воркер может умереть тихо, и пустая очередь выглядит точно так же, как
    при живом воркере, которому нечего делать;
  * резервные копии появились вчера, и их пропажу заметить неоткуда.

Отдельный тест на то, что отсутствующая метрика НЕ выдаётся нулём: "нет ни
одной копии" и "копия только что" -- противоположные вещи, а через ноль они
превращаются в "time() - 0" и рисуют полвека вместо честного пробела.
"""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import sar_common
import sar_health as h


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


# --- heartbeat: живой воркер против мёртвого ------------------------------

def test_heartbeat_absent_when_never_touched(conn):
    assert sar_common.heartbeat_age_sec(conn, "worker") is None


def test_heartbeat_is_fresh_after_touch(conn):
    sar_common.touch_heartbeat(conn, "worker")
    age = sar_common.heartbeat_age_sec(conn, "worker")
    assert age is not None and age < 5


def test_heartbeat_updates_not_duplicates(conn):
    sar_common.touch_heartbeat(conn, "worker")
    sar_common.touch_heartbeat(conn, "worker")
    n = conn.execute("SELECT COUNT(*) c FROM service_heartbeat").fetchone()["c"]
    assert n == 1


def test_dead_worker_is_critical(conn):
    old = (datetime.now() - timedelta(minutes=30)).isoformat()
    conn.execute("INSERT INTO service_heartbeat (service,last_seen) VALUES ('worker',?)", (old,))
    conn.commit()
    facts = h.collect(conn, ".")
    lvl, txt = h.evaluate(facts)["worker"]
    assert lvl == h.CRIT
    assert "молчит" in txt


def test_live_worker_is_ok(conn):
    sar_common.touch_heartbeat(conn, "worker")
    facts = h.collect(conn, ".")
    assert h.evaluate(facts)["worker"][0] == h.OK


def test_never_seen_worker_is_a_warning_not_a_crash(conn):
    """Старая версия воркера heartbeat не пишет -- это повод предупредить,
    а не объявить аварию."""
    facts = h.collect(conn, ".")
    assert h.evaluate(facts)["worker"][0] == h.WARN


# --- диск: то, что нас укусило -------------------------------------------

@pytest.mark.parametrize("free_gb,expect", [(100.0, h.OK), (8.0, h.WARN), (2.0, h.CRIT)])
def test_disk_thresholds(conn, free_gb, expect):
    facts = h.collect(conn, ".")
    facts["disk_free_gb"] = free_gb
    assert h.evaluate(facts)["disk"][0] == expect


def test_disk_unknown_is_warning(conn):
    facts = h.collect(conn, ".")
    facts["disk_free_gb"] = None
    assert h.evaluate(facts)["disk"][0] == h.WARN


# --- залипшие отчёты ------------------------------------------------------

def test_stuck_report_is_detected(conn):
    old = (datetime.now() - timedelta(hours=6)).isoformat()
    conn.execute(
        "INSERT INTO reports (report_id,rel_path,abs_path,kind,status,created_at,updated_at) "
        "VALUES ('r1','a.mp4','/a.mp4','video','processing',?,?)", (old, old))
    conn.commit()
    facts = h.collect(conn, ".")
    assert facts["stuck_reports"] == 1
    assert h.evaluate(facts)["stuck"][0] == h.WARN


def test_recently_started_report_is_not_stuck(conn):
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO reports (report_id,rel_path,abs_path,kind,status,created_at,updated_at) "
        "VALUES ('r1','a.mp4','/a.mp4','video','processing',?,?)", (now, now))
    conn.commit()
    facts = h.collect(conn, ".")
    assert facts["stuck_reports"] == 0
    assert h.evaluate(facts)["stuck"][0] == h.OK


def test_error_reports_are_surfaced(conn):
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO reports (report_id,rel_path,abs_path,kind,status,created_at,updated_at) "
        "VALUES ('r1','a.mp4','/a.mp4','video','error',?,?)", (now, now))
    conn.commit()
    facts = h.collect(conn, ".")
    assert h.evaluate(facts)["errors"][0] == h.WARN


# --- возраст бэкапа -------------------------------------------------------

def test_backup_age_none_when_no_backups(tmp_path):
    assert h.backup_age_hours(str(tmp_path)) is None


def test_backup_age_none_when_dir_missing():
    assert h.backup_age_hours("/нет/такой/папки") is None


def test_backup_age_reads_newest(tmp_path):
    (tmp_path / "sar_backup_20260101_120000.zip").write_bytes(b"x")
    fresh = datetime.now() - timedelta(hours=3)
    (tmp_path / f"sar_backup_{fresh:%Y%m%d_%H%M%S}.zip").write_bytes(b"x")
    age = h.backup_age_hours(str(tmp_path))
    assert 2.5 < age < 3.5, age


def test_no_backup_at_all_is_critical(conn):
    facts = h.collect(conn, ".")
    facts["backup_age_hours"] = None
    assert h.evaluate(facts)["backup"][0] == h.CRIT


def test_fresh_backup_is_ok(conn):
    facts = h.collect(conn, ".")
    facts["backup_age_hours"] = 5.0
    assert h.evaluate(facts)["backup"][0] == h.OK


# --- Prometheus -----------------------------------------------------------

def test_missing_metric_is_omitted_not_zeroed(conn):
    """Главный тест формата.

    Отсутствующий бэкап обязан ОТСУТСТВОВАТЬ в выводе, а не быть нулём:
    Grafana считает "time() - 0" и рисует полвека, что выглядит как данные.
    Эти грабли описаны в мониторинге wildflight -- повторять незачем."""
    facts = h.collect(conn, ".")
    facts["backup_age_hours"] = None
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert "sar_backup_age_seconds" not in text


def test_present_metric_is_rendered(conn):
    facts = h.collect(conn, ".")
    facts["backup_age_hours"] = 2.0
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert "sar_backup_age_seconds 7200.0" in text


def test_prometheus_format_is_parseable(conn):
    """Каждая строка -- либо комментарий, либо 'имя[{метки}] число'."""
    sar_common.touch_heartbeat(conn, "worker")
    facts = h.collect(conn, ".")
    text = h.render_prometheus(facts, h.evaluate(facts))
    for line in text.strip().split("\n"):
        if line.startswith("#") or not line:
            continue
        name, _, value = line.rpartition(" ")
        assert name, line
        float(value)          # бросит, если это не число


def test_check_levels_are_exported_as_numbers(conn):
    facts = h.collect(conn, ".")
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert 'sar_check_level{check="disk"}' in text
    assert 'sar_check_level{check="backup"}' in text


def test_reports_are_broken_down_by_status(conn):
    now = datetime.now().isoformat()
    for i, st in enumerate(("done", "done", "error")):
        conn.execute(
            "INSERT INTO reports (report_id,rel_path,abs_path,kind,status,created_at,updated_at) "
            "VALUES (?,?,?,'video',?,?,?)", (f"r{i}", f"{i}.mp4", f"/{i}.mp4", st, now, now))
    conn.commit()
    facts = h.collect(conn, ".")
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert 'sar_reports{status="done"} 2' in text
    assert 'sar_reports{status="error"} 1' in text


# --- сводный уровень ------------------------------------------------------

def test_overall_takes_the_worst(conn):
    assert h.overall({"a": (h.OK, ""), "b": (h.WARN, ""), "c": (h.CRIT, "")}) == h.CRIT
    assert h.overall({"a": (h.OK, ""), "b": (h.WARN, "")}) == h.WARN
    assert h.overall({"a": (h.OK, "")}) == h.OK


def test_streams_table_absent_does_not_break_collect(conn):
    """Ветка стримов ещё не влита -- мониторинг обязан работать без неё."""
    conn.execute("DROP TABLE IF EXISTS streams")
    conn.commit()
    facts = h.collect(conn, ".")
    assert facts["streams_active"] is None
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert "sar_streams_active" not in text
