"""Тесты триажа детекций (detection_priorities) -- обсуждалось с
пользователем отдельно перед реализацией: 5 статусов (confirmed_person,
likely_person, confirmed_object, likely_object, rejected), ставит только
человек через /api/report/<id>/priorities, сервер сам туда никогда не
пишет. Плюс счётчики manual_count/ai_count на /api/tree для сортировки на
главной странице."""
import json
import sqlite3

import sar_common
import sar_server


def _client(app, db_path, watch_dir, monkeypatch, viewer_name="tester"):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": str(watch_dir)}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", "/no/such/config/dir", raising=False)
    monkeypatch.setattr(sar_server, "_ai_scenes_cache", {}, raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = viewer_name
    return client


def _insert_report(db_path, report_id, rel_path, status, out_dir="/out",
                    updated_at="2026-01-01T00:00:00", kind="video"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, progress_pct, "
        "out_dir, file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (report_id, rel_path, f"/watch/{rel_path}", kind, status, 0.0,
         out_dir, 0.0, updated_at, updated_at))
    conn.commit()
    conn.close()


def _hit(frame_idx, cx, cy, object_class="person", source="model", confidence=0.5, **extra):
    hit = {
        "frame_idx": frame_idx, "timestamp": str(frame_idx), "seconds": float(frame_idx),
        "confidence": confidence, "object_class": object_class, "source": source,
        "bbox": [cx - 10, cy - 10, cx + 10, cy + 10], "lat": None, "lon": None, "alt": None,
        "image_path": f"crop_{frame_idx}.jpg", "full_image_path": None, "group_id": "",
    }
    hit.update(extra)
    return hit


# --- ref_key стабильности AI-сцен ---

