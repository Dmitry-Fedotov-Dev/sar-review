"""Откуда берётся кадр для превью находки.

ЧЕТВЁРТЫЙ случай одной и той же ошибки в этом проекте: вопрос «где файл»
задаётся ДИСКУ, а облачный материал на него не отвечает -- оригинала там
нет по определению.

    abs_path = sar_common.material_path(WATCH_DIR, rel_path)
    if not abs_path or not os.path.exists(abs_path):
        continue

Пометка на облачном видео молча оставалась без превью навсегда: в списке
находок пустой квадрат вместо кадра, и находку не отличить от соседней,
не открыв её. Причём пропуск тихий -- ни строки в журнале.

Предыдущие три: отдача видео (404 при готовой копии), find_material_file,
отдача превью материала.
"""
import json
import os

import pytest

import sar_common
import sar_worker


@pytest.fixture
def env(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('c1','оп/DJI_1.MP4','','video','idle','f1',"
        "datetime('now'),datetime('now'))")
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES "
        "('c1','Иван',12.5,?,'жёлтый предмет',datetime('now'))",
        (json.dumps([0.1, 0.1, 0.4, 0.4]),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sar_worker, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_worker, "WATCH_DIR", str(watch), raising=False)
    return db, str(watch), str(data)


def make_proxy(data, rel="оп/DJI_1.MP4"):
    p = sar_common.proxy_video_path(data, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"proxy")
    return p


def run(db, monkeypatch):
    """Прогоняет проход, перехватывая саму нарезку кадра."""
    used = []
    monkeypatch.setattr(
        sar_worker, "_generate_finding_preview",
        lambda src, secs, bbox, small, full: (used.append((src, secs, bbox)), True)[1])
    conn = sar_common.get_db_connection(db)
    try:
        sar_worker.ensure_finding_previews(conn)
    finally:
        conn.close()
    return used


# --- главное ---------------------------------------------------------------

def test_preview_is_cut_from_the_proxy_when_there_is_no_original(env, monkeypatch):
    """РАДИ ЭТОГО ВСЁ. Оригинала облачного видео на диске нет и не будет --
    пока человек не попросит его скачать. Копия есть."""
    db, _, data = env
    proxy = make_proxy(data)
    used = run(db, monkeypatch)
    assert used, "превью не сделано вовсе"
    assert os.path.normcase(used[0][0]) == os.path.normcase(proxy)


def test_original_wins_over_the_proxy(env, monkeypatch):
    """Оригинал качеством выше -- если он есть, берём его."""
    db, watch, data = env
    make_proxy(data)
    orig = os.path.join(watch, "оп", "DJI_1.MP4")
    os.makedirs(os.path.dirname(orig), exist_ok=True)
    open(orig, "wb").write(b"original")
    used = run(db, monkeypatch)
    assert os.path.normcase(used[0][0]) == os.path.normcase(orig)


def test_nothing_available_is_skipped_quietly(env, monkeypatch):
    """Ни оригинала, ни копии -- это «рано», а не ошибка: копия появится,
    когда человек попросит подготовить материал."""
    db, _, _ = env
    assert run(db, monkeypatch) == []


def test_timestamp_and_bbox_survive(env, monkeypatch):
    """Копия -- тот же материал с теми же таймкодами. Если бы они ехали,
    кадр вырезался бы не в том месте."""
    db, _, data = env
    make_proxy(data)
    used = run(db, monkeypatch)
    assert used[0][1] == 12.5
    assert used[0][2] == [0.1, 0.1, 0.4, 0.4]


# --- страж -----------------------------------------------------------------

def test_source_is_not_looked_up_on_disk_only():
    """Тот же вопрос уже четырежды задавали диску. Страж на пятый раз."""
    import inspect
    src = inspect.getsource(sar_worker.ensure_finding_previews)
    assert "find_material_file" in src, (
        "источник кадра снова ищется только в наблюдаемой папке")
    assert "proxy_video_path" in src, "лёгкая копия не рассматривается"
