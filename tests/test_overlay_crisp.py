"""Рамки разметки не должны портиться при увеличении кадра.

Жалоба: при зуме бокс ручной разметки выглядит «шакально». Рамки и так
рисуются в SVG, то есть вектором, -- дело было в двух других вещах.

1. На сцену стояло will-change:transform. Оно поднимает её в отдельный
   слой, который браузер растрирует ОДИН раз, а дальше просто увеличивает
   получившуюся картинку. Видео -- растр, ему всё равно; вектор рамок
   превращался в лесенку.

2. Обводка масштабировалась вместе с кадром: при восьмикратном увеличении
   линия в 2px становилась 16px и закрывала собой то, что обводит. На
   поисковом инструменте закрывать находку рамкой нельзя.
"""
import re

import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML


def test_stage_is_not_cached_as_a_bitmap():
    """Собственно причина «шакальности»."""
    m = re.search(r"#stage \{\{([^}]*)\}\}", PLAYER)
    assert m, "правило сцены не найдено"
    assert "will-change" not in m.group(1), (
        "сцена снова кэшируется в битмап -- вектор рамок будет лесенкой")


def test_stroke_width_does_not_grow_with_zoom():
    """vector-effect:non-scaling-stroke -- штатное средство SVG ровно для
    этого."""
    assert "#overlay rect {{ vector-effect:non-scaling-stroke; }}" in PLAYER


def test_model_boxes_too():
    """У рамок модели стиль задаётся строкой в коде, и про них легко
    забыть."""
    assert "vector-effect:non-scaling-stroke;`)" in PLAYER


def test_labels_do_not_grow_with_zoom():
    """У текста нет non-scaling-stroke, поэтому размер делится на масштаб:
    иначе при 8x шрифт в 14px становится 112px и закрывает пол-кадра."""
    assert "font-size:calc(14px / var(--zoom, 1))" in PLAYER
    assert "font-size:calc(12px / var(--zoom, 1))" in PLAYER, (
        "подписи рамок модели растут вместе с кадром")


def test_zoom_is_published_to_the_overlay():
    """Без этого calc(... / var(--zoom)) всегда делит на единицу."""
    assert "overlay.style.setProperty('--zoom', vz.scale)" in PLAYER
    body = PLAYER[PLAYER.index("function applyStage"):]
    body = body[:body.index(chr(10) + "}}")]
    assert "--zoom" in body, "масштаб не обновляется при изменении зума"


def test_fallback_when_zoom_is_not_set_yet():
    """До первого applyStage переменной нет -- подписи не должны исчезнуть
    или стать нулевыми."""
    assert "var(--zoom, 1)" in PLAYER


def test_boxes_are_still_vector():
    """Растровая рамка испортилась бы при любом увеличении, как ни
    настраивай слой."""
    assert "createElementNS('http://www.w3.org/2000/svg', 'rect')" in PLAYER
    assert "<svg id=\"overlay\">" in PLAYER


def test_page_still_renders():
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    html = PLAYER.format(**{f: "X" for f in fields})
    assert len(html) > 10000
