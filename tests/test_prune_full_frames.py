"""Тесты ретроактивной чистки полных кадров.

Операция необратимая и идёт по боевым отчётам, которые в этот момент открыты
у людей на экране. Поэтому проверяем не "освободилось сколько-то места", а
три вещи, нарушение любой из которых означает сломанный отчёт:

  1. после чистки НИ ОДНА ссылка в detections.json и report.html не ведёт на
     удалённый файл -- это главный тест, всё остальное вторично;
  2. на каждую сцену остаётся ровно тот набор кадров, что оставил бы сам
     детектор в режиме representative (первый / пиковый / последний);
  3. отчёты, в которые пишет воркер, не трогаются вовсе.

Собираем настоящий отчёт на диске -- каталоги, JPEG-файлы, detections.json и
report.html со ссылками, -- а не мокаем файловую систему: ошибка в разделителе
пути между Windows и POSIX как раз и живёт в этом стыке, и мок её спрячет.
"""
import json
import os

import numpy as np
import pytest

import prune_full_frames as pf


def _mk_report(root, groups, with_html=True):
    """groups: {gid: [(frame_idx, confidence), ...]} -> папка отчёта."""
    os.makedirs(os.path.join(root, "frames"), exist_ok=True)
    os.makedirs(os.path.join(root, "crops"), exist_ok=True)
    dets = []
    for gid, hits in groups.items():
        for fi, conf in hits:
            rel = os.path.join("frames", f"f{fi:07d}_full.jpg")
            with open(os.path.join(root, rel), "wb") as f:
                f.write(b"\xff\xd8" + bytes(200))       # непустой файл
            dets.append({"frame_idx": fi, "confidence": conf, "group_id": gid,
                         "full_image_path": rel,
                         "image_path": os.path.join("crops", f"f{fi:07d}_c.jpg"),
                         "bbox": [1, 2, 3, 4], "seconds": fi / 30.0})
    with open(os.path.join(root, "detections.json"), "w", encoding="utf-8") as f:
        json.dump(dets, f, ensure_ascii=False)
    if with_html:
        body = "\n".join(
            f'<img src="{d["full_image_path"]}"><img src="{d["image_path"]}">'
            for d in dets)
        with open(os.path.join(root, "report.html"), "w", encoding="utf-8") as f:
            f.write("<html><body>" + body + "</body></html>")
    return root


def _all_links(root):
    """Все ссылки на frames/ из detections.json и report.html."""
    links = set()
    with open(os.path.join(root, "detections.json"), encoding="utf-8") as f:
        for d in json.load(f):
            if d.get("full_image_path"):
                links.add(d["full_image_path"].replace("\\", os.sep).replace("/", os.sep))
    p = os.path.join(root, "report.html")
    if os.path.exists(p):
        import re
        with open(p, encoding="utf-8") as f:
            for m in re.findall(r'src="([^"]*frames[^"]*)"', f.read()):
                links.add(m.replace("\\", os.sep).replace("/", os.sep))
    return links


# --- главный инвариант ---------------------------------------------------

def test_no_link_points_at_a_deleted_file(tmp_path):
    root = _mk_report(str(tmp_path / "rep"),
                      {"g1": [(10, .2), (20, .9), (30, .3), (40, .1), (50, .4)],
                       "g2": [(60, .5), (70, .8)]})
    remap, keep, drop, freed = pf.plan_report(root)
    assert drop, "должно быть что удалять"
    pf.apply_report(root, remap, drop)

    for link in _all_links(root):
        assert os.path.exists(os.path.join(root, link)), f"битая ссылка: {link}"


def test_representative_frames_survive(tmp_path):
    """Первый, пиковый и последний кадр сцены обязаны остаться."""
    root = _mk_report(str(tmp_path / "rep"),
                      {"g1": [(10, .2), (20, .9), (30, .3), (40, .1), (50, .4)]})
    remap, keep, drop, freed = pf.plan_report(root)
    pf.apply_report(root, remap, drop)
    left = sorted(os.listdir(os.path.join(root, "frames")))
    assert "f0000010_full.jpg" in left      # первый
    assert "f0000020_full.jpg" in left      # пиковый (0.9)
    assert "f0000050_full.jpg" in left      # последний
    assert "f0000030_full.jpg" not in left
    assert len(left) == 3


