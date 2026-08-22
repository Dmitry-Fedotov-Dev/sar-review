"""Размеры слоя разметки: производитель и потребитель должны совпадать.

Баг, найденный пользователем: рамка не появлялась при переходе к находке.
На деле она не появлялась ВООБЩЕ НИКОГДА -- с момента, как отрисовку
перевели с overlay.getBoundingClientRect() на собственную overlaySize().

Функция возвращала {w, h}, а весь код отрисовки читает rect.width и
rect.height. Получался undefined, координата становилась NaN, и рамка не
рисовалась. Ошибка при этом НЕ бросается и в консоль ничего не пишет:
setAttribute спокойно принимает строку "NaN".

Прошлые тесты проверяли, что overlaySize() вызывается -- и проходили,
давая ложную уверенность. Проверять надо согласование имён.
"""
import re

import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML


def overlay_size_body():
    body = PLAYER[PLAYER.index("function overlaySize"):]
    return body[:body.index(chr(10) + "}}")]


def render_body():
    body = PLAYER[PLAYER.index("function renderVisibleObservations"):]
    return body[:body.index(chr(10) + "}}")]


def test_overlay_size_returns_width_and_height():
    """Собственно регрессия."""
    body = overlay_size_body()
    assert "width:" in body and "height:" in body, (
        "overlaySize снова возвращает другие имена -- отрисовка получит "
        "undefined и рамки исчезнут молча")


def test_overlay_size_does_not_return_short_names():
    body = overlay_size_body()
    assert not re.search(r"\{\{\s*w:", body), "вернулись короткие имена w/h"


def test_renderer_reads_the_same_names():
    """Главная проверка: то, что функция отдаёт, и то, что отрисовка
    читает, должно совпадать."""
    body = render_body()
    assert "rect.width" in body and "rect.height" in body
    assert "rect.w " not in body and "rect.h " not in body


def test_drawing_reads_the_same_names():
    body = PLAYER[PLAYER.index("function overlayPoint"):]
    body = body[:body.index(chr(10) + "}}")]
    assert "size.width" in body and "size.height" in body, (
        "разметка читает имена, которых overlaySize не отдаёт")


def test_no_leftover_short_field_reads():
    """Ищем по всему шаблону: обе половины должны говорить на одном языке."""
    assert "size.w," not in PLAYER
    assert "size.h," not in PLAYER


def test_size_comes_from_the_overlays_own_units():
    """Причина, по которой getBoundingClientRect тут не годится:
    он даёт размер НА ЭКРАНЕ, уже умноженный на масштаб сцены, а SVG
    рисует в своих непреобразованных единицах."""
    body = overlay_size_body()
    assert "overlay.clientWidth" in body and "overlay.clientHeight" in body


def test_page_still_renders():
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    html = PLAYER.format(**{f: "X" for f in fields})
    assert len(html) > 10000
