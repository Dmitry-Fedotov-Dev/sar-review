#!/usr/bin/env python3
"""
SAR Batch — очередь обработки для sar_video_review.py.

Что делает:
  - находит все видео в указанной папке (--input-dir)
  - обрабатывает их по очереди (или параллельно, --workers N)
  - пропускает уже готовые (если в выходной папке видео уже есть report.html
    -> считает готовым и не трогает повторно; удобно для resume после сбоя)
  - пишет общий лог batch_log.csv: файл, статус, время начала/конца, ошибка (если была)

ВАЖНО про --workers (распараллеливание):
  - Если инференс идёт на CPU (нет NVIDIA-карты/CUDA):
      --workers = (число физических ядер - 1). Смотрите в диспетчере
      задач -> Производительность -> ЦП -> "Ядра". Логические потоки
      (Threads) считать не нужно, torch внутри и так многопоточный.
  - Если есть GPU (NVIDIA, CUDA):
      начинайте с --workers 1. Пробуйте --workers 2 только если видеопамяти
      достаточно (смотрите `nvidia-smi` во время работы — если VRAM почти
      забита при одном воркере, больше одного не имеет смысла, карта одна).
  - Если не уверены — оставьте --workers 1 (это просто очередь без
    параллелизма, но зато без риска, что видеокарта/память захлебнётся).

Запуск (очередь, без параллелизма):
    python sar_batch.py --input-dir ./videos --model yolov8s-worldv2.pt \
        --model-type yolo-world --classes "person,tent" --conf 0.15 \
        --out-root ./review_all --workers 1

Запуск (2 видео параллельно, если позволяет железо):
    python sar_batch.py --input-dir ./videos --model yolov8s-worldv2.pt \
        --model-type yolo-world --classes "person" --out-root ./review_all --workers 2

Повторный запуск той же команды после сбоя/остановки просто продолжит
с того места, где остановились — уже готовые видео обрабатываться не будут.
"""

import argparse
import csv
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

# sar_video_review.py должен лежать в той же папке
from sar_video_review import process_video
import sar_common

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".ts", ".m4v", ".wmv"}


def find_videos(input_dir):
    videos = []
    for root, _, files in os.walk(input_dir):
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                videos.append(os.path.join(root, f))
    return sorted(videos)


def is_already_done(out_dir):
    return os.path.exists(os.path.join(out_dir, "report.html"))


def find_srt(video_path):
    candidate = os.path.splitext(video_path)[0] + ".srt"
    return candidate if os.path.exists(candidate) else None


def process_one(video_path, out_dir, args_dict):
    """Обёртка для одного видео — выполняется в отдельном процессе при --workers > 1."""
    t0 = time.time()
    try:
        srt_path = find_srt(video_path)  # старое поведение: то же имя рядом с видео, приоритет
        if srt_path is None and args_dict.get("telemetry_index"):
            # резервный источник -- индекс telemetry/, построенный ОДИН РАЗ в main()
            # до начала очереди (см. там же), а не сканирование диска на каждое видео
            match, reason = sar_common.find_telemetry_for_video(video_path, args_dict["telemetry_index"])
            if match:
                srt_path = str(match)
        process_video(
            video_path=video_path,
            weights_path=args_dict["model"],
            model_type=args_dict["model_type"],
            target_classes=args_dict["target_classes"],
            conf=args_dict["conf"],
            tile_size=args_dict["tile"],
            overlap=args_dict["overlap"],
            frame_skip=args_dict["frame_skip"],
            out_dir=out_dir,
            srt_path=srt_path,
            use_color_anomaly=args_dict["use_color_anomaly"],
        )
        return (video_path, "OK", time.time() - t0, "")
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        return (video_path, "ERROR", time.time() - t0, err)