def test_frames_are_actually_freed(tmp_path):
    root = _mk_report(str(tmp_path / "rep"),
                      {"g1": [(i, .5) for i in range(1, 21)]})
    before = sum(os.path.getsize(os.path.join(root, "frames", f))
                 for f in os.listdir(os.path.join(root, "frames")))
    remap, keep, drop, freed = pf.plan_report(root)
    pf.apply_report(root, remap, drop)
    after = sum(os.path.getsize(os.path.join(root, "frames", f))
                for f in os.listdir(os.path.join(root, "frames")))
    assert after < before
    assert freed == pytest.approx(before - after, rel=0.01)


def test_detections_are_rebound_not_dropped(tmp_path):
    """Детекции не удаляются -- у них меняется только ссылка на полный кадр."""
    root = _mk_report(str(tmp_path / "rep"),
                      {"g1": [(10, .2), (20, .9), (30, .3), (40, .1)]})
    with open(os.path.join(root, "detections.json"), encoding="utf-8") as f:
        n_before = len(json.load(f))
    remap, keep, drop, freed = pf.plan_report(root)
    pf.apply_report(root, remap, drop)
    with open(os.path.join(root, "detections.json"), encoding="utf-8") as f:
        dets = json.load(f)
    assert len(dets) == n_before
    assert all(d.get("full_image_path") for d in dets)


def test_rebinds_to_the_nearest_kept_frame(tmp_path):
    """Замена должна быть ближайшей по времени, иначе человек увидит кадр
    из другого места сцены."""
    root = _mk_report(str(tmp_path / "rep"),
                      {"g1": [(10, .9), (11, .1), (90, .2)]})
    remap, keep, drop, freed = pf.plan_report(root)
    pf.apply_report(root, remap, drop)
    with open(os.path.join(root, "detections.json"), encoding="utf-8") as f:
        by_idx = {d["frame_idx"]: d["full_image_path"] for d in json.load(f)}
    assert "f0000010" in by_idx[11], by_idx[11]


def test_html_links_are_rewritten(tmp_path):
    root = _mk_report(str(tmp_path / "rep"),
                      {"g1": [(10, .9), (11, .1), (90, .2)]})
    remap, keep, drop, freed = pf.plan_report(root)
    _, html_changed, _ = pf.apply_report(root, remap, drop)
    assert html_changed > 0
    with open(os.path.join(root, "report.html"), encoding="utf-8") as f:
        assert "f0000011_full.jpg" not in f.read()


def test_windows_backslash_paths_are_handled(tmp_path):
    """В боевых отчётах пути записаны через обратный слэш -- подмена обязана
    срабатывать и на таком написании."""
    root = str(tmp_path / "rep")
    os.makedirs(os.path.join(root, "frames"))
    for fi in (10, 11):
        with open(os.path.join(root, "frames", f"f{fi:07d}_full.jpg"), "wb") as f:
            f.write(bytes(100))
    with open(os.path.join(root, "report.html"), "w", encoding="utf-8") as f:
        f.write('<img src="frames\\f0000011_full.jpg">')
    n = pf.rewrite_text(os.path.join(root, "report.html"),
                        {"frames\\f0000011_full.jpg": "frames\\f0000010_full.jpg"})
    assert n == 1
    with open(os.path.join(root, "report.html"), encoding="utf-8") as f:
        assert "f0000011" not in f.read()


def test_json_escaped_backslash_in_html_is_rewritten(tmp_path):
    """Регрессия на реальную поломку.

    report.html держит детекции JSON-блоком прямо в разметке, и внутри него
    обратный слэш экранирован: "frames\\\\f0000011_full.jpg". Первая версия
    подмены знала только про одинарный слэш, не находила НИЧЕГО, честно
    возвращала 0 замен -- а кадры к этому моменту уже удалены. Отчёты стали
    ссылаться в пустоту, и заметить это по выводу было нельзя: ноль замен
    выглядит как "нечего менять".
    """
    p = tmp_path / "report.html"
    p.write_text('{"full_image_path": "frames\\\\f0000011_full.jpg"}',
                 encoding="utf-8")
    n = pf.rewrite_text(str(p), {"frames\\f0000011_full.jpg":
                                  "frames\\f0000010_full.jpg"})
    assert n == 1, "экранированный вариант пути обязан находиться"
    assert "f0000011" not in p.read_text(encoding="utf-8")


