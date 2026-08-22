"""Управление плеером: кнопки, клавиатура, разметка.

Три бага, о которых сообщил пользователь, и все три -- следствия того, что
нативные элементы управления живут ВНУТРИ <video>, а слой разметки лежит
поверх него:

1. Две кнопки полного экрана -- своя и нативная.
2. В режиме разметки не нажимались кнопки плеера: слою включали
   pointer-events:auto, и он накрывал полосу управления целиком. Ровно эта
   грабля описана в CLAUDE.md -- и она вернулась.
3. Пробел не ставил паузу: нативные горячие клавиши <video> работают,
   только когда фокус на самом видео, а человек его туда не ставит.

Отдельно проверяется то, что чинить БЫЛО НЕ НУЖНО: замедленное
воспроизведение живёт в нативном меню "⋮" и работает. Первая версия
требований объявила его отсутствующим -- это была ошибка, и повторять её
не стоит.
"""
import re

import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML


def rendered():
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    return PLAYER.format(**{f: "X" for f in fields})


# --- 1. одна кнопка полного экрана ---------------------------------------

def test_no_fullscreen_button_at_all():
    """Кнопки полного экрана нет ни своей, ни нативной.

    Нативная развернула бы САМ <video>, потеряв слой разметки, и починить
    это на лету нельзя. Своя пробовалась дважды -- в полосе браузера она
    уезжала, потому что полоса не сообщает геометрии, а поверх кадра
    выглядела чужеродно. Полный экран открывается двойным кликом и
    клавишей F.
    """
    assert 'id="fs-toggle"' not in PLAYER
    assert "media-controls-fullscreen-button" in PLAYER, "нативная не скрыта"
    assert "clickCatch.addEventListener('dblclick'" in PLAYER, (
        "полный экран стал недоступен мышью")


def test_fullscreen_expands_the_whole_wrapper():
    """Разворачивается обёртка, внутри которой и видео, и слой разметки."""
    assert "videoWrap.requestFullscreen" in PLAYER


def test_video_going_fullscreen_alone_is_caught_and_reported():
    """Если браузер всё-таки развернул само видео мимо нашей кнопки --
    выходим и говорим вслух, а не молчим."""
    assert "document.fullscreenElement === video" in PLAYER
    assert "развернулось без слоя разметки" in PLAYER


def test_slow_playback_stays_available():
    """Замедление -- требование методики отсмотра.

    Оно живёт в штатном меню "⋮". Своя полоса со списком скоростей
    пробовалась и убрана: поверх кадра она выглядела чужеродно. Значит
    меню трогать нельзя.
    """
    html = rendered()
    assert "media-controls-overflow-button" not in html, (
        "спрятано меню, в котором живёт замедление")
    assert "controls" in html, "нативная панель управления убрана целиком"


# --- 2. кнопки плеера нажимаются в режиме разметки ------------------------

def test_overlay_never_catches_clicks():
    """Слой разметки накрывал полосу управления собой."""
    m = re.search(r"#overlay \{\{([^}]*)\}\}", PLAYER)
    assert m, "правило слоя разметки не найдено"
    assert "pointer-events:none" in m.group(1)
    assert "#overlay.draw-mode" not in PLAYER, (
        "слою снова включают перехват кликов -- кнопки плеера перестанут "
        "нажиматься при включённой разметке")


def test_clicks_are_caught_by_a_separate_layer():
    assert 'id="draw-catch"' in PLAYER
    assert "drawCatch.addEventListener('mousedown'" in PLAYER


def test_catcher_stops_above_the_controls():
    """Полоса управления должна остаться свободной."""
    m = re.search(r"#draw-catch \{\{([^}]*)\}\}", PLAYER)
    assert m, "правило ловушки кликов не найдено"
    assert "bottom:var(--controls-h)" in m.group(1), (
        "ловушка достаёт до низа и снова накроет кнопки плеера")


def test_overlay_keeps_its_full_size():
    """У слоя нельзя менять размер: по нему считаются нормализованные
    координаты рамок, и укоротишь его -- поедут все сохранённые пометки."""
    m = re.search(r"#overlay \{\{([^}]*)\}\}", PLAYER)
    assert "width:100%" in m.group(1) and "height:100%" in m.group(1)


