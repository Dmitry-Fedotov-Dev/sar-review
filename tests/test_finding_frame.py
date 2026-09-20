"""Страница кадра находки, крупное превью и постоянная ссылка.

Жалобы, из которых это выросло:

  * в окне предпросмотра при увеличении видно кашу -- кадр резался в
    960 px, а окно увеличивает до восьми крат;
  * курсор показывает лупу, но клик ничего не делает: зум был только
    колесом, что на тачпаде неудобно;
  * находку негде РАЗГЛЯДЫВАТЬ: окно предпросмотра живёт по наведению
    курсора и захлопывается, стоит его увести;
  * ссылку на находку некому дать -- внешний адрес платформы меняется
    при каждом перезапуске туннеля.
"""
import io
import json
import os
import re

import pytest

import sar_common
import sar_server
import sar_worker


PAGE = sar_server.FINDING_FRAME_HTML
CARD = sar_server.OPERATION_CARD_HTML


# --- крупный кадр ---------------------------------------------------------

def test_two_preview_files_are_distinct():
    """Мелкий и крупный кадр -- разные файлы, иначе один затирал бы другой."""
    small = sar_common.finding_preview_path("/tmp/data", 7)
    full = sar_common.finding_preview_path("/tmp/data", 7, full=True)
    assert small != full
    assert os.path.basename(full) == "obs_7_full.jpg"


def test_full_preview_is_large_enough_for_deep_zoom():
    """Окно предпросмотра увеличивает до 8 крат при ширине около 380 px.

    Значит в глубоком зуме отрисовывается около 3000 px. Кадр в 960 px
    растягивался бы втрое -- ровно та каша, с которой начали.
    """
    assert sar_worker.FINDING_PREVIEW_FULL_WIDTH >= 2000, (
        "крупный кадр недостаточно крупный, чтобы зум имел смысл")
    assert (sar_worker.FINDING_PREVIEW_FULL_WIDTH
            > sar_worker.FINDING_PREVIEW_WIDTH * 2)


def test_full_preview_has_higher_quality_than_thumbnail():
    assert sar_worker.FINDING_PREVIEW_FULL_QUALITY > 80


def test_neither_preview_has_a_burned_in_box():
    """Обе картинки чистые: рамку рисует интерфейс поверх, в SVG."""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sar_worker.py"), encoding="utf-8").read()
    body = src[src.index("def _generate_finding_preview"):]
    body = body[:body.index(chr(10) + "def ")]
    assert "_draw_box" not in body


# --- отдача крупного кадра ------------------------------------------------

def test_preview_endpoint_falls_back_to_small(tmp_path, monkeypatch):
    """У находок, снятых до появления крупного кадра, его на диске нет.
    Пустое окно предпросмотра хуже, чем окно с картинкой похуже."""
    data = tmp_path / "data"
    (data / "finding_previews").mkdir(parents=True)
    small = sar_common.finding_preview_path(str(data), 3)
    io.open(small, "wb").write(b"\xff\xd8small")
    # DATA_DIR появляется только в main(), до запуска сервера его нет
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data), raising=False)

    app = sar_server.app
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "тест"
    r = c.get("/api/finding/3/preview?full=1")
    assert r.status_code == 200
    assert r.data == b"\xff\xd8small"


# --- зум по клику ---------------------------------------------------------

def test_preview_window_zooms_on_click():
    """Регрессия: курсор показывал лупу, но клик не делал ничего."""
    assert "zoomPeek(e.shiftKey ? 1 / 1.6 : 1.6, e)" in CARD, (
        "клик по окну предпросмотра не приближает")


def test_click_is_told_apart_from_dragging():
    """Без порога перетаскивание заканчивалось бы приближением."""
    assert "press.moved > 5" in CARD


def test_wheel_zoom_is_kept():
    """Колесо просили оставить."""
    assert "zoomPeek(e.deltaY < 0 ? 1.25 : 1 / 1.25, e)" in CARD


