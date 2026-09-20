"""Детектор не хранит кропы, которых отчёт не показывает.

Кроп пишется на КАЖДУЮ детекцию прямо в цикле по кадрам -- до того, как
детекции сгруппированы в сцены, то есть заранее неизвестно, какие
понадобятся. А в report.html встраивается не больше
MAX_HITS_PER_SCENE_IN_REPORT кадров на сцену: иначе страница вырастает до
сотен мегабайт и перестаёт открываться (на реальном горном видео цветовой
детектор дал 516 110 детекций и отчёт в 296 МБ).

Всё, что сверх этого порога, лежит на диске и не показывается никогда.
На боевых данных медианная сцена -- 9 детекций, но девяностый процентиль
уже 121, а максимум 1598: весь объём даёт длинный хвост крупных сцен.

Опасность правки в том, что она УДАЛЯЕТ файлы. Поэтому проверяется прежде
всего обратное: что показываемое остаётся на месте и что после чистки не
осталось ни одной ссылки в пустоту.
"""
import os

import pytest

import sar_video_review as v


class FakeHit:
    """Минимальный Hit: чистке нужны только эти три поля."""
    def __init__(self, frame_idx, name, confidence=0.5):
        self.frame_idx = frame_idx
        self.image_path = os.path.join("crops", name)
        self.confidence = confidence


@pytest.fixture
def scene(tmp_path):
    crops = tmp_path / "crops"
    crops.mkdir()
    hits = []
    for i in range(50):
        name = f"f{i:07d}_person_c0.50.jpg"
        (crops / name).write_bytes(b"x" * 10)
        hits.append(FakeHit(i, name, confidence=0.9 if i == 33 else 0.5))
    return tmp_path, [{"hits": hits}], hits


def test_files_beyond_the_limit_are_removed(scene):
    out, groups, _ = scene
    removed = v.prune_unshown_crops(groups, str(out), limit=10)
    assert removed == 40
    assert len(os.listdir(out / "crops")) == 10


def test_nothing_is_removed_when_scene_fits(scene):
    out, groups, _ = scene
    assert v.prune_unshown_crops(groups, str(out), limit=100) == 0
    assert len(os.listdir(out / "crops")) == 50


def test_no_hit_points_at_a_deleted_file(scene):
    """Самое важное. Ссылка в пустоту не падает и не кричит -- сцена просто
    открывается с битыми картинками, и замечает это только человек."""
    out, groups, hits = scene
    v.prune_unshown_crops(groups, str(out), limit=10)
    for h in hits:
        assert os.path.exists(os.path.join(str(out), h.image_path)), (
            f"ссылка в пустоту: {h.image_path}")


def test_peak_confidence_frame_survives(scene):
    """Пиковый кадр показан на карточке сцены -- потерять его значит
    поменять то, что человек видит в списке находок."""
    out, groups, hits = scene
    v.prune_unshown_crops(groups, str(out), limit=10)
    peak_name = "f0000033_person_c0.50.jpg"
    assert peak_name in os.listdir(out / "crops")


def test_dropped_hits_are_remapped_to_the_nearest_kept_frame(scene):
    """Не к первому попавшемуся: человек листает кадры сцены по порядку,
    и подмена дальним кадром сбивает представление о том, что происходило."""
    out, groups, hits = scene
    v.prune_unshown_crops(groups, str(out), limit=10)
    for h in hits:
        kept_idx = int(os.path.basename(h.image_path)[1:8])
        assert abs(kept_idx - h.frame_idx) <= 6, (
            f"кадр {h.frame_idx} перепривязан к далёкому {kept_idx}")


def test_default_limit_matches_what_the_report_embeds():
    """Порог по умолчанию равен тому, что и так встраивается в HTML.

    Значит включение чистки НЕ меняет видимого поведения: удаляются ровно
    те файлы, которых в отчёте нет и не было. Разойдись эти два числа --
    и чистка начала бы удалять показываемое.
    """
    import inspect
    default = inspect.signature(v.process_video).parameters["max_crops_per_scene"].default
    assert default == v.MAX_HITS_PER_SCENE_IN_REPORT


def test_missing_crops_dir_is_not_an_error(tmp_path):
    assert v.prune_unshown_crops([], str(tmp_path), limit=10) == 0


def test_scene_without_crops_is_skipped(tmp_path):
    """У сцены могут быть детекции без сохранённого кропа."""
    (tmp_path / "crops").mkdir()
    (tmp_path / "crops" / "other.jpg").write_bytes(b"x")
    groups = [{"hits": [FakeHit(0, "gone.jpg")]}]
    groups[0]["hits"][0].image_path = ""
    v.prune_unshown_crops(groups, str(tmp_path), limit=5)