def main():
    p = argparse.ArgumentParser(description="SAR: очередь обработки папки с видео")
    p.add_argument("--input-dir", required=True, help="папка с видео (ищет рекурсивно во всех подпапках)")
    p.add_argument("--out-root", required=True, help="папка, куда складывать результаты (по подпапке на видео)")
    p.add_argument("--model", required=True)
    p.add_argument("--model-type", choices=["custom", "yolo-world"], default="custom")
    p.add_argument("--classes", default="person")
    p.add_argument("--conf", type=float, default=0.15)
    p.add_argument("--tile", type=int, default=640)
    p.add_argument("--overlap", type=float, default=0.25)
    p.add_argument("--frame-skip", type=int, default=2)
    p.add_argument("--no-color-anomaly", action="store_true")
    p.add_argument("--workers", type=int, default=1,
                    help="сколько видео обрабатывать одновременно (см. рекомендации в шапке файла)")
    p.add_argument("--force", action="store_true",
                    help="пересчитать даже уже готовые видео (по умолчанию они пропускаются)")
    p.add_argument("--telemetry-dir", default=None,
                    help="папка с SRT-телеметрией (рекурсивно), резервный источник, если рядом с "
                         "видео нет videoname.srt (по умолчанию 'telemetry' рядом с --input-dir)")
    args = p.parse_args()

    target_classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    # индекс telemetry/ строится ОДИН РАЗ здесь, до очереди -- не на каждое
    # видео -- и передаётся дальше через args_dict (обычный словарь, спокойно
    # переживает pickle при --workers>1 через ProcessPoolExecutor)
    telemetry_dir_name = args.telemetry_dir or sar_common.DEFAULT_TELEMETRY_DIR_NAME
    telemetry_dir = sar_common.resolve_telemetry_dir(os.path.abspath(args.input_dir), telemetry_dir_name)
    telemetry_index = sar_common.build_telemetry_index(telemetry_dir)
    print(f"Телеметрия: проиндексировано {len(telemetry_index['by_stem'])} SRT-файл(ов) в {telemetry_dir}")

    args_dict = {
        "model": args.model,
        "model_type": args.model_type,
        "target_classes": target_classes,
        "conf": args.conf,
        "tile": args.tile,
        "overlap": args.overlap,
        "frame_skip": args.frame_skip,
        "use_color_anomaly": not args.no_color_anomaly,
        "telemetry_index": telemetry_index,
    }

    videos = find_videos(args.input_dir)
    if not videos:
        print(f"В папке {args.input_dir} видео не найдено (расширения: {sorted(VIDEO_EXTS)})")
        sys.exit(1)

    os.makedirs(args.out_root, exist_ok=True)
    log_path = os.path.join(args.out_root, "batch_log.csv")

    jobs = []
    skipped = 0
    for video_path in videos:
        stem = os.path.splitext(os.path.basename(video_path))[0]
        out_dir = os.path.join(args.out_root, stem)
        if not args.force and is_already_done(out_dir):
            skipped += 1
            continue
        jobs.append((video_path, out_dir))

    print(f"Всего видео найдено: {len(videos)}")
    print(f"Уже обработано ранее (пропускаем): {skipped}")
    print(f"В очереди на обработку: {len(jobs)}")
    if not jobs:
        print("Обрабатывать нечего — все видео уже готовы. Используйте --force, чтобы пересчитать заново.")
        return

    log_rows = []
    log_exists = os.path.exists(log_path)
    log_file = open(log_path, "a", newline="", encoding="utf-8")
    log_writer = csv.writer(log_file)
    if not log_exists:
        log_writer.writerow(["video", "status", "seconds", "error"])

    done_count = 0
    t_start = time.time()

    if args.workers <= 1:
        # простая последовательная очередь — самый безопасный режим
        for video_path, out_dir in jobs:
            print(f"\n[{done_count + 1}/{len(jobs)}] Обрабатываю: {video_path}")
            video_path, status, secs, err = process_one(video_path, out_dir, args_dict)
            log_writer.writerow([video_path, status, round(secs, 1), err.replace("\n", " | ")])
            log_file.flush()
            done_count += 1
            if status == "OK":
                print(f"  готово за {secs:.0f} сек")
            else:
                print(f"  ОШИБКА: {err.splitlines()[0]}")
    else:
        print(f"\nЗапускаю {args.workers} параллельных воркеров...")
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(process_one, video_path, out_dir, args_dict): video_path
                for video_path, out_dir in jobs
            }
            for future in as_completed(futures):
                video_path, status, secs, err = future.result()
                log_writer.writerow([video_path, status, round(secs, 1), err.replace("\n", " | ")])
                log_file.flush()
                done_count += 1
                print(f"[{done_count}/{len(jobs)}] {os.path.basename(video_path)}: "
                      f"{status} ({secs:.0f} сек)" + (f" — {err.splitlines()[0]}" if err else ""))

    log_file.close()
    total_time = time.time() - t_start
    print(f"\nГотово. Обработано: {done_count}/{len(jobs)} за {total_time/60:.1f} мин.")
    print(f"Лог: {log_path}")
    print(f"Результаты по каждому видео: {args.out_root}\\<имя_видео>\\report.html")


if __name__ == "__main__":
    main()
