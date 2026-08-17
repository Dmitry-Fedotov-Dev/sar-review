"""Личная ссылка приходит отдельным сообщением и самоуничтожается.

Ссылка -- ключ на предъявителя. Оставаясь в переписке, она живёт в истории
Telegram, попадает в резервные копии и видна любому, кто заглянет в чужой
телефон через плечо. Поэтому она уходит ОТДЕЛЬНЫМ сообщением (стереть можно
только сообщение целиком) и удаляется по таймеру.

Общий пароль в переписку больше не отправляется вовсе -- по той же причине,
только он ещё и общий для всех."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import sar_common
import sar_telegram_bot as bot


CHAT = 555


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    monkeypatch.setattr(bot, "DB_PATH", db_path)
    monkeypatch.setattr(bot, "CFG", {
        "admin_chat_ids": [], "service_url": "https://example.com",
        "service_password": "s3cr3t-pw", "guide_url": "https://example.com/guide",
        "personal_link_ttl_sec": 3,
    })
    bot.upsert_request(CHAT, "vol", "Аяу")
    bot.set_status(CHAT, "approved", 1)
    yield


def _bot_obj():
    b = MagicMock()
    msg = MagicMock()
    msg.message_id = 4242
    b.send_message = AsyncMock(return_value=msg)
    b.delete_message = AsyncMock()
    return b


def test_link_goes_in_a_separate_message(monkeypatch):
    b = _bot_obj()
    asyncio.run(bot.send_access(b, CHAT))

    assert b.send_message.await_count == 2
    instruction = b.send_message.await_args_list[0].args[1]
    link_msg = b.send_message.await_args_list[1].args[1]
    assert "/login?key" not in instruction, "ссылка не должна быть в остающемся сообщении"
    assert "/login?key" in link_msg


def test_password_is_never_sent_to_chat():
    b = _bot_obj()
    asyncio.run(bot.send_access(b, CHAT))
    everything = " ".join(c.args[1] for c in b.send_message.await_args_list)
    assert "s3cr3t-pw" not in everything


def test_link_message_warns_it_will_disappear():
    text = bot.link_message(CHAT)
    assert "исчезнет" in text
    assert "/help" in text, "человек должен знать, как получить ссылку заново"


def test_link_is_deleted_after_the_ttl(monkeypatch):
    """Проверяем сам факт удаления, не выжидая реальные секунды."""
    slept = {}

    async def fake_sleep(d):
        slept["delay"] = d

    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
    b = _bot_obj()
    asyncio.run(bot._delete_after(b, CHAT, 4242, 3))

    assert slept["delay"] == 3
    b.delete_message.assert_awaited_once_with(chat_id=CHAT, message_id=4242)


def test_deletion_failure_is_swallowed(monkeypatch):
    """Человек мог удалить сообщение сам, могла пропасть связь -- ни то, ни
    другое не повод роняться."""
    async def fake_sleep(d):
        return None

    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
    b = _bot_obj()
    b.delete_message = AsyncMock(side_effect=RuntimeError("message to delete not found"))
    asyncio.run(bot._delete_after(b, CHAT, 4242, 1))  # не должно бросить


def test_ttl_zero_means_keep_the_message(monkeypatch):
    bot.CFG["personal_link_ttl_sec"] = 0
    assert bot.link_ttl() == 0
    assert "исчезнет" not in bot.link_message(CHAT)


def test_broken_ttl_value_falls_back_to_default():
    bot.CFG["personal_link_ttl_sec"] = "три секунды"
    assert bot.link_ttl() == 3


def test_no_link_sent_when_service_url_missing():
    """Без адреса сервиса ссылка была бы битой -- лучше не слать вовсе."""
    bot.CFG["service_url"] = ""
    b = _bot_obj()
    asyncio.run(bot.send_access(b, CHAT))
    assert b.send_message.await_count == 1
