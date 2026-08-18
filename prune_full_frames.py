"""Задним числом сжимает готовые отчёты до режима representative.

Зачем. Опция full_frame_save_mode="representative" появилась в проекте, но в
боевом конфиге осталась "all" (в коде это дефолт -- намеренно, чтобы
обновление не меняло поведение молча). В результате каждый отчёт хранит ВСЕ
полные кадры: 16.9 ГБ против ~1 ГБ, диск забился, а операция продолжается.

Что делает: ровно то же, что делает детектор в режиме representative, только
для уже посчитанных отчётов. На каждую сцену остаются три кадра -- первый,
пиковый по уверенности и последний, -- остальные детекции сцены
перепривязываются на ближайший оставшийся кадр, лишние файлы удаляются.

Почему это безопасно для отчётов. report.html -- самодостаточный файл со
ссылками на frames/ и crops/ прямо в разметке, поэтому переписывается вместе
с detections.json: каждая ссылка на удалённый кадр заменяется на ближайший
оставшийся. Ни одна ссылка не остаётся битой -- это проверяется тестом.

Что НЕ трогается: кропы (crops/) -- по ним построена вся сетка карточек, и
весят они вчетверо меньше; отчёты в работе (queued/processing) -- в них
пишет воркер, лезть туда нельзя.

По умолчанию только показывает. Реальная чистка -- с --go.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common


def reps_for_group(hits):
    """Первый / пиковый по уверенности / последний -- как в детекторе."""
    ordered = sorted(hits, key=lambda h: h.get("frame_idx", 0))
    peak = max(ordered, key=lambda h: h.get("confidence") or 0.0)
    chosen = [ordered[0], peak, ordered[-1]]
    out = []
    for h in chosen:
        p = h.get("full_image_path")
        if p and (h.get("frame_idx"), p) not in out:
            out.append((h.get("frame_idx"), p))
    return sorted(set(out))


def plan_report(out_dir):
    """Считает, что оставить и что удалить. Ничего не меняет.

    Возвращает (remap, keep, drop, освобождается_байт) либо None, если
    отчёт трогать не надо.
    """
    dj = os.path.join(out_dir, "detections.json")
    frames_dir = os.path.join(out_dir, "frames")
    if not (os.path.exists(dj) and os.path.isdir(frames_dir)):
        return None
    try:
        with open(dj, encoding="utf-8") as f:
            dets = json.load(f)
    except Exception:
        return None
    if not isinstance(dets, list) or not dets:
        return None

    groups = defaultdict(list)
    for d in dets:
        groups[d.get("group_id")].append(d)

    keep, remap = set(), {}
    for gid, hits in groups.items():
        reps = reps_for_group(hits)
        if not reps:
            continue
        for _, p in reps:
            keep.add(p)
        for h in hits:
            p = h.get("full_image_path")
            if p and p not in {r[1] for r in reps}:
                nearest = min(reps, key=lambda rp: abs((rp[0] or 0) - (h.get("frame_idx") or 0)))
                remap[p] = nearest[1]

    on_disk = {}
    for fn in os.listdir(frames_dir):
        rel_bs = os.path.join("frames", fn)          # как записано в отчёте
        on_disk[rel_bs] = os.path.getsize(os.path.join(frames_dir, fn))

    keep_norm = {k.replace("/", os.sep) for k in keep}
    drop = [p for p in on_disk if p not in keep_norm]
    freed = sum(on_disk[p] for p in drop)
    return remap, keep_norm, drop, freed


def path_variants(p):
    """Все написания одного пути, встречающиеся в report.html.

    Это не педантизм, а разбор реальной ошибки: отчёт держит детекции
    JSON-блоком прямо в разметке, и внутри него обратный слэш экранирован --
    "frames\\\\f0003290_full.jpg". Подмена, знавшая только про одинарный слэш,
    не находила НИЧЕГО и молча возвращала 0 замен, притом что файлы уже
    удалены. Отчёт после такой чистки ссылается в пустоту.
    """
    bs = p.replace("/", "\\")
    return (bs.replace("\\", "\\\\"),   # JSON внутри HTML -- проверять ПЕРВЫМ,
            bs,                          # иначе одинарный вариант съест половину
            p.replace("\\", "/"))


def rewrite_text(path, remap):
    """Меняет ссылки на удалённые кадры в текстовом файле (report.html)."""
    if not os.path.exists(path) or not remap:
        return 0
    with open(path, encoding="utf-8", errors="replace") as f:
        s = f.read()
    n = 0
    for old, new in remap.items():
        for a, b in zip(path_variants(old), path_variants(new)):
            if a in s and a != b:
                n += s.count(a)
                s = s.replace(a, b)
    if n:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(s)
        os.replace(tmp, path)            # атомарно -- отчёт открыт у людей
    return n


def apply_report(out_dir, remap, drop):
    dj = os.path.join(out_dir, "detections.json")
    with open(dj, encoding="utf-8") as f:
        dets = json.load(f)
    changed = 0
    for d in dets:
        p = d.get("full_image_path")
        if p in remap:
            d["full_image_path"] = remap[p]
            changed += 1
    tmp = dj + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dets, f, ensure_ascii=False)
    os.replace(tmp, dj)

    html = rewrite_text(os.path.join(out_dir, "report.html"), remap)

    removed = 0
    for rel in drop:
        try:
            os.remove(os.path.join(out_dir, rel))
            removed += 1
        except OSError:
            pass
    return changed, html, removed


FRAME_RE = re.compile(r"frames(?:\\\\|\\|/)f(\d+)_full\.jpg")


def repair_html(out_dir):
    """Чинит report.html, ссылающийся на уже удалённые кадры.

    Нужен после чистки версией, которая не знала про экранированный слэш:
    файлы удалены, detections.json переписан правильно, а разметка осталась
    со старыми путями.

    Восстанавливать нечего гадать: detections.json уже содержит актуальную
    привязку кадр -> оставшийся файл, причём привязку ПОСЦЕННУЮ. Берём её и
    применяем к разметке -- по номеру кадра, он есть прямо в имени файла.
    """
    hp = os.path.join(out_dir, "report.html")
    dj = os.path.join(out_dir, "detections.json")
    frames_dir = os.path.join(out_dir, "frames")
    if not (os.path.exists(hp) and os.path.exists(dj) and os.path.isdir(frames_dir)):
        return 0, 0
    with open(dj, encoding="utf-8") as f:
        dets = json.load(f)
    by_idx = {}
    for d in dets:
        fi, p = d.get("frame_idx"), d.get("full_image_path")
        if fi is not None and p:
            by_idx[int(fi)] = p
    alive = set(os.listdir(frames_dir))

    with open(hp, encoding="utf-8", errors="replace") as f:
        s = f.read()

    broken = {int(m) for m in FRAME_RE.findall(s)
              if f"f{int(m):07d}_full.jpg" not in alive}
    if not broken:
        return 0, 0

    fixed = 0
    for idx in broken:
        target = by_idx.get(idx)
        if not target or os.path.basename(target) not in alive:
            continue
        old = os.path.join("frames", f"f{idx:07d}_full.jpg")
        for a, b in zip(path_variants(old), path_variants(target)):
            if a in s and a != b:
                fixed += s.count(a)
                s = s.replace(a, b)
    if fixed:
        tmp = hp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(s)
        os.replace(tmp, hp)
    return len(broken), fixed


def busy_report_ids(db_path):
    """Отчёты, в которые прямо сейчас пишет воркер -- их не трогаем."""
    try:
        conn = sar_common.get_db_connection(db_path)
        rows = conn.execute(
            "SELECT report_id FROM reports WHERE status IN ('queued','processing')").fetchall()
        conn.close()
        return {r["report_id"] for r in rows}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser(
        description="Сжать готовые отчёты до представительных кадров")
    ap.add_argument("--root", default=".")
    ap.add_argument("--go", action="store_true", help="без него -- только показать")
    ap.add_argument("--limit", type=int, default=0, help="обработать только N крупнейших")
    ap.add_argument("--repair-html", action="store_true",
                    help="починить отчёты, ссылающиеся на уже удалённые кадры")
    args = ap.parse_args()

    cfg, _ = sar_common.load_server_config(args.root)
    # watch_dir в конфиге обычно "." -- то есть ОТНОСИТЕЛЬНЫЙ путь, и по
    # умолчанию он разрешился бы от текущего каталога. Запускать чистку из
    # другого места (например из рабочей копии) при этом молча привело бы к
    # пустому результату вместо ошибки, что как раз и случилось при отладке.
    watch = cfg["watch_dir"]
    if not os.path.isabs(watch):
        watch = os.path.abspath(os.path.join(args.root, watch))
    _, _, db_path, _ = sar_common.resolve_paths(watch)
    reports_root = os.path.join(os.path.dirname(db_path), "reports")
    busy = busy_report_ids(db_path)
    if busy:
        print(f"пропускаю {len(busy)} отчёт(ов) в работе -- в них пишет воркер")

    dirs = [d for d in sorted(os.listdir(reports_root))
            if os.path.isdir(os.path.join(reports_root, d)) and d not in busy]

    if args.repair_html:
        tot_broken = tot_fixed = touched = 0
        for d in dirs:
            broken, fixed = repair_html(os.path.join(reports_root, d))
            if broken:
                touched += 1
                tot_broken += broken
                tot_fixed += fixed
                print(f"  {d[:50]:<52} битых кадров {broken:<5} заменено ссылок {fixed}")
        print(f"\nотчётов починено: {touched}, "
              f"уникальных битых кадров: {tot_broken}, замен: {tot_fixed}")
        return 0

    plans = []
    for d in dirs:
        p = plan_report(os.path.join(reports_root, d))
        if p and p[3] > 0:
            plans.append((d, p))
    plans.sort(key=lambda x: -x[1][3])
    if args.limit:
        plans = plans[:args.limit]

    total = sum(p[1][3] for p in plans)
    print(f"\n{'отчёт':<54}{'удалить':>9}{'освободит':>12}")
    print("-" * 76)
    for d, (remap, keep, drop, freed) in plans[:20]:
        print(f"{d[:52]:<54}{len(drop):>9}{freed/1e6:>10.0f} МБ")
    if len(plans) > 20:
        print(f"... и ещё {len(plans)-20} отчёт(ов)")
    print("-" * 76)
    print(f"итого отчётов: {len(plans)}, освободится: {total/1e9:.2f} ГБ")

    if not args.go:
        print("\nПОКАЗ. Для реальной чистки добавьте --go")
        return 0

    print("\nчищу...\n")
    freed_real = 0
    for d, (remap, keep, drop, freed) in plans:
        ch, html, rm = apply_report(os.path.join(reports_root, d), remap, drop)
        freed_real += freed
        print(f"  {d[:46]:<48} кадров -{rm:<5} ссылок в html {html}")
    print(f"\nосвобождено: {freed_real/1e9:.2f} ГБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
