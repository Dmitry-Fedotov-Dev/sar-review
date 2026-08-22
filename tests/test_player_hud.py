"""Кадр без лишнего поверх него, и статус находки сразу при разметке.

Блок кнопок масштаба стоял поверх видео и отнимал у кадра угол -- ровно у
той картинки, ради которой плеер и существует. Масштаб делается колесом и
клавишами, а справочная информация вынесена в легенду ПОД видео.

Кнопка полного экрана осталась кнопкой: без неё полный экран был бы
доступен только с клавиатуры. Стоит там же, где стояла нативная -- справа
внизу.

Отдельно: статус находки ставится прямо в форме разметки. Человек в момент
разметки уже знает, насколько он уверен; заставлять его возвращаться к
этому позже значит терять оценку -- именно так половина пометок и
оставалась без статуса.
"""
import json
import os
import re

import pytest

import sar_common
import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML


def rendered():
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    return PLAYER.format(**{f: "X" for f in fields})


# --- кадр свободен --------------------------------------------------------

def test_zoom_buttons_are_gone_from_the_frame():
    for gone in ('id="zoom-in"', 'id="zoom-out"', 'id="zoom-level"'):
        assert gone not in PLAYER, f"{gone} снова закрывает кадр"


def test_zoom_still_works_by_wheel_and_keys():
    """Кнопки убрали, а сам масштаб остаться обязан."""
    assert "'wheel'" in PLAYER
    assert "zoomCentre(1.4)" in PLAYER, "масштаб с клавиатуры пропал"
    assert "'touchmove'" in PLAYER


def test_native_fullscreen_button_is_hidden():
    """Нативная кнопка разворачивает САМ <video>, а слой разметки -- его
    сосед, и в полноэкранном видео его не существует.

    Обойти это нельзя: попытка «выйти и развернуть обёртку» не работает,
    потому что requestFullscreen требует свежего действия пользователя, а
    после асинхронного выхода оно уже истекло. Запрос отклонялся, отказ
    проглатывался пустым catch -- и кнопка выглядела сломанной и на
    включение, и на выключение.
    """
    assert "media-controls-fullscreen-button" in PLAYER
    assert 'controlsList="nofullscreen"' in PLAYER


def test_no_custom_controls_on_the_frame():
    """Своя полоса поверх кадра выглядела чужеродно и загораживала видео.
    Убрана целиком: полный экран открывается двойным кликом, а параметры
    воспроизведения живут в штатном меню плеера."""
    assert 'id="vbar"' not in PLAYER
    assert 'id="fs-toggle"' not in PLAYER
    assert 'id="speed"' not in PLAYER


def test_native_settings_menu_is_left_alone():
    """В меню «⋮» живут параметры воспроизведения, включая замедление,
    которое требует методика отсмотра."""
    assert "media-controls-overflow-button" not in PLAYER, (
        "штатное меню параметров снова спрятано")


def test_native_fullscreen_button_stays_hidden():
    """Она разворачивает САМ <video>, а слой разметки -- его сосед, и в
    полноэкранном видео его не существует."""
    assert "media-controls-fullscreen-button" in PLAYER


def test_fullscreen_failures_are_not_silent():
    """Молчаливый отказ неотличим от сломанной кнопки -- на этом и
    обожглись."""
    body = PLAYER[PLAYER.index("document.addEventListener('fullscreenchange'"):]
    body = body[:body.index("}});")]
    assert "catch(() => {{}})" not in body
    assert "console.warn" in body


def test_click_toggles_playback():
    """Chrome не переключает воспроизведение по клику на встроенное в
    страницу видео -- это делают своими руками все плееры, где такое
    поведение есть. Ожидание при этом естественное."""
    assert 'id="click-catch"' in PLAYER
    body = PLAYER[PLAYER.index("clickCatch.addEventListener('click'"):]
    body = body[:body.index("}});")]
    assert "togglePlayback()" in body


