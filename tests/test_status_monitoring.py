"""/status отдаёт ссылку на дашборд вместе с состоянием.

Адрес у бесплатного туннеля временный и меняется при каждом перезапуске --
за одни сутки он сменился трижды. Держать его в /status удобнее, чем искать
в переписке: команда видна в меню и всегда отдаёт текущий адрес из конфига.

Главное требование, которое здесь и проверяется: ссылка приходит И ТОГДА,
КОГДА ПЛАТФОРМА НЕ ОТВЕЧАЕТ. Именно в этот момент дашборд -- единственный
оставшийся инструмент, и оборвать сообщение без него значит оставить
человека ни с чем.
"""
import pytest

import sar_telegram_bot as bot


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(bot, "CFG", {
        "admin_chat_ids": [1],
        "service_url": "https://platform.example",
        "grafana_url": "https://dash.example",
        "grafana_login": "sar-viewer",
        "grafana_password": "секрет",
    })


def test_block_contains_url_and_credentials():
    text = "\n".join(bot.monitoring_block())
    assert "https://dash.example" in text
    assert "sar-viewer" in text and "секрет" in text


def test_block_says_the_account_is_read_only():
    """Иначе человек решит, что ему дали админский доступ, и удивится,
    почему ничего не меняется."""
    assert "только чтение" in "\n".join(bot.monitoring_block())


def test_block_is_empty_without_url():
    """Мониторинг опционален -- без него /status должен просто молчать
    про дашборд, а не показывать пустую строку."""
    bot.CFG["grafana_url"] = ""
    assert bot.monitoring_block() == []


def test_block_without_credentials_still_gives_the_link():
    """Логин может быть не задан -- ссылка всё равно полезна."""
    bot.CFG["grafana_login"] = ""
    text = "\n".join(bot.monitoring_block())
    assert "https://dash.example" in text
    assert "вход:" not in text


def test_whitespace_only_url_counts_as_absent():
    bot.CFG["grafana_url"] = "   "
    assert bot.monitoring_block() == []


def test_status_command_includes_the_block():
    import inspect
    src = inspect.getsource(bot.status_cmd)
    assert src.count("monitoring_block()") == 2, (
        "блок должен добавляться и в обычный ответ, и в ветку "
        "«платформа не отвечает»")


def test_link_preview_is_disabled():
    """Ссылка на дашборд не должна разворачиваться картинкой на пол-экрана."""
    import inspect
    src = inspect.getsource(bot.status_cmd)
    assert src.count("disable_web_page_preview=True") == 2
