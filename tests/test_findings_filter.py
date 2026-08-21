"""Отбор находок по статусу и навигация в окне предпросмотра.

Когда отметки триажа наконец стали видны, вылезло следствие: больше
половины из них -- "отклонено". Список находок превратился в перечень
того, что находкой НЕ оказалось. Прятать отклонённое насовсем нельзя --
в поиске к отвергнутому возвращаются, -- поэтому сделан отбор по статусу
с разумным умолчанием.

Отдельно: в окне предпросмотра увеличенный кадр можно было только зумить,
но не двигать. Край снимка становился недостижим, а находка нередко как
раз с краю.
"""
import re

import sar_server


CARD = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")


# --- отбор ----------------------------------------------------------------

def test_filter_exists_with_all_and_active():
    assert "function passesFilter" in CARD
    assert "'Актуальные'" in CARD, "нет отбора по умолчанию"
    assert "'Все'" in CARD, "нельзя показать всё"


def test_rejected_is_hidden_by_default_but_reachable():
    """Умолчание прячет отклонённое, но кнопка для него остаётся."""
    assert "findFilter = 'active'" in CARD, "по умолчанию показывается не то"
    assert "f.priority !== 'rejected'" in CARD
    assert "'rejected': '❌ отклонено'" in CARD, "нет кнопки для отклонённого"


def test_every_priority_has_a_button():
    """Просили отбор по всем приоритетам."""
    for key in ("confirmed_person", "likely_person", "confirmed_object",
                "likely_object", "anomaly", "rejected"):
        assert f"'{key}'" in CARD, f"нет кнопки для статуса {key}"


def test_buttons_show_counts():
    """Сколько разобрано и сколько нет -- полезно само по себе."""
    assert 'class="n"' in CARD


def test_empty_categories_are_not_shown():
    """Пустые рубрики создают ощущение, что чего-то не хватает."""
    assert "filter(k => counts[k])" in CARD


def test_filtering_happens_in_the_browser():
    """Находок десятки, а не тысячи: поход на сервер за каждым
    переключением был бы медленнее и заметнее."""
    assert "findings.filter(passesFilter)" in CARD
    assert "loadFindings" in CARD
    # отбор не должен превращаться в новый запрос
    m = re.search(r"function setFindFilter\(key\) \{([^}]*)\}", CARD)
    assert m and "fetch" not in m.group(1)


def test_empty_selection_says_it_is_a_selection():
    """«Находок пока нет» при непустом списке -- вранье: они есть, просто
    отобраны другие."""
    assert "В этом отборе находок нет" in CARD


# --- статус отдельной меткой ---------------------------------------------

def test_status_is_shown_as_its_own_tag():
    assert 'class="tag st' in CARD


def test_statuses_are_visually_distinct():
    """Подтверждённый человек и отклонённое не должны выглядеть одинаково."""
    assert ".tag.st.confirmed_person" in CARD
    assert ".tag.st.rejected" in CARD


# --- перетаскивание в окне предпросмотра ---------------------------------

def test_image_can_be_dragged_with_the_mouse():
    """Увеличенный кадр можно было только зумить: край снимка становился
    недостижим, а находка нередко как раз с краю."""
    assert "'mousedown'" in CARD
    assert "function panPeek" in CARD
    assert "peekDrag" in CARD


def test_drag_only_with_the_left_button():
    assert "e.button !== 0" in CARD


def test_drag_survives_the_cursor_leaving_the_window():
    """При быстром движении курсор выскакивает за край; перетаскивание не
    должно застревать, а окно -- закрываться посреди движения."""
    assert "document.addEventListener('mousemove'" in CARD
    assert "if (!peekDrag) hidePeek();" in CARD, (
        "окно закроется прямо во время перетаскивания")


def test_drag_works_by_touch_too():
    assert "'touchstart'" in CARD
    assert "e.touches.length === 1 && peekDrag" in CARD


def test_image_cannot_be_dragged_off_the_window():
    """Иначе легко «потерять» картинку и смотреть в пустоту."""
    assert "function applyPeekTransform" in CARD
    m = re.search(r"function applyPeekTransform\(\) \{(.*?)\n\}", CARD, re.S)
    assert m and "Math.min(0, Math.max(" in m.group(1)


def test_cursor_shows_that_dragging_is_possible():
    assert "'grab'" in CARD and "'grabbing'" in CARD


def test_hint_mentions_dragging():
    assert "перетаскивание" in CARD