# --- рамка поверх кадра ---------------------------------------------------

def test_box_is_drawn_as_svg_over_the_frame():
    """В SVG она остаётся чёткой при увеличении и её можно выключить."""
    assert 'class="peek-box"' in CARD
    assert "vector-effect:non-scaling-stroke" in CARD


def test_box_never_eats_clicks():
    """Перехватывая события, рамка съела бы зум и перетаскивание."""
    m = re.search(r"\.peek-box\{\{([^}]*)\}\}", CARD)
    assert m and "pointer-events:none" in m.group(1)


def test_findings_payload_carries_box_and_id():
    """Без них клиент не нарисует рамку и не откроет страницу кадра."""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sar_server.py"), encoding="utf-8").read()
    assert '"obs_id": _finding_obs_id(f)' in src
    assert '"bbox": _finding_bbox(f)' in src


def test_broken_box_does_not_break_the_list():
    """Одна кривая запись не должна уносить весь список находок."""
    assert sar_server._finding_bbox({"bbox": "не json"}) is None
    assert sar_server._finding_bbox({"bbox": "[1,2]"}) is None
    assert sar_server._finding_bbox({"bbox": None}) is None
    assert sar_server._finding_bbox(
        {"bbox": "[0.1,0.2,0.3,0.4]"}) == [0.1, 0.2, 0.3, 0.4]


# --- страница кадра -------------------------------------------------------

def test_frame_page_can_hide_the_box():
    """Обводка притягивает взгляд: посмотреть своими глазами иначе нельзя."""
    assert 'id="showbox"' in PAGE
    assert "box.style.display = e.target.checked ? '' : 'none'" in PAGE


def test_frame_page_loads_the_full_frame():
    assert "preview?full=1" in PAGE or "{img_src}" in PAGE


def test_frame_page_links_into_the_player():
    assert "{player_btn}" in PAGE


def test_frame_page_has_presence_heartbeat():
    """Страница без пульса занижала бы счётчик работающих -- этим уже
    обжигались, когда пульса не было на половине страниц."""
    assert "FINDING_FRAME_HTML" in sar_server.PAGES_WITH_PRESENCE
    assert "/api/heartbeat" in PAGE


def test_row_button_is_not_a_nested_link():
    """Строка находки уже обёрнута в <a>. Ссылка внутри ссылки невалидна и
    разбирается браузерами непредсказуемо -- в проекте это уже ломало
    кнопку плеера в списке файлов."""
    m = re.search(r'class="find-open"[^>]*>', CARD)
    assert m, "кнопка открытия кадра не найдена"
    assert not m.group(0).lstrip().startswith("<a"), "кнопка стала ссылкой"
    assert "openFrame(event," in CARD
    assert "e.stopPropagation()" in CARD


# --- постоянная ссылка ----------------------------------------------------

def test_external_base_is_read_from_config(tmp_path, monkeypatch):
    cfg = {"telegram_bot": {"service_url": "https://example.trycloudflare.com/"}}
    io.open(tmp_path / "sar_config.json", "w", encoding="utf-8").write(
        json.dumps(cfg, ensure_ascii=False))
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", str(tmp_path))
    sar_server._EXTERNAL_BASE_CACHE.update({"url": None, "at": 0.0})
    assert sar_server.external_base() == "https://example.trycloudflare.com"


def test_external_base_survives_missing_config(tmp_path, monkeypatch):
    """Конфига может не быть -- ссылка тогда просто относительная, но
    страница находки обязана открыться."""
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", str(tmp_path))
    sar_server._EXTERNAL_BASE_CACHE.update({"url": None, "at": 0.0})
    assert sar_server.external_base() == ""


def test_external_base_is_not_frozen_at_startup():
    """Адрес быстрого туннеля меняется при каждом падении канала, а сервер
    при этом не перезапускается. Прочитанное один раз значение протухало бы,
    и кнопка отдавала бы мёртвую ссылку -- ровно то, против чего она."""
    assert sar_server._EXTERNAL_BASE_TTL <= 60, (
        "кэш внешнего адреса живёт слишком долго, ссылка успеет протухнуть")


