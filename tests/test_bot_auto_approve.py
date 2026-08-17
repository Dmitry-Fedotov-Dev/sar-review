"""Временная автовыдача доступа в боте (auto_approve_until).

Включается, когда координатор не может одобрять заявки вручную (ночь,
выезд). Сделана ВРЕМЕНЕМ, а не флагом: флаг включают на ночь и забывают
выключить, и единственная защита доступа к боевому инструменту тихо
остаётся открытой навсегда. По истечении срока бот сам возвращается к
ручному одобрению.

Любая неясность со значением трактуется как "выключено" -- ошибаться
безопаснее в сторону закрытого доступа."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import sar_common
import sar_telegram_bot as bot


ADMIN, USER = 111, 999


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    monkeypatch.setattr(bot, "DB_PATH", db_path)
    monkeypatch.setattr(bot, "CFG", {
        "bot_token": "t", "admin_chat_ids": [ADMIN],
        "service_url": "https://example.com", "service_password": "pw",
        "guide_url": "https://example.com/guide", "auto_approve_until": "",
    })
    yield


def _run(coro):
    return asyncio.run(coro)


def c_sent(context):
    """Все вызовы bot.send_message -- доступ теперь уходит двумя
    сообщениями, поэтому проверяем список, а не единственный вызов."""
    return list(context.bot.send_message.await_args_list)


def _upd(chat_id=USER):
    u = MagicMock()
    u.effective_user.username = "vol"
    u.effective_user.first_name = "Аяу"
    u.effective_chat.id = chat_id
    u.message.reply_text = AsyncMock()
    c = MagicMock()
    c.bot.send_message = AsyncMock()
    return u, c


# --- само окно ---

def test_disabled_when_not_configured():
    assert bot.auto_approve_active() is False


def test_active_before_deadline():
    bot.CFG["auto_approve_until"] = (datetime.now() + timedelta(hours=2)).isoformat()
    assert bot.auto_approve_active() is True


def test_expires_by_itself():
    bot.CFG["auto_approve_until"] = (datetime.now() - timedelta(minutes=1)).isoformat()
    assert bot.auto_approve_active() is False


def test_garbage_value_is_treated_as_disabled():
    bot.CFG["auto_approve_until"] = "завтра утром"
    assert bot.auto_approve_active() is False


def test_empty_and_whitespace_are_disabled():
    for v in ("", "   ", None):
        bot.CFG["auto_approve_until"] = v
        assert bot.auto_approve_active() is False


# --- поведение /start ---

def test_start_grants_access_immediately_inside_window():
    bot.CFG["auto_approve_until"] = (datetime.now() + timedelta(hours=8)).isoformat()
    u, c = _upd()
    _run(bot.start(u, c))

    assert bot.get_request(USER)["status"] == "approved"
    # доступ приходит сразу, без ожидания одобрения: инструкция + личная
    # ссылка отдельным сообщением
    to_user = [c for c in c_sent(c) if c.args[0] == USER]
    assert len(to_user) == 2
    assert "/login?key" in to_user[1].args[1]


def test_admin_is_still_notified_on_auto_approval():
    """Координатор должен утром видеть, кто зашёл ночью."""
    bot.CFG["auto_approve_until"] = (datetime.now() + timedelta(hours=8)).isoformat()
    u, c = _upd()
    _run(bot.start(u, c))

    to_admin = [x for x in c_sent(c) if x.args[0] == ADMIN]
    assert len(to_admin) == 1
    text = to_admin[0].args[1]
    assert "АВТОМАТИЧЕСКИ" in text
    assert "@vol" in text


def test_auto_approval_sends_no_decision_buttons():
    """Решение уже принято -- кнопки одобрить/отклонить только запутают."""
    bot.CFG["auto_approve_until"] = (datetime.now() + timedelta(hours=8)).isoformat()
    u, c = _upd()
    _run(bot.start(u, c))
    assert "reply_markup" not in c.bot.send_message.await_args.kwargs


def test_outside_window_falls_back_to_manual_approval():
    bot.CFG["auto_approve_until"] = (datetime.now() - timedelta(hours=1)).isoformat()
    u, c = _upd()
    _run(bot.start(u, c))

    assert bot.get_request(USER)["status"] == "pending"
    assert "координатору" in u.message.reply_text.await_args[0][0]
    assert "reply_markup" in c.bot.send_message.await_args.kwargs


def test_denied_user_is_not_auto_approved():
    """Явный отказ координатора сильнее окна автовыдачи."""
    bot.CFG["auto_approve_until"] = (datetime.now() + timedelta(hours=8)).isoformat()
    bot.upsert_request(USER, "vol", "Аяу")
    bot.set_status(USER, "denied", ADMIN)

    u, c = _upd()
    _run(bot.start(u, c))

    assert bot.get_request(USER)["status"] == "denied"
    assert "pw" not in u.message.reply_text.await_args[0][0]
