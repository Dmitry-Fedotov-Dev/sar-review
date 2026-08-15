"""Полоса покрытия ручного просмотра должна работать ДО обработки детектором.

Из эксплуатации: очередь на CPU может быть на сутки вперёд, а ручной плеер
доступен с момента появления файла (см. test_player_accessibility.py). Люди
реально смотрят видео, пока оно стоит в очереди -- и именно тогда важнее
всего видеть, какие куски уже отсмотрены, потому что автоматических
подсказок ещё нет вообще. Раньше полоса показывалась только для status='done'.

Для этого нужна длительность видео, которую детектор пишет только когда
доходит до файла -- поэтому watcher в sar_worker.py заполняет duration_sec
заранее, из метаданных контейнера (кадры не декодируются)."""
import sqlite3

import cv2
import numpy as np

import sar_common
import sar_server
import sar_worker


def _make_video(path, seconds=2, fps=10, width=160, height=120):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    for _ in range(seconds * fps):
        writer.write(np.zeros((height, width, 3), dtype=np.uint8))
    writer.release()


def _insert(db_path, report_id, rel_path, status, duration_sec=None, kind="video"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, duration_sec, "
        "out_dir, file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (report_id, rel_path, f"/watch/{rel_path}", kind, status, duration_sec, "/out", 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


def _watched(db_path, report_id, viewer, start, end):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO watch_segments (report_id, viewer_name, start_sec, end_sec, ts) VALUES (?,?,?,?,?)",
        (report_id, viewer, start, end, "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


def _client(app, db_path, watch_dir, monkeypatch):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": str(watch_dir)}, raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


# --- duration заполняется до обработки ---

def test_read_video_duration_from_metadata(tmp_path):
    video = tmp_path / "v.mp4"
    _make_video(video, seconds=3, fps=10)
    duration = sar_worker._read_video_duration(str(video))
    assert duration is not None
    assert 2.5 < duration < 3.5


def test_read_video_duration_returns_none_for_broken_file(tmp_path):
    bad = tmp_path / "not_a_video.mp4"
    bad.write_bytes(b"definitely not a video")
    assert sar_worker._read_video_duration(str(bad)) is None


def test_ensure_duration_fills_it_once(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "queued", duration_sec=None)
    video = tmp_path / "v.mp4"
    _make_video(video, seconds=2, fps=10)
    monkeypatch.setattr(sar_worker, "DB_PATH", db_path, raising=False)

    sar_worker._ensure_duration("rep1", str(video), None)

    conn = sqlite3.connect(db_path)
    got = conn.execute("SELECT duration_sec FROM reports WHERE report_id='rep1'").fetchone()[0]
    conn.close()
    assert got is not None and 1.5 < got < 2.5


def test_ensure_duration_is_noop_when_already_known(tmp_path, monkeypatch):
    # на каждом скане не должно быть ни чтения файла, ни записи в БД
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "queued", duration_sec=42.0)
    monkeypatch.setattr(sar_worker, "DB_PATH", db_path, raising=False)

    def _boom(*a, **kw):
        raise AssertionError("не должно читать видео, длительность уже известна")
    monkeypatch.setattr(sar_worker, "_read_video_duration", _boom)

    sar_worker._ensure_duration("rep1", "/whatever.mp4", 42.0)

    conn = sqlite3.connect(db_path)
    got = conn.execute("SELECT duration_sec FROM reports WHERE report_id='rep1'").fetchone()[0]
    conn.close()
    assert got == 42.0


# --- /api/tree отдаёт покрытие до обработки ---

def test_tree_returns_coverage_for_queued_video(tmp_path, monkeypatch):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "v.mp4").write_bytes(b"fake")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "queued", duration_sec=100.0)
    _watched(db_path, "rep1", "tester", 0.0, 50.0)

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    item = client.get("/api/tree").get_json()["items"][0]

    assert item["status"] == "queued"
    assert item["buckets"], "полоса покрытия должна быть и для файла в очереди"
    assert item["percent"] == 50.0
    assert item["viewer_count"] == 1


def test_tree_returns_coverage_for_processing_video(tmp_path, monkeypatch):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "v.mp4").write_bytes(b"fake")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "processing", duration_sec=200.0)
    _watched(db_path, "rep1", "a", 0.0, 50.0)

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    item = client.get("/api/tree").get_json()["items"][0]
    assert item["percent"] == 25.0
    assert item["buckets"]


def test_tree_handles_video_without_known_duration(tmp_path, monkeypatch):
    # длительность ещё не успели прочитать -- не должно падать, просто нет полосы
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "v.mp4").write_bytes(b"fake")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "queued", duration_sec=None)

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    item = client.get("/api/tree").get_json()["items"][0]
    assert item["percent"] is None
    assert not item["buckets"]


def test_frontend_no_longer_gates_coverage_bar_on_done():
    html = sar_server.TREE_PAGE_HTML
    assert "it.kind === 'video' && it.buckets" in html
    assert "it.status === 'done' && it.buckets" not in html
