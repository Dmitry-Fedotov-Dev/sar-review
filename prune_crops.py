"""Убирает из готовых отчётов кропы, которые никто не показывает.

ОТКУДА ВЗЯЛСЯ МУСОР. На боевых данных на диске лежит 567 336 кропов на
4,47 ГБ, а ссылается на них 154 416. Остальные 412 920 (2,81 ГБ) не нужны
никому. Это не течь в обработке, а остаток разовой операции:
cleanup_blue_detections.py вычистил из отчётов детекции цветового
детектора по синему (на горном материале синий -- это небо и тени в снегу,
одно видео дало 503 499 ложных срабатываний), но файлы намеренно оставил.
Он это честно документирует: "файлы кропов/кадров на диске не удаляются --
они дешевле места, чем риск удалить нужное; при необходимости чистятся
отдельно". Вот отдельно.

Синий из детектора убран (COLOR_RANGES в sar_video_review.py), новые такие
кропы не появляются -- то есть чистка разовая, а не затыкание дыры.

ЧТО ДЕЛАЕТ

  --orphans (по умолчанию)
      Удаляет файлы из crops/, на которые НЕ ссылается detections.json.
      Ни отчёт, ни платформа их не показывают, поэтому ничего не
      переписывается: удаление файла, которого нет ни в одной ссылке,
      изменить ничего не может.

  --cap N
      Дополнительно оставляет не больше N кропов на сцену. Смысл: в
      report.html и так встраивается не больше MAX_HITS_PER_SCENE_IN_REPORT
      (80) кадров на сцену (см. _limit_scene_hits), остальные лежат
      мёртвым грузом. Здесь сложнее: на такие кропы ссылается
      detections.json, поэтому ссылки перепривязываются на ближайший
      оставшийся -- ровно как это делает prune_full_frames.py для кадров.

ЧЕГО НЕ ТРОГАЕТ: отчёты в работе (queued/processing) -- в них пишет
воркер; кадры (frames/) -- для них есть свой prune_full_frames.py.

По умолчанию только показывает. Реальная чистка -- с --go.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common


def _basename(p):
    return (p or "").replace("\\", "/").split("/")[-1]


def plan_report(out_dir, cap=None):
    """Что удалить и что перепривязать. Ничего не меняет.

    Возвращает (drop, remap, освобождается_байт) или None, если отчёт
    трогать не надо. drop -- имена файлов внутри crops/.
    """
    dj = os.path.join(out_dir, "detections.json")
    crops_dir = os.path.join(out_dir, "crops")
    if not (os.path.exists(dj) and os.path.isdir(crops_dir)):
        return None
    try:
        with open(dj, encoding="utf-8") as f:
            dets = json.load(f)
    except Exception:
        return None
    if not isinstance(dets, list) or not dets:
        return None

    referenced = {_basename(d.get("image_path")) for d in dets}
    referenced.discard("")

    keep = set(referenced)
    remap = {}

    if cap:
        # Оставляем равномерно по сцене и обязательно пиковый по
        # уверенности: именно он показан на карточке сцены. Это тот же
        # отбор, что делает _limit_scene_hits для встраивания в HTML.
        groups = defaultdict(list)
        for d in dets:
            groups[d.get("group_id")].append(d)
        keep = set()
        for gid, hits in groups.items():
            ordered = sorted(hits, key=lambda h: h.get("frame_idx") or 0)
            if len(ordered) <= cap:
                chosen = ordered
            else:
                peak = max(ordered, key=lambda h: h.get("confidence") or 0.0)
                step = len(ordered) / float(cap)
                chosen = [ordered[int(i * step)] for i in range(cap)]
                if peak not in chosen:
                    chosen[len(chosen) // 2] = peak
            chosen_names = {_basename(h.get("image_path")) for h in chosen}
            chosen_names.discard("")
            keep |= chosen_names
            # ссылки выброшенных -- на ближайший по кадру оставшийся
            by_frame = sorted((h.get("frame_idx") or 0, _basename(h.get("image_path")))
                              for h in chosen if _basename(h.get("image_path")))
            if not by_frame:
                continue
            for h in ordered:
                name = _basename(h.get("image_path"))
                if name and name not in chosen_names:
                    near = min(by_frame, key=lambda fp: abs(fp[0] - (h.get("frame_idx") or 0)))
                    remap[name] = near[1]

    on_disk = {}
    for fn in os.listdir(crops_dir):
        try:
            on_disk[fn] = os.path.getsize(os.path.join(crops_dir, fn))
        except OSError:
            pass

    drop = [fn for fn in on_disk if fn not in keep]
    freed = sum(on_disk[fn] for fn in drop)
    return drop, remap, freed


def rewrite_refs(out_dir, remap):
    """Перепривязывает ссылки в detections.json и report.html.

    В HTML детекции лежат JSON-блоком прямо в разметке, и обратный слэш
    там экранирован ("crops\\\\f0003290_person_c0.72.jpg"). Замена,
    знающая только про одинарный слэш, молча не находит НИЧЕГО -- и
    отчёт после чистки ссылается в пустоту. На этих граблях уже стояли
    при чистке кадров, см. path_variants в prune_full_frames.py.
    """
    if not remap:
        return 0, 0
    dj = os.path.join(out_dir, "detections.json")
    changed = 0
    with open(dj, encoding="utf-8") as f:
        dets = json.load(f)
    for d in dets:
        name = _basename(d.get("image_path"))
        if name in remap:
            d["image_path"] = os.path.join("crops", remap[name])
            changed += 1
    if changed:
        tmp = dj + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dets, f, ensure_ascii=False)
        os.replace(tmp, dj)          # атомарно: отчёт могут читать прямо сейчас

    html_path = os.path.join(out_dir, "report.html")
    n = 0
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8", errors="replace") as f:
            s = f.read()
        for old, new in remap.items():
            for a, b in ((f"crops\\\\{old}", f"crops\\\\{new}"),
                         (f"crops\\{old}", f"crops\\{new}"),
                         (f"crops/{old}", f"crops/{new}")):
                if a in s and a != b:
                    n += s.count(a)
                    s = s.replace(a, b)
        if n:
            tmp = html_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(s)
            os.replace(tmp, html_path)
    return changed, n


def busy_report_ids(db_path):
    """Отчёты, в которые прямо сейчас пишет воркер -- их не трогаем."""
    conn = sar_common.get_db_connection(db_path)
    rows = conn.execute(
        "SELECT report_id FROM reports WHERE status IN ('queued','processing')").fetchall()
    conn.close()
    return {r["report_id"] for r in rows}


def human(n):
    return "%.2f ГБ" % (n / 1e9) if n >= 1e8 else "%.1f МБ" % (n / 1e6)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--go", action="store_true", help="действительно удалять")
    p.add_argument("--cap", type=int, default=None,
                   help="оставить не больше N кропов на сцену (по умолчанию только сироты)")
    p.add_argument("--report-id", default=None, help="только один отчёт")
    p.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    args = p.parse_args()

    cfg, _ = sar_common.load_server_config(args.root)
    watch = cfg["watch_dir"]
    if not os.path.isabs(watch):
        watch = os.path.abspath(os.path.join(args.root, watch))
    _, _, db_path, reports_dir = sar_common.resolve_paths(watch, cfg.get("data_dir"))

    busy = busy_report_ids(db_path)
    total_drop = total_freed = total_remap = 0
    touched = 0

    for rid in sorted(os.listdir(reports_dir)):
        if args.report_id and rid != args.report_id:
            continue
        if rid in busy:
            print("  пропускаю (в работе): %s" % rid)
            continue
        out_dir = os.path.join(reports_dir, rid)
        if not os.path.isdir(out_dir):
            continue
        plan = plan_report(out_dir, cap=args.cap)
        if plan is None:
            continue
        drop, remap, freed = plan
        if not drop:
            continue
        touched += 1
        total_drop += len(drop)
        total_freed += freed
        total_remap += len(remap)
        print("  %-44s %6d файлов, %s" % (rid[:44], len(drop), human(freed)))
        if args.go:
            crops_dir = os.path.join(out_dir, "crops")
            # ССЫЛКИ ПЕРЕПИСЫВАЕМ ДО УДАЛЕНИЯ: если процесс прервётся между
            # этими шагами, отчёт останется рабочим (ссылается на файлы,
            # которые ещё на месте), а не битым.
            rewrite_refs(out_dir, remap)
            for fn in drop:
                try:
                    os.remove(os.path.join(crops_dir, fn))
                except OSError:
                    pass

    print()
    print("отчётов затронуто: %d" % touched)
    print("файлов:            %d" % total_drop)
    print("ссылок переписано: %d" % total_remap)
    print("освободится:       %s" % human(total_freed))
    if not args.go:
        print()
        print("Это предварительный просмотр. Для реальной чистки добавьте --go")
    return 0


if __name__ == "__main__":
    sys.exit(main())
