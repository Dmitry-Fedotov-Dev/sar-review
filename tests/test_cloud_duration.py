"""Длительность облачного видео -- из лёгкой копии.

ПОЧЕМУ ЭТО НЕ КОСМЕТИКА. «Отснято» и «просмотрено» в шапке операции
складываются ТОЛЬКО по видео с известной длительностью: остальные не
участвуют ни в числителе, ни в знаменателе. На боевых данных из 114 видео
длительность была известна у 32 -- все облачные её не имели, потому что
`_ensure_duration` читает ОРИГИНАЛ, а обход папки облачные записи не видит.

Экран операции показывал «просмотрено 1 ч 21 мин из 1 ч 34 мин» -- то есть
86%, -- хотя это 86% от 28% материала: 80 видео из 114 не открывал никто.
Покрытие было завышено втрое, и по нему можно было решить, что смотреть
больше нечего. Ровно тот вид молчаливой ошибки, на котором проект уже
обжигался: число, похожее на ответ, посчитанное не по всем данным.
"""
import os

import pytest

import sar_common
import sar_worker


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('c1','оп/DJI_1.MP4','','video','idle','f1',datetime('now'),datetime('now'))")
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "duration_sec, cloud_file_id, created_at, updated_at) VALUES "
        "('c2','оп/DJI_2.MP4','','video','idle',137,'f2',datetime('now'),datetime('now'))")
    conn.commit()
    conn.close()
    monkeypatch.setattr(sar_worker, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_worker, "_failures", {}, raising=False)
    return db, str(data)


def make_proxy(data, rel):
    p = sar_common.proxy_video_path(data, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"proxy")
    return p


def duration(db, rid):
    conn = sar_common.get_db_connection(db)
    v = conn.execute("SELECT duration_sec FROM reports WHERE report_id=?",
                     (rid,)).fetchone()[0]
    conn.close()
    return v


def test_duration_is_read_from_the_proxy(env, monkeypatch):
    """ГЛАВНОЕ. Оригинала на диске нет и не будет, копия есть."""
    db, data = env
    make_proxy(data, "оп/DJI_1.MP4")
    monkeypatch.setattr(sar_worker, "_read_video_duration", lambda p: 212.5)
    sar_worker.ensure_cloud_durations()
    assert duration(db, "c1") == 212.5


def test_the_proxy_is_what_gets_read(env, monkeypatch):
    """Не оригинал: его нет, и попытка читать его -- бессмысленный отказ."""
    db, data = env
    make_proxy(data, "оп/DJI_1.MP4")
    seen = []
    monkeypatch.setattr(sar_worker, "_read_video_duration",
                        lambda p: seen.append(p) or 100.0)
    sar_worker.ensure_cloud_durations()
    assert seen and "proxies" in seen[0].replace("\\", "/")


def test_video_without_a_proxy_is_skipped_quietly(env, monkeypatch):
    """Копии ещё нет -- это не ошибка, а «рано». Отмечать неудачу здесь
    значит израсходовать попытки до того, как появится что читать."""
    db, _ = env
    calls = []
    monkeypatch.setattr(sar_worker, "_read_video_duration",
                        lambda p: calls.append(p) or 1.0)
    noted = []
    monkeypatch.setattr(sar_worker, "_note_failure",
                        lambda k, w: noted.append(k) or 1)
    sar_worker.ensure_cloud_durations()
    assert calls == [] and noted == []
    assert duration(db, "c1") is None


def test_known_duration_is_not_recomputed(env, monkeypatch):
    db, data = env
    make_proxy(data, "оп/DJI_2.MP4")
    calls = []
    monkeypatch.setattr(sar_worker, "_read_video_duration",
                        lambda p: calls.append(p) or 999.0)
    sar_worker.ensure_cloud_durations()
    assert duration(db, "c2") == 137, "перечитали уже известную длительность"


def test_unreadable_proxy_backs_off(env, monkeypatch):
    """Битая копия отказывает одинаково каждый раз -- без паузы проход
    будет биться о неё каждые 15 секунд."""
    db, data = env
    make_proxy(data, "оп/DJI_1.MP4")
    monkeypatch.setattr(sar_worker, "_read_video_duration", lambda p: None)
    noted = []
    monkeypatch.setattr(sar_worker, "_note_failure",
                        lambda k, w: noted.append(k) or 1)
    sar_worker.ensure_cloud_durations()
    assert noted and noted[0].startswith("cdur:")
    assert duration(db, "c1") is None


def test_pass_has_a_budget(env, monkeypatch):
    db, data = env
    make_proxy(data, "оп/DJI_1.MP4")
    monkeypatch.setattr(sar_worker, "DURATIONS_PER_PASS", 0)
    monkeypatch.setattr(sar_worker, "_read_video_duration", lambda p: 10.0)
    sar_worker.ensure_cloud_durations()
    assert duration(db, "c1") is None


def test_local_material_is_not_touched_here(env, monkeypatch):
    """У локального видео свой путь (_ensure_duration по оригиналу).
    Дублировать работу незачем."""
    db, data = env
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('L1','local.MP4','','video','done',"
        "datetime('now'),datetime('now'))")
    conn.commit()
    conn.close()
    make_proxy(data, "local.MP4")
    monkeypatch.setattr(sar_worker, "_read_video_duration", lambda p: 50.0)
    sar_worker.ensure_cloud_durations()
    assert duration(db, "L1") is None
