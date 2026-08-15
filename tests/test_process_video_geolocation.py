"""Интеграционный тест: process_video() реально прокидывает высоту/углы
подвеса/размер кадра в estimate_ground_point() и сохраняет результат на Hit
-- не просто "функция сама по себе работает" (это уже покрыто
test_geolocation.py), а что реальный пайплайн её действительно вызывает с
правильными аргументами (bbox в долях кадра, телеметрия по таймкоду и т.д.).

Модель полностью замокана (load_model/detect_frame_tiled) -- реальный
YOLO/torch здесь не запускается, только настоящее видео через cv2 (2 кадра),
чтобы process_video() честно прошёл через открытие видео/запись кропов."""
import cv2
import numpy as np
import pytest

import sar_video_review as svr


def test_process_video_computes_drone_and_estimated_coordinates(tmp_path, monkeypatch):
    video_path = tmp_path / "test.mp4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (640, 480))
    for _ in range(2):
        writer.write(np.zeros((480, 640, 3), dtype=np.uint8))
    writer.release()

    srt_path = tmp_path / "test.srt"
    srt_path.write_text(
        "1\n00:00:00,000 --> 00:00:10,000\n"
        "[latitude: 42.000000] [longitude: 74.000000] [rel_alt: 1000.0] "
        "[gb_yaw: 0.0 gb_pitch: -45.0 gb_roll: 0.0]\n",
        encoding="utf-8")

    monkeypatch.setattr(svr, "load_model", lambda *a, **k: None)
    # одна детекция ровно в центре кадра (640x480 -> центр 320,240)
    monkeypatch.setattr(
        svr, "detect_frame_tiled",
        lambda frame, conf, tile, overlap, classes: [(295, 215, 345, 265, 0.9, "person")])

    out_dir = tmp_path / "out"
    hits = svr.process_video(
        video_path=str(video_path), weights_path="unused", model_type="custom",
        target_classes=["person"], conf=0.1, tile_size=640, overlap=0.25, frame_skip=1,
        out_dir=str(out_dir), srt_path=str(srt_path), use_color_anomaly=False,
        camera_hfov_deg=84.0,
    )

    assert len(hits) == 2  # по одной детекции на каждый из 2 кадров видео
    h = hits[0]

    # координаты ДРОНА -- взяты из SRT как есть
    assert h.lat == 42.0 and h.lon == 74.0
    assert h.alt == 1000.0
    assert h.yaw == 0.0 and h.pitch == -45.0

    # process_video() сам заполнил размер кадра, не пришлось передавать отдельно
    assert h.frame_width == 640 and h.frame_height == 480

    # bbox ровно по центру кадра -> доля (0.5, 0.5) -> тот же угол, что и
    # у самого подвеса (-45°) -> расстояние = altitude * tan(45°) = 1000 м
    assert h.est_lat is not None and h.est_lon is not None
    assert h.est_distance_m == pytest.approx(1000.0, rel=1e-3)

    # оценка объекта -- НЕ то же самое, что координаты дрона (иначе смысла
    # в отдельном поле бы не было -- это ключевая проверка "не перепутали")
    assert (h.est_lat, h.est_lon) != (h.lat, h.lon)


def test_process_video_without_telemetry_leaves_estimate_none(tmp_path, monkeypatch):
    video_path = tmp_path / "test.mp4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (640, 480))
    writer.write(np.zeros((480, 640, 3), dtype=np.uint8))
    writer.release()

    monkeypatch.setattr(svr, "load_model", lambda *a, **k: None)
    monkeypatch.setattr(
        svr, "detect_frame_tiled",
        lambda frame, conf, tile, overlap, classes: [(295, 215, 345, 265, 0.9, "person")])

    out_dir = tmp_path / "out"
    hits = svr.process_video(
        video_path=str(video_path), weights_path="unused", model_type="custom",
        target_classes=["person"], conf=0.1, tile_size=640, overlap=0.25, frame_skip=1,
        out_dir=str(out_dir), srt_path=None, use_color_anomaly=False,
    )
    assert len(hits) == 1
    assert hits[0].lat is None
    assert hits[0].est_lat is None
    assert hits[0].est_distance_m is None
