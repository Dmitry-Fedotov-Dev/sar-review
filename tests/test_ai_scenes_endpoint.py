"""Тест нового API-эндпоинта /api/report/<id>/ai_scenes -- группирует сырые
AI-детекции (detections.json) в сцены на лету, чтобы плеер мог показать их
в правом блоке как кликабельные моменты с таймкодом (раньше рамки модели
были видны ТОЛЬКО как оверлей на видео, без списка)."""
import json
import sqlite3

import sar_common
import sar_server


def _make_report_with_detections(tmp_path, hits, status="done"):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "detections.json").write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")

    db_path = tmp_path / "sar_data.db"
    sar_common.init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep1", "video.mp4", str(tmp_path / "video.mp4"), "video", status, str(out_dir), 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    return str(db_path)


def _hit(frame_idx, cx, cy, object_class="person", source="model", confidence=0.5, **extra):
    hit = {
        "frame_idx": frame_idx, "timestamp": str(frame_idx), "seconds": float(frame_idx),
        "confidence": confidence, "object_class": object_class, "source": source,
        "bbox": [cx - 10, cy - 10, cx + 10, cy + 10], "lat": None, "lon": None, "alt": None,
        "image_path": f"crop_{frame_idx}.jpg", "full_image_path": None, "group_id": "",
    }
    hit.update(extra)
    return hit


def _client(app, db_path, monkeypatch):
    # DB_PATH -- обычный module-level global, но выставляется только внутри
    # main(); в тестовом процессе main() не запускается, поэтому атрибута
    # ещё не существует -- raising=False создаёт его вместо ошибки
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", "/no/such/config/dir")
    monkeypatch.setattr(sar_server, "_ai_scenes_cache", {})
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


def test_ai_scenes_groups_and_sorts_by_time(tmp_path, monkeypatch):
    hits = [_hit(0, 100, 100), _hit(50, 105, 100),   # одна сцена, person
            _hit(200, 500, 500, object_class="tent", source="color", confidence=0.9)]  # другая сцена
    db_path = _make_report_with_detections(tmp_path, hits)
    client = _client(sar_server.app, db_path, monkeypatch)

    resp = client.get("/api/report/rep1/ai_scenes")
    assert resp.status_code == 200
    scenes = resp.get_json()

    assert len(scenes) == 2
    # отсортировано по времени начала -- person (t=0) раньше tent (t=200)
    assert scenes[0]["object_class"] == "person"
    assert scenes[0]["count"] == 2
    assert scenes[0]["time_start_sec"] == 0.0
    assert scenes[1]["object_class"] == "tent"
    assert scenes[1]["source"] == "color"
    assert scenes[1]["count"] == 1


def test_ai_scenes_includes_drone_and_estimated_coordinates(tmp_path, monkeypatch):
    """Раньше /api/report/<id>/ai_scenes не отдавал координаты вообще --
    плеер показывал сцены без GPS. Проверяем, что drone_lat/lon и
    est_lat/lon (вероятные координаты объекта) теперь приходят с эндпоинта,
    и что при отсутствии координат у пикового кадра берётся первое
    ненулевое значение среди остальных детекций сцены."""
    hits = [
        _hit(0, 100, 100, lat=None, lon=None, est_lat=None, est_lon=None),
        _hit(1, 101, 100, confidence=0.9,  # пиковый по уверенности, но без GPS
             lat=None, lon=None, est_lat=None, est_lon=None,
             alt=1000.0, alt_abs=2500.0, yaw=45.0, pitch=-56.6, roll=0.0, focal_len=24.0,
             raw_telemetry="[latitude: X] [gb_pitch: -56.6] ...полная телеметрия пикового кадра..."),
        _hit(2, 102, 100, lat=39.48, lon=73.59, est_lat=39.4805, est_lon=73.5905),
    ]
    db_path = _make_report_with_detections(tmp_path, hits)
    client = _client(sar_server.app, db_path, monkeypatch)

    resp = client.get("/api/report/rep1/ai_scenes")
    assert resp.status_code == 200
    scenes = resp.get_json()
    assert len(scenes) == 1
    s = scenes[0]

    # пиковый кадр (frame_idx=1, conf=0.9) без GPS -> взят из третьей детекции
    assert s["drone_lat"] == 39.48 and s["drone_lon"] == 73.59
    assert s["est_lat"] == 39.4805 and s["est_lon"] == 73.5905
    # телеметрия -- с самого пикового кадра (там она есть)
    assert s["alt"] == 1000.0 and s["alt_abs"] == 2500.0
    assert s["yaw"] == 45.0 and s["pitch"] == -56.6 and s["focal_len"] == 24.0
    assert s["raw_telemetry"] == "[latitude: X] [gb_pitch: -56.6] ...полная телеметрия пикового кадра..."


def test_ai_scenes_empty_when_no_detections(tmp_path, monkeypatch):
    db_path = _make_report_with_detections(tmp_path, [])
    client = _client(sar_server.app, db_path, monkeypatch)
    resp = client.get("/api/report/rep1/ai_scenes")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_ai_scenes_404_for_unknown_report(tmp_path, monkeypatch):
    db_path = _make_report_with_detections(tmp_path, [])
    client = _client(sar_server.app, db_path, monkeypatch)
    resp = client.get("/api/report/does-not-exist/ai_scenes")
    assert resp.status_code == 404


