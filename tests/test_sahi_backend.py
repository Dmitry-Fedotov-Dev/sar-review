"""Тесты для опционального бэкенда тайлинга SAHI (detector_backend='sahi').

ВАЖНО: реальный SAHI/ultralytics/torch инференс здесь НЕ вызывается --
sahi.predict.get_sliced_prediction и AutoDetectionModel.from_pretrained
полностью замоканы (мокаем на уровне модуля sahi, а не detect_frame_sahi --
внутри функции используется ленивый `from sahi.predict import ...`, который
резолвится заново при каждом вызове, так что монки на sahi.predict.* работают).

Дефолт остаётся "custom" -- эти тесты проверяют именно то, что SAHI-ветка
не трогает поведение по умолчанию и правильно связана в process_video().
"""
import sys

import numpy as np
import pytest

import sar_video_review as svr


def test_default_backend_is_custom():
    assert svr.DEFAULT_CONFIG["detector_backend"] == "custom"


def test_load_model_sahi_raises_actionable_error_when_package_missing(monkeypatch):
    # имитируем отсутствие пакета sahi через sys.modules, не трогая реально
    # установленный пакет в этом окружении
    monkeypatch.setitem(sys.modules, "sahi", None)
    with pytest.raises(RuntimeError, match="requirements-sahi"):
        svr.load_model_sahi("weights.pt", "custom", ["person"], 0.15)


def test_load_model_sahi_wraps_already_loaded_model_preserving_set_classes(monkeypatch):
    calls = {}

    def fake_load_model(weights_path, model_type, target_classes):
        # имитируем то, что реальный load_model() уже сделал бы --
        # включая set_classes() для yolo-world -- присваиваем _MODEL
        svr._MODEL = object()
        calls["load_model_args"] = (weights_path, model_type, target_classes)

    class FakeAutoDetectionModel:
        @staticmethod
        def from_pretrained(**kwargs):
            calls["from_pretrained_kwargs"] = kwargs
            return "FAKE_SAHI_WRAPPER"

    monkeypatch.setattr(svr, "load_model", fake_load_model)
    fake_sahi_module = type(sys)("sahi")
    fake_sahi_module.AutoDetectionModel = FakeAutoDetectionModel
    monkeypatch.setitem(sys.modules, "sahi", fake_sahi_module)

    result = svr.load_model_sahi("weights.pt", "yolo-world", ["person", "tent"], 0.2)

    assert calls["load_model_args"] == ("weights.pt", "yolo-world", ["person", "tent"])
    # КЛЮЧЕВАЯ проверка: модель передана как УЖЕ ЗАГРУЖЕННЫЙ объект (не путь) --
    # это заставляет SAHI использовать set_model() вместо своей загрузки с нуля
    assert calls["from_pretrained_kwargs"]["model"] is svr._MODEL
    assert calls["from_pretrained_kwargs"]["model_type"] == "ultralytics"
    assert calls["from_pretrained_kwargs"]["confidence_threshold"] == 0.2
    assert result == "FAKE_SAHI_WRAPPER"
    assert svr._SAHI_MODEL == "FAKE_SAHI_WRAPPER"


def test_detect_frame_sahi_converts_bgr_to_rgb_before_calling_sahi(monkeypatch):
    captured = {}

    def fake_get_sliced_prediction(image, model, **kwargs):
        captured["image"] = image
        captured["kwargs"] = kwargs

        class FakeResult:
            object_prediction_list = []
        return FakeResult()

    fake_predict_module = type(sys)("sahi.predict")
    fake_predict_module.get_sliced_prediction = fake_get_sliced_prediction
    monkeypatch.setitem(sys.modules, "sahi.predict", fake_predict_module)

    # синтетический кадр: чистый синий в BGR (255 в канале 0) -- после
    # правильной конвертации в RGB синий должен оказаться в канале 2
    frame_bgr = np.zeros((100, 100, 3), dtype=np.uint8)
    frame_bgr[:, :, 0] = 255  # B=255, G=0, R=0 (BGR)

    svr.detect_frame_sahi(frame_bgr, conf_threshold=0.15, tile_size=640,
                           overlap=0.25, target_classes=["person"])

    seen = captured["image"]
    assert seen[0, 0, 0] == 0    # R канал -- должен быть 0
    assert seen[0, 0, 2] == 255  # B канал сдвинулся на позицию 2 (RGB) -- конвертация произошла
    assert captured["kwargs"]["slice_height"] == 640
    assert captured["kwargs"]["overlap_height_ratio"] == 0.25


def test_process_video_dispatches_to_sahi_backend_when_configured(tmp_path, monkeypatch):
    """process_video(detector_backend='sahi') должен вызвать load_model_sahi()/
    detect_frame_sahi(), а НЕ обычные load_model()/detect_frame_tiled() --
    проверяем именно связку в реальном пайплайне, не изолированные функции."""
    import cv2

    video_path = tmp_path / "test.mp4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (320, 240))
    writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
    writer.release()

    calls = []
    monkeypatch.setattr(svr, "load_model_sahi", lambda *a, **k: calls.append("load_model_sahi"))
    monkeypatch.setattr(svr, "load_model", lambda *a, **k: calls.append("load_model"))
    monkeypatch.setattr(svr, "detect_frame_sahi",
                         lambda *a, **k: calls.append("detect_frame_sahi") or [])
    monkeypatch.setattr(svr, "detect_frame_tiled",
                         lambda *a, **k: calls.append("detect_frame_tiled") or [])

    svr.process_video(
        video_path=str(video_path), weights_path="unused", model_type="custom",
        target_classes=["person"], conf=0.1, tile_size=320, overlap=0.25, frame_skip=1,
        out_dir=str(tmp_path / "out"), srt_path=None, use_color_anomaly=False,
        detector_backend="sahi",
    )

    assert calls == ["load_model_sahi", "detect_frame_sahi"]


def test_detect_frame_sahi_filters_by_target_classes_and_extracts_bbox(monkeypatch):
    from sahi.prediction import ObjectPrediction

    op_person = ObjectPrediction(bbox=[10, 20, 30, 40], category_id=0, category_name="person", score=0.9)
    op_car = ObjectPrediction(bbox=[50, 60, 70, 80], category_id=1, category_name="car", score=0.8)

    def fake_get_sliced_prediction(image, model, **kwargs):
        class FakeResult:
            object_prediction_list = [op_person, op_car]
        return FakeResult()

    fake_predict_module = type(sys)("sahi.predict")
    fake_predict_module.get_sliced_prediction = fake_get_sliced_prediction
    monkeypatch.setitem(sys.modules, "sahi.predict", fake_predict_module)

    frame_bgr = np.zeros((100, 100, 3), dtype=np.uint8)
    dets = svr.detect_frame_sahi(frame_bgr, conf_threshold=0.15, tile_size=640,
                                  overlap=0.25, target_classes=["person"])

    assert len(dets) == 1  # "car" отфильтрован -- не входит в target_classes
    x1, y1, x2, y2, score, cls_name = dets[0]
    assert (x1, y1, x2, y2) == (10, 20, 30, 40)
    assert score == 0.9
    assert cls_name == "person"
