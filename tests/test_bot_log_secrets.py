"""Токен бота не должен попадать в журнал.

httpx на уровне INFO пишет полный URL каждого запроса, а у Telegram токен
лежит прямо в пути: api.telegram.org/bot<ТОКЕН>/getUpdates. Журнал набирал
по строке с секретом на каждое обращение -- 8 штук за двадцать секунд
работы.

Значение это имеет не теоретическое: журнал -- ровно то, что человек
пересылает, когда что-то сломалось. Токен по устройству проекта живёт
только в sar_config.json и не попадает ни в репозиторий, ни в архив
обновлений; утечка через собственный журнал обходила всю эту осторожность.
"""
import logging
import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parent.parent / "sar_telegram_bot.py"
       ).read_text(encoding="utf-8")


def test_httpx_logging_is_silenced():
    assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in SRC


def test_httpcore_is_silenced_too():
    """httpcore пишет то же самое уровнем ниже."""
    assert 'logging.getLogger("httpcore").setLevel(logging.WARNING)' in SRC


def test_silencing_happens_before_the_logger_is_used():
    """После basicConfig, но до начала работы: иначе первые запросы успеют
    записаться с токеном."""
    i = SRC.index("logging.basicConfig")
    j = SRC.index('logging.getLogger("httpx")')
    k = SRC.index("def main(")
    assert i < j < k


def test_warning_level_still_lets_errors_through():
    """Глушим шум, а не диагностику: сбои сети остаются видны."""
    assert "logging.CRITICAL" not in SRC
    assert "disable(" not in SRC


def test_httpx_at_warning_drops_request_lines(caplog):
    """Проверка поведения, а не текста: на WARNING строка запроса не
    пишется, на INFO писалась бы."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").info(
            "HTTP Request: POST https://api.telegram.org/bot123:СЕКРЕТ/getUpdates")
    assert "СЕКРЕТ" not in caplog.text


def test_no_token_is_printed_anywhere_in_the_bot():
    """Страж на печать токена своими руками."""
    for m in re.finditer(r"(print|log\.\w+)\([^)]*", SRC):
        chunk = m.group(0)
        assert "bot_token" not in chunk, chunk[:80]
        assert "CFG[\"bot_token\"]" not in chunk, chunk[:80]
