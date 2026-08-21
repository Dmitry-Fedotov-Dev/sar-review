"""Форма пометки: рядом с рамкой, а не под плеером.

Жалоба пользователя: в полноэкранном режиме рамку нарисовать можно, а
заполнить заметку нечем -- форма осталась под плеером, то есть за пределами
того, что развёрнуто на весь экран. Пометка при этом остаётся
недосохранённой: человек обвёл находку и не может её описать.

Форма перенесена внутрь обёртки видео и открывается рядом с нарисованной
рамкой. Заодно исчезает лишний путь взглядом: глаз и так на кадре, уводить
его вниз страницы незачем.
"""
import re

import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML


def rendered():
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    return PLAYER.format(**{f: "X" for f in fields})


# --- где живёт форма ------------------------------------------------------

def test_form_is_inside_the_fullscreen_element():
    """Собственно баг: под плеером форма не видна в полном экране."""
    html = rendered()
    wrap = html[html.index('<div class="video-wrap"'):]
    wrap = wrap[:wrap.index("<div class=\"toolbar\"")]
    assert 'id="draw-form"' in wrap, (
        "форма снова вне обёртки видео -- в полном экране её не будет")


def test_old_slot_under_the_player_is_gone():
    assert 'id="draw-form-slot"' not in PLAYER


def test_form_is_not_scaled_by_zoom():
    """Форма снаружи #stage: внутри зум растягивал бы и её вместе с
    кадром, и при увеличении она стала бы нечитаемой."""
    html = rendered()
    stage = html[html.index('<div id="stage">'):]
    stage = stage[:stage.index("</div>")]
    assert 'id="draw-form"' not in stage


def test_form_follows_the_box():
    assert "function placeDrawForm" in PLAYER
    assert "pendingBox.rectEl.getBoundingClientRect()" in PLAYER


def test_form_stays_inside_the_frame():
    """Иначе у правого края кадра форма уедет за экран."""
    body = PLAYER[PLAYER.index("function placeDrawForm"):]
    body = body[:body.index("\n}}")]
    assert "Math.max(gap, Math.min(" in body


def test_form_moves_with_zoom():
    """При зуме рамка едет -- форма должна ехать за ней, иначе укажет не
    на то место."""
    body = PLAYER[PLAYER.index("function applyStage"):]
    body = body[:body.index("\n}}")]
    assert "placeDrawForm()" in body


# --- работа с формой ------------------------------------------------------

def test_catcher_is_disabled_while_the_form_is_open():
    """Прозрачный слой ловли кликов перекрывает и саму форму: по её полям
    нельзя было бы попасть мышью."""
    body = PLAYER[PLAYER.index("function showDrawForm"):]
    body = body[:body.index("function placeDrawForm")]
    assert "drawCatch.classList.remove('on')" in body


def test_catcher_returns_only_if_draw_mode_is_still_on():
    """Человек мог выключить разметку, пока форма была открыта."""
    body = PLAYER[PLAYER.index("function hideDrawForm"):]
    body = body[:body.index("\n}}")]
    assert "drawCatch.classList.toggle('on', drawMode)" in body


def test_enter_saves_and_escape_cancels():
    body = PLAYER[PLAYER.index("function showDrawForm"):]
    body = body[:body.index("function placeDrawForm")]
    assert "saveObservation()" in body
    assert "cancelDraw()" in body


def test_note_field_keeps_enter_for_new_lines():
    """Пометки бывают в несколько предложений: в заметке Enter обязан
    переносить строку, а сохраняет Ctrl+Enter."""
    body = PLAYER[PLAYER.index("obs-note-input').addEventListener"):]
    body = body[:body.index("\n}}")]
    assert "e.ctrlKey || e.metaKey" in body


def test_escape_outside_the_fields_also_cancels():
    """Фокус мог быть где угодно -- Esc должен работать всегда."""
    assert "if (pendingBox) cancelDraw();" in PLAYER


# --- сохранение -----------------------------------------------------------

def test_failed_save_is_not_swallowed():
    """Пометка -- это находка. Человек обязан узнать, что она не
    сохранилась, а не думать, что отметил."""
    body = PLAYER[PLAYER.index("async function saveObservation"):]
    body = body[:body.index("// --- отрисовка боксов")]
    assert "if (!res.ok) throw" in body
    assert "catch (e) {}" not in body
    assert "alert(" in body


def test_box_is_kept_when_saving_fails():
    """Иначе человек теряет и рамку, и текст, и находку заодно."""
    body = PLAYER[PLAYER.index("async function saveObservation"):]
    body = body[:body.index("// --- отрисовка боксов")]
    # именно ТЕЛО catch, а не всё до конца функции: после try/catch идёт
    # успешный путь, и он рамку убирает совершенно правильно
    err = body[body.index("}} catch (e) {{"):]
    err = err[:err.index("\n  }}\n")]
    assert "pendingBox = null" not in err, "рамка стирается при ошибке"
    assert "return;" in err, "после ошибки выполнение идёт дальше как при успехе"


def test_double_save_is_prevented():
    """Двойной клик по «Сохранить» не должен создавать две пометки."""
    body = PLAYER[PLAYER.index("async function saveObservation"):]
    body = body[:body.index("// --- отрисовка боксов")]
    assert "btn.disabled = true" in body


def test_page_still_renders():
    assert len(rendered()) > 10000
