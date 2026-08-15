"""Тесты группировки детекций в сцены (sar_video_review.group_hits_into_scenes),
особенно граничные случаи вокруг max_frame_gap -- ключевая для качества
отчёта логика, которую CLAUDE.md прямо просит покрыть тестами."""
from sar_video_review import Hit, group_hits_into_scenes


def make_hit(frame_idx, cx, cy, size=20, object_class="person", source="model", confidence=0.5):
    return Hit(
        frame_idx=frame_idx, timestamp=str(frame_idx), seconds=float(frame_idx),
        confidence=confidence, object_class=object_class, source=source,
        bbox=[cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2],
        lat=None, lon=None, alt=None,
        image_path=f"crop_{frame_idx}.jpg", full_image_path=None,
    )


def test_hits_within_max_frame_gap_merge_into_one_scene():
    hits = [make_hit(0, 100, 100), make_hit(100, 105, 100), make_hit(200, 110, 100)]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    assert len(groups) == 1
    assert len(groups[0]["hits"]) == 3


def test_hits_beyond_max_frame_gap_split_into_two_scenes():
    hits = [make_hit(0, 100, 100), make_hit(200, 100, 100)]  # разрыв 200 > gap 100
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    assert len(groups) == 2


def test_gap_exactly_at_boundary_still_merges():
    # разрыв РОВНО равен max_frame_gap -- граница включительно (<=  в коде)
    hits = [make_hit(0, 100, 100), make_hit(100, 100, 100)]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    assert len(groups) == 1


def test_gap_one_frame_beyond_boundary_splits():
    hits = [make_hit(0, 100, 100), make_hit(101, 100, 100)]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    assert len(groups) == 2


def test_different_object_classes_never_share_a_scene():
    hits = [make_hit(0, 100, 100, object_class="person"),
            make_hit(1, 100, 100, object_class="tent")]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    assert len(groups) == 2


def test_far_apart_boxes_in_same_frame_range_split_by_distance():
    # рядом по времени, но далеко в кадре -- два разных объекта, не один
    hits = [make_hit(0, 10, 10, size=10), make_hit(1, 900, 900, size=10)]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    assert len(groups) == 2


def test_empty_hits_produce_no_groups():
    assert group_hits_into_scenes([], max_frame_gap=100, distance_multiplier=2.5) == []
