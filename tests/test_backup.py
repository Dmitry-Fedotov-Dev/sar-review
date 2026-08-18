"""Тесты резервного копирования.

Резервная копия проверяется ровно один раз -- в тот день, когда всё уже
сломалось. Поэтому тесты бьют не в "функция отработала", а в свойства, от
которых зависит, окажется ли копия пригодной:

  * снимок берётся с базы, В КОТОРУЮ ПИШУТ (режим WAL, открытый писатель) --
    это боевое условие, воркер не останавливается ради бэкапа;
  * данные в копии реально те же, а не пустая схема;
  * испорченная копия НЕ выдаётся за годную;
  * ротация не съедает свежие копии.

Работаем с настоящими файлами SQLite, без моков: весь риск здесь как раз в
поведении WAL и в том, что копирование файла даёт битый результат.
"""
import os
import sqlite3
import zipfile

import pytest

import sar_backup as sb


def _make_db(path, rows=5, wal=True):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE manual_observations (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("CREATE TABLE reports (report_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE detection_priorities (id INTEGER PRIMARY KEY)")
    for i in range(rows):
        conn.execute("INSERT INTO manual_observations (note) VALUES (?)", (f"n{i}",))
        conn.execute("INSERT INTO reports (report_id) VALUES (?)", (f"r{i}",))
    conn.commit()
    return conn


# --- снимок --------------------------------------------------------------

def test_backup_captures_rows(tmp_path):
    src = str(tmp_path / "s.db")
    conn = _make_db(src, rows=7)
    conn.close()
    dst = str(tmp_path / "b.db")
    counts = sb.make_backup(src, dst)
    assert counts["manual_observations"] == 7
    problems, got = sb.verify(dst, counts)
    assert problems == []
    assert got["manual_observations"] == 7


def test_backup_works_while_a_writer_holds_the_db(tmp_path):
    """Боевое условие: воркер пишет в базу, бэкап идёт параллельно.
    Простое копирование файла тут дало бы снимок без содержимого WAL."""
    src = str(tmp_path / "s.db")
    writer = _make_db(src, rows=3)
    writer.execute("INSERT INTO manual_observations (note) VALUES ('в WAL')")
    writer.commit()                      # закоммичено, но лежит в WAL

    dst = str(tmp_path / "b.db")
    counts = sb.make_backup(src, dst)
    assert counts["manual_observations"] == 4

    check = sqlite3.connect(dst)
    n = check.execute(
        "SELECT COUNT(*) FROM manual_observations WHERE note='в WAL'").fetchone()[0]
    check.close()
    writer.close()
    assert n == 1, "запись из WAL не попала в копию -- копия неполная"


def test_backup_does_not_disturb_the_source(tmp_path):
    src = str(tmp_path / "s.db")
    conn = _make_db(src, rows=4)
    dst = str(tmp_path / "b.db")
    sb.make_backup(src, dst)
    # писатель должен продолжать работать как ни в чём не бывало
    conn.execute("INSERT INTO manual_observations (note) VALUES ('после')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM manual_observations").fetchone()[0] == 5
    conn.close()


# --- проверка копии ------------------------------------------------------

def test_verify_rejects_a_truncated_copy(tmp_path):
    """Главный тест: негодная копия не должна пройти проверку."""
    src = str(tmp_path / "s.db")
    _make_db(src, rows=6).close()
    dst = str(tmp_path / "b.db")
    counts = sb.make_backup(src, dst)

    with open(dst, "r+b") as f:          # портим файл
        f.truncate(os.path.getsize(dst) // 2)

    problems, _ = sb.verify(dst, counts)
    assert problems, "битая копия прошла проверку как годная"


def test_verify_catches_row_count_mismatch(tmp_path):
    src = str(tmp_path / "s.db")
    _make_db(src, rows=3).close()
    dst = str(tmp_path / "b.db")
    counts = sb.make_backup(src, dst)
    counts["manual_observations"] = 99   # как будто оригинал был больше
    problems, _ = sb.verify(dst, counts)
    assert any("manual_observations" in p for p in problems)


def test_missing_table_is_not_a_crash(tmp_path):
    """Старая база может не иметь новых таблиц -- это не повод падать."""
    src = str(tmp_path / "s.db")
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE reports (report_id TEXT)")
    conn.commit()
    conn.close()
    dst = str(tmp_path / "b.db")
    counts = sb.make_backup(src, dst)
    assert counts["detection_comments"] is None
    problems, _ = sb.verify(dst, counts)
    assert problems == []


# --- ротация -------------------------------------------------------------

def test_prune_keeps_the_newest(tmp_path):
    names = [f"sar_backup_2026081{i}_120000.zip" for i in range(1, 8)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    removed = sb.prune_old(str(tmp_path), keep=3)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert removed == 4
    assert left == sorted(names[-3:]), left


def test_prune_zero_keeps_everything(tmp_path):
    (tmp_path / "sar_backup_20260818_120000.zip").write_bytes(b"x")
    assert sb.prune_old(str(tmp_path), keep=0) == 0


def test_prune_ignores_foreign_files(tmp_path):
    (tmp_path / "важное.txt").write_bytes(b"x")
    (tmp_path / "sar_backup_20260818_120000.zip").write_bytes(b"x")
    sb.prune_old(str(tmp_path), keep=0)
    assert (tmp_path / "важное.txt").exists()


# --- содержимое архива ---------------------------------------------------

def test_restore_note_explains_the_wal_trap(tmp_path):
    """Восстановление без удаления -wal/-shm даёт повреждённую базу --
    инструкция обязана это сказать, иначе копия бесполезна."""
    note = sb.RESTORE_NOTE.format(stamp="x", counts="")
    assert "-wal" in note and "УДАЛИТЬ" in note
    assert "secret.key" in note


def test_restore_note_warns_about_credentials():
    note = sb.RESTORE_NOTE.format(stamp="x", counts="")
    assert "ключи входа" in note or "ключи доступа" in note.lower()