def test_copy_link_has_a_fallback_without_clipboard():
    """Буфер обмена недоступен без https, а платформа ходит по http внутри
    сети. Молчаливое «ничего не произошло» -- худший исход."""
    assert "navigator.clipboard.writeText" in PAGE
    assert "window.prompt" in PAGE


# --- полная карточка находки ----------------------------------------------

def test_frame_page_shows_description_priority_and_discussion():
    """Просили всё, что есть у находки, а не только кадр."""
    assert "{note_block}" in PAGE, "нет описания"
    assert 'id="prio"' in PAGE, "нет статуса проверки"
    assert "{coords_block}" in PAGE, "нет координат"
    assert 'id="cmts"' in PAGE, "нет обсуждения"
    assert "{comment_form}" in PAGE, "нет формы комментария"


def test_discussion_uses_the_same_endpoint_as_the_player():
    """Синхронность обсуждения не делается отдельным механизмом: страница
    и плеер пишут в одну таблицу через один эндпоинт, поэтому расходиться
    им попросту нечем."""
    assert "/comments" in PAGE
    assert "kind: 'manual'" in PAGE
    assert "ref_key: String(OBS_ID)" in PAGE


def test_priority_uses_the_same_endpoint_as_the_player():
    assert "/priorities" in PAGE
    assert "x.kind === 'manual'" in PAGE


def test_discussion_refreshes_itself():
    """Разговор общий с плеером: чужие сообщения должны появляться сами."""
    assert "setInterval(loadComments" in PAGE


def test_comment_form_is_hidden_from_those_who_may_not_write():
    """Аноним по общему паролю смотрит и размечает, но в обсуждении не
    участвует -- иначе лишённый слова просто зашёл бы по общему паролю."""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sar_server.py"), encoding="utf-8").read()
    route = src[src.index("def finding_frame("):]
    route = route[:route.index(chr(10) + chr(10) + chr(10))]
    assert "can_comment()" in route
    assert "ROLE_MUTED" in route


def test_only_author_or_moderator_sees_delete():
    assert "c.author === VIEWER_NAME || IS_MODERATOR" in PAGE


def test_drone_position_is_not_passed_off_as_the_object():
    """Позиция дрона и вероятная точка объекта -- РАЗНЫЕ вещи. При высоте
    больше километра и наклоне подвеса они расходятся на километры, и
    подписать одно другим значит увести поиск в соседнее ущелье."""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sar_server.py"), encoding="utf-8").read()
    route = src[src.index("def finding_frame("):]
    route = route[:route.index("return FINDING_FRAME_HTML")]
    assert "позиция дрона" in route
    assert "вероятная точка объекта" in route


# --- кнопки ссылки --------------------------------------------------------

def test_link_button_in_the_findings_table():
    assert "copyFindingLink(event," in CARD


def test_link_uses_the_external_address_not_the_current_one():
    """Платформа живёт за туннелем, и вкладка может быть открыта по
    локальному адресу. Ссылка из location была бы бесполезна другому."""
    assert "/api/external_base" in CARD
    assert "EXTERNAL_BASE || location.origin" in CARD


def test_scene_without_its_own_page_still_gets_a_usable_link():
    """У сцены модели своей страницы кадра нет -- ведём в плеер на таймкод,
    а не отдаём пустую ссылку."""
    assert "if (obsId) return" in CARD
    assert "/player/" in CARD


def test_findings_tab_has_its_own_address():
    """Без адреса у вкладки нельзя дать ссылку на находки: человек открывал
    страницу и должен был сам догадаться нажать нужную вкладку."""
    assert "location.search).get('tab')" in CARD
    assert "searchParams.set('tab', tab)" in CARD
    assert "history.replaceState" in CARD, "адрес меняется с перезагрузкой"


