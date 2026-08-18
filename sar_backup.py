"""Резервная копия базы SAR Review.

Зачем. Вся память проекта живёт в одном файле sar_data.db на одной машине:
триаж находок, ручные наблюдения, покрытие просмотра, личности и права 50
человек. Отчёты на диске пересчитываются, видео лежат на карточках -- база
не восстанавливается ниоткуда. До этого скрипта копий не было вообще.

Как копируется. Через штатный механизм онлайн-бэкапа SQLite
(Connection.backup), а НЕ копированием файла. Разница принципиальная: база
в режиме WAL, в неё в этот момент пишет воркер, и обычный copy даёт либо
рваный снимок, либо копию без незаписанного WAL -- то есть тихо битую.
Онлайн-бэкап берёт согласованный снимок, не останавливая работу.

Копия проверяется перед выдачей: integrity_check плюс сверка числа строк с
оригиналом. Непроверенная резервная копия -- это не резервная копия, а
надежда; выяснять, что она пустая, в момент отказа диска поздно.

ВНИМАНИЕ, что внутри. Помимо рабочих данных база содержит персональные
сведения (имена, telegram-ники, chat_id) и ДЕЙСТВУЮЩИЕ КЛЮЧИ ДОСТУПА
открытым текстом. Кто получит файл -- войдёт в систему под любым
пользователем. Хранить как пароль: личный диск, не общая папка, не ссылка
"кому угодно". Скрипт печатает это предупреждение каждый раз намеренно.

Запуск:
    python sar_backup.py                      # в sar_backups/ рядом с проектом
    python sar_backup.py --out D:\\backups     # в свою папку
    python sar_backup.py --keep 14            # хранить 14 последних
"""
import argparse
import os
import sqlite3
import sys
import zipfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common

# Таблицы, потеря которых невосполнима: их содержимое создано людьми и не
# пересчитывается из файлов на диске. По ним же сверяем копию.
CRITICAL_TABLES = ("manual_observations", "detection_priorities",
                   "detection_comments", "telegram_access_requests",
                   "watch_segments", "reports")


def table_counts(conn):
    out = {}
    for t in CRITICAL_TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None          # таблицы может не быть в старой базе
    return out


