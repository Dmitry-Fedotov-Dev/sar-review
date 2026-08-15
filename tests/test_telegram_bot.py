"""Тесты sar_telegram_bot.py -- бот для выдачи доступа волонтёрам/тестерам
с одобрением координатора (см. обсуждение с пользователем: "с одобрением").
БД-логику (upsert/list/set_status) гоняем через реальный sqlite (как и
остальной проект -- не ML-инференс, мокать незачем). Сами Telegram-объекты
(Update/CallbackQuery/Bot) -- MagicMock/AsyncMock, это внешняя сетевая
граница, аналогично тому, как в проекте не поднимают реальный YOLO в тестах."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import sar_common
import sar_telegram_bot as bot


ADMIN_A, ADMIN_B, USER = 111, 222, 999


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    monkeypatch.setattr(bot, "DB_PATH", db_path)
    monkeypatch.setattr(bot, "CFG", {
        "bot_token": "test-token",
        "admin_chat_ids": [ADMIN_A, ADMIN_B],
        "service_url": "https://example.trycloudflare.com",
        "service_password": "s3cr3t-pw",
        "guide_url": "https://claude.ai/code/artifact/guide",
        "presentation_url": "https://claude.ai/code/artifact/pitch",
    })
    yield


def _run(coro):
    return asyncio.run(coro)


def _fake_start_update(chat_id, username="volunteer", first_name="Аяу"):
    update = MagicMock()
    update.effective_user.username = username
    update.effective_user.first_name = first_name
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


# --- чистая БД-логика ---

def test_upsert_request_creates_pending_and_is_idempotent():
    row, is_new = bot.upsert_request(USER, "vol", "Аяу")
    assert is_new is True
    assert row["status"] == "pending"

    row2, is_new2 = bot.upsert_request(USER, "vol", "Аяу")
    assert is_new2 is False
    assert row2["status"] == "pending"


def test_set_status_records_decider_and_timestamp():
    bot.upsert_request(USER, "vol", "Аяу")
    bot.set_status(USER, "approved", ADMIN_A)
    row = bot.get_request(USER)
    assert row["status"] == "approved"
    assert row["decided_by"] == ADMIN_A
    assert row["decided_at"] is not None


def test_list_pending_excludes_decided_requests():
    bot.upsert_request(1, "a", "A")
    bot.upsert_request(2, "b", "B")
    bot.set_status(2, "approved", ADMIN_A)
    pending = bot.list_pending()
    assert [r["chat_id"] for r in pending] == [1]


def test_access_message_contains_service_url_password_and_guide():
    msg = bot.access_message()
    assert "https://example.trycloudflare.com" in msg
    assert "s3cr3t-pw" in msg
    assert "https://claude.ai/code/artifact/guide" in msg


def test_access_message_recommends_desktop_over_phone():
    # человека на снегу видно как несколько пикселей -- на телефоне находку
    # реально пропустить, поэтому рекомендация должна быть в самом первом
    # сообщении, а не только в гиде
    msg = bot.access_message()
    assert "ноутбука или компьютера" in msg
    assert "не с телефона" in msg


def test_access_message_no_longer_includes_presentation_link():
    # по просьбе пользователя -- презентацию из сообщения бота убрали,
    # остаётся только гид волонтёра
    msg = bot.access_message()
    assert "https://claude.ai/code/artifact/pitch" not in msg


# --- /start ---

def test_start_new_request_notifies_all_admins_with_buttons():
    update, context = _fake_start_update(USER)
    _run(bot.start(update, context))

    update.message.reply_text.assert_awaited_once()
    assert "координатору" in update.message.reply_text.await_args[0][0]

    assert context.bot.send_message.await_count == 2
    notified_ids = {call.args[0] for call in context.bot.send_message.await_args_list}
    assert notified_ids == {ADMIN_A, ADMIN_B}
    for call in context.bot.send_message.await_args_list:
        assert "reply_markup" in call.kwargs


def test_start_repeat_while_pending_does_not_reping_admins():
    update1, context1 = _fake_start_update(USER)
    _run(bot.start(update1, context1))
    assert context1.bot.send_message.await_count == 2

    update2, context2 = _fake_start_update(USER)
    _run(bot.start(update2, context2))
    # повторный /start от той же (ещё не решённой) заявки НЕ должен снова
    # дёргать админов -- иначе спам при каждом повторном /start
    context2.bot.send_message.assert_not_awaited()
    update2.message.reply_text.assert_awaited_once()
    assert "ждёт одобрения" in update2.message.reply_text.await_args[0][0]


def test_start_when_already_approved_sends_access_directly_without_notifying_admins():
    bot.upsert_request(USER, "vol", "Аяу")
    bot.set_status(USER, "approved", ADMIN_A)

    update, context = _fake_start_update(USER)
    _run(bot.start(update, context))

    context.bot.send_message.assert_not_awaited()
    update.message.reply_text.assert_awaited_once()
    assert "s3cr3t-pw" in update.message.reply_text.await_args[0][0]


def test_start_when_denied_sends_decline_message():
    bot.upsert_request(USER, "vol", "Аяу")
    bot.set_status(USER, "denied", ADMIN_A)

    update, context = _fake_start_update(USER)
    _run(bot.start(update, context))

    update.message.reply_text.assert_awaited_once_with(bot.DECLINE_MESSAGE)
    # отклонённому пароль ни при каких условиях не должен уйти
    assert "s3cr3t-pw" not in update.message.reply_text.await_args[0][0]


# --- одобрение/отклонение кнопками ---

def _fake_callback_update(admin_id, action, target_chat_id, message_text="Новая заявка..."):
    update = MagicMock()
    update.callback_query.from_user.id = admin_id
    update.callback_query.data = f"{action}:{target_chat_id}"
    update.callback_query.message.text = message_text
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


def test_on_decision_approve_sets_status_and_sends_access_to_requester():
    bot.upsert_request(USER, "vol", "Аяу")
    update, context = _fake_callback_update(ADMIN_A, "approve", USER)

    _run(bot.on_decision(update, context))

    assert bot.get_request(USER)["status"] == "approved"
    context.bot.send_message.assert_awaited_once_with(USER, bot.access_message())
    update.callback_query.edit_message_text.assert_awaited_once()


def test_on_decision_deny_sets_status_and_sends_decline_to_requester():
    bot.upsert_request(USER, "vol", "Аяу")
    update, context = _fake_callback_update(ADMIN_A, "deny", USER)

    _run(bot.on_decision(update, context))

    assert bot.get_request(USER)["status"] == "denied"
    context.bot.send_message.assert_awaited_once_with(USER, bot.DECLINE_MESSAGE)


def test_on_decision_rejects_non_admin_and_leaves_status_pending():
    bot.upsert_request(USER, "vol", "Аяу")
    intruder_id = 555
    update, context = _fake_callback_update(intruder_id, "approve", USER)

    _run(bot.on_decision(update, context))

    assert bot.get_request(USER)["status"] == "pending"  # решение постороннего не применилось
    context.bot.send_message.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()
    assert update.callback_query.answer.await_args.kwargs.get("show_alert") is True


# --- /pending ---

def test_pending_cmd_lists_only_for_admin():
    bot.upsert_request(1, "a", "A")
    bot.upsert_request(2, "b", "B")

    update = MagicMock()
    update.effective_chat.id = ADMIN_A
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    _run(bot.pending_cmd(update, context))
    assert update.message.reply_text.await_count == 2


def test_pending_cmd_silently_ignores_non_admin():
    bot.upsert_request(1, "a", "A")

    update = MagicMock()
    update.effective_chat.id = USER  # не координатор
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    _run(bot.pending_cmd(update, context))
    update.message.reply_text.assert_not_awaited()


# --- меню команд ---

def test_post_init_registers_start_and_help_but_not_pending():
    fake_app = MagicMock()
    fake_app.bot.set_my_commands = AsyncMock()

    _run(bot._post_init(fake_app))

    fake_app.bot.set_my_commands.assert_awaited_once()
    registered = fake_app.bot.set_my_commands.await_args[0][0]
    names = {cmd[0] for cmd in registered}
    assert names == {"start", "help"}  # /pending -- админская, в публичном меню не нужна
