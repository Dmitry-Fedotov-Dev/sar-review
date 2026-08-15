"""Тесты backfill_telemetry.py -- пересчёт GPS/починка кодировки для уже
обработанных отчётов без повторного запуска детектора.

Отдельно проверяем ту саму регрессию, которая уже случилась при первом
использовании скрипта: save_outputs() вызывался БЕЗ tracking_report_id/
tracking_api_base, из-за чего report.html молча терял ссылки "к списку
файлов" и "ручной плеер" на каждом отчёте, который скрипт трогал."""
import json
import sqlite3

import backfill_telemetry as bt
import sar_common
from sar_video_review import Hit


def _write_detections(out_dir, hits_dicts):
    (out_dir / "detections.json").write_text(json.dumps(hits_dicts, ensure_ascii=False), encoding="utf-8")


def _hit_dict(frame_idx, seconds, lat=None, group_id="g00000"):
    return {
        "frame_idx": frame_idx, "timestamp": str(frame_idx), "seconds": seconds,
        "confidence": 0.8, "object_class": "person", "source": "model",
        "bbox": [10, 10, 50, 50], "lat": lat, "lon": (73.5 if lat else None), "alt": None,
        "image_path": f"c{frame_idx}.jpg", "full_image_path": None, "group_id": group_id,
    }


SRT_CONTENT = (
    "1\n00:00:00,000 --> 00:00:10,000\n"
    "[latitude: 39.480800] [longitude: 73.592544] [rel_alt: 1000.0]\n"
)


