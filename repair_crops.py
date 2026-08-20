"""Восстановление кропов с испорченными именами.

Что произошло. cv2.imwrite на Windows записывает файл, но имя с не-ASCII
символами уродует: байты UTF-8 попадают в файловую систему как CP1251.
Имя кропа собирается из названия класса, а цветовые аномалии называются
по-русски -- поэтому на диске лежит «f0000180_С†РІРµС‚_...jpg», а
detections.json помнит правильное «f0000180_цвет_...jpg». Ссылка указывает
в пустоту при формально успешной записи, и заметить это можно было только
глазами: у кропов модели («person», латиница) имена не портились, поэтому
часть карточек отчёта работала, а часть показывала битую картинку.

Самое важное: КАРТИНКИ ЦЕЛЫ. Испорчены только имена, поэтому чинится всё
переименованием -- пересчитывать и перечитывать видео не нужно.

Обратное преобразование однозначно: имя с диска кодируем обратно в CP1251
и читаем как UTF-8. Переименовываем ТОЛЬКО если результат действительно
упоминается в detections.json -- иначе это не наш случай и трогать файл
нельзя.

По умолчанию только показывает, что сделает.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common


def unmangle(name):
    """Имя, каким оно должно было быть. None, если это не искажение."""
    try:
        fixed = name.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return fixed if fixed != name else None


def plan_report(out_dir):
    """([(было, стало)], сколько ссылок битых) для одного отчёта."""
    crops = os.path.join(out_dir, "crops")
    dj = os.path.join(out_dir, "detections.json")
    if not (os.path.isdir(crops) and os.path.exists(dj)):
        return [], 0
    try:
        with open(dj, encoding="utf-8") as f:
            dets = json.load(f)
    except Exception:
        return [], 0

    referenced = {os.path.basename((d["image_path"] or "").replace("\\", "/"))
                  for d in dets if d.get("image_path")}
    on_disk = set(os.listdir(crops))
    missing = referenced - on_disk

    pairs = []
    for name in on_disk - referenced:
        fixed = unmangle(name)
        # чиним, только если восстановленное имя ДЕЙСТВИТЕЛЬНО ждут:
        # иначе это посторонний файл, и переименование только навредит
        if fixed and fixed in missing:
            pairs.append((name, fixed))
    return pairs, len(missing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--go", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    cfg, _ = sar_common.load_server_config(root)
    watch = cfg["watch_dir"]
    if not os.path.isabs(watch):
        watch = os.path.abspath(os.path.join(root, watch))
    _, _, db_path, _ = sar_common.resolve_paths(watch)

    conn = sar_common.get_db_connection(db_path)
    rows = conn.execute(
        "SELECT report_id, rel_path, out_dir FROM reports "
        "WHERE out_dir IS NOT NULL").fetchall()
    conn.close()

    plans, total_fix, total_missing = [], 0, 0
    for r in rows:
        if not (r["out_dir"] and os.path.isdir(r["out_dir"])):
            continue
        pairs, missing = plan_report(r["out_dir"])
        if not pairs and not missing:
            continue
        plans.append((r, pairs, missing))
        total_fix += len(pairs)
        total_missing += missing

    plans.sort(key=lambda x: -len(x[1]))
    print(f"отчётов с битыми ссылками на кропы: {len(plans)}")
    print(f"битых ссылок всего                : {total_missing}")
    print(f"чинится переименованием           : {total_fix}")
    if total_missing > total_fix:
        print(f"останется битыми                  : {total_missing - total_fix}")
    print()
    for r, pairs, missing in plans[:10]:
        name = os.path.basename(r["rel_path"])
        print(f"   {name[:44]:<46} чинится {len(pairs):>5} из {missing}")
    if len(plans) > 10:
        print(f"   ... и ещё {len(plans)-10} отчёт(ов)")

    if not args.go:
        print("\nПОКАЗ. Для восстановления: --go")
        return 0

    print()
    done = failed = 0
    for r, pairs, _ in plans:
        crops = os.path.join(r["out_dir"], "crops")
        for old, new in pairs:
            try:
                os.replace(os.path.join(crops, old), os.path.join(crops, new))
                done += 1
            except OSError as e:
                failed += 1
                if failed <= 3:
                    print(f"   ! {old}: {e}")
        if pairs:
            print(f"   {os.path.basename(r['rel_path'])[:44]:<46} +{len(pairs)}")

    print(f"\nпереименовано: {done}, ошибок: {failed}")

    left = sum(plan_report(r["out_dir"])[1] for r, _, _ in plans)
    print(f"осталось битых ссылок: {left}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
