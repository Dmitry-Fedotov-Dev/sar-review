"""Тесты: ручные наблюдения (POST /api/report/<id>/observations) теперь
получают не только координаты дрона, но и "вероятные координаты" объекта
(та же оценка, что и для AI-детекций, см. sar_common.estimate_ground_point) --
bbox ручного наблюдения уже нормализован 0..1 (см. схему manual_observations
в sar_common.py), поэтому используется напрямую, без размеров кадра."""
import sqlite3

import sar_common
import sar_server


SRT_CONTENT = (
    "1\n00:00:00,000 --> 00:00:10,000\n"
    "[latitude: 42.000000] [longitude: 74.000000] [rel_alt: 1000.0] "
    "[gb_yaw: 0.0 gb_pitch: -45.0 gb_roll: 0.0]\n"
)


def _client(app, db_path, script_dir, monkeypatch):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", script_dir)
    monkeypatch.setattr(sar_server, "_telemetry_cache", {})
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX", {"by_stem": {}, "by_timestamp": []})
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


def _setup_report(tmp_path):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake")
    (tmp_path / "video.srt").write_text(SRT_CONTENT, encoding="utf-8")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep1", "video.mp4", str(video_path), "video", "done", str(tmp_path / "out"), 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    return db_path


def test_observation_gets_drone_and_estimated_coordinates(tmp_path, monkeypatch):
    db_path = _setup_report(tmp_path)
    client = _client(sar_server.app, db_path, str(tmp_path), monkeypatch)

    resp = client.post("/api/report/rep1/observations", json={
        "timestamp_sec": 1.0, "bbox": [0.5, 0.5, 0.5, 0.5], "label": "человек",
    })
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    resp2 = client.get("/api/report/rep1/observations")
    obs = resp2.get_json()
    assert len(obs) == 1
    o = obs[0]

    assert o["lat"] == 42.0 and o["lon"] == 74.0  # координаты дрона
    assert o["est_lat"] is not None and o["est_lon"] is not None
    assert o["est_distance_m"] > 0
    # оценка -- НЕ то же самое, что координаты дрона
    assert (o["est_lat"], o["est_lon"]) != (o["lat"], o["lon"])
    # "вся телеметрия кадра" -- полный текст SRT-блока, не только разобранные поля
    assert o["raw_telemetry"] is not None
    assert "latitude: 42.000000" in o["raw_telemetry"]
    assert "gb_pitch: -45.0" in o["raw_telemetry"]


def test_observation_without_telemetry_has_no_estimate(tmp_path, monkeypatch):
    video_path = tmp_path / "no_telemetry.mp4"
    video_path.write_bytes(b"fake")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep2", "no_telemetry.mp4", str(video_path), "video", "done", str(tmp_path / "out"), 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    client = _client(sar_server.app, db_path, str(tmp_path), monkeypatch)
    resp = client.post("/api/report/rep2/observations", json={
        "timestamp_sec": 1.0, "bbox": [0.5, 0.5, 0.5, 0.5], "label": "человек",
    })
    assert resp.status_code == 200

    obs = client.get("/api/report/rep2/observations").get_json()
    assert obs[0]["lat"] is None
    assert obs[0]["est_lat"] is None