def test_backfill_adds_gps_and_passes_tracking_info_to_report_html(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_detections(out_dir, [_hit_dict(0, 1.0)])  # lat=None -- телеметрия ещё не была найдена

    video_path = tmp_path / "DJI_20260101000000_0001_Z.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "DJI_20260101000000_0001_Z.SRT").write_text(SRT_CONTENT, encoding="utf-8")

    report = {"report_id": "rep1", "out_dir": str(out_dir), "abs_path": str(video_path)}
    cfg = {"srt_gps_tuple_order": "lat_lon"}

    changed = bt.backfill_one_report(report, telemetry_dir, cfg, "http://127.0.0.1:8080")
    assert changed is True

    det = json.loads((out_dir / "detections.json").read_text(encoding="utf-8"))
    assert det[0]["lat"] == 39.4808

    # РЕГРЕССИЯ, которую уже поймали руками: без передачи tracking_report_id/
    # tracking_api_base в save_outputs() эти ссылки в report.html пропадают молча
    html = (out_dir / "report.html").read_text(encoding="utf-8")
    assert '<a href="/">' in html and "к списку файлов" in html
    assert "ручной плеер" in html
    assert "rep1" in html  # tracking_report_id реально попал в ссылку на плеер


def test_backfill_skips_report_needing_nothing(tmp_path, capsys):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_detections(out_dir, [_hit_dict(0, 1.0, lat=39.48)])  # GPS уже есть

    video_path = tmp_path / "video_no_telemetry.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()  # пусто -- телеметрии нет

    report = {"report_id": "rep2", "out_dir": str(out_dir), "abs_path": str(video_path)}
    cfg = {"srt_gps_tuple_order": "lat_lon"}

    changed = bt.backfill_one_report(report, telemetry_dir, cfg, "http://127.0.0.1:8080")
    assert changed is False
    assert not (out_dir / "report.html").exists()  # ничего не перезаписывали


def test_backfill_force_regenerates_even_without_changes(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_detections(out_dir, [_hit_dict(0, 1.0, lat=39.48)])

    video_path = tmp_path / "video_no_telemetry.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()

    report = {"report_id": "rep3", "out_dir": str(out_dir), "abs_path": str(video_path)}
    cfg = {"srt_gps_tuple_order": "lat_lon"}

    changed = bt.backfill_one_report(report, telemetry_dir, cfg, "http://127.0.0.1:8080", force=True)
    assert changed is True
    assert (out_dir / "report.html").exists()


def test_backfill_dry_run_does_not_write_files(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_detections(out_dir, [_hit_dict(0, 1.0)])

    video_path = tmp_path / "DJI_20260101000000_0001_Z.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "DJI_20260101000000_0001_Z.SRT").write_text(SRT_CONTENT, encoding="utf-8")

    report = {"report_id": "rep4", "out_dir": str(out_dir), "abs_path": str(video_path)}
    cfg = {"srt_gps_tuple_order": "lat_lon"}

    changed = bt.backfill_one_report(report, telemetry_dir, cfg, "http://127.0.0.1:8080", dry_run=True)
    assert changed is True
    assert not (out_dir / "report.html").exists()
    det = json.loads((out_dir / "detections.json").read_text(encoding="utf-8"))
    assert det[0]["lat"] is None  # исходный файл не тронут


def test_read_detections_tolerant_reports_which_encoding_was_used(tmp_path):
    p = tmp_path / "detections.json"
    p.write_bytes(json.dumps([{"a": "цвет"}], ensure_ascii=False).encode("cp1251"))
    data, enc = bt._read_detections_tolerant(str(p))
    assert enc == "cp1251"
    assert data == [{"a": "цвет"}]


SRT_WITH_GIMBAL = (
    "1\n00:00:00,000 --> 00:00:10,000\n"
    "[latitude: 39.480800] [longitude: 73.592544] [rel_alt: 1000.0] "
    "[gb_yaw: 0.0 gb_pitch: -45.0 gb_roll: 0.0]\n"
)


def test_backfill_computes_estimated_object_coordinates_for_old_report_without_frame_size(tmp_path):
    """Старый отчёт (до появления est_lat/est_lon) не хранит размер кадра --
    backfill должен один раз открыть видео за разрешением (не пересчитывая
    детектор) и на этом посчитать 'вероятные координаты' объекта."""
    import cv2
    import numpy as np

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # bbox ровно в центре кадра 640x480 -> центр (320, 240); lat=None по
    # умолчанию у _hit_dict -- имитирует старый отчёт без GPS
    hit = _hit_dict(0, 1.0)
    hit["bbox"] = [300, 220, 340, 260]
    _write_detections(out_dir, [hit])

    video_path = tmp_path / "DJI_20260101000000_0001_Z.MP4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (640, 480))
    writer.write(np.zeros((480, 640, 3), dtype=np.uint8))
    writer.release()

    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "DJI_20260101000000_0001_Z.SRT").write_text(SRT_WITH_GIMBAL, encoding="utf-8")

    report = {"report_id": "rep5", "out_dir": str(out_dir), "abs_path": str(video_path)}
    cfg = {"srt_gps_tuple_order": "lat_lon", "camera_hfov_deg": 84.0}

    changed = bt.backfill_one_report(report, telemetry_dir, cfg, "http://127.0.0.1:8080")
    assert changed is True

    det = json.loads((out_dir / "detections.json").read_text(encoding="utf-8"))
    assert det[0]["lat"] == 39.4808  # координаты дрона подтянулись
    assert det[0]["est_lat"] is not None  # и оценка объекта тоже посчиталась
    assert det[0]["frame_width"] == 640 and det[0]["frame_height"] == 480


def test_backfill_recomputes_and_corrects_stale_bad_estimate(tmp_path):
    """РЕГРЕССИЯ: раньше backfill пропускал детекции, у которых est_lat уже
    был заполнен -- это означало, что оценка, посчитанная ДО исправления
    защиты от численного взрыва при угле, близком к горизонту (см.
    max_distance_m в estimate_ground_point, реальный случай дал 69.8 км
    вместо разумной оценки), так и осталась бы неверной в отчёте НАВСЕГДА,
    потому что backfill её больше никогда бы не тронул. Теперь оценка
    пересчитывается всегда -- эта "заведомо плохая" заглушка (11.11, 22.22)
    должна замениться на реально посчитанное значение."""
    import cv2
    import numpy as np

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hit = _hit_dict(0, 1.0)
    hit["bbox"] = [300, 220, 340, 260]  # центр кадра 640x480
    hit["est_lat"], hit["est_lon"] = 11.11, 22.22  # заведомо неверная "старая" оценка
    _write_detections(out_dir, [hit])

    video_path = tmp_path / "DJI_20260101000000_0001_Z.MP4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (640, 480))
    writer.write(np.zeros((480, 640, 3), dtype=np.uint8))
    writer.release()

    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "DJI_20260101000000_0001_Z.SRT").write_text(SRT_WITH_GIMBAL, encoding="utf-8")

    report = {"report_id": "rep6", "out_dir": str(out_dir), "abs_path": str(video_path)}
    cfg = {"srt_gps_tuple_order": "lat_lon", "camera_hfov_deg": 84.0}

    changed = bt.backfill_one_report(report, telemetry_dir, cfg, "http://127.0.0.1:8080")
    assert changed is True

    det = json.loads((out_dir / "detections.json").read_text(encoding="utf-8"))
    assert det[0]["est_lat"] != 11.11 and det[0]["est_lon"] != 22.22
    assert det[0]["est_lat"] is not None  # заменена на реально посчитанную, не просто стёрта


def test_backfill_manual_observations_fills_missing_coordinates(tmp_path):
    """Ручные наблюдения, сделанные ДО появления оценки координат объекта
    (или ДО значения est_lat в принципе), не имеют своего detections.json --
    это строки в БД, и backfill_one_report() их не трогает вообще. Проверяем
    отдельную функцию для этой таблицы."""
    video_path = tmp_path / "DJI_20260101000000_0001_Z.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "DJI_20260101000000_0001_Z.SRT").write_text(SRT_WITH_GIMBAL, encoding="utf-8")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep7", "DJI_20260101000000_0001_Z.MP4", str(video_path), "video", "done",
         str(tmp_path / "out"), 0.0, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, "
        "label, note, lat, lon, est_lat, est_lon, est_distance_m, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rep7", "tester", 1.0, json.dumps([0.5, 0.5, 0.5, 0.5]), "человек", "",
         None, None, None, None, None, "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    cfg = {"srt_gps_tuple_order": "lat_lon", "camera_hfov_deg": 84.0}
    updated = bt.backfill_manual_observations(db_path, telemetry_dir, cfg)
    assert updated == 1

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT lat, lon, est_lat, est_lon FROM manual_observations WHERE report_id='rep7'").fetchone()
    conn.close()
    assert row[0] == 39.4808 and row[1] == 73.592544  # координаты дрона подтянулись
    assert row[2] is not None and row[3] is not None  # и оценка объекта тоже


def test_backfill_manual_observations_skips_unchanged_rows(tmp_path):
    """Если пересчёт даёт то же самое значение -- лишний UPDATE не делаем."""
    video_path = tmp_path / "no_telemetry.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()  # пусто -- телеметрии нет

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep8", "no_telemetry.MP4", str(video_path), "video", "done",
         str(tmp_path / "out"), 0.0, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, "
        "label, note, lat, lon, est_lat, est_lon, est_distance_m, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rep8", "tester", 1.0, json.dumps([0.5, 0.5, 0.5, 0.5]), "человек", "",
         None, None, None, None, None, "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    cfg = {"srt_gps_tuple_order": "lat_lon"}
    updated = bt.backfill_manual_observations(db_path, telemetry_dir, cfg)
    assert updated == 0  # телеметрии нет -- всё осталось None, как и было


def test_backfill_fills_missing_raw_telemetry_for_report_hits(tmp_path):
    """Старый отчёт (до появления raw_telemetry) не хранит полный текст
    SRT-блока -- backfill должен подтянуть его, не трогая при этом уже
    посчитанные GPS/оценку, если они не изменились."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hit = _hit_dict(0, 1.0, lat=39.4808)  # GPS уже есть, raw_telemetry -- нет (старый формат)
    hit["bbox"] = [300, 220, 340, 260]
    _write_detections(out_dir, [hit])

    video_path = tmp_path / "DJI_20260101000000_0001_Z.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "DJI_20260101000000_0001_Z.SRT").write_text(SRT_WITH_GIMBAL, encoding="utf-8")

    report = {"report_id": "rep9", "out_dir": str(out_dir), "abs_path": str(video_path)}
    cfg = {"srt_gps_tuple_order": "lat_lon"}

    changed = bt.backfill_one_report(report, telemetry_dir, cfg, "http://127.0.0.1:8080")
    assert changed is True

    det = json.loads((out_dir / "detections.json").read_text(encoding="utf-8"))
    assert det[0]["raw_telemetry"] is not None
    assert "latitude: 39.480800" in det[0]["raw_telemetry"]


def test_backfill_manual_observations_fills_missing_raw_telemetry(tmp_path):
    video_path = tmp_path / "DJI_20260101000000_0001_Z.MP4"
    video_path.write_bytes(b"fake")
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "DJI_20260101000000_0001_Z.SRT").write_text(SRT_WITH_GIMBAL, encoding="utf-8")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep10", "DJI_20260101000000_0001_Z.MP4", str(video_path), "video", "done",
         str(tmp_path / "out"), 0.0, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, "
        "label, note, lat, lon, est_lat, est_lon, est_distance_m, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rep10", "tester", 1.0, json.dumps([0.5, 0.5, 0.5, 0.5]), "человек", "",
         39.4808, 73.592544, None, None, None, "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    cfg = {"srt_gps_tuple_order": "lat_lon"}
    updated = bt.backfill_manual_observations(db_path, telemetry_dir, cfg)
    assert updated == 1

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT raw_telemetry FROM manual_observations WHERE report_id='rep10'").fetchone()
    conn.close()
    assert row[0] is not None
    assert "latitude: 39.480800" in row[0]