def test_click_layer_does_not_cover_the_controls():
    """Клики по нативной полосе приходят на тот же элемент: нажатие на
    громкость заодно ставило бы видео на паузу."""
    import re as _re
    m = _re.search(r"#click-catch \{\{([^}]*)\}\}", PLAYER)
    assert m, "правило слоя кликов не найдено"
    assert "bottom:var(--controls-h)" in m.group(1)


def test_double_click_opens_fullscreen_and_cancels_the_single_one():
    """Иначе двойной клик дважды дёрнет воспроизведение по дороге к
    полному экрану."""
    body = PLAYER[PLAYER.index("clickCatch.addEventListener('dblclick'"):]
    body = body[:body.index("}});")]
    assert "toggleFullscreen()" in body
    assert "clearTimeout(clickTimer)" in body


def test_click_layer_is_off_while_drawing():
    """Там протягивание мышью рисует рамку, и ловушка разметки должна
    получать события первой."""
    assert "function syncClickCatch" in PLAYER
    body = PLAYER[PLAYER.index("drawToggle.addEventListener('click'"):]
    body = body[:body.index("}});")]
    assert "syncClickCatch()" in body


def test_legend_mentions_double_click():
    assert "двойной клик" in rendered()


# --- легенда --------------------------------------------------------------

def test_legend_is_under_the_video():
    html = rendered()
    legend = html.index('class="legend"')
    video = html.index("<video")
    assert legend > video, "легенда оказалась над видео"


def test_legend_explains_what_the_buttons_used_to_show():
    html = rendered()
    for needle in ("колесо мыши", "пробел", "масштаб", "во весь экран"):
        assert needle in html, f"в легенде нет про «{needle}»"


def test_legend_is_quiet():
    """Она справочная: читается один раз и дальше не должна тянуть взгляд
    с кадра."""
    m = re.search(r"\.legend \{\{([^}]*)\}\}", PLAYER)
    assert m and "font-size:11.5px" in m.group(1)


# --- статус в форме -------------------------------------------------------

def test_form_has_a_status_field():
    assert 'id="obs-priority-input"' in PLAYER


def test_status_options_come_from_the_shared_dictionary():
    """Свой список тут неминуемо разошёлся бы с серверным при первой
    правке."""
    assert "Object.keys(PRIORITY_LABELS)" in PLAYER


def test_status_options_are_built_lazily():
    """PRIORITY_LABELS объявлен ниже по файлу. Обращение к нему на верхнем
    уровне падает с ReferenceError (const в temporal dead zone) и роняет
    весь скрипт плеера целиком -- поймано при проверке."""
    assert "function priorityOptions" in PLAYER
    js = rendered()
    assert js.index("const PRIORITY_LABELS") > js.index("function priorityOptions"), (
        "порядок изменился -- проверить, что обращение осталось ленивым")


def test_status_is_sent_with_the_observation():
    assert "priority: document.getElementById('obs-priority-input').value" in PLAYER