def test_drag_can_be_finished_below_the_catcher():
    """Ловушка не достаёт до низа, поэтому движение и отпускание мыши
    слушаются на документе -- иначе рамку нельзя дотянуть до нижнего края
    кадра."""
    assert "document.addEventListener('mousemove'" in PLAYER
    assert "document.addEventListener('mouseup'" in PLAYER


# --- 3. клавиатура --------------------------------------------------------

def test_space_toggles_playback():
    """Переключение вынесено в togglePlayback: там же обрабатывается отказ
    браузера начать воспроизведение."""
    assert "togglePlayback();" in PLAYER
    body = PLAYER[PLAYER.index("function togglePlayback"):]
    body = body[:body.index(chr(10) + "}}")]
    assert "video.pause()" in body and "video.play()" in body


def test_space_does_not_also_scroll_the_page():
    body = PLAYER[PLAYER.index("if (e.key !== ' ' && e.code !== 'Space') return;"):]
    body = body[:body.index("}}, true);")]
    assert "e.preventDefault()" in body, "страница уедет вниз на экран"


def test_space_is_intercepted_before_anything_else():
    """Пробел -- это ещё и «нажать кнопку в фокусе». Нажав кнопку «Режим
    разметки» мышью, человек оставляет на ней фокус, и следующий пробел
    переключал режим вместо паузы. Перехват в фазе capture отдаёт пробел
    видео раньше, чем до него доберётся кнопка."""
    body = PLAYER[PLAYER.index("if (e.key !== ' ' && e.code !== 'Space') return;"):]
    body = body[:body.index("}}, true);")]
    assert "e.stopPropagation()" in body, "кнопка в фокусе перехватит пробел"
    assert "}}, true);" in PLAYER, "обработчик не в фазе перехвата"


def test_space_works_in_draw_mode():
    """Отдельно оговорено пользователем: пробел обязан управлять видео
    ВСЕГДА, в том числе при включённой разметке."""
    body = PLAYER[PLAYER.index("if (e.key !== ' ' && e.code !== 'Space') return;"):]
    body = body[:body.index("}}, true);")]
    assert "drawMode" not in body, (
        "пробел завязан на режим разметки -- он должен работать всегда")
    assert "drawToggle.blur();" in PLAYER, (
        "фокус остаётся на кнопке разметки, и пробел будет нажимать её")


def test_shortcuts_do_not_fire_while_typing():
    """Пробел посреди комментария обязан ставить пробел, а не паузу."""
    assert "function typingNow" in PLAYER
    body = PLAYER[PLAYER.index("function typingNow"):]
    body = body[:body.index("\n}}")]
    for tag in ("INPUT", "TEXTAREA", "SELECT"):
        assert tag in body, f"{tag} не защищён от горячих клавиш"
    assert "isContentEditable" in body


def test_shortcuts_ignore_browser_combinations():
    """Ctrl+F -- поиск браузера, а не полный экран."""
    assert "e.ctrlKey || e.metaKey || e.altKey" in PLAYER


def test_arrows_seek_and_shift_seeks_further():
    assert "nudge(e.shiftKey ? -10 : -5)" in PLAYER
    assert "nudge(e.shiftKey ? 10 : 5)" in PLAYER


def test_seeking_stays_inside_the_video():
    body = PLAYER[PLAYER.index("function nudge"):]
    body = body[:body.index("\n}}")]
    assert "Math.max(" in body and "Math.min(" in body, (
        "перемотка может уехать за границы видео")


def test_shortcuts_work_with_a_russian_layout():
    """Человек в поле не переключает раскладку ради горячей клавиши."""
    assert "'а'" in PLAYER and "'ь'" in PLAYER


def test_escape_drops_an_unfinished_box():
    assert "if (drawing) {{ drawing.rectEl.remove(); drawing = null; }}" in PLAYER


def test_keys_are_documented_on_screen():
    """Без подсказки о клавишах никто не узнает, и работа окажется
    впустую.

    Подсказка переехала из панели разметки в легенду ПОД видео -- вместе с
    описанием масштаба, когда кнопки масштаба убрали с кадра."""
    html = rendered()
    assert 'class="legend"' in html, "легенда управления пропала"
    for needle in ("пробел", "разметка", "во весь экран"):
        assert needle in html, f"в легенде нет про «{needle}»"


def test_page_still_renders():
    assert len(rendered()) > 10000
