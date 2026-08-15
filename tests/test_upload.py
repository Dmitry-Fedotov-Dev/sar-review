"""Тесты для загрузки видео через браузер (POST /api/upload) и видимости
кнопки загрузки на главной странице.

Доступ ограничен именем пользователя 'uploader' -- ЕДИНСТВЕННАЯ реальная
защита здесь серверная (сессия), кнопка в UI просто скрыта для остальных.
Явно проверяем, что и без кнопки в HTML эндпоинт всё равно недоступен
не-uploader'у -- это то, что действительно останавливает обход через curl/
devtools, а не просто спрятанная кнопка."""
import io

import sar_server


def _client(app, watch_dir, monkeypatch, viewer_name):
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": str(watch_dir)}, raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = viewer_name
    return client


def test_index_shows_upload_box_only_for_uploader(tmp_path, monkeypatch):
    client_uploader = _client(sar_server.app, tmp_path, monkeypatch, "uploader")
    resp = client_uploader.get("/")
    assert resp.status_code == 200
    assert b'id="upload-file-input"' in resp.data

    client_other = _client(sar_server.app, tmp_path, monkeypatch, "spasatel")
    resp2 = client_other.get("/")
    assert resp2.status_code == 200
    assert b'id="upload-file-input"' not in resp2.data


def test_upload_rejected_for_non_uploader_and_file_not_written(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, monkeypatch, "spasatel")
    resp = client.post("/api/upload", data={
        "video": (io.BytesIO(b"fake video bytes"), "DJI_test.MP4"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 403
    assert list(tmp_path.iterdir()) == []


def test_upload_succeeds_for_uploader_and_writes_file(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload", data={
        "video": (io.BytesIO(b"fake video bytes"), "DJI_test.MP4"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    # расширение нормализуется в нижний регистр -- это НУЖНО, а не побочный
    # эффект: sar_common.VIDEO_EXTS хранит расширения в нижнем регистре, и
    # именно по ним watcher_loop потом узнаёт файл на следующем скане
    assert data["filename"] == "DJI_test.mp4"

    saved = tmp_path / "DJI_test.mp4"
    assert saved.exists()
    assert saved.read_bytes() == b"fake video bytes"
    # временный файл не должен остаться на диске
    assert not (tmp_path / "DJI_test.mp4.uploading").exists()


def test_upload_rejects_non_video_extension(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload", data={
        "video": (io.BytesIO(b"not a video"), "notes.txt"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert list(tmp_path.iterdir()) == []


def test_upload_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    existing = tmp_path / "DJI_test.mp4"  # уже на диске в нормализованном (нижнем) регистре
    existing.write_bytes(b"ORIGINAL CONTENT -- MUST SURVIVE")

    client = _client(sar_server.app, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload", data={
        "video": (io.BytesIO(b"new upload"), "DJI_test.MP4"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["filename"] == "DJI_test_1.mp4"

    assert existing.read_bytes() == b"ORIGINAL CONTENT -- MUST SURVIVE"
    assert (tmp_path / "DJI_test_1.mp4").read_bytes() == b"new upload"


def test_upload_sanitizes_path_traversal_in_filename(tmp_path, monkeypatch):
    outside_dir = tmp_path.parent / "outside_marker_dir"
    client = _client(sar_server.app, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload", data={
        "video": (io.BytesIO(b"malicious"), "../../evil.mp4"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    # файл должен оказаться ВНУТРИ watch_dir, а не подняться по дереву папок
    saved_files = list(tmp_path.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].parent == tmp_path
    assert not outside_dir.exists()


def test_upload_no_file_field_returns_400(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