# --- сквозная проверка: статус реально сохраняется ------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "                      created_at, updated_at) "
        "VALUES ('r1', 'DJI_1.MP4', ?, 'video', 'done', "
        "        '2026-08-15T10:00', '2026-08-15T10:00')",
        (str(watch / "DJI_1.MP4"),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(watch), raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "Айгуль"
    c.db = db
    return c


def post_obs(client, **extra):
    body = {"timestamp_sec": 12.5, "bbox": [0.1, 0.1, 0.2, 0.2],
            "label": "резко чёрное", "note": ""}
    body.update(extra)
    return client.post("/api/report/r1/observations", json=body)


def test_status_is_saved_together_with_the_observation(client):
    r = post_obs(client, priority="confirmed_person")
    assert r.status_code == 200
    assert r.get_json()["id"], "id новой пометки не возвращён"

    conn = sar_common.get_db_connection(client.db)
    row = conn.execute(
        "SELECT kind, ref_key, priority, set_by FROM detection_priorities").fetchone()
    conn.close()
    assert row is not None, "статус не сохранился"
    assert row["kind"] == "manual"
    assert row["priority"] == "confirmed_person"
    assert row["set_by"] == "Айгуль", "статус приписан не тому человеку"


def test_status_points_at_the_new_observation(client):
    obs_id = post_obs(client, priority="anomaly").get_json()["id"]
    conn = sar_common.get_db_connection(client.db)
    ref = conn.execute("SELECT ref_key FROM detection_priorities").fetchone()[0]
    conn.close()
    assert str(ref) == str(obs_id), "статус привязан к чужой пометке"


def test_observation_without_a_status_stays_unmarked(client):
    """Пустой статус -- это «пока не знаю», а не повод что-то записывать."""
    assert post_obs(client, priority="").status_code == 200
    conn = sar_common.get_db_connection(client.db)
    n = conn.execute("SELECT COUNT(*) FROM detection_priorities").fetchone()[0]
    conn.close()
    assert n == 0


def test_made_up_status_is_ignored(client):
    """Значение приходит из браузера -- принимать что попало нельзя."""
    assert post_obs(client, priority="выдумка").status_code == 200
    conn = sar_common.get_db_connection(client.db)
    n = conn.execute("SELECT COUNT(*) FROM detection_priorities").fetchone()[0]
    conn.close()
    assert n == 0, "в базу записан статус, которого не существует"


def test_observation_is_saved_even_without_priority_field(client):
    """Старые клиенты статус не присылают вовсе."""
    assert post_obs(client).status_code == 200
    conn = sar_common.get_db_connection(client.db)
    n = conn.execute("SELECT COUNT(*) FROM manual_observations").fetchone()[0]
    conn.close()
    assert n == 1


# --- переход к находке ----------------------------------------------------

def test_jumping_to_a_finding_pauses_the_video():
    """Человек открывает находку, чтобы её разглядеть. Если видео продолжит
    играть, момент тут же уедет, и отматывать придётся каждый раз."""
    body = PLAYER[PLAYER.index("function jumpTo(sec)"):]
    body = body[:body.index(chr(10) + "}}")]
    assert "video.pause()" in body
    assert "video.play()" not in body, "видео снова уезжает от находки"


def test_pause_happens_before_the_seek():
    """Иначе между перемоткой и паузой успевает проиграться несколько
    кадров, и на экране оказывается не тот момент."""
    body = PLAYER[PLAYER.index("function jumpTo(sec)"):]
    body = body[:body.index(chr(10) + "}}")]
    assert body.index("video.pause()") < body.index("video.currentTime = sec")


def test_boxes_are_redrawn_after_the_jump():
    """Рамки рисуются по timeupdate, а на паузе оно не приходит -- без
    явной перерисовки пометка не появится, пока видео не тронут."""
    body = PLAYER[PLAYER.index("function jumpTo(sec)"):]
    body = body[:body.index(chr(10) + "}}")]
    assert "renderVisibleObservations" in body


def test_redraw_waits_for_the_seek_to_finish():
    """Регрессия: после перехода к находке рамка пропадала.

    Сразу после присваивания currentTime видео ещё может стоять на прежнем
    месте. Перерисовка начинается с ОЧИСТКИ всех рамок -- она стирала их и
    не рисовала новую, а следующего timeupdate на паузе не будет.
    """
    body = PLAYER[PLAYER.index("function jumpTo(sec)"):]
    body = body[:body.index(chr(10) + "}}")]
    assert "'seeked'" in body, "перерисовка не ждёт завершения перемотки"
    assert "once: true" in body


def test_redraw_also_happens_without_a_seek():
    """Если перематывать некуда -- уже на этом месте -- события seeked не
    будет вовсе, и рамка не появилась бы."""
    body = PLAYER[PLAYER.index("function jumpTo(sec)"):]
    body = body[:body.index(chr(10) + "}}")]
    assert body.count("renderVisibleObservations") >= 2
