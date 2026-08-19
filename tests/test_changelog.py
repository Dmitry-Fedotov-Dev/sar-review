"""Команда /changelog в боте.

Список изменений написан ЯЗЫКОМ ВОЛОНТЁРА: человеку важно "что я теперь
могу", а не "какой модуль переписан". Поэтому текст ведётся вручную, а не
собирается из git-истории -- туда попадают внутренние правки, которые
пользователю ничего не говорят."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import sar_telegram_bot as bot


def _run(coro):
    return asyncio.run(coro)


def _update():
    u = MagicMock()
    u.message.reply_text = AsyncMock()
    u.effective_chat.id = 999
    return u, MagicMock()


def test_changelog_is_sent():
    u, c = _update()
    _run(bot.changelog_cmd(u, c))
    u.message.reply_text.assert_awaited_once()
    assert "Что нового" in u.message.reply_text.await_args[0][0]


def test_changelog_available_without_approved_access():
    """Список изменений ничего секретного не содержит, а человеку полезно
    понимать, что за инструмент он ждёт."""
    u, c = _update()
    _run(bot.changelog_cmd(u, c))          # никакой проверки статуса заявки
    u.message.reply_text.assert_awaited_once()


def test_changelog_mentions_key_updates():
    text = bot.CHANGELOG
    for expected in ("личной ссылке", "Обсуждения", "аномалия", "Поиск",
                     "Подсказки модели", "Фото", "Ручной режим"):
        assert expected in text, f"в списке изменений не упомянуто: {expected}"


def test_changelog_leaks_no_secrets():
    """Текст уходит всем подряд, включая неодобренных -- ни пароля, ни
    токенов, ни внутренних адресов в нём быть не должно."""
    text = bot.CHANGELOG.lower()
    for leak in ("пароль", "token", "ключ:", "/login?key", "localhost", "8080"):
        assert leak not in text, f"в changelog просочилось: {leak}"


def test_changelog_is_registered_in_command_menu():
    # команды переехали из тела _post_init в списки BASE/ADMIN_COMMANDS,
    # чтобы одно и то же меню можно было поставить в разных областях
    assert "changelog" in {c[0] for c in bot.BASE_COMMANDS}


def test_changelog_handler_is_wired():
    import inspect
    src = inspect.getsource(bot.build_application)
    assert 'CommandHandler("changelog"' in src


def test_changelog_fits_telegram_message_limit():
    """Telegram режет сообщения длиннее 4096 символов -- список изменений
    не должен молча обрезаться на середине."""
    assert len(bot.CHANGELOG) <= 4096, f"длина {len(bot.CHANGELOG)}, лимит 4096"
