"""Полный экран, масштаб и навигация плеера.

Два бага, о которых сообщил пользователь, растут из одного места. Разметка
плеера была такой:

    <div class="video-wrap">
      <video controls>      <- в полный экран уходил ТОЛЬКО он
      <svg id="overlay">    <- сосед, оставался в обычном документе

Нативная кнопка полного экрана разворачивает сам <video>, а слой разметки
-- его сосед. В полном экране слой оставался снаружи: рамки пропадали, а
рисовать было нечем. Зума же в плеере не было вовсе -- ни в окне, ни в
полном экране.

Третий баг -- навигационный: со страницы снимка кнопка вела «к списку
файлов», то есть в общую кучу всех материалов, мимо операции, из которой
человек пришёл.
"""
import re

import pytest

import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML
PHOTO = sar_server.PHOTO_VIEWER_HTML
PROC = sar_server.PROCESSING_PAGE_HTML


def rendered(tpl):
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", tpl))
    return tpl.format(**{f: "X" for f in fields})


# --- полный экран ---------------------------------------------------------

def test_video_and_overlay_live_in_one_scene():
    """Собственно причина обоих багов плеера."""
    html = rendered(PLAYER)
    stage = html[html.index('<div id="stage">'):]
    stage = stage[:stage.index("</div>")]
    assert "<video" in stage and "<svg" in stage, (
        "видео и слой разметки снова в разных контейнерах -- в полном "
        "экране слой останется снаружи")


def test_fullscreen_goes_to_the_wrapper_not_the_video():
    assert "videoWrap.requestFullscreen" in PLAYER
    assert "video.requestFullscreen" not in PLAYER, (
        "разворачивается сам <video> -- слой разметки останется снаружи")


def test_native_fullscreen_button_is_disabled():
    """Штатную кнопку пробовали оставить -- она разворачивает только
    <video>, а «выйти и развернуть обёртку» не срабатывает: к моменту
    повторного запроса действие пользователя уже истекло. Кнопка
    выглядела сломанной. Прячем её, свою ставим строкой ниже."""
    assert 'controlsList="nofullscreen"' in PLAYER
    assert "media-controls-fullscreen-button" in PLAYER


def test_there_is_a_fallback_if_the_browser_ignores_controlslist():
    """controlsList понимают не все браузеры. Если <video> всё-таки ушёл
    в полный экран сам -- разворачиваем вместо него обёртку."""
    assert "document.fullscreenElement === video" in PLAYER
    assert "exitFullscreen" in PLAYER


def test_fullscreen_is_reachable_by_mouse():
    """Своя кнопка убрана -- полный экран открывается штатной кнопкой
    плеера и двойным кликом по кадру."""
    assert "'dblclick'" in PLAYER
    assert "toggleFullscreen()" in PLAYER


# --- масштаб --------------------------------------------------------------

def test_zoom_exists_at_all():
    """Раньше зума в плеере не было вовсе.

    Кнопки масштаба с кадра убраны по просьбе пользователя: они отнимали у
    картинки угол. Сам масштаб остался -- колесом, щипком и с клавиатуры."""
    for needle in ("function zoomAt", "zoomCentre(1.4)", "'wheel'"):
        assert needle in PLAYER, f"нет {needle}"


def test_zoom_scales_the_scene_not_just_the_video():
    """Масштабировать одно видео нельзя: рамки разметки остались бы на
    прежнем месте и разошлись бы с картинкой."""
    assert "stage.style.transform" in PLAYER
    assert "video.style.transform" not in PLAYER


def test_zoom_by_wheel_and_pinch():
    assert "'wheel'" in PLAYER
    assert "'touchmove'" in PLAYER
    assert "Math.hypot" in PLAYER


def test_zoom_keeps_the_point_under_the_cursor():
    """Иначе при увеличении уезжает ровно то, что хотели рассмотреть."""
    assert "getBoundingClientRect" in PLAYER
    assert "vz.x = clientX - r.left - cx * next" in PLAYER


def test_zoom_is_bounded():
    assert "Math.min(8, Math.max(1," in PLAYER


def test_frame_cannot_be_dragged_out_of_sight():
    assert "function applyStage" in PLAYER
    body = PLAYER[PLAYER.index("function applyStage"):]
    body = body[:body.index("\n}}")]
    assert "Math.min(maxX, Math.max(minX" in body


def test_dragging_does_not_fight_with_drawing():
    """В режиме разметки протягивание мышью -- это рисование рамки,
    а не перетаскивание кадра."""
    assert "vz.scale <= 1 || drawMode" in PLAYER


# --- координаты при масштабе ---------------------------------------------

def test_coordinates_use_the_overlays_own_units():
    """getBoundingClientRect даёт размер НА ЭКРАНЕ, уже умноженный на
    масштаб, а SVG рисует в своих непреобразованных единицах. Смешаешь --
    и при любом зуме рамки уезжают."""
    assert "function overlaySize" in PLAYER
    assert "overlay.clientWidth" in PLAYER


def test_drawing_converts_screen_coordinates_to_scene():
    body = PLAYER[PLAYER.index("function overlayPoint"):]
    body = body[:body.index("\n}}")]
    assert "kx" in body and "ky" in body, (
        "экранные координаты попадают в SVG как есть -- при зуме рамка "
        "нарисуется не там, где её тянут")


def test_existing_boxes_are_redrawn_in_scene_units():
    body = PLAYER[PLAYER.index("function renderVisibleObservations"):]
    body = body[:body.index("\n}}")]
    assert "overlaySize()" in body, (
        "рамки рисуются по экранному размеру -- при зуме разойдутся "
        "с картинкой")


def test_boxes_are_redrawn_after_zoom():
    """Иначе рамки останутся нарисованными по старому размеру кадра."""
    body = PLAYER[PLAYER.index("function applyStage"):]
    body = body[:body.index("\n}}")]
    assert "renderVisibleObservations()" in body


# --- навигация ------------------------------------------------------------

@pytest.mark.parametrize("tpl,name", [
    (PHOTO, "просмотр снимка"),
    (PROC, "страница обработки"),
], ids=["фото", "обработка"])
def test_pages_do_not_send_back_to_the_flat_list(tpl, name):
    """Жалоба пользователя: со снимка кнопка вела «к списку файлов» --
    в кучу всех материалов, мимо операции, из которой он пришёл.

    Проверяем ССЫЛКУ, а не текст: сам текст ещё встречается в
    комментариях, объясняющих, как было раньше.
    """
    assert 'href="/"' not in tpl, (
        f"{name}: снова ведёт в общий список вместо операции")
    assert "{crumbs}" in tpl, f"{name}: нет пути назад в операцию"


def test_crumbs_lead_to_operations_and_the_operation():
    """material_crumbs строит путь Операции › Операция › файл."""
    import inspect
    src = inspect.getsource(sar_server.material_crumbs)
    assert '/operations' in src
    assert '/operation/%d/' in src


@pytest.mark.parametrize("tpl,name", [
    (PHOTO, "просмотр снимка"),
    (PROC, "страница обработки"),
], ids=["фото", "обработка"])
def test_crumbs_are_styled(tpl, name):
    """Без стилей крошки выглядят как случайная строка текста."""
    assert ".crumbs" in tpl, f"{name}: крошки без стилей"


def test_pages_still_render():
    for tpl in (PLAYER, PHOTO, PROC):
        html = rendered(tpl)
        assert len(html) > 500
