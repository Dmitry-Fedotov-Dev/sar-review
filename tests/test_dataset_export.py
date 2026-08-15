"""Тесты sar_dataset_export.py -- экспорт подтверждённых детекций (см.
detection_priorities) в датасет YOLO для дообучения. Реальные файлы на
диске (картинки, видео через cv2.VideoWriter) -- не ML-инференс, а простое
чтение/запись, мокать незачем (тот же принцип, что и в test_thumbnails.py/
test_process_video_geolocation.py)."""
import json
import sqlite3

import cv2
import numpy as np

import sar_common
import sar_dataset_export as export_mod


def _make_video(path, width=320, height=240, color=(10, 20, 30)):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (width, height))
    for _ in range(5):
        writer.write(np.full((height, width, 3), color, dtype=np.uint8))
    writer.release()


def _hit(frame_idx, x1, y1, x2, y2, object_class="person", source="model",
         frame_width=200, frame_height=100, full_image_path=None, image_path=None, **extra):
    hit = {
        "frame_idx": frame_idx, "timestamp": str(frame_idx), "seconds": float(frame_idx),
        "confidence": 0.5, "object_class": object_class, "source": source,
        "bbox": [x1, y1, x2, y2], "lat": None, "lon": None, "alt": None,
        "image_path": image_path or f"crops/crop_{frame_idx}.jpg",
        "full_image_path": full_image_path or f"frames/full_{frame_idx}.jpg",
        "group_id": "", "frame_width": frame_width, "frame_height": frame_height,
    }
    hit.update(extra)
    return hit


def _make_frame_image(path, width=200, height=100, color=(255, 0, 0)):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((height, width, 3), color, dtype=np.uint8))


def _setup_report(tmp_path, report_id="rep1", hits=None, kind="video"):
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    if hits is not None:
        (out_dir / "detections.json").write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")
        for h in hits:
            _make_frame_image(out_dir / h["full_image_path"])

    video_path = tmp_path / "video.mp4"
    if not video_path.exists():
        _make_video(video_path)

    db_path = str(tmp_path / "sar_data.db")
    if not (tmp_path / "sar_data.db").exists():
        sar_common.init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
            "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, "video.mp4", str(video_path), kind, "done", str(out_dir), 0.0,
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
        conn.commit()
        conn.close()
    return db_path, out_dir


def _set_priority(db_path, report_id, kind, ref_key, priority):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO detection_priorities (report_id, kind, ref_key, priority, set_by, set_at) "
        "VALUES (?,?,?,?,?,?)",
        (report_id, kind, ref_key, priority, "tester", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


def _label_lines(out_dir, split, stem):
    p = out_dir / "labels" / split / f"{stem}.txt"
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _ref_key(hit_dict):
    return sar_common.ai_scene_ref_key(
        hit_dict["object_class"], hit_dict["source"], hit_dict["frame_idx"], hit_dict["bbox"])


def _stem(report_id, hit_dict):
    rk = _ref_key(hit_dict)
    return f"{report_id}__ai__{rk.replace(':', '-')}__f{hit_dict['frame_idx']}"


# --- AI-сцены ---

def test_confirmed_ai_scene_exports_positive_yolo_box(tmp_path):
    hits = [_hit(10, 40, 20, 60, 40)]  # bbox в кадре 200x100
    db_path, out_dir = _setup_report(tmp_path, hits=hits)
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hits[0]), "confirmed_person")

    counts = export_mod.run_export(db_path, str(out_dir_export := tmp_path / "dataset"), val_split=0.0)
    assert counts["positive"] == 1
    assert counts["negative"] == 0

    stem = _stem("rep1", hits[0])
    lines = _label_lines(out_dir_export, "train", stem)
    assert len(lines) == 1
    class_id, cx, cy, w, h = lines[0].split()
    assert class_id == "0"  # person
    # центр бокса (40,20)-(60,40) в кадре 200x100 -> cx=0.25, cy=0.30, w=0.10, h=0.20
    assert abs(float(cx) - 0.25) < 1e-4
    assert abs(float(cy) - 0.30) < 1e-4
    assert abs(float(w) - 0.10) < 1e-4
    assert abs(float(h) - 0.20) < 1e-4
    assert (out_dir_export / "images" / "train" / f"{stem}.jpg").exists()


