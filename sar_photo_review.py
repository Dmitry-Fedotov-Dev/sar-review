#!/usr/bin/env python3
"""
SAR Photo Review — прогон одиночного фото через тот же детектор, что и видео
(object detection по классам + цветовые аномалии). Переиспользует функции
из sar_video_review.py (тайлинг, NMS, группировку, генерацию report.html),
чтобы у фото и у видео была одна и та же UI-логика сцен.

Запуск:
    python sar_photo_review.py --photo DJI_photo_001.jpg \
        --model yolov8s-worldv2.pt --model-type yolo-world \
        --classes "person,tent,backpack" --conf 0.15 --out ./review_photo1
"""

import argparse
import os
import re
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sar_video_review import (
    load_model, detect_frame_tiled, detect_color_anomalies, Hit,
    group_hits_into_scenes, save_outputs, load_config,
)


def process_photo(photo_path, weights_path, model_type, target_classes, conf,
                   tile_size, overlap, out_dir, crop_margin=60, use_color_anomaly=True,
                   full_frame_quality=92, distance_multiplier=2.5,
                   tracking_report_id=None, tracking_api_base=None):
    os.makedirs(out_dir, exist_ok=True)
    crops_dir = os.path.join(out_dir, "crops")
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(crops_dir, exist_ok=True)
    os.makedirs(frames_dir, exist_ok=True)

    load_model(weights_path, model_type, target_classes)

    frame = cv2.imread(photo_path)
    if frame is None:
        print(f"Не удалось открыть фото: {photo_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[{photo_path}] анализирую снимок...")

    dets = [(x1, y1, x2, y2, s, cls, "model") for (x1, y1, x2, y2, s, cls)
            in detect_frame_tiled(frame, conf, tile_size, overlap, target_classes)]
    if use_color_anomaly:
        dets += [(x1, y1, x2, y2, s, cls, "color") for (x1, y1, x2, y2, s, cls)
                 in detect_color_anomalies(frame)]

    print(f"...100% (найдено {len(dets)} кандидатов)")

    full_rel_path = None
    if dets:
        # чистый кадр без выжженных рамок — рамки рисуются поверх в браузере
        full_img_path = os.path.join(frames_dir, "photo_full.jpg")
        cv2.imwrite(full_img_path, frame, [cv2.IMWRITE_JPEG_QUALITY, full_frame_quality])
        full_rel_path = os.path.relpath(full_img_path, out_dir)

    h, w = frame.shape[:2]
    hits = []
    for i, (x1, y1, x2, y2, score, cls_name, source) in enumerate(dets):
        cx1 = max(0, int(x1) - crop_margin)
        cy1 = max(0, int(y1) - crop_margin)
        cx2 = min(w, int(x2) + crop_margin)
        cy2 = min(h, int(y2) + crop_margin)
        crop = frame[cy1:cy2, cx1:cx2].copy()
        box_color = (0, 0, 255) if source == "model" else (0, 200, 255)
        cv2.rectangle(crop, (int(x1) - cx1, int(y1) - cy1), (int(x2) - cx1, int(y2) - cy1), box_color, 2)

        safe_cls = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", cls_name)[:30]
        img_path = os.path.join(crops_dir, f"det{i:04d}_{safe_cls}_c{score:.2f}.jpg")
        cv2.imwrite(img_path, crop, [cv2.IMWRITE_JPEG_QUALITY, 90])

        hits.append(Hit(
            frame_idx=0, timestamp="фото", seconds=0.0, confidence=round(score, 3),
            object_class=cls_name, source=source,
            bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            lat=None, lon=None, alt=None,
            image_path=os.path.relpath(img_path, out_dir),
            full_image_path=full_rel_path,
        ))

    hits.sort(key=lambda h: h.confidence, reverse=True)
    # все детекции на одном "кадре" (frame_idx=0) — группировка по близости
    # всё равно разделит разные объекты на разные сцены, gap здесь неактуален
    groups = group_hits_into_scenes(hits, max_frame_gap=1, distance_multiplier=distance_multiplier)

    print(f"Сгруппировано в {len(groups)} сцен(ы) из {len(hits)} детекций.")
    save_outputs(hits, groups, out_dir, photo_path,
                 tracking_report_id=tracking_report_id, tracking_api_base=tracking_api_base)
    print(f"Готово. Отчёт: {os.path.join(out_dir, 'report.html')}")
    return hits


def main():
    p = argparse.ArgumentParser(description="SAR: анализ одиночного фото тем же детектором")
    p.add_argument("--photo", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--config", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--model-type", choices=["custom", "yolo-world"], default=None)
    p.add_argument("--classes", default=None)
    p.add_argument("--conf", type=float, default=None)
    p.add_argument("--tile", type=int, default=None)
    p.add_argument("--overlap", type=float, default=None)
    p.add_argument("--no-color-anomaly", action="store_true")
    p.add_argument("--crop-margin", type=int, default=None)
    p.add_argument("--full-frame-quality", type=int, default=None)
    p.add_argument("--distance-multiplier", type=float, default=None)
    p.add_argument("--tracking-report-id", default=None)
    p.add_argument("--tracking-api-base", default=None)
    args = p.parse_args()

    config_path = args.config
    if config_path is None:
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sar_config.json")
        if os.path.exists(candidate):
            config_path = candidate
    cfg = load_config(config_path)

    def pick(cli_val, cfg_key):
        return cli_val if cli_val is not None else cfg[cfg_key]

    model = pick(args.model, "model")
    if not model:
        p.error("--model не задан ни в аргументах, ни в конфиге")

    process_photo(
        photo_path=args.photo,
        weights_path=model,
        model_type=pick(args.model_type, "model_type"),
        target_classes=[c.strip() for c in pick(args.classes, "classes").split(",") if c.strip()],
        conf=pick(args.conf, "conf"),
        tile_size=pick(args.tile, "tile"),
        overlap=pick(args.overlap, "overlap"),
        out_dir=args.out,
        crop_margin=pick(args.crop_margin, "crop_margin"),
        use_color_anomaly=cfg["color_anomaly"] and not args.no_color_anomaly,
        full_frame_quality=pick(args.full_frame_quality, "full_frame_quality"),
        distance_multiplier=(args.distance_multiplier if args.distance_multiplier is not None
                              else cfg["grouping"]["distance_multiplier"]),
        tracking_report_id=args.tracking_report_id,
        tracking_api_base=args.tracking_api_base,
    )


if __name__ == "__main__":
    main()
