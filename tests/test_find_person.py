"""Поиск человека для выдачи прав -- по @username ИЛИ по chat_id.

Причина: username в Telegram НЕОБЯЗАТЕЛЕН, и у части волонтёров его нет.
Пока поиск умел только по нику, таким людям нельзя было выдать никакую
роль вообще -- поймано на реальном списке, где активный разметчик
оказался без username."""
import sqlite3

import pytest

import sar_common
import sar_telegram_bot as bot


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    monkeypatch.setattr(bot, "DB_PATH", db_path)
    monkeypatch.setattr(bot, "CFG", {"admin_chat_ids": [], "service_url": "http://x"})
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO telegram_access_requests (chat_id, username, first_name, status, "
        "requested_at) VALUES (?,?,?,'approved','2026-01-01T00:00:00')",
        [(111, "wild_high", "Дмитрий"),
         (222, None, "Gagarin"),          # без username -- главный случай
         (333, "MiXeDcAsE", "Кто-то")])
    conn.commit()
    conn.close()
    yield


def test_finds_by_username():
    assert bot.find_person("@wild_high")["chat_id"] == 111


def test_finds_by_username_without_at_sign():
    assert bot.find_person("wild_high")["chat_id"] == 111


def test_username_lookup_ignores_case():
    """В Telegram регистр ника не значим."""
    assert bot.find_person("@mixedcase")["chat_id"] == 333


def test_finds_by_chat_id():
    """Главный сценарий: у человека нет @username, выдать роль иначе нельзя."""
    row = bot.find_person("222")
    assert row is not None
    assert row["first_name"] == "Gagarin"


def test_finds_by_chat_id_given_as_number():
    assert bot.find_person(222)["chat_id"] == 222


def test_unknown_identifier_returns_none():
    assert bot.find_person("@нет_такого") is None
    assert bot.find_person("999999") is None


def test_numeric_username_still_resolvable_by_username():
    """Ник может состоять из цифр -- тогда поиск по id ничего не найдёт и
    должен продолжиться поиском по нику, а не молча вернуть None."""
    conn = sqlite3.connect(bot.DB_PATH)
    conn.execute("INSERT INTO telegram_access_requests (chat_id, username, first_name, status, "
                 "requested_at) VALUES (444,'12345','Цифровой','approved','2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    assert bot.find_person("12345")["chat_id"] == 444


def test_help_text_explains_both_ways():
    assert "chat_id" in bot.ROLE_HELP
    assert "@username" in bot.ROLE_HELP