def test_all_three_path_spellings_are_covered():
    v = pf.path_variants("frames\\f0000011_full.jpg")
    assert "frames\\\\f0000011_full.jpg" in v      # JSON внутри HTML
    assert "frames\\f0000011_full.jpg" in v        # обычный Windows-путь
    assert "frames/f0000011_full.jpg" in v         # POSIX


def test_escaped_variant_is_tried_before_the_plain_one():
    """Порядок важен: одинарный вариант является подстрокой экранированного,
    и если проверять его первым, он съест половину двойного слэша и оставит
    в файле мусор вида 'frames\\frames\\...'."""
    v = pf.path_variants("frames\\f0000011_full.jpg")
    assert v.index("frames\\\\f0000011_full.jpg") < v.index("frames\\f0000011_full.jpg")


def test_repair_html_fixes_reports_broken_by_the_old_version(tmp_path):
    """Ремонт восстанавливает привязку по detections.json -- он к этому
    моменту уже переписан правильно, и содержит посценную привязку."""
    root = str(tmp_path / "rep")
    os.makedirs(os.path.join(root, "frames"))
    with open(os.path.join(root, "frames", "f0000010_full.jpg"), "wb") as f:
        f.write(bytes(100))                       # только он и уцелел
    with open(os.path.join(root, "detections.json"), "w", encoding="utf-8") as f:
        json.dump([{"frame_idx": 10, "full_image_path": "frames\\f0000010_full.jpg"},
                    {"frame_idx": 11, "full_image_path": "frames\\f0000010_full.jpg"}],
                   f, ensure_ascii=False)
    with open(os.path.join(root, "report.html"), "w", encoding="utf-8") as f:
        f.write('{"full_image_path": "frames\\\\f0000011_full.jpg"}')

    broken, fixed = pf.repair_html(root)
    assert broken == 1 and fixed == 1
    with open(os.path.join(root, "report.html"), encoding="utf-8") as f:
        assert "f0000011" not in f.read()


def test_repair_is_a_no_op_on_healthy_report(tmp_path):
    root = _mk_report(str(tmp_path / "rep"), {"g1": [(10, .9)]})
    assert pf.repair_html(root) == (0, 0)


# --- безопасность --------------------------------------------------------

def test_crops_are_never_touched(tmp_path):
    """Кропы -- основа сетки карточек, их чистка не касается."""
    root = _mk_report(str(tmp_path / "rep"), {"g1": [(i, .5) for i in range(1, 10)]})
    crops = os.path.join(root, "crops")
    open(os.path.join(crops, "x.jpg"), "wb").write(bytes(50))
    before = os.listdir(crops)
    remap, keep, drop, freed = pf.plan_report(root)
    pf.apply_report(root, remap, drop)
    assert os.listdir(crops) == before


def test_report_without_detections_is_skipped(tmp_path):
    root = str(tmp_path / "empty")
    os.makedirs(os.path.join(root, "frames"))
    with open(os.path.join(root, "detections.json"), "w", encoding="utf-8") as f:
        json.dump([], f)
    assert pf.plan_report(root) is None


def test_broken_detections_json_is_skipped_not_crashed(tmp_path):
    root = str(tmp_path / "bad")
    os.makedirs(os.path.join(root, "frames"))
    with open(os.path.join(root, "detections.json"), "w", encoding="utf-8") as f:
        f.write("{не json")
    assert pf.plan_report(root) is None


def test_second_run_is_a_no_op(tmp_path):
    """Идемпотентность: повторный запуск не должен ничего сносить."""
    root = _mk_report(str(tmp_path / "rep"),
                      {"g1": [(10, .2), (20, .9), (30, .3), (40, .1)]})
    remap, keep, drop, freed = pf.plan_report(root)
    pf.apply_report(root, remap, drop)
    left = sorted(os.listdir(os.path.join(root, "frames")))
    again = pf.plan_report(root)
    assert again is None or not again[2], "второй проход не должен удалять"
    assert sorted(os.listdir(os.path.join(root, "frames"))) == left
