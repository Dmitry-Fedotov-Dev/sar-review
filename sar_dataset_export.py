#!/usr/bin/env python3
"""
sar_dataset_export.py — собирает подтверждённые человеком детекции
(таблица detection_priorities, см. плеер) в датасет формата YOLO для
дообучения детектора людей/снаряжения на видео с дрона в горной местности.

Что попадает в датасет (осознанно, обсуждалось с пользователем):
  - confirmed_person / confirmed_object -- позитивные примеры, класс person(0)/object(1)
  - rejected -- ХАРДОВЫЕ НЕГАТИВЫ: тот же полный кадр, но БЕЗ рамки, чтобы
    модель училась НЕ путать этот конкретный визуальный паттерн с целью
    (типичный источник ложных срабатываний -- камни/тени/цветовые аномалии,
    которые модель когда-то посчитала находкой, а человек отклонил)
  - likely_person / likely_object ("предположительно") и непроверенные
    (без приоритета вообще) -- НЕ экспортируются: датасет должен расти из
    того, что человек действительно проверил, а не из сырых догадок модели

2 класса, а не более мелкие (tent/backpack/...) -- ровно та детализация,
которую человек реально подтверждает в плеере; более узкая догадка модели
(object_class) сама никем не верифицирована.

Источники кадров:
  - AI-сцены -- уже сохранённые на диске полные кадры (full_image_path в
    detections.json), группировка идёт так же, как в /api/report/<id>/ai_scenes
    (см. sar_server.py) -- триаж привязан к стабильному ref_key
    ("класс:источник:первый_кадр"), не к позиционному group_id
  - ручные наблюдения -- кадр ИЗВЛЕКАЕТСЯ на лету из исходного видео по
    timestamp_sec (сами наблюдения кадр не сохраняют, только bbox+таймкод)

Повторный запуск ПЕРЕСОБИРАЕТ images/labels с нуля из текущего состояния
detection_priorities -- "постоянное обновление" датасета means повторные
запуски по мере роста триажа, а не потоковая запись в реальном времени.
Значит, если приоритет сняли/поменяли -- старый экспорт для этой картинки
не остаётся сиротой на диске.

Использование:
    python sar_dataset_export.py --out sar_dataset
    python sar_dataset_export.py --out sar_dataset --val-split 0.15
    python sar_dataset_export.py --out sar_dataset --report-id <id>
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common
from sar_video_review import Hit, group_hits_into_scenes, load_config

CLASS_NAMES = ["person", "object"]
PRIORITY_TO_CLASS = {"confirmed_person": 0, "confirmed_object": 1}
NEGATIVE_PRIORITIES = {"rejected"}
EXPORTED_PRIORITIES = set(PRIORITY_TO_CLASS) | NEGATIVE_PRIORITIES


def _read_detections_tolerant(path):
    for enc in ("utf-8", "cp1251"):
        try:
            with open(path, "r", encoding=enc) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"не удалось прочитать {path} ни как utf-8, ни как cp1251")


def _split_for_key(key, val_split):
    """Детерминированный train/val сплит по хэшу ключа -- одна и та же
    картинка ВСЕГДА попадает в один и тот же сплит между перезапусками
    экспорта. Без этого при каждом повторном экспорте картинки случайно
    перетасовывались бы между train/val, и метрики валидации между прогонами
    были бы несопоставимы (плюс риск, что вчерашняя val-картинка сегодня
    окажется в train — классическая утечка)."""
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16)
    return "val" if (h % 10000) / 10000.0 < val_split else "train"


def _yolo_box_line(class_id, x1, y1, x2, y2, img_w, img_h):
    cx = ((x1 + x2) / 2.0) / img_w
    cy = ((y1 + y2) / 2.0) / img_h
    bw = abs(x2 - x1) / img_w
    bh = abs(y2 - y1) / img_h
    return f"{class_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"


def _resolve_frame_size(hit, abs_path, _cache={}):
    """frame_width/frame_height есть на самом Hit для отчётов, обработанных
    текущей версией кода; для более старых -- один раз читаем разрешение из
    метаданных видео (не декодируя кадры), кэшируя по видео -- та же идея,
    что и _resolve_frame_dimensions в backfill_telemetry.py."""
    if hit.frame_width and hit.frame_height:
        return hit.frame_width, hit.frame_height
    if abs_path in _cache:
        return _cache[abs_path]
    w = h = None
    if abs_path and os.path.exists(abs_path):
        try:
            import cv2
            cap = cv2.VideoCapture(abs_path)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
            cap.release()
        except Exception:
            pass
    _cache[abs_path] = (w, h)
    return w, h


def _extract_video_frame(abs_path, timestamp_sec):
    """Достаёт один кадр видео по таймкоду -- используется только для ручных
    наблюдений (они не сохраняют кадр как файл, в отличие от AI-детекций).
    Возвращает numpy-массив BGR или None, если видео недоступно/кадр не
    читается (файл удалили/переместили с момента разметки -- не редкость)."""
    if not abs_path or not os.path.exists(abs_path):
        return None
    try:
        import cv2
        cap = cv2.VideoCapture(abs_path)
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp_sec) * 1000.0)
        ok, frame = cap.read()
        cap.release()
        return frame if ok else None
    except Exception:
        return None


def _write_labeled_image(images_dir, labels_dir, stem, frame_bgr_or_none, src_path_or_none, label_lines):
    """Пишет пару картинка+txt. Ровно один из frame_bgr_or_none/src_path_or_none
    должен быть задан -- либо уже готовый файл на диске (AI-детекции), либо
    сырой кадр в памяти (извлечённый из видео для ручных наблюдений)."""
    img_path = os.path.join(images_dir, stem + ".jpg")
    if src_path_or_none is not None:
        shutil.copyfile(src_path_or_none, img_path)
    else:
        import cv2
        cv2.imwrite(img_path, frame_bgr_or_none, [cv2.IMWRITE_JPEG_QUALITY, 92])
    with open(os.path.join(labels_dir, stem + ".txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(label_lines))
        if label_lines:
            f.write("\n")


def _reset_output_dirs(out_dir):
    for split in ("train", "val"):
        for sub in ("images", "labels"):
            d = os.path.join(out_dir, sub, split)
            if os.path.exists(d):
                shutil.rmtree(d)
            os.makedirs(d, exist_ok=True)


def _write_dataset_yaml(out_dir):
    lines = [
        f"path: {os.path.abspath(out_dir)}",
        "train: images/train",
        "val: images/val",
        "names:",
    ]
    lines += [f"  {i}: {name}" for i, name in enumerate(CLASS_NAMES)]
    with open(os.path.join(out_dir, "dataset.yaml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def export_ai_scenes(conn, report, priorities_by_ref_key, out_dir, val_split, counts, grouping_cfg=None):
    report_id = report["report_id"]
    det_path = os.path.join(report["out_dir"], "detections.json")
    if not os.path.exists(det_path):
        return
    raw_hits = _read_detections_tolerant(det_path)
    if not raw_hits:
        return
    hits = [Hit(**h) for h in raw_hits]
    grouping_cfg = grouping_cfg or {}
    # ТЕ ЖЕ параметры группировки, что и в _get_ai_scenes_for_report
    # (sar_server.py) -- иначе границы сцен, которые видел и триажил
    # человек в плеере, и то, что заново пересчитывает экспорт, разъедутся
    # при нестандартном grouping в sar_config.json
    groups = group_hits_into_scenes(
        hits, max_frame_gap=grouping_cfg.get("max_frame_gap", 360),
        distance_multiplier=grouping_cfg.get("distance_multiplier", 2.5))

    for grp in groups:
        g_hits = grp["hits"]
        first = g_hits[0]
        ref_key = sar_common.ai_scene_ref_key(first.object_class, first.source, first.frame_idx, first.bbox)
        priority = priorities_by_ref_key.get(ref_key)
        if priority not in EXPORTED_PRIORITIES:
            continue

        class_id = PRIORITY_TO_CLASS.get(priority)  # None для rejected -- нарочно, кадр без рамки
        for h in g_hits:
            if not h.full_image_path:
                continue
            src_path = os.path.join(report["out_dir"], h.full_image_path)
            if not os.path.exists(src_path):
                continue
            w, ht = _resolve_frame_size(h, report["abs_path"])
            label_lines = []
            if class_id is not None and w and ht:
                label_lines = [_yolo_box_line(class_id, *h.bbox, w, ht)]
            elif class_id is not None:
                # позитив, но не удалось узнать разрешение кадра -- лучше
                # пропустить картинку целиком, чем записать заведомо неверный
                # (ненормированный) бокс в обучающие данные
                continue

            key = f"{report_id}:ai:{ref_key}:{h.frame_idx}"
            split = _split_for_key(key, val_split)
            stem = f"{report_id}__ai__{ref_key.replace(':', '-')}__f{h.frame_idx}"
            _write_labeled_image(
                os.path.join(out_dir, "images", split), os.path.join(out_dir, "labels", split),
                stem, None, src_path, label_lines)
            counts["positive" if class_id is not None else "negative"] += 1
            counts[split] += 1


def export_manual_observations(conn, report, out_dir, val_split, counts):
    report_id = report["report_id"]
    placeholders = ",".join("?" for _ in EXPORTED_PRIORITIES)
    rows = conn.execute(
        "SELECT mo.id, mo.timestamp_sec, mo.bbox, dp.priority "
        "FROM manual_observations mo "
        "JOIN detection_priorities dp ON dp.report_id = mo.report_id "
        "  AND dp.kind = 'manual' AND dp.ref_key = CAST(mo.id AS TEXT) "
        f"WHERE mo.report_id = ? AND dp.priority IN ({placeholders})",
        (report_id, *EXPORTED_PRIORITIES)).fetchall()

    for row in rows:
        priority = row["priority"]
        class_id = PRIORITY_TO_CLASS.get(priority)  # None для rejected
        frame = _extract_video_frame(report["abs_path"], row["timestamp_sec"])
        if frame is None:
            continue
        label_lines = []
        if class_id is not None:
            x1, y1, x2, y2 = json.loads(row["bbox"])  # уже нормализовано 0..1
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            bw, bh = abs(x2 - x1), abs(y2 - y1)
            label_lines = [f"{class_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"]

        key = f"{report_id}:manual:{row['id']}"
        split = _split_for_key(key, val_split)
        stem = f"{report_id}__manual__{row['id']}"
        _write_labeled_image(
            os.path.join(out_dir, "images", split), os.path.join(out_dir, "labels", split),
            stem, frame, None, label_lines)
        counts["positive" if class_id is not None else "negative"] += 1
        counts[split] += 1


def run_export(db_path, out_dir, val_split, report_id_filter=None, grouping_cfg=None):
    conn = sar_common.get_db_connection(db_path)
    _reset_output_dirs(out_dir)

    query = "SELECT * FROM reports WHERE kind IN ('video', 'photo')"
    params = ()
    if report_id_filter:
        query += " AND report_id = ?"
        params = (report_id_filter,)
    reports = conn.execute(query, params).fetchall()

    counts = defaultdict(int)
    for report_row in reports:
        report = dict(report_row)
        report_id = report["report_id"]

        priority_rows = conn.execute(
            "SELECT ref_key, priority FROM detection_priorities "
            "WHERE report_id=? AND kind='ai_scene'", (report_id,)).fetchall()
        priorities_by_ref_key = {r["ref_key"]: r["priority"] for r in priority_rows}
        if priorities_by_ref_key:
            export_ai_scenes(conn, report, priorities_by_ref_key, out_dir, val_split, counts, grouping_cfg)

        if report["kind"] == "video":
            export_manual_observations(conn, report, out_dir, val_split, counts)

    conn.close()
    _write_dataset_yaml(out_dir)
    return counts


def main():
    p = argparse.ArgumentParser(
        description="Экспортировать подтверждённые детекции в датасет формата YOLO")
    p.add_argument("--out", default="sar_dataset", help="папка для датасета (по умолчанию ./sar_dataset)")
    p.add_argument("--val-split", type=float, default=0.15, help="доля картинок в val, по умолчанию 0.15")
    p.add_argument("--report-id", default=None, help="экспортировать только один отчёт")
    args = p.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    server_cfg, _ = sar_common.load_server_config(script_dir)
    watch_dir, data_dir, db_path, reports_dir = sar_common.resolve_paths(server_cfg["watch_dir"])
    sar_common.init_db(db_path)  # идемпотентно -- та же миграция, что при старте воркера/сервера

    config_path = os.path.join(script_dir, "sar_config.json")
    detection_cfg = load_config(config_path if os.path.exists(config_path) else None)
    grouping_cfg = detection_cfg.get("grouping", {})

    out_dir = args.out if os.path.isabs(args.out) else os.path.join(watch_dir, args.out)
    os.makedirs(out_dir, exist_ok=True)

    counts = run_export(db_path, out_dir, args.val_split, report_id_filter=args.report_id,
                         grouping_cfg=grouping_cfg)

    total = counts["train"] + counts["val"]
    print(f"Готово: {out_dir}")
    print(f"  всего картинок: {total} (train: {counts['train']}, val: {counts['val']})")
    print(f"  позитивных (person/object): {counts['positive']}")
    print(f"  хардовых негативов (rejected, без рамки): {counts['negative']}")
    if total == 0:
        print("  Пока пусто -- нет детекций со статусом confirmed_*/rejected. "
              "Проставьте приоритеты в плеере и запустите снова.")


if __name__ == "__main__":
    main()
