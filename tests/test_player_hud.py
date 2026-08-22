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


def test_fullscreen_button_stays_where_the_native_one_was():
    """Просьба пользователя: как в обычном плеере, справа внизу."""
    m = re.search(r"#fs-toggle \{\{([^}]*)\}\}", PLAYER)
    assert m, "правило кнопки полного экрана не найдено"
    rule = m.group(1)
    assert "bottom:" in rule and "right:" in rule
    assert "top:" not in rule, "кнопка снова наверху"


def test_fullscreen_button_does_not_cover_the_speed_menu():
    """В меню «⋮» живёт замедленное воспроизведение -- налезать нельзя."""
    m = re.search(r"#fs-toggle \{\{([^}]*)\}\}", PLAYER)
    assert "right:46px" in m.group(1), (
        "кнопка прижата к краю и накроет меню со скоростью")


def test_fullscreen_button_behaves_like_native_controls():
    """Иначе она одна висела бы поверх кадра, когда все прочие кнопки
    спрятаны."""
    assert ".video-wrap:hover #fs-toggle" in PLAYER
    assert ".video-wrap.paused #fs-toggle" in PLAYER
    assert "function syncPaused" in PLAYER


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
