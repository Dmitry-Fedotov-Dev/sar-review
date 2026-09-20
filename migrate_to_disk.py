# -*- coding: utf-8 -*-
"""Перенос рабочих данных платформы на другой диск.

ЧТО ПЕРЕЕЗЖАЕТ: папки операций с материалом и sar_data (база, отчёты,
прокси-копии, превью). Код остаётся на месте -- это git-репозиторий, ему
переезжать незачем.

ЧТО ОСТАЁТСЯ НАМЕРЕННО: sar_backups. Папка копий привязана к каталогу с
кодом (sar_common.backups_dir), и после переезда база окажется на одном
диске, а её копии -- на другом. Это не недосмотр, а именно то, что нужно:
сейчас копии лежат рядом с базой, и отказ диска уносит и то, и другое.

ДВЕ ЛОВУШКИ, ради которых написан отдельный скрипт:

1. В базе 58 строк с АБСОЛЮТНЫМИ путями (reports.abs_path и out_dir).
   Простое копирование оставит их указывать на старый диск.

2. Время создания файла. report_id собирается в том числе из него, и
   shutil.copy2 его НЕ переносит -- копия получает текущее. Проверено
   отдельным опытом: id при таком копировании сдвигается.

   На опознание материала это, однако, не влияет: воркер ищет знакомый
   файл ПО rel_path, а не по пересчитанному report_id (см. комментарий
   в watcher_loop -- там это уже ловили в бою, когда ctime большого
   файла не устаивался между двумя сканами). robocopy /COPY:DAT время
   создания сохраняет, так что восстановление ниже -- страховка, а не
   необходимость.

   ПРОВЕРЯТЬ пересчётом report_id НЕЛЬЗЯ: сохранённые идентификаторы
   считались от другой формы пути, и пересчёт не сходится даже для
   файлов, которые никуда не переезжали.

Использование:
    python migrate_to_disk.py E:\\sar --dry-run    # только проверки
    python migrate_to_disk.py E:\\sar              # перенос
    python migrate_to_disk.py E:\\sar --verify     # проверить уже перенесённое

Старые папки скрипт НЕ удаляет: сначала убедитесь, что платформа
работает с нового диска.
"""
import argparse
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MOVE = ["Курумды август 2026", "sar_data"]      # что переносим
PROCESSES = ("sar_worker.py", "sar_server.py", "sar_telegram_bot.py")


# --- время создания файла на Windows ---------------------------------------
#
# shutil.copy2 переносит время изменения, но НЕ время создания: копия
# получает текущее. А os.path.getctime на Windows возвращает именно время
# создания, и из него собирается report_id.

def _filetime(ts):
    # FILETIME -- сотни наносекунд с 1601 года
    return int(ts * 10_000_000) + 116_444_736_000_000_000


def set_creation_time(path, ts):
    handle = ctypes.windll.kernel32.CreateFileW(
        str(path), 256, 0, None, 3, 0x02000000, None)   # FILE_WRITE_ATTRIBUTES
    if handle == -1:
        return False
    try:
        ft = wintypes.FILETIME()
        value = _filetime(ts)
        ft.dwLowDateTime = value & 0xFFFFFFFF
        ft.dwHighDateTime = value >> 32
        ok = ctypes.windll.kernel32.SetFileTime(
            handle, ctypes.byref(ft), None, None)
        return bool(ok)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


# --- проверки перед стартом ------------------------------------------------

def running_processes():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=60).stdout or ""
    except Exception:
        return []
    return [n for n in PROCESSES if n in out]


def preflight(target):
    problems = []
    src_size = 0
    for name in MOVE:
        p = os.path.join(SCRIPT_DIR, name)
        if not os.path.isdir(p):
            problems.append("нет папки %s" % name)
            continue
        src_size += sum(os.path.getsize(os.path.join(r, f))
                        for r, _, fs in os.walk(p) for f in fs)

    os.makedirs(target, exist_ok=True)
    free = shutil.disk_usage(target).free
    print("  переносим:      %.1f ГБ" % (src_size / 1e9))
    print("  свободно там:   %.1f ГБ" % (free / 1e9))
    if free < src_size * 1.05:
        problems.append("на целевом диске мало места")

    alive = running_processes()
    if alive:
        problems.append("запущены процессы платформы: %s -- остановите их"
                        % ", ".join(alive))
    else:
        print("  процессы платформы: остановлены")
    return problems


# --- перенос ---------------------------------------------------------------

def copy_tree(src, dst):
    """robocopy: он единственный внятно тянет полмиллиона мелких файлов.

    /COPY:DAT -- данные, атрибуты, отметки времени; /DCOPY:DAT -- то же
    для папок. Коды возврата меньше 8 у robocopy означают успех.
    """
    r = subprocess.run(["robocopy", src, dst, "/E", "/COPY:DAT", "/DCOPY:DAT",
                        "/R:2", "/W:2", "/NFL", "/NDL", "/NP", "/NJH"],
                       capture_output=True, text=True)
    return r.returncode < 8, r.returncode


def fix_creation_times(old_root, new_root):
    """Возвращает копиям исходное время создания."""
    fixed = failed = 0
    for r, _, files in os.walk(old_root):
        rel = os.path.relpath(r, old_root)
        for f in files:
            src = os.path.join(r, f)
            dst = os.path.join(new_root, rel, f) if rel != "." else os.path.join(new_root, f)
            if not os.path.exists(dst):
                continue
            try:
                want = os.path.getctime(src)
                if abs(os.path.getctime(dst) - want) > 2:
                    if set_creation_time(dst, want):
                        fixed += 1
                    else:
                        failed += 1
            except OSError:
                failed += 1
    return fixed, failed


