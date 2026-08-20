"""Тесты восстановления кропов с испорченными именами.

Операция переименовывает файлы в боевых отчётах, поэтому проверяется не
«сколько починилось», а то, что она не тронет ничего лишнего: посторонние
файлы, уже правильные имена и случаи, когда восстановленного имени никто
не ждёт.
"""
import json
import os

import pytest

import repair_crops as rc


MANGLED = "f0000180_С†РІРµС‚_РѕСЂР°РЅР¶РµРІС‹Р№_c0.43.jpg"
CORRECT = "f0000180_цвет_оранжевый_c0.43.jpg"


def test_unmangle_restores_the_name():
    assert rc.unmangle(MANGLED) == CORRECT


def test_unmangle_returns_none_for_correct_name():
    assert rc.unmangle("f0000180_person_c0.79.jpg") is None
    assert rc.unmangle(CORRECT) is None


def test_unmangle_survives_garbage():
    for name in ("", "просто_файл.jpg", "❤.jpg"):
        rc.unmangle(name)      # не должно бросать


def _report(tmp_path, disk_names, referenced):
    out = tmp_path / "rep"
    (out / "crops").mkdir(parents=True)
    for n in disk_names:
        (out / "crops" / n).write_bytes(b"\xff\xd8")
    dets = [{"image_path": "crops\\" + n, "frame_idx": 1} for n in referenced]
    (out / "detections.json").write_text(json.dumps(dets), encoding="utf-8")
    return str(out)


def test_plan_finds_the_pair(tmp_path):
    out = _report(tmp_path, [MANGLED], [CORRECT])
    pairs, missing = rc.plan_report(out)
    assert pairs == [(MANGLED, CORRECT)]
    assert missing == 1


def test_nothing_to_do_when_all_present(tmp_path):
    out = _report(tmp_path, [CORRECT], [CORRECT])
    pairs, missing = rc.plan_report(out)
    assert pairs == [] and missing == 0


def test_foreign_file_is_left_alone(tmp_path):
    """Файл, чьё восстановленное имя никто не ждёт, трогать нельзя."""
    out = _report(tmp_path, [MANGLED, "посторонний.jpg"], [CORRECT])
    pairs, _ = rc.plan_report(out)
    assert [p[0] for p in pairs] == [MANGLED]


def test_missing_without_a_source_stays_missing(tmp_path):
    """Кроп потерян по-настоящему -- переименовывать нечего, и об этом
    надо честно сообщить, а не сделать вид, что починили."""
    out = _report(tmp_path, [], ["f0000999_person_c0.5.jpg"])
    pairs, missing = rc.plan_report(out)
    assert pairs == [] and missing == 1


def test_broken_detections_json_is_skipped(tmp_path):
    out = tmp_path / "bad"
    (out / "crops").mkdir(parents=True)
    (out / "detections.json").write_text("{не json", encoding="utf-8")
    assert rc.plan_report(str(out)) == ([], 0)


def test_report_without_crops_dir_is_skipped(tmp_path):
    out = tmp_path / "empty"
    out.mkdir()
    assert rc.plan_report(str(out)) == ([], 0)
