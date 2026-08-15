"""Тесты превью (первый кадр видео) для списка файлов на главной странице.

Генерация -- в sar_worker.py (watcher_loop/_generate_thumbnail), sar_server.py
только ОТДАЁТ уже готовый файл (см. api_thumbnail) -- та же граница
ответственности, что и для report.html/detections.json/crops в остальном
проекте. Реальный cv2 здесь используется напрямую (не мок) -- это простое
чтение кадра видео, а не ML-инференс (YOLO/torch), который CLAUDE.md просит
не запускать в тестах; тот же подход уже применялся в
tests/test_process_video_geolocation.py."""
import os
import time

import cv2
import numpy as np

import sar_common
import sar_server
import sar_worker


def _make_video(path, width=640, height=480, color=(10, 20, 30)):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (width, height))
    frame = np.full((height, width, 3), color, dtype=np.uint8)
    writer.write(frame)
    writer.release()


def test_generate_thumbnail_creates_valid_jpeg(tmp_path):
    video_path = tmp_path / "video.mp4"
    _make_video(video_path)
    thumb_path = tmp_path / "thumb.jpg"

    ok = sar_worker._generate_thumbnail(str(video_path), str(thumb_path))
    assert ok is True
    assert thumb_path.exists()

    # реально валидный JPEG -- перечитываем через cv2, а не просто проверяем размер файла
    img = cv2.imread(str(thumb_path))
    assert img is not None
    assert img.shape[2] == 3


def test_generate_thumbnail_downscales_wide_frames(tmp_path):
    video_path = tmp_path / "wide.mp4"
    _make_video(video_path, width=1920, height=1080)
    thumb_path = tmp_path / "thumb.jpg"

    sar_worker._generate_thumbnail(str(video_path), str(thumb_path), max_width=320)
    img = cv2.imread(str(thumb_path))
    assert img.shape[1] == 320
    assert img.shape[0] == 180  # 1080 * (320/1920)


def test_generate_thumbnail_no_temp_file_left_behind(tmp_path):
    video_path = tmp_path / "video.mp4"
    _make_video(video_path)
    thumb_path = tmp_path / "thumb.jpg"
    sar_worker._generate_thumbnail(str(video_path), str(thumb_path))
    assert not os.path.exists(str(thumb_path) + ".tmp")


def test_generate_thumbnail_returns_false_for_unreadable_video(tmp_path):
    fake_video = tmp_path / "not_really_a_video.mp4"
    fake_video.write_bytes(b"this is not a video file")
    thumb_path = tmp_path / "thumb.jpg"
    ok = sar_worker._generate_thumbnail(str(fake_video), str(thumb_path))
    assert ok is False
    assert not thumb_path.exists()


def test_ensure_thumbnail_generates_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(tmp_path))
    video_path = tmp_path / "video.mp4"
    _make_video(video_path)

    sar_worker._ensure_thumbnail("video.mp4", str(video_path))
    thumb_path = sar_common.get_thumbnail_path(str(tmp_path), "video.mp4")
    assert os.path.exists(thumb_path)


def test_ensure_thumbnail_regenerates_when_video_is_newer(tmp_path, monkeypatch):
    """Регрессия на ту же идею, что и с ctime-дрейфом report_id: если видео
    под тем же именем перезаписали (например, повторная загрузка) -- старое
    закэшированное превью не должно остаться навсегда."""
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(tmp_path))
    video_path = tmp_path / "video.mp4"
    _make_video(video_path, color=(10, 10, 10))
    sar_worker._ensure_thumbnail("video.mp4", str(video_path))
    thumb_path = sar_common.get_thumbnail_path(str(tmp_path), "video.mp4")
    first_bytes = open(thumb_path, "rb").read()

    time.sleep(0.05)
    _make_video(video_path, color=(200, 200, 200))  # "перезаписали" видео новым содержимым
    os.utime(video_path, None)  # гарантируем более новый mtime
    sar_worker._ensure_thumbnail("video.mp4", str(video_path))
    second_bytes = open(thumb_path, "rb").read()

    assert first_bytes != second_bytes


def test_ensure_thumbnail_does_not_regenerate_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(tmp_path))
    video_path = tmp_path / "video.mp4"
    _make_video(video_path)
    sar_worker._ensure_thumbnail("video.mp4", str(video_path))
    thumb_path = sar_common.get_thumbnail_path(str(tmp_path), "video.mp4")
    mtime_after_first = os.path.getmtime(thumb_path)

    time.sleep(0.05)
    sar_worker._ensure_thumbnail("video.mp4", str(video_path))
    assert os.path.getmtime(thumb_path) == mtime_after_first  # не тронули повторно


def _client(app, watch_dir, data_dir, monkeypatch):
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": str(watch_dir)}, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data_dir), raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


def test_api_thumbnail_serves_generated_file(tmp_path, monkeypatch):
    video_path = tmp_path / "video.mp4"
    _make_video(video_path)
    thumb_path = sar_common.get_thumbnail_path(str(tmp_path), "video.mp4")
    sar_worker._generate_thumbnail(str(video_path), thumb_path)

    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch)
    resp = client.get("/api/thumbnail/video.mp4")
    assert resp.status_code == 200
    assert resp.content_type == "image/jpeg"
    assert resp.data == open(thumb_path, "rb").read()


def test_api_thumbnail_404_when_not_yet_generated(tmp_path, monkeypatch):
    video_path = tmp_path / "video.mp4"
    _make_video(video_path)  # видео есть, превью воркер ещё не сгенерировал

    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch)
    resp = client.get("/api/thumbnail/video.mp4")
    assert resp.status_code == 404


def test_api_thumbnail_404_for_unknown_file(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch)
    resp = client.get("/api/thumbnail/does_not_exist.mp4")
    assert resp.status_code == 404


def test_api_thumbnail_404_for_photo_kind(tmp_path, monkeypatch):
    # превью реализовано только для видео -- фото не должно отдаваться через
    # этот же эндпоинт, даже если как-то оказался закэшированный файл
    photo_path = tmp_path / "photo.jpg"
    photo_path.write_bytes(b"fake jpeg")

    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch)
    resp = client.get("/api/thumbnail/photo.jpg")
    assert resp.status_code == 404


def test_api_thumbnail_rejects_path_traversal(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch)
    resp = client.get("/api/thumbnail/..%2f..%2fsecret.mp4")
    assert resp.status_code == 404