def test_rejected_ai_scene_exports_image_with_empty_label(tmp_path):
    hits = [_hit(10, 40, 20, 60, 40, object_class="tent", source="color")]
    db_path, out_dir = _setup_report(tmp_path, hits=hits)
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hits[0]), "rejected")

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert counts["positive"] == 0
    assert counts["negative"] == 1

    stem = _stem("rep1", hits[0])
    assert (out_dir_export / "images" / "train" / f"{stem}.jpg").exists()
    assert _label_lines(out_dir_export, "train", stem) == []  # хард-негатив -- без рамки


def test_unset_and_likely_priorities_are_not_exported(tmp_path):
    hits = [
        _hit(1, 10, 10, 20, 20, object_class="person"),          # без приоритета вообще
        _hit(50, 10, 10, 20, 20, object_class="backpack", source="color"),
    ]
    db_path, out_dir = _setup_report(tmp_path, hits=hits)
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hits[1]), "likely_object")

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert counts["positive"] == 0
    assert counts["negative"] == 0
    assert list((out_dir_export / "images" / "train").iterdir()) == []


def test_all_hits_in_confirmed_scene_are_exported_not_just_peak(tmp_path):
    # сцена из двух кадров -- обе картинки должны попасть в датасет, чтобы
    # использовать весь накопленный материал, а не только пиковый кадр
    hits = [_hit(10, 40, 20, 60, 40), _hit(11, 41, 21, 61, 41)]
    db_path, out_dir = _setup_report(tmp_path, hits=hits)
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hits[0]), "confirmed_person")

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert counts["positive"] == 2


def test_ai_scene_uses_full_frame_image_not_crop(tmp_path):
    # кроп и полный кадр -- РАЗНОЕ содержимое; в датасет должен пойти
    # полный кадр (детектор должен учиться на контексте сцены, не на
    # уже вырезанном пятне) -- проверяем по цвету пикселя
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hit = _hit(10, 40, 20, 60, 40, image_path="crops/crop_10.jpg", full_image_path="frames/full_10.jpg")
    (out_dir / "detections.json").write_text(json.dumps([hit], ensure_ascii=False), encoding="utf-8")
    _make_frame_image(out_dir / hit["image_path"], color=(0, 255, 0))    # кроп -- зелёный
    _make_frame_image(out_dir / hit["full_image_path"], color=(255, 0, 0))  # полный кадр -- синий (BGR)

    video_path = tmp_path / "video.mp4"
    _make_video(video_path)
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep1", "video.mp4", str(video_path), "video", "done", str(out_dir), 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hit), "confirmed_person")

    out_dir_export = tmp_path / "dataset"
    export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    exported = cv2.imread(str(out_dir_export / "images" / "train" / f"{_stem('rep1', hit)}.jpg"))
    # BGR (255,0,0) -- синий канал максимален
    assert exported[0, 0, 0] > 200 and exported[0, 0, 2] < 50


def test_frame_size_falls_back_to_video_metadata_when_missing_on_hit(tmp_path):
    # старые отчёты могли не писать frame_width/height на Hit -- экспорт
    # должен подстраховаться чтением метаданных исходного видео
    hits = [_hit(1, 40, 20, 60, 40, frame_width=None, frame_height=None)]
    db_path, out_dir = _setup_report(tmp_path, hits=hits)
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hits[0]), "confirmed_person")

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert counts["positive"] == 1  # видео 320x240 из _make_video -- размер нашёлся


# --- ручные наблюдения ---

def test_confirmed_manual_observation_extracts_frame_and_writes_box(tmp_path):
    db_path, out_dir = _setup_report(tmp_path, hits=None)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, created_at) "
        "VALUES (?,?,?,?,?)",
        ("rep1", "tester", 0.1, json.dumps([0.2, 0.3, 0.4, 0.5]), "2026-01-01T00:00:00"))
    obs_id = conn.execute("SELECT last_insert_rowid() id").fetchone()[0]
    conn.commit()
    conn.close()
    _set_priority(db_path, "rep1", "manual", str(obs_id), "confirmed_object")

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert counts["positive"] == 1

    stem = f"rep1__manual__{obs_id}"
    assert (out_dir_export / "images" / "train" / f"{stem}.jpg").exists()
    lines = _label_lines(out_dir_export, "train", stem)
    class_id, cx, cy, w, h = lines[0].split()
    assert class_id == "1"  # object
    assert abs(float(cx) - 0.3) < 1e-4
    assert abs(float(cy) - 0.4) < 1e-4
    assert abs(float(w) - 0.2) < 1e-4
    assert abs(float(h) - 0.2) < 1e-4


