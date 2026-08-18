"""Перенос существующих материалов в первую операцию.

До появления операций всё лежало плоским списком. Этот скрипт заводит
операцию и привязывает к ней ВСЕ существующие материалы.

Про осторожность. Данные боевые и невосстановимые: 28 ручных пометок, 47
триаж-статусов и больше пяти тысяч сегментов просмотра создавали живые люди
во время реального поиска на Курумды. Восстановить их неоткуда -- заново
никто не отсмотрит те же часы.

Поэтому скрипт устроен так:
  * ничего не удаляет и не изменяет в существующих таблицах -- только
    ДОБАВЛЯЕТ строки связи. Даже при полностью неверной логике исходные
    данные остаются нетронутыми;
  * считает контрольные суммы ДО и ПОСЛЕ по каждой таблице и отказывается
    считать работу успешной, если хоть одна цифра изменилась;
  * по умолчанию только показывает, что сделает.

Запуск:
    python migrate_to_operations.py                     # показать
    python migrate_to_operations.py --go                # выполнить
    python migrate_to_operations.py --go --title "..."  # своё название
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common

DEFAULT_TITLE = "Курумды август 2026"

# Таблицы, которые обязаны остаться нетронутыми. Миграция их не касается,
# и сверка существует ровно затем, чтобы это доказать, а не предполагать.
UNTOUCHED = ("reports", "manual_observations", "detection_priorities",
             "detection_comments", "watch_segments", "logs",
             "telegram_access_requests", "presence")


def counts(conn):
    out = {}
    for t in UNTOUCHED:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--area", default="Алайский район, пик Курумды")
    ap.add_argument("--go", action="store_true", help="без него -- только показать")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    cfg, _ = sar_common.load_server_config(root)
    watch = cfg["watch_dir"]
    if not os.path.isabs(watch):
        watch = os.path.abspath(os.path.join(root, watch))
    _, _, db_path, _ = sar_common.resolve_paths(watch)

    sar_common.init_db(db_path)          # создаёт таблицы операций, если их нет
    conn = sar_common.get_db_connection(db_path)

    before = counts(conn)
    existing_ops = sar_common.list_operations(conn)
    reports = conn.execute("SELECT report_id, rel_path FROM reports").fetchall()
    already = conn.execute("SELECT COUNT(*) c FROM operation_materials").fetchone()["c"]

    print(f"база: {db_path}")
    print(f"материалов всего      : {len(reports)}")
    print(f"уже привязано к операциям: {already}")
    print(f"операций сейчас       : {len(existing_ops)}")
    for o in existing_ops:
        print(f"   #{o['id']} {o['title']}")
    print()
    print("состояние таблиц (не должно измениться):")
    for t, n in before.items():
        print(f"   {t:<26} {n}")

    if existing_ops and not args.go:
        print("\nВНИМАНИЕ: операции уже есть. Повторный запуск только досвяжет "
              "материалы без операции, дубликатов не создаст.")

    if not args.go:
        print(f"\nБудет создана операция: «{args.title}»")
        print(f"и к ней привязано материалов: {len(reports)}")
        print("\nПОКАЗ. Для выполнения: --go")
        conn.close()
        return 0

    op_id = sar_common.create_operation(
        conn, args.title, area=args.area,
        coordinator=None, folder=None)
    print(f"\nсоздана операция #{op_id}: {args.title}")

    for r in reports:
        sar_common.attach_material(conn, op_id, r["report_id"])
    linked = conn.execute("SELECT COUNT(*) c FROM operation_materials "
                          "WHERE operation_id=?", (op_id,)).fetchone()["c"]
    print(f"привязано материалов  : {linked}")

    after = counts(conn)
    unsorted_left = len(sar_common.unsorted_materials(conn))

    print("\nсверка:")
    bad = []
    for t in UNTOUCHED:
        mark = "ок" if before[t] == after[t] else "ИЗМЕНИЛОСЬ"
        if before[t] != after[t]:
            bad.append(t)
        print(f"   {t:<26} {before[t]} -> {after[t]}  {mark}")
    print(f"   {'материалов без операции':<26} {unsorted_left}")

    conn.close()

    if bad:
        print(f"\nОШИБКА: изменились таблицы, которые не должны были: {bad}")
        print("Восстановите базу из резервной копии.")
        return 2
    if linked != len(reports):
        print(f"\nОШИБКА: привязано {linked} из {len(reports)}")
        return 2
    if unsorted_left != 0:
        print(f"\nПРЕДУПРЕЖДЕНИЕ: без операции осталось материалов: {unsorted_left}")

    print("\nготово: ни одна существующая запись не изменена")
    return 0


if __name__ == "__main__":
    sys.exit(main())