def make_backup(db_path, dst_path):
    """Согласованный снимок работающей базы. Возвращает счётчики строк."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    before = table_counts(src)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)            # штатный онлайн-бэкап SQLite
    dst.close()
    src.close()
    return before


def verify(dst_path, expected):
    """Проверяет копию: целостность структуры и совпадение числа строк.

    Ошибки чтения ловим и возвращаем как проблему, а не даём исключению
    вылететь наружу: копия, повреждённая настолько, что SQLite отказывается
    её открыть, -- это ровно тот случай, ради которого проверка и написана.
    Падение скрипта здесь выглядело бы как сбой бэкапа вообще, а не как
    честное "копия негодная, оригинал цел".
    """
    problems, got = [], {}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{dst_path}?mode=ro", uri=True)
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if res != "ok":
            problems.append(f"integrity_check: {res}")
        got = table_counts(conn)
    except sqlite3.Error as e:
        problems.append(f"копия не читается: {e}")
    finally:
        if conn is not None:
            conn.close()
    if not problems:
        for t, n in expected.items():
            if got.get(t) != n:
                problems.append(f"{t}: было {n}, в копии {got.get(t)}")
    return problems, got


def prune_old(out_dir, keep):
    if keep <= 0:
        return 0
    files = sorted((f for f in os.listdir(out_dir)
                    if f.startswith("sar_backup_") and f.endswith(".zip")),
                   reverse=True)
    removed = 0
    for f in files[keep:]:
        try:
            os.remove(os.path.join(out_dir, f))
            removed += 1
        except OSError:
            pass
    return removed


def main():
    ap = argparse.ArgumentParser(description="Резервная копия базы SAR Review")
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default=None, help="куда класть (по умолчанию sar_backups/)")
    ap.add_argument("--keep", type=int, default=10, help="сколько копий хранить, 0 = все")
    ap.add_argument("--no-key", action="store_true",
                    help="не класть secret.key (сессии после восстановления сбросятся)")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    cfg, _ = sar_common.load_server_config(root)
    watch = cfg["watch_dir"]
    if not os.path.isabs(watch):
        watch = os.path.abspath(os.path.join(root, watch))
    _, _, db_path, _ = sar_common.resolve_paths(watch)
    if not os.path.exists(db_path):
        print(f"ОШИБКА: база не найдена: {db_path}")
        return 1

    out_dir = args.out or os.path.join(root, "sar_backups")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_db = os.path.join(out_dir, f"_snapshot_{stamp}.db")
    zip_path = os.path.join(out_dir, f"sar_backup_{stamp}.zip")

    print(f"база : {db_path} ({os.path.getsize(db_path)/1e6:.1f} МБ)")
    print("снимаю согласованную копию (воркер можно не останавливать)...")
    expected = make_backup(db_path, tmp_db)

    problems, got = verify(tmp_db, expected)
    print("\nсодержимое копии:")
    for t, n in got.items():
        print(f"   {t:<28} {n if n is not None else '—'}")
    if problems:
        print("\nКОПИЯ НЕ ПРОШЛА ПРОВЕРКУ:")
        for p in problems:
            print("   " + p)
        os.remove(tmp_db)
        return 2
    print("\nпроверка: целостность ok, число строк совпадает с оригиналом")

    key_path = os.path.join(os.path.dirname(db_path), "secret.key")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp_db, "sar_data.db")
        if os.path.exists(key_path) and not args.no_key:
            z.write(key_path, "secret.key")
        z.writestr("ВОССТАНОВЛЕНИЕ.txt", RESTORE_NOTE.format(
            stamp=stamp,
            counts="\n".join(f"  {t}: {n}" for t, n in got.items())))
    os.remove(tmp_db)

    size = os.path.getsize(zip_path)
    removed = prune_old(out_dir, args.keep)

    print(f"\nготово: {zip_path}")
    print(f"размер: {size/1e6:.1f} МБ" + (f"   (удалено старых копий: {removed})" if removed else ""))
    print("\n" + "!" * 70)
    print("В АРХИВЕ ПЕРСОНАЛЬНЫЕ ДАННЫЕ И ДЕЙСТВУЮЩИЕ КЛЮЧИ ДОСТУПА.")
    print("Кто получит файл -- войдёт под любым пользователем платформы.")
    print("Хранить как пароль: личный диск, НЕ общая папка и не ссылка")
    print("«доступ по ссылке кому угодно».")
    print("!" * 70)
    return 0


RESTORE_NOTE = """Резервная копия SAR Review от {stamp}

ЧТО ВНУТРИ
  sar_data.db   -- база целиком: отчёты, триаж находок, ручные наблюдения,
                   покрытие просмотра, участники и их права доступа
  secret.key    -- ключ подписи сессий Flask (если не исключён при создании)

СОДЕРЖИМОЕ НА МОМЕНТ КОПИИ
{counts}

КАК ВОССТАНОВИТЬ
  1. Остановить sar_worker.py и sar_server.py
  2. Распаковать sar_data.db в папку sar_data/ проекта, заменив имеющийся файл
     (рядом лежащие sar_data.db-wal и sar_data.db-shm при этом УДАЛИТЬ --
      иначе SQLite попытается накатить журнал от другой базы)
  3. Положить secret.key туда же. Без него все входы придётся выдать заново:
     сессии перестанут проверяться, но данные не пострадают
  4. Запустить оба процесса

ЧТО НЕ ВХОДИТ В КОПИЮ
  Готовые отчёты (sar_data/reports/) и исходные видео -- они большие и
  пересчитываются из исходников. База ссылается на них по пути; если
  отчётов нет, записи останутся, а страницы отчётов будут пустыми.

ХРАНЕНИЕ
  В файле персональные данные участников и ДЕЙСТВУЮЩИЕ ключи входа
  открытым текстом. Обращаться как с паролем.
"""


if __name__ == "__main__":
    sys.exit(main())
