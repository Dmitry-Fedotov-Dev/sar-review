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


# --- онлайн: совпадение типов с тем, что пишет сервер ---------------------

def _heartbeat(conn, name, seconds_ago=0.0):
    """Пишет отметку присутствия ТОЧНО ТАК ЖЕ, как api_heartbeat в
    sar_server.py -- числом time.time(), а не строкой."""
    import time as _t
    conn.execute(
        "INSERT INTO presence (viewer_name, last_seen) VALUES (?,?) "
        "ON CONFLICT(viewer_name) DO UPDATE SET last_seen=excluded.last_seen",
        (name, _t.time() - seconds_ago))
    conn.commit()


def test_online_counts_a_present_viewer(conn):
    """Регрессия на реальный баг.

    Первая версия сравнивала числовой столбец last_seen со строкой ISO.
    В SQLite число всегда сортируется раньше текста, поэтому условие было
    ложным ВСЕГДА -- метрика показывала ноль при любом числе зрителей и
    ни на что не жаловалась. Поймано на живом дашборде, а не тестом,
    потому что тест писал отметку в своём формате, а не в серверном."""
    _heartbeat(conn, "Айгуль", seconds_ago=5)
    facts = h.collect(conn, ".")
    assert facts["viewers_online"] == 1


def test_online_ignores_stale_viewers(conn):
    _heartbeat(conn, "давно-ушёл", seconds_ago=600)
    facts = h.collect(conn, ".")
    assert facts["viewers_online"] == 0


def test_online_counts_each_person_once(conn):
    _heartbeat(conn, "Айгуль", seconds_ago=1)
    _heartbeat(conn, "Айгуль", seconds_ago=1)      # повторный heartbeat
    _heartbeat(conn, "Karpuha", seconds_ago=2)
    facts = h.collect(conn, ".")
    assert facts["viewers_online"] == 2


def test_online_window_matches_the_server():
    """Окно должно совпадать с ONLINE_WINDOW_SEC сервера, иначе главная
    страница и дашборд покажут разные числа и им перестанут верить."""
    import re
    with open("sar_server.py", encoding="utf-8") as f:
        m = re.search(r"^ONLINE_WINDOW_SEC\s*=\s*(\d+)", f.read(), re.M)
    assert m, "не нашёл ONLINE_WINDOW_SEC в sar_server.py"
    assert int(m.group(1)) == h.ONLINE_WINDOW_SEC


def test_online_is_exported_as_a_metric(conn):
    _heartbeat(conn, "Айгуль", seconds_ago=3)
    facts = h.collect(conn, ".")
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert "sar_viewers_online 1" in text


# --- железо: процессор и память -------------------------------------------

def test_hardware_metrics_are_collected(conn):
    if h.psutil is None:
        pytest.skip("psutil не установлен")
    h.hardware()                       # поднимаем фоновый замер
    import time as _t
    for _ in range(30):                # ждём первую пробу
        if h.hardware().get("cpu_cores"):
            break
        _t.sleep(0.5)
    facts = h.collect(conn, ".")
    assert facts["cpu_cores"] >= 1
    assert 0.0 <= facts["cpu_percent"] <= 100.0
    assert facts["mem_total_bytes"] > 0
    assert 0 < facts["mem_used_bytes"] <= facts["mem_total_bytes"]


def test_repeated_calls_do_not_zero_the_cpu_metric():
    """Регрессия на реальный баг.

    Первая версия звала psutil.cpu_percent(interval=None) прямо в
    обработчике. Эта функция возвращает долю времени С ПРОШЛОГО ВЫЗОВА В
    ЭТОМ ПРОЦЕССЕ, а вызывающих несколько: Prometheus раз в 30 секунд плюс
    ручные запросы. Кто приходил вторым в пределах секунды, получал почти
    нулевое окно -- и на боевом сервере метрика показывала 0.0 ВСЕГДА,
    притом что psutil в отдельном процессе давал 7-15%.

    Теперь значение берётся из фонового замера, поэтому частые вызовы
    подряд обязаны отдавать одно и то же, а не обнулять друг друга."""
    if h.psutil is None:
        pytest.skip("psutil не установлен")
    import time as _t
    h.hardware()
    for _ in range(30):
        if h.hardware().get("cpu_cores"):
            break
        _t.sleep(0.5)
    a = h.hardware()
    b = h.hardware()                   # сразу вторым, как делал Prometheus
    c = h.hardware()
    assert a.get("cpu_percent") == b.get("cpu_percent") == c.get("cpu_percent")
    assert a.get("cpu_cores") == b.get("cpu_cores")