# --- быстрый путь по уже сохранённому group_id для готовых отчётов ---
#
# Регрессия, найденная на реальных данных: на видео с 25000+ детекциями
# полная перегруппировка (group_hits_into_scenes, трекинг по близости)
# занимала 7+ секунд НА КАЖДЫЙ холодный запрос -- отсюда жалоба "список
# видео на главной грузится очень долго" (/api/tree дёргает ту же функцию
# ради счётчика ai_count на каждый готовый отчёт). Готовые отчёты уже несут
# финальный group_id на каждом hit -- группировка по нему тривиальна и на
# порядки дешевле, повторный трекинг больше не нужен.

def test_ai_scenes_done_report_does_not_recompute_grouping(tmp_path, monkeypatch):
    hits = [_hit(0, 100, 100, group_id="g00000"), _hit(1, 105, 100, group_id="g00000"),
            _hit(200, 500, 500, object_class="tent", source="color", confidence=0.9, group_id="g00001")]
    db_path = _make_report_with_detections(tmp_path, hits, status="done")
    client = _client(sar_server.app, db_path, monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("group_hits_into_scenes НЕ должна вызываться для готового "
                              "отчёта с уже проставленным group_id")
    monkeypatch.setattr(sar_server, "group_hits_into_scenes", _boom)

    resp = client.get("/api/report/rep1/ai_scenes")
    assert resp.status_code == 200
    scenes = resp.get_json()
    assert len(scenes) == 2
    assert {s["object_class"] for s in scenes} == {"person", "tent"}


def test_ai_scenes_matches_full_regrouping_result(tmp_path, monkeypatch):
    """Быстрый путь (группировка по существующему group_id) должен давать
    ТОТ ЖЕ результат, что и полная перегруппировка -- иначе это оптимизация
    ценой правильности."""
    raw_hits_no_gid = [_hit(0, 100, 100), _hit(1, 105, 100),
                        _hit(200, 500, 500, object_class="tent", source="color", confidence=0.9)]
    # прогоняем "как воркер" -- реальная группировка проставляет group_id
    from sar_video_review import Hit, group_hits_into_scenes
    hit_objs = [Hit(**h) for h in raw_hits_no_gid]
    group_hits_into_scenes(hit_objs, max_frame_gap=360, distance_multiplier=2.5)
    hits_with_gid = [
        {**raw, "group_id": obj.group_id} for raw, obj in zip(raw_hits_no_gid, hit_objs)]

    (tmp_path / "slow").mkdir()
    (tmp_path / "fast").mkdir()
    db_path_slow = _make_report_with_detections(tmp_path / "slow", raw_hits_no_gid, status="processing")
    db_path_fast = _make_report_with_detections(tmp_path / "fast", hits_with_gid, status="done")

    client_slow = _client(sar_server.app, db_path_slow, monkeypatch)
    scenes_slow = client_slow.get("/api/report/rep1/ai_scenes").get_json()
    client_fast = _client(sar_server.app, db_path_fast, monkeypatch)  # переставляет DB_PATH/кэш заново
    scenes_fast = client_fast.get("/api/report/rep1/ai_scenes").get_json()

    key = lambda s: (s["object_class"], s["time_start_sec"])
    scenes_slow.sort(key=key)
    scenes_fast.sort(key=key)
    for slow, fast in zip(scenes_slow, scenes_fast):
        assert slow["object_class"] == fast["object_class"]
        assert slow["count"] == fast["count"]
        assert slow["time_start_sec"] == fast["time_start_sec"]
        assert slow["time_end_sec"] == fast["time_end_sec"]


def test_ai_scenes_falls_back_to_regrouping_while_still_processing(tmp_path, monkeypatch):
    # group_id тут ЕСТЬ (например, остался от предыдущей обработки перед
    # переобработкой), но статус НЕ done -- пока идёт обработка, доверять
    # старому group_id нельзя, он ещё не финален
    hits = [_hit(0, 100, 100, group_id="g00000"), _hit(1, 105, 100, group_id="g00000")]
    db_path = _make_report_with_detections(tmp_path, hits, status="processing")
    client = _client(sar_server.app, db_path, monkeypatch)

    called = []
    real_group = sar_server.group_hits_into_scenes
    def _spy(*a, **kw):
        called.append(True)
        return real_group(*a, **kw)
    monkeypatch.setattr(sar_server, "group_hits_into_scenes", _spy)

    resp = client.get("/api/report/rep1/ai_scenes")
    assert resp.status_code == 200
    assert called, "во время обработки группировка должна пересчитываться на лету"


def test_ai_scenes_falls_back_to_regrouping_when_group_id_missing(tmp_path, monkeypatch):
    # done, но у части hits почему-то нет group_id (повреждённые/старые
    # данные) -- безопаснее пересчитать заново, чем показать неполные сцены
    hits = [_hit(0, 100, 100, group_id="g00000"), _hit(1, 105, 100, group_id="")]
    db_path = _make_report_with_detections(tmp_path, hits, status="done")
    client = _client(sar_server.app, db_path, monkeypatch)

    called = []
    real_group = sar_server.group_hits_into_scenes
    def _spy(*a, **kw):
        called.append(True)
        return real_group(*a, **kw)
    monkeypatch.setattr(sar_server, "group_hits_into_scenes", _spy)

    resp = client.get("/api/report/rep1/ai_scenes")
    assert resp.status_code == 200
    assert called
