"""Тесты разовой чистки синих цветовых детекций (cleanup_blue_detections.py)
и того, что синий убран из самого детектора.

Причина: на горном материале синий -- это небо и тени в снегу, а не находка
(503 499 срабатываний против 12 611 у модели на одном видео)."""
import cleanup_blue_detections as cleanup
import sar_video_review as svr


def test_blue_removed_from_detector_ranges():
    labels = " ".join(svr.COLOR_RANGES).lower()
    assert "син" not in labels and "голуб" not in labels
    # оранжевый и жёлтый на снегу осмысленны -- должны остаться
    assert any("оранж" in k.lower() for k in svr.COLOR_RANGES)
    assert any("жёлт" in k.lower() for k in svr.COLOR_RANGES)


def test_blue_color_hits_are_dropped():
    assert cleanup._is_dropped(
        {"source": "color", "object_class": "цвет: синий/голубой (тенты, куртки)"})


def test_other_color_hits_are_kept():
    assert not cleanup._is_dropped(
        {"source": "color", "object_class": "цвет: оранжевый/красный (снаряжение)"})
    assert not cleanup._is_dropped({"source": "color", "object_class": "цвет: жёлтый"})


def test_model_detections_are_never_dropped():
    """Даже если класс модели вдруг называется 'синий что-то' -- это находка
    модели, а не цветовое пятно, и трогать её нельзя."""
    assert not cleanup._is_dropped({"source": "model", "object_class": "person"})
    assert not cleanup._is_dropped({"source": "model", "object_class": "синий рюкзак"})


def test_missing_class_does_not_crash():
    assert not cleanup._is_dropped({"source": "color"})
    assert not cleanup._is_dropped({"source": "color", "object_class": None})
