"""Чистка кропов, которые никто не показывает.

На боевых данных на диске 567 336 кропов (4,47 ГБ), а ссылается на них
154 416. Остальные 412 920 (2,81 ГБ) -- остаток разовой операции:
cleanup_blue_detections.py убрал из отчётов синие ложные детекции
цветового детектора, но файлы намеренно оставил ("дешевле места, чем риск
удалить нужное; при необходимости чистятся отдельно").

Удаление файлов -- необратимая операция на боевых отчётах, которыми прямо
сейчас пользуются. Поэтому здесь проверяется не столько "удалилось", сколько
"НЕ удалилось лишнее" и "отчёт после чистки не ссылается в пустоту".
"""
import json
import os

import pytest

import prune_crops


def _det(frame_idx, name, group="g0", conf=0.5):
    return {"frame_idx": frame_idx, "timestamp": str(frame_idx),
            "seconds": float(frame_idx), "confidence": conf,
            "object_class": "person", "source": "model",
            "bbox": [1, 1, 2, 2], "lat": None, "lon": None, "alt": None,
            "image_path": os.path.join("crops", name),
            "full_image_path": None, "group_id": group}


@pytest.fixture
def report(tmp_path):
    out = tmp_path / "rep1"
    crops = out / "crops"
    crops.mkdir(parents=True)

    dets = [_det(i, f"f{i:07d}_person_c0.50.jpg") for i in range(5)]
    for d in dets:
        (out / d["image_path"]).write_bytes(b"x" * 100)
    # мусор: файлы есть, ссылок на них нет -- ровно синие детекции,
    # вычищенные из отчёта, но оставшиеся на диске
    for i in range(20):
        (crops / f"f{i:07d}_цвет_синий_c0.30.jpg").write_bytes(b"y" * 100)

    (out / "detections.json").write_text(json.dumps(dets, ensure_ascii=False),
                                          encoding="utf-8")
    (out / "report.html").write_text(
        "<html>" + json.dumps(dets, ensure_ascii=False) + "</html>",
        encoding="utf-8")
    return out


# --- сироты ---------------------------------------------------------------

def test_orphans_are_planned_for_removal(report):
    drop, remap, freed = prune_crops.plan_report(str(report))
    assert len(drop) == 20
    assert freed == 20 * 100
    assert remap == {}, "для сирот перепривязывать нечего"


def test_referenced_crops_are_never_touched(report):
    """Главное свойство. Удалить показанный кроп значит испортить отчёт,
    которым пользуются прямо сейчас."""
    drop, _, _ = prune_crops.plan_report(str(report))
    for name in drop:
        assert "синий" in name, f"под удаление попал нужный файл: {name}"


def test_nothing_happens_without_go(report, monkeypatch):
    """План ничего не меняет на диске сам по себе."""
    before = sorted(os.listdir(report / "crops"))
    prune_crops.plan_report(str(report))
    assert sorted(os.listdir(report / "crops")) == before


def test_report_without_detections_is_skipped(tmp_path):
    out = tmp_path / "empty"
    (out / "crops").mkdir(parents=True)
    assert prune_crops.plan_report(str(out)) is None


def test_empty_detections_list_is_skipped(tmp_path):
    """Пустой detections.json -- это НЕ повод считать все кропы сиротами.

    Отчёт мог оборваться на полпути; снести по такому признаку все файлы
    значит потерять уже посчитанное.
    """
    out = tmp_path / "half"
    (out / "crops").mkdir(parents=True)
    (out / "crops" / "f0000000_person_c0.50.jpg").write_bytes(b"x")
    (out / "detections.json").write_text("[]", encoding="utf-8")
    assert prune_crops.plan_report(str(out)) is None


# --- ограничение на сцену -------------------------------------------------

def test_cap_keeps_at_most_n_per_scene(report):
    drop, remap, _ = prune_crops.plan_report(str(report), cap=2)
    kept = set(os.listdir(report / "crops")) - set(drop)
    person = [k for k in kept if "person" in k]
    assert len(person) == 2


def test_cap_remaps_dropped_references(report):
    """Иначе detections.json ссылается на удалённые файлы."""
    _, remap, _ = prune_crops.plan_report(str(report), cap=2)
    assert remap, "ссылки выброшенных кропов не перепривязаны"
    for old, new in remap.items():
        assert old != new


def test_after_cap_no_reference_points_at_a_missing_file(report):
    """Сквозная проверка: чистим по-настоящему и убеждаемся, что ни одна
    ссылка в detections.json не ведёт в пустоту."""
    drop, remap, _ = prune_crops.plan_report(str(report), cap=2)
    prune_crops.rewrite_refs(str(report), remap)
    for fn in drop:
        os.remove(report / "crops" / fn)

    dets = json.loads((report / "detections.json").read_text(encoding="utf-8"))
    for d in dets:
        p = report / d["image_path"]
        assert p.exists(), f"ссылка в пустоту: {d['image_path']}"


def test_html_escaped_backslashes_are_rewritten_too(tmp_path):
    """Грабли, на которых уже стояли при чистке кадров.

    В report.html детекции лежат JSON-блоком прямо в разметке, и обратный
    слэш там экранирован. Замена, знающая только про одинарный слэш, молча
    не находит НИЧЕГО и возвращает ноль замен -- притом что файлы уже
    удалены. Отчёт после такой чистки ссылается в пустоту, и выясняется
    это, только когда человек откроет сцену.
    """
    out = tmp_path / "rep"
    (out / "crops").mkdir(parents=True)
    (out / "detections.json").write_text("[]", encoding="utf-8")
    (out / "report.html").write_text(
        r'{"image_path": "crops\\f0000001_person_c0.50.jpg"}', encoding="utf-8")

    _, n = prune_crops.rewrite_refs(
        str(out), {"f0000001_person_c0.50.jpg": "f0000002_person_c0.50.jpg"})
    body = (out / "report.html").read_text(encoding="utf-8")
    assert n > 0, "экранированный слэш в HTML не найден -- замена молча не сработала"
    assert "f0000001" not in body
    assert "f0000002" in body


# --- безопасность ---------------------------------------------------------

def test_busy_reports_are_excluded(tmp_path):
    """В отчёт со статусом queued/processing прямо сейчас пишет воркер."""
    import sar_common
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    for rid, status in (("busy1", "processing"), ("busy2", "queued"),
                        ("calm", "done")):
        conn.execute(
            "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
            "created_at, updated_at) VALUES (?,?,?,'video',?, "
            "datetime('now'), datetime('now'))", (rid, rid + ".mp4", "/x", status))
    conn.commit()
    conn.close()
    busy = prune_crops.busy_report_ids(db)
    assert busy == {"busy1", "busy2"}
    assert "calm" not in busy
