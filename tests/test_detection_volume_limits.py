"""Защита от вырождения детектора и неоткрываемого отчёта.

Из реальной эксплуатации: на горном видео цветовой детектор дал 503 499
срабатываний по "синий/голубой (тенты, куртки)" -- это небо и синеватые тени
в снегу, ~167 ложных пятен на КАЖДЫЙ кадр (у модели за то же видео 12 611).
report.html вырос до 296 МБ, и страница сцен фактически перестала
открываться -- команда из 6 человек жаловалась на тормоза.

Ограничения ставятся в двух местах и по разным причинам:
  1. на кадр в самом детекторе -- чтобы шум не попадал в данные вообще;
  2. на то, что встраивается в HTML -- страховка, чтобы даже при всплеске
     отчёт оставался открываемым (сырые детекции при этом полностью
     сохраняются в detections.json/csv)."""
import numpy as np

import sar_video_review as svr


def _noisy_blue_frame(w=640, h=480, blobs=200):
    """Кадр с множеством синих пятен -- имитация неба/теней в снегу."""
    frame = np.full((h, w, 3), 200, dtype=np.uint8)  # светлый фон
    rng = np.random.default_rng(42)
    for _ in range(blobs):
        x = int(rng.integers(0, w - 12))
        y = int(rng.integers(0, h - 12))
        frame[y:y + 10, x:x + 10] = (200, 60, 20)  # BGR -- насыщенный синий
    return frame


def test_color_detector_caps_detections_per_frame():
    frame = _noisy_blue_frame()
    unlimited = svr.detect_color_anomalies(frame, max_per_frame=0)
    limited = svr.detect_color_anomalies(frame, max_per_frame=12)
    assert len(unlimited) > 12, "тест бессмысленен, если пятен и так мало"
    assert len(limited) == 12


def test_color_detector_keeps_the_most_confident():
    frame = _noisy_blue_frame()
    unlimited = svr.detect_color_anomalies(frame, max_per_frame=0)
    limited = svr.detect_color_anomalies(frame, max_per_frame=5)
    best = sorted((r[4] for r in unlimited), reverse=True)[:5]
    assert sorted((r[4] for r in limited), reverse=True) == best


def test_color_detector_unlimited_when_cap_is_zero():
    frame = _noisy_blue_frame(blobs=50)
    assert len(svr.detect_color_anomalies(frame, max_per_frame=0)) > 12


# --- ограничение того, что встраивается в report.html ---

def _hit(frame_idx, confidence=0.5):
    return svr.Hit(
        frame_idx=frame_idx, timestamp=str(frame_idx), seconds=float(frame_idx),
        confidence=confidence, object_class="person", source="model",
        bbox=[0, 0, 10, 10], lat=None, lon=None, alt=None,
        image_path="c.jpg", full_image_path="f.jpg")


def test_scene_hits_are_capped_for_the_report():
    hits = [_hit(i) for i in range(500)]
    limited = svr._limit_scene_hits(hits, limit=80)
    assert len(limited) == 80


def test_scene_hits_below_limit_are_untouched():
    hits = [_hit(i) for i in range(10)]
    assert svr._limit_scene_hits(hits, limit=80) is hits


def test_scene_hits_sampled_across_whole_scene_not_just_start():
    """Берём равномерно по сцене, а не первые N -- иначе теряется обзор
    второй половины эпизода."""
    hits = [_hit(i) for i in range(500)]
    limited = svr._limit_scene_hits(hits, limit=50)
    assert limited[0].frame_idx < 20
    assert limited[-1].frame_idx > 400


def test_scene_hits_keep_peak_confidence_frame():
    """Пиковый кадр показан на карточке сцены -- он обязан остаться в наборе,
    иначе карточка ссылается на кадр, которого нет в данных страницы."""
    hits = [_hit(i) for i in range(500)]
    hits[377] = _hit(377, confidence=0.99)
    limited = svr._limit_scene_hits(hits, limit=40)
    assert any(h.frame_idx == 377 for h in limited)


def test_scene_hits_stay_ordered_by_frame():
    hits = [_hit(i) for i in range(500)]
    hits[377] = _hit(377, confidence=0.99)
    limited = svr._limit_scene_hits(hits, limit=40)
    assert [h.frame_idx for h in limited] == sorted(h.frame_idx for h in limited)


# --- резерв ядер под веб-сервис ---

def test_cpu_reserve_leaves_cores_for_the_web_service(monkeypatch):
    monkeypatch.setattr(svr.os, "cpu_count", lambda: 6)
    assert svr._limit_cpu_threads(reserve_cores=2) == 4


def test_cpu_reserve_never_returns_zero_threads(monkeypatch):
    monkeypatch.setattr(svr.os, "cpu_count", lambda: 2)
    assert svr._limit_cpu_threads(reserve_cores=8) == 1