# --- вечная ссылка --------------------------------------------------------

def test_share_link_goes_through_the_bot(monkeypatch):
    """Прямой адрес платформы живёт до следующего перезапуска туннеля:
    имя случайное, старое исчезает из DNS, перенаправить с него нельзя --
    домена больше нет, запрос до нас не доходит. Адрес t.me постоянен."""
    sar_server._EXTERNAL_BASE_CACHE.update(
        {"url": "https://x.trycloudflare.com", "bot": "sar_bot", "at": 9e18})
    assert sar_server.finding_share_link(38) == "https://t.me/sar_bot?start=finding_38"


def test_share_link_falls_back_to_direct_address(monkeypatch):
    """Пока бот не сообщил своё имя, прямая ссылка хотя бы работает сейчас."""
    sar_server._EXTERNAL_BASE_CACHE.update(
        {"url": "https://x.trycloudflare.com", "bot": "", "at": 9e18})
    assert sar_server.finding_share_link(38) == "https://x.trycloudflare.com/finding/38/"


def test_bot_understands_the_permanent_link():
    """Полезная нагрузка finding_<id> должна разбираться ботом обратно
    в путь внутри платформы."""
    import sar_telegram_bot as bot
    assert bot.parse_start_payload(["finding_38"]) == "/finding/38/"
    assert bot.parse_start_payload(["finding_abc"]) is None
    assert bot.parse_start_payload([]) is None
    assert bot.parse_start_payload(["мусор"]) is None


def test_bot_link_lands_on_the_finding_not_on_the_front_page():
    """Иначе человек, пришедший по ссылке на находку, оказался бы на общем
    экране и должен был искать её сам."""
    import sar_telegram_bot as bot
    bot.CFG = {"service_url": "https://x.test/", "bot_username": "sar_bot"}
    bot.ensure_token = lambda chat_id: "TOKEN"
    url = bot.personal_link(1, "/finding/38/")
    assert url.startswith("https://x.test/login?key=TOKEN")
    assert "next=%2Ffinding%2F38%2F" in url, "адрес находки не передан во вход"

    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sar_telegram_bot.py"), encoding="utf-8").read()
    assert "await send_access(context.bot, chat_id, next_path)" in src, (
        "/start с параметром не ведёт на находку")


def test_deleted_finding_gets_an_explanation_not_a_bare_404():
    """Голый текст оставлял человека в тупике: непонятно, ошибся ли он,
    сломалась ли платформа и куда идти дальше."""
    page = sar_server.FINDING_GONE_HTML
    assert "/operations" in page, "некуда уйти со страницы"
    assert "{obs_id}" in page, "не сказано, какой находки нет"


def test_target_survives_waiting_for_approval(tmp_path, monkeypatch):
    """Человек может прийти по вечной ссылке на находку, ещё не имея
    доступа, и ждать одобрения часами. Держать цель в памяти процесса
    нельзя: сторож туннеля перезапускает бота при каждом обрыве канала,
    и цель терялась бы чаще, чем срабатывала."""
    import sqlite3
    import sar_telegram_bot as bot

    db = str(tmp_path / "t.db")
    sar_common.init_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO telegram_access_requests "
                 "(chat_id, status, requested_at) VALUES (?,?,?)",
                 (777, "pending", "2026-08-31T20:00:00"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(bot, "_db",
                        lambda: sar_common.get_db_connection(db))

    bot.remember_target(777, "/finding/38/")
    assert bot.take_target(777) == "/finding/38/"
    # цель одноразовая: второй раз её быть не должно
    assert bot.take_target(777) is None


def test_approval_leads_to_the_remembered_finding():
    """Иначе одобренный позже попадал бы на общий экран, а находку,
    ради которой пришёл, искал бы сам."""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sar_telegram_bot.py"), encoding="utf-8").read()
    assert "await send_access(context.bot, chat_id, take_target(chat_id))" in src