def test_hardware_is_absent_until_first_sample(monkeypatch):
    """Пока фоновый замер не отработал, значений нет -- и это честнее
    нуля, который не отличить от простаивающей машины."""
    monkeypatch.setattr(h, "_HW_SAMPLE", {})
    monkeypatch.setattr(h, "_HW_THREAD", object())   # не поднимать поток
    assert h.hardware() == {}


def test_hardware_is_exported_with_the_ceiling(conn):
    """Потолок обязателен: без него проценты не читаются -- 80% на двух
    ядрах и на двадцати означают совершенно разное."""
    facts = h.collect(conn, ".")
    if h.psutil is None:
        pytest.skip("psutil не установлен")
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert "sar_cpu_cores" in text
    assert "sar_cpu_percent" in text
    assert "sar_memory_total_bytes" in text
    assert "sar_memory_used_bytes" in text


def test_missing_psutil_does_not_break_anything(conn, monkeypatch):
    """Зависимость мягкая: без psutil метрики железа отсутствуют, всё
    остальное продолжает считаться."""
    monkeypatch.setattr(h, "psutil", None)
    facts = h.collect(conn, ".")
    assert "cpu_percent" not in facts
    assert "reports_by_status" in facts          # остальное на месте
    text = h.render_prometheus(facts, h.evaluate(facts))
    assert "sar_cpu_percent" not in text          # отсутствующее не нулём
    assert "sar_up 1" in text


def test_hardware_never_blocks(conn):
    """psutil.cpu_percent(interval=None) не должен ждать: блокирующий замер
    добавил бы секунду к каждому скрейпу."""
    import time as _t
    t0 = _t.time()
    h.hardware()
    assert _t.time() - t0 < 0.3


# --- размер отчётов не должен блокировать запрос --------------------------

def test_dir_size_never_blocks_the_caller(tmp_path, monkeypatch):
    """Регрессия на реальную поломку.

    Первая версия считала размер папки отчётов прямо в обработчике
    /healthz. На боевой машине там 573 тысячи файлов -- запрос завис, и
    сервер перестал отвечать. Плюс эндпоинт открыт без пароля, то есть это
    ещё и способ положить платформу повторными запросами.

    Теперь вызов обязан возвращаться немедленно, даже если сам обход
    бесконечно долгий.
    """
    import time as _t

    started = {"n": 0}

    def slow_walk(path):
        started["n"] += 1
        _t.sleep(30)          # если вызывающего заблокирует -- тест провалится
        return 123

    monkeypatch.setattr(h, "dir_size_bytes", slow_walk)
    monkeypatch.setattr(h, "_SIZE_CACHE", {})
    monkeypatch.setattr(h, "_SIZE_INFLIGHT", set())

    t0 = _t.time()
    val = h.dir_size_cached(str(tmp_path))
    assert _t.time() - t0 < 1.0, "вызов заблокировался на обходе папки"
    assert val is None, "пока значения нет, надо отдавать None, а не ноль"


def test_dir_size_returns_cached_value(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_SIZE_CACHE", {str(tmp_path): (4242, 1000.0)})
    monkeypatch.setattr(h, "_SIZE_INFLIGHT", set())
    assert h.dir_size_cached(str(tmp_path), ttl=900, now=1100.0) == 4242


def test_stale_cache_is_still_served_while_refreshing(tmp_path, monkeypatch):
    """Протухшее значение лучше пустого: показать чуть устаревший размер
    полезнее, чем пробел, пока считается новый."""
    monkeypatch.setattr(h, "_SIZE_CACHE", {str(tmp_path): (4242, 0.0)})
    monkeypatch.setattr(h, "_SIZE_INFLIGHT", set())
    monkeypatch.setattr(h, "dir_size_bytes", lambda p: 999)
    assert h.dir_size_cached(str(tmp_path), ttl=1, now=1e6) == 4242


def test_only_one_background_walk_at_a_time(tmp_path, monkeypatch):
    """Иначе частые запросы к открытому эндпоинту наплодят потоков и
    заставят диск молотить параллельно -- ровно то, от чего уходили."""
    import time as _t
    calls = {"n": 0}

    def slow_walk(path):
        calls["n"] += 1
        _t.sleep(0.5)
        return 1

    monkeypatch.setattr(h, "dir_size_bytes", slow_walk)
    monkeypatch.setattr(h, "_SIZE_CACHE", {})
    monkeypatch.setattr(h, "_SIZE_INFLIGHT", set())
    for _ in range(10):
        h.dir_size_cached(str(tmp_path))
    _t.sleep(0.9)
    assert calls["n"] == 1, f"запущено обходов: {calls['n']}"


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