# --- база ------------------------------------------------------------------

def rewrite_db(db_path, old_root, new_root, dry_run=False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    changes = []
    for row in conn.execute("SELECT report_id, abs_path, out_dir FROM reports"):
        new = {}
        for col in ("abs_path", "out_dir"):
            v = row[col]
            if v and v.startswith(old_root):
                new[col] = new_root + v[len(old_root):]
        if new:
            changes.append((row["report_id"], new))
    print("  строк с путями старого диска: %d" % len(changes))
    if dry_run:
        conn.close()
        return len(changes)
    with conn:
        for rid, new in changes:
            for col, val in new.items():
                conn.execute("UPDATE reports SET %s=? WHERE report_id=?" % col,
                             (val, rid))
    conn.close()
    return len(changes)


def verify(new_root, db_path, old_root=None):
    """Проверка после переезда.

    Сверяем ТО, ЧТО ДЕЙСТВИТЕЛЬНО ВАЖНО: файл на месте, папка отчёта на
    месте, размер совпал с исходным. Пересчёт report_id сюда не годится --
    сохранённые id считались от другой формы пути и не сходятся даже без
    всякого переезда, так что такая проверка давала бы ложную тревогу на
    каждой записи.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    missing_file = missing_out = size_diff = 0
    for row in conn.execute("SELECT report_id, rel_path, abs_path, out_dir FROM reports"):
        ap = row["abs_path"]
        if ap and not os.path.exists(ap):
            missing_file += 1
            if missing_file <= 5:
                print("     нет файла: %s" % ap)
            continue
        if row["out_dir"] and not os.path.isdir(row["out_dir"]):
            missing_out += 1
        if old_root and ap:
            src = old_root + ap[len(new_root):] if ap.startswith(new_root) else None
            if src and os.path.exists(src):
                if os.path.getsize(src) != os.path.getsize(ap):
                    size_diff += 1
                    print("     размер не совпал: %s" % os.path.basename(ap))
    conn.close()
    print("  файлов не найдено:        %d" % missing_file)
    print("  папок отчётов не найдено: %d" % missing_out)
    if old_root:
        print("  размер не совпал:         %d" % size_diff)
    return missing_file == 0 and missing_out == 0 and size_diff == 0


def update_config(new_root, dry_run=False):
    path = os.path.join(SCRIPT_DIR, "sar_config.json")
    cfg = json.load(open(path, encoding="utf-8"))
    was = cfg.get("server", {}).get("watch_dir")
    print("  watch_dir: %r -> %r" % (was, new_root))
    if dry_run:
        return
    bak = os.path.join(SCRIPT_DIR, "sar_data", "config_backups")
    os.makedirs(bak, exist_ok=True)
    shutil.copy2(path, os.path.join(
        bak, "sar_config.before_migrate_%s.json"
        % datetime.now().strftime("%Y%m%d_%H%M%S")))
    cfg.setdefault("server", {})["watch_dir"] = new_root
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="новая папка данных, например E:\\sar")
    ap.add_argument("--dry-run", action="store_true", help="только проверки")
    ap.add_argument("--verify", action="store_true", help="проверить уже перенесённое")
    args = ap.parse_args()
    target = os.path.abspath(args.target)
    old_root = SCRIPT_DIR

    print("ОТКУДА: %s" % old_root)
    print("КУДА:   %s\n" % target)

    if args.verify:
        db = os.path.join(target, "sar_data", "sar_data.db")
        print("ПРОВЕРКА")
        sys.exit(0 if verify(target, db, old_root) else 1)

    print("ПРОВЕРКИ ПЕРЕД СТАРТОМ")
    problems = preflight(target)
    if problems:
        print("\nНЕЛЬЗЯ НАЧИНАТЬ:")
        for p in problems:
            print("  - %s" % p)
        sys.exit(1)

    db_old = os.path.join(old_root, "sar_data", "sar_data.db")
    print("\nЧТО ИЗМЕНИТСЯ В БАЗЕ")
    rewrite_db(db_old, old_root, target, dry_run=True)
    print("\nЧТО ИЗМЕНИТСЯ В КОНФИГЕ")
    update_config(target, dry_run=True)

    if args.dry_run:
        print("\nПробный прогон. Ничего не изменено.")
        return

    print("\nКОПИРОВАНИЕ")
    for name in MOVE:
        src = os.path.join(old_root, name)
        dst = os.path.join(target, name)
        print("  %s ..." % name, flush=True)
        ok, code = copy_tree(src, dst)
        if not ok:
            print("     robocopy вернул %d -- перенос прерван" % code)
            sys.exit(1)
        print("     скопировано")

    print("\nВОССТАНОВЛЕНИЕ ВРЕМЕНИ СОЗДАНИЯ")
    for name in MOVE:
        fixed, failed = fix_creation_times(os.path.join(old_root, name),
                                           os.path.join(target, name))
        print("  %s: поправлено %d, не удалось %d" % (name, fixed, failed))

    print("\nПРАВКА БАЗЫ")
    db_new = os.path.join(target, "sar_data", "sar_data.db")
    rewrite_db(db_new, old_root, target)

    print("\nПРАВКА КОНФИГА")
    update_config(target)

    print("\nПРОВЕРКА")
    ok = verify(target, db_new, old_root)
    print("\n%s" % ("ГОТОВО. Запускайте воркер и сервер." if ok else
                    "ЕСТЬ ЗАМЕЧАНИЯ -- см. выше, старые папки не трогайте."))
    print("Старые папки на месте. Удаляйте только после того, как убедитесь,")
    print("что платформа работает с нового диска.")


if __name__ == "__main__":
    main()
