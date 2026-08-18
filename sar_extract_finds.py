"""Нарезка обучающих кадров по реестру известных находок.

Зачем. В проекте до сих пор не было НИ ОДНОГО достоверно положительного
примера: триаж-статусы и ручные пометки -- это "человек счёл подозрительным",
а не "здесь точно лежит вещь группы". По разбору другого волонтёра известны
таймкоды пяти ОПОЗНАННЫХ предметов. Кадры на этих таймкодах, вырезанные из
оригиналов, -- первый настоящий позитивный материал для дообучения и для
проверки детектора.

Что делает: по finds_manifest.json находит исходники, вырезает окно вокруг
каждого таймкода и раскладывает кадры по папкам с метаданными.

Про происхождение данных. Из разбора берутся только ФАКТЫ -- имя файла,
момент времени, координата. Изображения оттуда не копируются: кадры режутся
из ваших исходников, поэтому набор чист по правам. Автор разбора пишется в
метаданные каждого кадра и в README выгрузки -- это его работа, и она должна
быть указана.

Боксы НЕ размечаются. Скрипт не знает, где именно в кадре лежит предмет, и
выдумывать рамку было бы хуже, чем её отсутствие: неверная рамка отравит
обучающую выборку. Кадры готовятся к разметке человеком.

Запуск:
    python sar_extract_finds.py --videos D:\\originals          # что есть, что скачать
    python sar_extract_finds.py --videos D:\\originals --go     # нарезать
    python sar_extract_finds.py --videos D:\\originals --go --status confirmed
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".m4v")
PHOTO_EXT = (".jpg", ".jpeg", ".png")


def parse_tc(tc):
    """'3:26' | '1:25.3' | '95' | '1:02:03' -> секунды."""
    if tc is None:
        return None
    s = str(tc).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"не разобрать таймкод: {tc!r}")
    sec = 0.0
    for v in vals:                      # ч:м:с, м:с или просто с
        sec = sec * 60.0 + v
    return sec


def slug(s, n=40):
    s = re.sub(r"[^\w\d\u0400-\u04FF]+", "_", s, flags=re.UNICODE).strip("_")
    return s[:n] or "find"


def imread_any(path):
    """cv2.imread не открывает пути с кириллицей на Windows -- читаем байтами."""
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite_any(path, img, quality=95):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return False
    buf.tofile(path)                    # тот же обход, что и при чтении
    return True


# Служебные папки, куда лезть незачем: sar_data -- это выгрузка отчётов, там
# лежат СОТНИ ТЫСЯЧ кропов и полных кадров. Обход её замедляет поиск на
# порядки, а главное -- вырезанный кроп не должен иметь ни единого шанса
# подменить собой оригинал, из которого он сделан.
SKIP_DIRS = {"sar_data", "sar_dataset", ".git", ".claude", "__pycache__",
             ".pytest_cache", "node_modules"}


def index_sources(dirs, skip=SKIP_DIRS):
    """{нижний_регистр_имени_без_расширения: полный путь} по всем папкам."""
    idx = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [s for s in subdirs if s not in skip]
            for f in files:
                stem, ext = os.path.splitext(f)
                if ext.lower() in VIDEO_EXT + PHOTO_EXT:
                    idx.setdefault(stem.lower(), os.path.join(root, f))
    return idx


def resolve(find, idx):
    stem = os.path.splitext(find["file"])[0].lower()
    return idx.get(stem)


def extract_video(src, find, out_dir, fps, pad, quality):
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        return 0, "не открылось (битый файл или нет кодека)"
    vfps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / vfps if vfps else 0

    t0 = max(0.0, parse_tc(find.get("start")) - pad)
    t1 = parse_tc(find.get("end"))
    t1 = (t1 if t1 is not None else parse_tc(find.get("start"))) + pad
    if dur and t0 >= dur:
        cap.release()
        return 0, f"таймкод {t0:.1f}с за пределами видео ({dur:.1f}с)"
    t1 = min(t1, dur) if dur else t1

    step = 1.0 / max(fps, 0.01)
    n = 0
    t = t0
    while t <= t1 + 1e-6:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok:
            break
        name = f"{os.path.splitext(os.path.basename(src))[0]}__t{t:07.2f}.jpg"
        imwrite_any(os.path.join(out_dir, name), frame, quality)
        n += 1
        t += step
    cap.release()
    return n, None


def main():
    ap = argparse.ArgumentParser(
        description="Нарезка кадров с известными находками из оригиналов")
    ap.add_argument("--videos", nargs="+", required=True,
                    help="папки с оригиналами (можно несколько)")
    ap.add_argument("--manifest", default=os.path.join(HERE, "finds_manifest.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "sar_dataset", "finds"))
    ap.add_argument("--fps", type=float, default=2.0,
                    help="кадров в секунду внутри окна (по умолчанию 2)")
    ap.add_argument("--pad", type=float, default=1.0,
                    help="запас в секундах с каждой стороны (таймкоды неточные)")
    ap.add_argument("--quality", type=int, default=95)
    ap.add_argument("--status", choices=["confirmed", "candidate", "all"],
                    default="all")
    ap.add_argument("--go", action="store_true", help="без него -- только показать")
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8") as f:
        man = json.load(f)
    finds = [x for x in man["finds"]
             if args.status == "all" or x.get("status") == args.status]

    idx = index_sources(args.videos)
    print(f"исходников найдено в {len(args.videos)} папке(ах): {len(idx)}")
    print(f"находок в реестре: {len(finds)}"
          + (f" (фильтр: {args.status})" if args.status != "all" else ""))
    print()

    have, missing = [], []
    for fd in finds:
        (have if resolve(fd, idx) else missing).append(fd)

    print(f"{'статус':<11}{'файл':<32}{'таймкод':<14}{'предмет'}")
    print("-" * 84)
    for fd in finds:
        ok = resolve(fd, idx) is not None
        tc = "фото" if fd.get("photo") else f"{fd.get('start')}–{fd.get('end')}"
        mark = "ЕСТЬ  " if ok else "СКАЧАТЬ"
        print(f"{mark:<11}{fd['file'][:30]:<32}{tc:<14}{fd['item']}")

    if missing:
        uniq = sorted({m["file"] for m in missing})
        print(f"\nНЕ ХВАТАЕТ ФАЙЛОВ: {len(uniq)}")
        for u in uniq:
            items = [m["item"] for m in missing if m["file"] == u]
            print(f"   {u:<34} {', '.join(items)}")
        conf = sorted({m["file"] for m in missing if m.get("status") == "confirmed"})
        if conf:
            print(f"\n   из них с ПОДТВЕРЖДЁННЫМИ вещами: {len(conf)} -- качать в первую очередь")

    if not args.go:
        print("\nПОКАЗ. Для нарезки добавьте --go")
        return 0
    if not have:
        print("\nнечего нарезать: ни одного исходника не найдено")
        return 1

    os.makedirs(args.out, exist_ok=True)
    print(f"\nнарезаю в {args.out} ({args.fps} кадр/с, запас {args.pad} с)\n")

    total, report = 0, []
    for fd in have:
        src = resolve(fd, idx)
        sub = os.path.join(args.out, f"{fd.get('status','?')}__{slug(fd['item'])}")
        os.makedirs(sub, exist_ok=True)
        if fd.get("photo"):
            img = imread_any(src)
            if img is None:
                print(f"  ! {fd['item']}: не прочиталось")
                continue
            imwrite_any(os.path.join(sub, os.path.basename(src)), img, args.quality)
            n, err = 1, None
        else:
            n, err = extract_video(src, fd, sub, args.fps, args.pad, args.quality)
        if err:
            print(f"  ! {fd['item']} ({fd['file']}): {err}")
        else:
            print(f"  + {fd['item']:<40} {n:3} кадр(ов)")
        total += n
        report.append({**fd, "source_path": src, "frames": n, "out_dir": sub,
                       "error": err})

    meta = {
        "создано": datetime.now().isoformat(timespec="seconds"),
        "источник_таймкодов": man.get("source"),
        "важно": ("Кадры вырезаны из собственных исходников. Изображения из "
                   "стороннего разбора не использовались -- оттуда взяты только "
                   "факты (файл, таймкод, координата)."),
        "боксы": "НЕ размечены -- предмет в кадре ищет и обводит человек",
        "параметры": {"fps": args.fps, "pad_sec": args.pad, "quality": args.quality},
        "находки": report,
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    readme = os.path.join(args.out, "README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "Кадры с известными находками (операция на Курумды)\n"
            "==================================================\n\n"
            f"Всего кадров: {total}\n\n"
            "Таймкоды и координаты сведены с публичного разбора другого\n"
            f"волонтёра: {man.get('source')}\n"
            "Сами изображения оттуда НЕ брались -- кадры вырезаны из наших\n"
            "оригиналов. При публикации набора авторство разбора указывать.\n\n"
            "Рамки не размечены: скрипт не знает, где именно в кадре лежит\n"
            "предмет, а неверная рамка испортила бы обучающую выборку\n"
            "сильнее, чем её отсутствие. Обводить человеку.\n\n"
            "Папки confirmed__* -- опознанные вещи группы, самый ценный\n"
            "материал. candidate__* -- неподтверждённые кандидаты.\n")

    print(f"\nготово: {total} кадров")
    print(f"метаданные: {os.path.join(args.out, 'manifest.json')}")
    print(f"пояснение:  {readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
