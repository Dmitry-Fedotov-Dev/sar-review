"""Тесты для загрузки SRT-телеметрии через браузер (POST /api/upload_telemetry).

Та же серверная защита по имени 'uploader', что и у /api/upload (см.
tests/test_upload.py) -- сюда не дублируем те проверки целиком, но явно
проверяем 403 для не-uploader'а, чтобы не полагаться только на скрытую
кнопку в UI. Отдельно: множественная загрузка (несколько SRT за один
запрос) и то, что индекс telemetry/ перестраивается сразу же, без рестарта
сервера."""
import io

import sar_common
import sar_server


def _client(app, watch_dir, script_dir, monkeypatch, viewer_name):
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": str(watch_dir)}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", str(script_dir))
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX", {"by_stem": {}, "by_timestamp": []})
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = viewer_name
    return client


def test_index_shows_telemetry_upload_input_only_for_uploader(tmp_path, monkeypatch):
    client_uploader = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "uploader")
    resp = client_uploader.get("/")
    assert b'id="upload-telemetry-input"' in resp.data

    client_other = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "spasatel")
    resp2 = client_other.get("/")
    assert b'id="upload-telemetry-input"' not in resp2.data


def test_upload_telemetry_rejected_for_non_uploader(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "spasatel")
    resp = client.post("/api/upload_telemetry", data={
        "telemetry": (io.BytesIO(b"fake srt"), "DJI_0001.SRT"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 403
    telemetry_dir = tmp_path / "telemetry"
    assert not telemetry_dir.exists() or list(telemetry_dir.iterdir()) == []


def test_upload_telemetry_single_file_succeeds(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload_telemetry", data={
        "telemetry": (io.BytesIO(b"1\n00:00:00,000 --> 00:00:01,000\nfake"), "DJI_0001.SRT"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["saved"] == ["DJI_0001.srt"]  # расширение нормализуется в нижний регистр

    saved_path = tmp_path / "telemetry" / "DJI_0001.srt"
    assert saved_path.exists()
    assert not (tmp_path / "telemetry" / "DJI_0001.srt.uploading").exists()


def test_upload_telemetry_multiple_files_at_once(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload_telemetry", data={
        "telemetry": [
            (io.BytesIO(b"srt content 1"), "DJI_0001.SRT"),
            (io.BytesIO(b"srt content 2"), "DJI_0002.SRT"),
            (io.BytesIO(b"srt content 3"), "DJI_0003.SRT"),
        ],
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert sorted(data["saved"]) == ["DJI_0001.srt", "DJI_0002.srt", "DJI_0003.srt"]
    assert len(list((tmp_path / "telemetry").iterdir())) == 3


def test_upload_telemetry_rejects_non_srt_but_keeps_valid_ones(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload_telemetry", data={
        "telemetry": [
            (io.BytesIO(b"real telemetry"), "DJI_0001.SRT"),
            (io.BytesIO(b"not telemetry"), "notes.txt"),
        ],
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False  # были ошибки
    assert data["saved"] == ["DJI_0001.srt"]
    assert len(data["errors"]) == 1
    assert "notes.txt" in data["errors"][0]


def test_upload_telemetry_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    existing = telemetry_dir / "DJI_0001.srt"
    existing.write_bytes(b"ORIGINAL -- MUST SURVIVE")

    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload_telemetry", data={
        "telemetry": (io.BytesIO(b"new upload"), "DJI_0001.SRT"),
    }, content_type="multipart/form-data")
    data = resp.get_json()
    assert data["saved"] == ["DJI_0001_1.srt"]
    assert existing.read_bytes() == b"ORIGINAL -- MUST SURVIVE"


def test_upload_telemetry_no_files_returns_400(tmp_path, monkeypatch):
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "uploader")
    resp = client.post("/api/upload_telemetry", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_telemetry_rebuilds_index_without_restart(tmp_path, monkeypatch):
    """Раньше индекс telemetry/ строился только один раз при старте сервера --
    загруженный файл не находился бы, пока сервер не перезапустят."""
    client = _client(sar_server.app, tmp_path, tmp_path, monkeypatch, "uploader")
    assert sar_server._TELEMETRY_INDEX["by_stem"] == {}

    client.post("/api/upload_telemetry", data={
        "telemetry": (io.BytesIO(b"telemetry"), "DJI_20260101000000_0001_Z.SRT"),
    }, content_type="multipart/form-data")

    assert "dji_20260101000000_0001_z" in sar_server._TELEMETRY_INDEX["by_stem"]
    match, reason = sar_common.find_telemetry_for_video(
        "DJI_20260101000000_0001_Z.MP4", sar_server._TELEMETRY_INDEX)
    assert match is not None