def test_ai_scene_ref_key_is_content_based_not_positional(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hits = [_hit(10, 100, 100), _hit(60, 105, 100),
            _hit(300, 500, 500, object_class="tent", source="color", confidence=0.9)]
    (out_dir / "detections.json").write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert_report(db_path, "rep1", "video.mp4", "done", out_dir=str(out_dir))

    client = _client(sar_server.app, db_path, tmp_path, monkeypatch)
    scenes = client.get("/api/report/rep1/ai_scenes").get_json()
    scenes.sort(key=lambda s: s["time_start_sec"])

    # ref_key должен зависеть от содержимого (класс+источник+первый кадр+
    # позиция), не от g["id"] ("g00000" и т.п.) -- см. sar_common.ai_scene_ref_key
    assert scenes[0]["ref_key"] == sar_common.ai_scene_ref_key("person", "model", 10, [90, 90, 110, 110])
    assert scenes[1]["ref_key"] == sar_common.ai_scene_ref_key("tent", "color", 300, [490, 490, 510, 510])


def test_ai_scene_ref_key_distinguishes_scenes_starting_on_the_same_frame(tmp_path, monkeypatch):
    """Регрессия, найденная на реальных данных: несколько РАЗНЫХ ложных
    "person"-детекций одного видео стартовали на кадре 0 в разных углах
    кадра. Со старой формулой ref_key ("класс:источник:первый_кадр", без
    координат) все три сцены получали ОДИН ref_key -- триаж одной из них
    (например rejected) тихо перезаписывал/задевал две другие через
    UNIQUE(report_id, kind, ref_key) в detection_priorities, и экспорт
    датасета тянул в хард-негативы кадры совсем других, непроверенных сцен."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hits = [
        _hit(0, 100, 100),   # сцена A -- левый верхний угол
        _hit(0, 900, 700),   # сцена B -- другой угол, тот же стартовый кадр
    ]
    (out_dir / "detections.json").write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert_report(db_path, "rep1", "video.mp4", "done", out_dir=str(out_dir))

    client = _client(sar_server.app, db_path, tmp_path, monkeypatch)
    scenes = client.get("/api/report/rep1/ai_scenes").get_json()

    assert len(scenes) == 2
    ref_keys = {s["ref_key"] for s in scenes}
    assert len(ref_keys) == 2  # раньше здесь было 1 -- сцены путались друг с другом


# --- /api/report/<id>/priorities ---

def _make_basic_report(tmp_path):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert_report(db_path, "rep1", "video.mp4", "processing")
    return db_path


def test_post_priority_creates_and_get_reflects_it(tmp_path, monkeypatch):
    db_path = _make_basic_report(tmp_path)
    client = _client(sar_server.app, db_path, tmp_path, monkeypatch, viewer_name="Аяу")

    resp = client.post("/api/report/rep1/priorities", json={
        "kind": "manual", "ref_key": "5", "priority": "confirmed_person"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["priority"] == "confirmed_person"
    assert body["set_by"] == "Аяу"

    rows = client.get("/api/report/rep1/priorities").get_json()
    assert len(rows) == 1
    assert rows[0]["kind"] == "manual"
    assert rows[0]["ref_key"] == "5"
    assert rows[0]["priority"] == "confirmed_person"
    assert rows[0]["set_by"] == "Аяу"
    assert rows[0]["set_at"]


def test_post_priority_upserts_instead_of_duplicating(tmp_path, monkeypatch):
    db_path = _make_basic_report(tmp_path)
    client = _client(sar_server.app, db_path, tmp_path, monkeypatch, viewer_name="A")

    client.post("/api/report/rep1/priorities",
                 json={"kind": "ai_scene", "ref_key": "person:model:10", "priority": "likely_person"})
    resp = client.post("/api/report/rep1/priorities",
                        json={"kind": "ai_scene", "ref_key": "person:model:10", "priority": "confirmed_person"})
    assert resp.status_code == 200

    rows = client.get("/api/report/rep1/priorities").get_json()
    assert len(rows) == 1  # не два -- перезаписано, не задублировано
    assert rows[0]["priority"] == "confirmed_person"


def test_post_empty_priority_clears_existing_mark(tmp_path, monkeypatch):
    db_path = _make_basic_report(tmp_path)
    client = _client(sar_server.app, db_path, tmp_path, monkeypatch)

    client.post("/api/report/rep1/priorities",
                 json={"kind": "manual", "ref_key": "1", "priority": "rejected"})
    resp = client.post("/api/report/rep1/priorities",
                        json={"kind": "manual", "ref_key": "1", "priority": ""})
    assert resp.status_code == 200
    assert resp.get_json()["priority"] is None

    rows = client.get("/api/report/rep1/priorities").get_json()
    assert rows == []


def test_post_priority_rejects_unknown_kind(tmp_path, monkeypatch):
    db_path = _make_basic_report(tmp_path)
    client = _client(sar_server.app, db_path, tmp_path, monkeypatch)
    resp = client.post("/api/report/rep1/priorities",
                        json={"kind": "robot", "ref_key": "1", "priority": "rejected"})
    assert resp.status_code == 400


def test_post_priority_rejects_unknown_priority_value(tmp_path, monkeypatch):
    # система физически не может проставить произвольное значение -- только
    # одно из 5 согласованных с пользователем статусов
    db_path = _make_basic_report(tmp_path)
    client = _client(sar_server.app, db_path, tmp_path, monkeypatch)
    resp = client.post("/api/report/rep1/priorities",
                        json={"kind": "manual", "ref_key": "1", "priority": "definitely_a_yeti"})
    assert resp.status_code == 400
    assert client.get("/api/report/rep1/priorities").get_json() == []


def test_post_priority_records_the_logged_in_viewer_not_a_system_actor(tmp_path, monkeypatch):
    # ключевое ограничение из обсуждения: "точно человек" ставит только
    # человек -- проверяем, что set_by -- реальное имя из сессии, не пусто
    # и не какая-то системная метка
    db_path = _make_basic_report(tmp_path)
    client = _client(sar_server.app, db_path, tmp_path, monkeypatch, viewer_name="Данияр")
    client.post("/api/report/rep1/priorities",
                 json={"kind": "manual", "ref_key": "9", "priority": "confirmed_person"})
    rows = client.get("/api/report/rep1/priorities").get_json()
    assert rows[0]["set_by"] == "Данияр"
    assert rows[0]["set_by"] not in ("", "система", "auto", None)


# --- /api/tree: manual_count / ai_count ---

def test_tree_reports_manual_observation_count(tmp_path, monkeypatch):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "DJI_x.MP4").write_bytes(b"fake")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert_report(db_path, "rep1", "DJI_x.MP4", "processing")

    conn = sqlite3.connect(db_path)
    for i in range(3):
        conn.execute(
            "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, "
            "created_at) VALUES (?,?,?,?,?)",
            ("rep1", "tester", float(i), "[0,0,1,1]", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    item = client.get("/api/tree").get_json()["items"][0]
    assert item["manual_count"] == 3


def test_tree_ai_count_is_none_while_not_done(tmp_path, monkeypatch):
    # группировка на лету слишком дорогая, чтобы гонять на каждый опрос ещё
    # обрабатывающегося видео -- см. комментарий в api_tree()
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "DJI_y.MP4").write_bytes(b"fake")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert_report(db_path, "rep1", "DJI_y.MP4", "processing")

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    item = client.get("/api/tree").get_json()["items"][0]
    assert item["ai_count"] is None


def test_tree_ai_count_computed_for_done_report(tmp_path, monkeypatch):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "DJI_z.MP4").write_bytes(b"fake")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hits = [_hit(0, 100, 100), _hit(50, 105, 100),
            _hit(300, 500, 500, object_class="tent", source="color", confidence=0.9)]
    (out_dir / "detections.json").write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert_report(db_path, "rep1", "DJI_z.MP4", "done", out_dir=str(out_dir))

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    item = client.get("/api/tree").get_json()["items"][0]
    assert item["ai_count"] == 2  # 2 сцены: person (2 кадра) + tent (1 кадр)


def test_tree_new_unqueued_file_has_zero_manual_and_none_ai_count(tmp_path, monkeypatch):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "DJI_brand_new.MP4").write_bytes(b"fake")
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    item = client.get("/api/tree").get_json()["items"][0]
    assert item["manual_count"] == 0
    assert item["ai_count"] is None
