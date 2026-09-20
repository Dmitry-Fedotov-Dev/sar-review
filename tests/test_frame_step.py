"""Покадровая перемотка: Ctrl со стрелками.

Нужна, чтобы разглядеть сомнительное место, не перематывая туда-сюда по
пять секунд. У <video> собственного "шага" нет -- шаг это перемотка ровно
на длительность одного кадра, а её надо откуда-то взять.
"""
import json
import os
import re

import pytest

import sar_common
import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML


def step_body():
    body = PLAYER[PLAYER.index("function stepFrame"):]
    return body[:body.index(chr(10) + "}}")]


# --- сам шаг --------------------------------------------------------------

def test_frame_step_exists():
    assert "function stepFrame" in PLAYER


def test_step_is_one_frame_long():
    assert "const FRAME_SEC = 1 / VIDEO_FPS" in PLAYER
    assert "direction * FRAME_SEC" in step_body()


def test_step_pauses_first():
    """Шагать покадрово во время воспроизведения бессмысленно -- видео тут
    же уедет дальше само."""
    body = step_body()
    assert "video.pause()" in body
    assert body.index("video.pause()") < body.index("video.currentTime = at")


def test_step_is_skipped_while_a_seek_is_running():
    """Регрессия: после Ctrl+стрелки переставали работать и пауза, и
    воспроизведение.

    Быстрые нажатия ставили перемотки одну на другую, и <video> застревал
    в состоянии seeking -- переставая отвечать и нам, и собственным
    кнопкам браузера.
    """
    body = step_body()
    assert "if (video.seeking) return;" in body, (
        "новая перемотка начинается поверх незаконченной")


def test_step_needs_loaded_metadata():
    """Без длительности любое значение -- наугад."""
    body = step_body()
    assert "!isFinite(video.duration)" in body


def test_step_stays_inside_what_the_browser_can_seek():
    """У частично загруженного файла перемотать можно не весь ролик."""
    body = step_body()
    assert "video.seekable" in body


def test_failed_seek_is_visible():
    body = step_body()
    assert "console.warn" in body, "неудачная перемотка молчит"


def test_rejected_playback_is_visible():
    """play() возвращает обещание, которое браузер может отклонить. Без
    обработки отказ уходит в никуда, и это выглядит как «кнопка не
    работает»."""
    assert "function togglePlayback" in PLAYER
    body = PLAYER[PLAYER.index("function togglePlayback"):]
    body = body[:body.index(chr(10) + "}}")]
    assert "started.catch" in body
    assert "console.warn" in body


def test_step_stays_inside_the_video():
    body = step_body()
    assert "Math.max(0," in body and "Math.min(" in body


def test_boxes_are_redrawn_after_the_step():
    """На паузе timeupdate не приходит, а рамки рисуются по нему -- без
    явной перерисовки они застынут на прежнем кадре."""
    body = step_body()
    assert "'seeked'" in body
    assert "renderVisibleObservations" in body


# --- клавиши --------------------------------------------------------------

def test_ctrl_arrows_are_handled():
    assert "stepFrame(e.key === 'ArrowRight' ? 1 : -1)" in PLAYER


def test_ctrl_branch_runs_before_the_modifier_guard():
    """Общая проверка отсекает сочетания с Ctrl -- покадровый шаг как раз
    сочетание, и без отдельной ветки до него бы не дошло."""
    ctrl = PLAYER.index("(e.ctrlKey || e.metaKey) && !e.altKey")
    guard = PLAYER.index("if (e.ctrlKey || e.metaKey || e.altKey) return;")
    assert ctrl < guard, "ветка покадрового шага недостижима"


def test_plain_arrows_still_seek_by_seconds():
    assert "nudge(e.shiftKey ? -10 : -5)" in PLAYER
    assert "nudge(e.shiftKey ? 10 : 5)" in PLAYER


def test_step_does_not_fire_while_typing():
    """Ctrl+стрелки в тексте -- это перемещение по словам."""
    body = PLAYER[PLAYER.index("if (typingNow()) return;"):]
    body = body[:body.index("switch (e.key)")]
    assert "stepFrame" in body, "ветка шага оказалась вне защиты от ввода"


def test_keys_are_documented():
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    html = PLAYER.format(**{f: "X" for f in fields})
    assert "Ctrl+←/→" in html


# --- откуда берётся частота кадров ---------------------------------------

def test_fps_comes_from_the_report():
    assert "const VIDEO_FPS = {fps_js} || 30" in PLAYER


@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    src = watch / "DJI_1.MP4"
    src.write_bytes(b"x")
    # У записи r2 свой rel_path, значит на диске должен лежать свой файл.
    # Раньше обе записи ссылались abs_path-ом на один и тот же DJI_1.MP4
    # при разных rel_path -- фикстура противоречила сама себе, и это
    # проходило только потому, что путь БРАЛСЯ из базы. Теперь он
    # вычисляется из rel_path, и разойтись они уже не могут.
    (watch / "DJI_2.MP4").write_bytes(b"x")
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, fps, "
        "                      created_at, updated_at) "
        "VALUES ('r1', 'DJI_1.MP4', ?, 'video', 'done', 29.97, "
        "        '2026-08-15T10:00', '2026-08-15T10:00')", (str(src),))
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, fps, "
        "                      created_at, updated_at) "
        "VALUES ('r2', 'DJI_2.MP4', ?, 'video', 'queued', NULL, "
        "        '2026-08-15T10:00', '2026-08-15T10:00')", (str(src),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(tmp_path), raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "Тестовый"
    return c


def test_real_fps_reaches_the_page(client):
    html = client.get("/report/r1/player/").get_data(as_text=True)
    assert "const VIDEO_FPS = 29.97" in html


def test_unprocessed_video_gets_a_sane_default(client):
    """У необработанного видео частоты ещё нет, а шагать человек может уже
    сейчас -- плеер доступен до обработки."""
    html = client.get("/report/r2/player/").get_data(as_text=True)
    assert "const VIDEO_FPS = 30" in html
    assert "null" not in html.split("const VIDEO_FPS =")[1][:20]
