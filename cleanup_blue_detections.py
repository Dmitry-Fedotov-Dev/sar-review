#!/usr/bin/env python3
"""
cleanup_blue_detections.py — разовый скрипт: убирает из УЖЕ обработанных
отчётов детекции цветового детектора по синему/голубому и перегенерирует
detections.json/csv/report.html, БЕЗ повторного запуска модели.

Зачем: на горном материале синий -- это небо и тени в снегу, а не находка.
На одном реальном видео он дал 503 499 срабатываний против 12 611 у модели
(~167 ложных пятен на кадр), из-за чего report.html вырастал до 296 МБ и
страница сцен переставала открываться. Из детектора синий убран
(см. COLOR_RANGES в sar_video_review.py), но в уже посчитанных отчётах эти
детекции остались -- этот скрипт их вычищает.

Заодно применяются новые потолки на размер отчёта (см. _limit_scene_hits и
MAX_BOXES_PER_FRAME), поэтому HTML ужимается даже там, где синего не было.

Что НЕ делается: файлы кропов/кадров на диске не удаляются -- они дешевле
места, чем риск удалить нужное; при необходимости чистятся отдельно.

Использование:
    python cleanup_blue_detections.py --dry-run    # только показать
    python cleanup_blue_detections.py              # применить ко всем
    python cleanup_blue_detections.py --report-id <id>
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common
from sar_video_review import Hit, load_config, save_outputs

DROP_MARKERS = ("синий", "голубой")


def _is_dropped(hit_dict):
    if hit_dict.get("source") != "color":
        return False
    label = (hit_dict.get("object_class") or "").lower()
    return any(m in label for m in DROP_MARKERS)


def _read_tolerant(path):
    for enc in ("utf-8", "cp1251"):
        try:
            with open(path, "r", encoding=enc) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"не прочитать {path}")


def clean_report(report, tracking_api_base, dry_run=False):
    report_id = report["report_id"]
    out_dir = report["out_dir"]
    det_path = os.path.join(out_dir, "detections.json")
    if not os.path.exists(det_path):
        return None

    raw = _read_tolerant(det_path)
    if not raw:
        return None

    kept = [h for h in raw if not _is_dropped(h)]
    dropped = len(raw) - len(kept)
    html_before = os.path.getsize(os.path.join(out_dir, "report.html")) \
        if os.path.exists(os.path.join(out_dir, "report.html")) else 0

    if dry_run:
        return {"report_id": report_id, "was": len(raw), "now": len(kept),
                "dropped": dropped, "html_before": html_before, "html_after": None}

    if not kept:
        print(f"[{report_id}] после чистки не осталось детекций -- пропускаю, "
              f"чтобы не затирать отчёт пустым")
        return {"report_id": report_id, "was": len(raw), "now": 0,
                "dropped": dropped, "html_before": html_before, "html_after": html_before}

    hits = [Hit(**h) for h in kept]
    # группы -- из уже проставленных group_id, заново не пересчитываем
    by_gid = defaultdict(list)
    for h in hits:
        by_gid[h.group_id].append(h)
    groups = []
    for gid, g_hits in by_gid.items():
        g_hits.sort(key=lambda x: x.frame_idx)
        groups.append({"id": gid, "hits": g_hits})

    save_outputs(hits, groups, out_dir, report["abs_path"],
                 tracking_report_id=report_id, tracking_api_base=tracking_api_base)
    html_after = os.path.getsize(os.path.join(out_dir, "report.html"))
    return {"report_id": report_id, "was": len(raw), "now": len(kept),
            "dropped": dropped, "html_before": html_before, "html_after": html_after}


def main():
    p = argparse.ArgumentParser(description="Убрать синие цветовые детекции из готовых отчётов")
    p.add_argument("--report-id", default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(script_dir, "sar_config.json")
    load_config(cfg_path if os.path.exists(cfg_path) else None)
    server_cfg, _ = sar_common.load_server_config(script_dir)
    watch_dir, data_dir, db_path, reports_dir = sar_common.resolve_paths(server_cfg["watch_dir"])
    sar_common.init_db(db_path)
    api_base = f"http://127.0.0.1:{server_cfg['port']}"

    conn = sar_common.get_db_connection(db_path)
    q = "SELECT * FROM reports WHERE status='done'"
    rows = conn.execute(q + " AND report_id=?", (args.report_id,)).fetchall() \
        if args.report_id else conn.execute(q).fetchall()
    conn.close()

    total_dropped = 0
    mb_before = mb_after = 0.0
    for row in rows:
        try:
            res = clean_report(dict(row), api_base, dry_run=args.dry_run)
        except Exception as e:
            print(f"[{row['report_id']}] ОШИБКА: {e}")
            continue
        if not res or not res["dropped"] and not args.dry_run and res["html_after"] == res["html_before"]:
            continue
        if res:
            total_dropped += res["dropped"]
            mb_before += res["html_before"] / 1024 / 1024
            if res["html_after"] is not None:
                mb_after += res["html_after"] / 1024 / 1024
            after = "—" if res["html_after"] is None else f"{res['html_after']/1024/1024:.1f} МБ"
            print(f"[{res['report_id'][:38]:38s}] детекций {res['was']:7d} -> {res['now']:6d} "
                  f"(убрано {res['dropped']:6d}) · html {res['html_before']/1024/1024:6.1f} МБ -> {after}")

    tail = " (dry-run, ничего не менялось)" if args.dry_run else ""
    print(f"\nИтого убрано детекций: {total_dropped}{tail}")
    if not args.dry_run:
        print(f"Суммарный размер report.html: {mb_before:.1f} МБ -> {mb_after:.1f} МБ")


if __name__ == "__main__":
    main()
