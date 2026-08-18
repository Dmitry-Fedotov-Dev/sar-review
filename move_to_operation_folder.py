"""Безопасный перенос материалов в папку операции.

Зачем отдельный инструмент, а не проводник. Запись материала ищется по
rel_path. Если перетащить файлы мышью, платформа увидит другой путь и решит,
что это НОВЫЕ материалы, а прежние записи останутся висеть на несуществующих
файлах. Вместе с ними отвяжется вся человеческая работа: ручные пометки,
статусы триажа, покрытие просмотра. На боевой базе это 28 пометок, 47
статусов и 5326 сегментов -- часы, которые заново никто не отсмотрит.

Почему перенос безопасен. report_id НЕ пересчитывается: он остаётся прежним,
меняются только rel_path и abs_path. Вся разметка ссылается на report_id,
поэтому она не может отвязаться в принципе -- ни одна её строка не
затрагивается.

Порядок на каждый файл: сначала переносится файл, потом обновляется запись.
Если что-то оборвётся посередине, уже перенесённые файлы будут согласованы с
базой, а ещё не тронутые останутся как были. Промежуточного состояния, в
котором запись указывает в пустоту, не возникает.

Запуск:
    python move_to_operation_folder.py --op 1                 # показать
    python move_to_operation_folder.py --op 1 --go            # выполнить
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common

CHECK_TABLES = ("reports", "manual_observations", "detection_priorities",
                "detection_comments", "watch_segments")


def counts(conn):
    out = {}
    for t in CHECK_TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = None
    return out


def work_for(conn, report_id):
    """Сколько человеческой работы висит на материале -- для сверки."""
    q = lambda sql: conn.execute(sql, (report_id,)).fetchone()[0]
    return (q("SELECT COUNT(*) FROM manual_observations WHERE report_id=?"),
            q("SELECT COUNT(*) FROM detection_priorities WHERE report_id=?"),
            q("SELECT COUNT(*) FROM watch_segments WHERE report_id=?"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--op", type=int, required=True, help="id операции")
    ap.add_argument("--folder", default=None,
                    help="имя папки (по умолчанию -- название операции)")
    ap.add_argument("--go", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    cfg, _ = sar_common.load_server_config(root)
    watch = cfg["watch_dir"]
    if not os.path.isabs(watch):
        watch = os.path.abspath(os.path.join(root, watch))
    _, _, db_path, _ = sar_common.resolve_paths(watch)
    data_dir = os.path.dirname(db_path)

    conn = sar_common.get_db_connection(db_path)
    op = sar_common.get_operation(conn, args.op)
    if op is None:
        print(f"ОТКАЗ: операции #{args.op} нет")
        return 1

    folder = args.folder or op["title"]
    target = os.path.join(watch, folder)
    materials = sar_common.materials_of_operation(conn, args.op)

    print(f"операция #{op['id']}: {op['title']}")
    print(f"папка назначения : {target}")
    print(f"материалов       : {len(materials)}")

    plan, skipped, missing = [], [], []
    for m in materials:
        src = m["abs_path"]
        if not os.path.exists(src):
            missing.append(m)
            continue
        # уже внутри папки операции -- трогать не надо
        if os.path.abspath(src).startswith(os.path.abspath(target) + os.sep):
            skipped.append(m)
            continue
        dst = os.path.join(target, os.path.basename(src))
        plan.append((m, src, dst))

    print(f"будет перенесено : {len(plan)}")
    if skipped:
        print(f"уже на месте     : {len(skipped)}")
    if missing:
        print(f"файла нет на диске: {len(missing)}")
        for m in missing[:5]:
            print(f"   {m['rel_path']}")

    # Один физический файл может стоять за НЕСКОЛЬКИМИ записями: из-за сноса
    # ctime видео однажды обработалось дважды под разными report_id, и люди
    # размечали обе копии (см. грабли про ctime в CLAUDE.md). Это старая
    # проблема данных, чинить её переносом нельзя -- но и отказываться
    # незачем: файл переносится ОДИН раз, а все записи, ссылающиеся на него,
    # получают новый путь и сохраняют каждая свою разметку.
    by_src = {}
    for m, src, dst in plan:
        by_src.setdefault(os.path.normcase(os.path.abspath(src)), []).append((m, src, dst))
    shared = {k: v for k, v in by_src.items() if len(v) > 1}
    if shared:
        print(f"\nодин файл за несколькими записями: {len(shared)} шт. "
              f"-- перенесём один раз, пути обновим у всех")

    # А вот РАЗНЫЕ файлы, попадающие в одно имя назначения, -- настоящая
    # опасность: перенос затёр бы один другим. Тут отказываемся.
    dst_sources = {}
    for k, items in by_src.items():
        dst_sources.setdefault(os.path.normcase(items[0][2]), set()).add(k)
    real = {d: s for d, s in dst_sources.items() if len(s) > 1}
    if real:
        print(f"\nОТКАЗ: {len(real)} имён совпадают у РАЗНЫХ файлов, "
              f"перенос затёр бы их:")
        for d in list(real)[:5]:
            print(f"   {os.path.basename(d)}")
        conn.close()
        return 2

    before = counts(conn)
    work_before = {m["report_id"]: work_for(conn, m["report_id"]) for m in materials}
    total_work = tuple(sum(x[i] for x in work_before.values()) for i in range(3))
    print(f"\nчеловеческая работа на этих материалах:")
    print(f"   ручных пометок     {total_work[0]}")
    print(f"   статусов триажа    {total_work[1]}")
    print(f"   сегментов просмотра {total_work[2]}")

    if not args.go:
        print("\nПОКАЗ. Для переноса: --go")
        conn.close()
        return 0

    os.makedirs(target, exist_ok=True)
    sar_common.write_operation_marker(target, args.op)
    conn.execute("UPDATE operations SET folder=? WHERE id=?", (folder, args.op))
    conn.commit()
    print(f"\nметка операции записана в {target}")

    moved = failed = 0
    for items in by_src.values():
        _, src, dst = items[0]
        new_rel = os.path.relpath(dst, watch).replace(os.sep, "/")
        try:
            # сначала файл, потом записи: оборвись оно посередине -- запись
            # никогда не будет указывать в пустоту
            shutil.move(src, dst)
            for m, _, _ in items:
                conn.execute(
                    "UPDATE reports SET rel_path=?, abs_path=? WHERE report_id=?",
                    (new_rel, dst, m["report_id"]))
                # превью именуется по rel_path -- переносим вслед за материалом
                old_t = sar_common.get_thumbnail_path(data_dir, m["rel_path"])
                new_t = sar_common.get_thumbnail_path(data_dir, new_rel)
                if os.path.exists(old_t) and old_t != new_t:
                    try:
                        shutil.move(old_t, new_t)
                    except OSError:
                        pass          # превью пересоздаётся, терять нечего
            conn.commit()
            moved += 1
        except Exception as e:
            failed += 1
            print(f"   ✗ {os.path.basename(src)}: {e}")

    after = counts(conn)
    print(f"\nперенесено: {moved}, ошибок: {failed}")
    print("\nсверка -- ни одна строка не должна была измениться:")
    bad = [t for t in CHECK_TABLES if before[t] != after[t]]
    for t in CHECK_TABLES:
        print(f"   {t:<24} {before[t]} -> {after[t]}  {'ок' if before[t]==after[t] else 'ИЗМЕНИЛОСЬ'}")

    lost = []
    for rid, was in work_before.items():
        now = work_for(conn, rid)
        if now != was:
            lost.append((rid, was, now))
    print(f"   {'разметка по материалам':<24} "
          f"{'вся на месте' if not lost else 'ПОТЕРЯНА у %d' % len(lost)}")

    broken = 0
    for m in sar_common.materials_of_operation(conn, args.op):
        if not os.path.exists(m["abs_path"]):
            broken += 1
    print(f"   {'записей без файла':<24} {broken}")
    conn.close()

    if bad or lost or failed:
        print("\nОШИБКА: восстановите базу и файлы из резервной копии")
        return 2
    print("\nготово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