def test_rejected_manual_observation_writes_empty_label(tmp_path):
    db_path, out_dir = _setup_report(tmp_path, hits=None)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, created_at) "
        "VALUES (?,?,?,?,?)",
        ("rep1", "tester", 0.1, json.dumps([0.2, 0.3, 0.4, 0.5]), "2026-01-01T00:00:00"))
    obs_id = conn.execute("SELECT last_insert_rowid() id").fetchone()[0]
    conn.commit()
    conn.close()
    _set_priority(db_path, "rep1", "manual", str(obs_id), "rejected")

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert counts["negative"] == 1
    assert _label_lines(out_dir_export, "train", f"rep1__manual__{obs_id}") == []


def test_manual_observation_without_priority_is_not_exported(tmp_path):
    db_path, out_dir = _setup_report(tmp_path, hits=None)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, created_at) "
        "VALUES (?,?,?,?,?)",
        ("rep1", "tester", 0.1, json.dumps([0.2, 0.3, 0.4, 0.5]), "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert counts["positive"] == 0
    assert counts["negative"] == 0


# --- сплит, dataset.yaml, идемпотентность ---

def test_split_is_deterministic_for_the_same_key():
    a = export_mod._split_for_key("rep1:ai:person:model:10:10", 0.3)
    b = export_mod._split_for_key("rep1:ai:person:model:10:10", 0.3)
    assert a == b


def test_dataset_yaml_lists_class_names(tmp_path):
    db_path, out_dir = _setup_report(tmp_path, hits=None)
    out_dir_export = tmp_path / "dataset"
    export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    yaml_text = (out_dir_export / "dataset.yaml").read_text(encoding="utf-8")
    assert "0: person" in yaml_text
    assert "1: object" in yaml_text
    assert "train: images/train" in yaml_text
    assert "val: images/val" in yaml_text


def test_rerun_removes_stale_export_when_priority_changes(tmp_path):
    hits = [_hit(10, 40, 20, 60, 40)]
    db_path, out_dir = _setup_report(tmp_path, hits=hits)
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hits[0]), "confirmed_person")

    out_dir_export = tmp_path / "dataset"
    export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    stem_path = out_dir_export / "images" / "train" / f"{_stem('rep1', hits[0])}.jpg"
    assert stem_path.exists()

    # приоритет сняли (человек передумал / ошибся) -- повторный экспорт не
    # должен оставлять осиротевшую картинку с прошлого прогона
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM detection_priorities WHERE report_id='rep1'")
    conn.commit()
    conn.close()

    export_mod.run_export(db_path, str(out_dir_export), val_split=0.0)
    assert not stem_path.exists()


def test_report_id_filter_exports_only_that_report(tmp_path):
    (tmp_path / "r1").mkdir()
    hits1 = [_hit(10, 40, 20, 60, 40)]
    db_path, out_dir1 = _setup_report(tmp_path / "r1", report_id="rep1", hits=hits1)
    # второй отчёт в ТОЙ ЖЕ БД
    out_dir2 = tmp_path / "r1" / "out2"
    out_dir2.mkdir()
    hits2 = [_hit(20, 40, 20, 60, 40)]
    (out_dir2 / "detections.json").write_text(json.dumps(hits2, ensure_ascii=False), encoding="utf-8")
    for h in hits2:
        _make_frame_image(out_dir2 / h["full_image_path"])
    video2 = tmp_path / "r1" / "video2.mp4"
    _make_video(video2)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep2", "video2.mp4", str(video2), "video", "done", str(out_dir2), 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    _set_priority(db_path, "rep1", "ai_scene", _ref_key(hits1[0]), "confirmed_person")
    _set_priority(db_path, "rep2", "ai_scene", _ref_key(hits2[0]), "confirmed_person")

    out_dir_export = tmp_path / "dataset"
    counts = export_mod.run_export(db_path, str(out_dir_export), val_split=0.0, report_id_filter="rep1")
    assert counts["positive"] == 1
    assert (out_dir_export / "images" / "train" / f"{_stem('rep1', hits1[0])}.jpg").exists()
    assert not (out_dir_export / "images" / "train" / f"{_stem('rep2', hits2[0])}.jpg").exists()
