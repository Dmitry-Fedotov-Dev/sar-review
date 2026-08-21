"""Превью находок и окно предпросмотра.

На вкладке находок были только подписи: «резко чёрное», «прямое и
блестящее», «квадратная шапка снега». Имена файлов с дрона по смыслу
неразличимы, а подпись человека мало что говорит спустя неделю. Отличить
одну находку от другой и вспомнить её можно только по самому кадру.

У ручной пометки готовой картинки НЕТ: человек обвёл область прямо на
проигрываемом видео, и на диске остались лишь таймкод и координаты рамки.
Кадр приходится вырезать -- и делает это воркер, потому что открыть видео
и перемотать к секунде это обработка, а sar_server.py по устройству
проекта только отдаёт готовые файлы.

У триажа сцены модели картинка уже есть -- кроп в папке отчёта; его и
показываем, генерировать нечего.
"""
import os
import re

import pytest

import sar_common
import sar_server
import sar_worker


CARD = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")
PLAYER = sar_server.PLAYER_PAGE_HTML


# --- граница ответственности ---------------------------------------------

def test_server_does_not_extract_frames():
    """Ключевое ограничение проекта: веб-слой ничего не обрабатывает.

    К тому же в скорость списка только что вложились -- распаковка кадров
    в обработчике запроса обнулила бы это."""
    with open("sar_server.py", encoding="utf-8") as f:
        src = f.read()
    assert "VideoCapture" not in src, (
        "сервер открывает видео -- это работа воркера")


def test_worker_generates_previews():
    assert hasattr(sar_worker, "ensure_finding_previews")


def test_worker_limits_work_per_pass():
    """Цикл наблюдения не должен вставать из-за того, что кто-то за раз
    наставил полсотни пометок."""
    assert 0 < sar_worker.FINDING_PREVIEWS_PER_PASS <= 20


# --- путь и генерация -----------------------------------------------------

def test_preview_path_is_keyed_by_observation(tmp_path):
    a = sar_common.finding_preview_path(str(tmp_path), 12)
    b = sar_common.finding_preview_path(str(tmp_path), 13)
    assert a != b
    assert a.endswith(".jpg")


def test_box_is_drawn_from_normalised_coordinates():
    """bbox хранится долями 0..1 от размера кадра, чтобы пережить смену
    разрешения видео. Нарисовать его как пиксели -- значит промахнуться."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    frame = np.zeros((200, 400, 3), dtype=np.uint8)
    out = sar_worker._draw_box(cv2, frame, [0.25, 0.25, 0.75, 0.75])
    # внутри рамки центр остался чёрным, а на её линии появился цвет
    assert out[100, 200].sum() == 0, "закрасили содержимое вместо рамки"
    assert out[50, 200].sum() > 0, "рамка не нарисована по верхней границе"


def test_broken_bbox_does_not_crash_the_pass():
    """Координаты приходят из базы, где лежат записи разных версий."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    frame = np.zeros((50, 50, 3), dtype=np.uint8)
    for bad in ([], [1, 2], ["a", "b", "c", "d"], None):
        if bad is None:
            continue
        sar_worker._draw_box(cv2, frame, bad)


# --- отдача ---------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", data_dir, raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "Тестовый"
    c.data_dir = data_dir
    return c


def test_preview_is_served(client):
    path = sar_common.finding_preview_path(client.data_dir, 7)
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0jpeg")
    r = client.get("/api/finding/7/preview")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("image/")


def test_missing_preview_is_not_an_error(client):
    """Воркер мог ещё не дойти до пометки -- это нормальное состояние."""
    assert client.get("/api/finding/999/preview").status_code == 404


def test_broken_preview_lookup_does_not_kill_the_findings_list():
    """Картинка -- украшение строки, а список находок -- рабочий инструмент.

    Ровно на этом уже обжигались: один отчёт без out_dir клал главную
    страницу целиком. Поэтому поиск картинки не имеет права поднимать
    исключение наружу.
    """
    assert sar_server._finding_preview_url(None, {"kind": "manual"}) is None
    assert sar_server._finding_preview_url(None, {}) is None


# --- разметка списка ------------------------------------------------------

def test_findings_row_shows_the_frame():
    assert 'class="shot"' in CARD
    assert "f.preview" in CARD


def test_missing_frame_leaves_a_tidy_placeholder():
    """Не пустая дыра и не значок битой картинки: строки не должны прыгать
    по высоте оттого, что до части пометок воркер ещё не дошёл."""
    assert "noshot" in CARD
    assert "this.parentNode.classList.add('noshot')" in CARD


def test_frame_matches_the_size_of_material_previews():
    """Списки находок и материалов живут на одной странице -- разнобой в
    размере читался бы как разная важность."""
    # CARD -- уже отрендеренный шаблон: удвоенные скобки в нём схлопнуты
    m = re.search(r"\.shot\{([^}]*)\}", CARD)
    assert m, "правило размера кадра не найдено"
    assert "width:var(--thumb)" in m.group(1), m.group(1)


# --- окно предпросмотра ---------------------------------------------------

def test_preview_window_opens_on_hover_not_on_click():
    """Клик по строке уже занят: он ведёт в плеер на таймкод находки."""
    assert "onmouseenter=" in CARD
    assert "function showPeek" in CARD


def test_preview_window_has_both_ways_to_close():
    assert "peek-x" in CARD, "нет крестика"
    assert "mouseleave" in CARD, "не закрывается уводом курсора"


def test_preview_window_zooms_by_wheel_and_pinch():
    assert "'wheel'" in CARD
    assert "'touchmove'" in CARD
    assert "Math.hypot" in CARD, "щипок не считает расстояние между пальцами"


def test_zoom_keeps_the_point_under_the_cursor():
    """Иначе при увеличении уезжает как раз то, что хотели рассмотреть."""
    assert "getBoundingClientRect" in CARD
    assert "peekX = at.clientX" in CARD


def test_zoom_is_bounded():
    assert "Math.min(8, Math.max(1," in CARD, "масштаб не ограничен"


def test_window_sizes_differ_for_desktop_and_phone():
    assert "width:20vw" in CARD, "не задана ширина в 20% экрана"
    assert "width:100vw" in CARD, "на телефоне окно не во всю ширину"
    assert "@media (max-width:760px)" in CARD


def test_window_does_not_hang_over_the_wrong_row():
    """Прокрутка уводит строку из-под окна -- оно должно закрыться."""
    assert "window.addEventListener('scroll', hidePeek" in CARD


# --- кнопка удаления наблюдения ------------------------------------------

def test_delete_button_sits_with_the_observation_not_with_the_comments():
    """Жалоба пользователя: кнопка стояла ПОСЛЕ блока обсуждения, сразу под
    полем ввода комментария, и читалась как «удалить комментарий». Место
    кнопки и есть её подпись."""
    head = PLAYER[PLAYER.index('<div class="obs-head">'):]
    head = head[:head.index("</div>")]
    assert "deleteObservation" in head, (
        "кнопка удаления не в шапке наблюдения -- её снова спутают "
        "с удалением комментария")

    # и её больше нет после обсуждения
    tail = PLAYER[PLAYER.index("renderComments('manual', o.id)"):]
    tail = tail[:400]
    assert "deleteObservation" not in tail


def test_delete_confirmation_says_what_disappears():
    assert "Удалить это наблюдение?" in PLAYER
