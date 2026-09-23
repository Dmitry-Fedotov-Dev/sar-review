# -*- coding: utf-8 -*-
"""Спиннер ожидания превью.

ЗАЧЕМ. Кадр находки режет воркер, и между созданием пометки и готовым
превью проходит до полуминуты (замерено на боевой находке #42: 24 секунды).
Всё это время страница показывала значок битой картинки -- то есть
«сломалось», хотя на деле «ещё не готово», и человек уходил, считая находку
испорченной.

ГЛАВНОЕ ПРАВИЛО, которое тут и сторожится: спиннер снимается И при
успехе, И при отказе.

 * при успехе -- чтобы браузер не крутил анимацию под непрозрачной
   картинкой: в списке таких строк две сотни;
 * при отказе -- потому что крутилка над тем, что уже никогда не
   загрузится, ОБЕЩАЕТ НЕСБЫТОЧНОЕ. Человек ждёт вместо того, чтобы
   понять, что ждать нечего. Это ровно тот молчаливый отказ, против
   которого весь проект: платформа не падает, «всё идёт», а результата
   нет и не будет.

Где-нибудь потерять `onerror` легко -- поэтому страж, а не доверие.
"""
import os
import re

import pytest

import sar_common
import sar_server


SPINNER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "static", "spinner.gif")


@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO operations (id, title, created_at, updated_at) "
        "VALUES (1, 'Операция', datetime('now'), datetime('now'))")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "tester"
    return c


# --- сам файл --------------------------------------------------------------

def test_spinner_file_exists_and_is_a_gif():
    assert os.path.exists(SPINNER), "static/spinner.gif пропал -- спиннера не будет"
    with open(SPINNER, "rb") as f:
        assert f.read(6) in (b"GIF87a", b"GIF89a"), "это не GIF"


def test_spinner_is_small_enough():
    """Потолок веса.

    Исходник был 1,44 МБ (1600x1200, 84 кадра). Такой файл в списке на
    двести строк -- это мегабайты трафика и заметная нагрузка на отрисовку
    ради индикатора ожидания. Сжат до 96x96 при 10 к/с.
    """
    size = os.path.getsize(SPINNER)
    assert size < 120 * 1024, (
        "спиннер раздулся до %.0f КБ -- пережми, это всего лишь индикатор"
        % (size / 1024))


def test_spinner_is_served_by_platform(client):
    """Отдаётся СВОЕЙ статикой, а не из сети: в поле интернета нет."""
    r = client.get("/static/spinner.gif")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/gif"


# --- разметка --------------------------------------------------------------

def _page(client, url):
    r = client.get(url)
    assert r.status_code == 200, url
    return r.get_data(as_text=True)


@pytest.mark.parametrize("url", ["/", "/operation/1/"])
def test_spinner_is_cleared_on_both_outcomes(client, url):
    """СТРАЖ. У каждого onload со снятием класса есть парный onerror.

    Считаем вхождения: снятие класса должно встречаться не реже, чем
    установка. Если кто-то добавит спиннер и забудет onerror, число
    разъедется.
    """
    html = _page(client, url)
    assert "spinner.gif" in html, "спиннер не подключён на " + url
    on_load = len(re.findall(r"onload=\"[^\"]*classList\.remove\('spin'\)", html))
    on_err = len(re.findall(r"onerror=\"[^\"]*classList\.remove\('spin'\)", html))
    assert on_load > 0, "нет снятия спиннера при успешной загрузке"
    assert on_err >= on_load, (
        "снятие спиннера при ОТКАЗЕ встречается реже (%d), чем при успехе (%d): "
        "где-то крутилка останется навсегда" % (on_err, on_load))


def test_finding_without_preview_gets_no_spinner(client):
    """Превью нет вовсе -- сразу ровный прямоугольник, а не крутилка.

    Разные состояния: «кадр идёт» и «кадра не будет». Спиннер на втором
    означал бы вечное ожидание.
    """
    html = _page(client, "/operation/1/")
    assert 'class="shot noshot"' in html, \
        "ветка «превью нет» должна давать noshot без spin"
    assert 'class="shot noshot spin"' not in html
    assert 'class="shot spin noshot"' not in html


@pytest.mark.parametrize("url", ["/", "/operation/1/"])
def test_reduced_motion_is_respected(client, url):
    """Анимацию GIF нельзя остановить из CSS -- значит при просьбе убрать
    движение её просто не показываем. Под ней остаётся статичный значок."""
    html = _page(client, url)
    block = re.search(r"prefers-reduced-motion[^}]*\{[^}]*\}", html, re.S)
    assert block, "нет правила для prefers-reduced-motion на " + url
    assert "background-image:none" in block.group(0).replace(" ", "")


def test_finding_page_drops_spinner_when_frame_gave_up(client):
    """На странице находки после исчерпания попыток спиннер УБИРАЕТСЯ.

    Иначе получается худший из вариантов: подпись говорит «не удалось»,
    а рядом бодро крутится кот.
    """
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('r1','v.MP4','','video','done',"
        "datetime('now'), datetime('now'))")
    conn.execute(
        "INSERT INTO manual_observations (id, report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES (1,'r1','t',1.0,"
        "'[0.1,0.1,0.2,0.2]','метка', datetime('now'))")
    conn.commit()
    conn.close()

    html = _page(client, "/finding/1/")
    assert "shotMissing()" in html
    # подпись меняется точечно, а не заменой всей разметки: иначе <i> с
    # крутилкой уцелел бы только потому, что его забыли стереть
    assert "icon.style.display = 'none'" in html
    assert "wait.textContent" not in html, \
        "замена всего текста снесла бы разметку спиннера"


def test_inline_onerror_does_not_call_a_later_function(client):
    """СТРАЖ ПОРЯДКА ЗАГРУЗКИ.

    Скрипт страницы находки объявлен НИЖЕ картинки. Если inline-обработчик
    зовёт функцию из него (`onerror="shotMissing()"`), то при отказе,
    случившемся до разбора скрипта, вызов падает с «shotMissing is not
    defined» -- молча, в консоли, -- и остаётся ровно битая картинка.
    Именно так это и выглядело в бою.

    Поэтому inline-обработчик только СТАВИТ ПОМЕТКУ, а разбирает её скрипт,
    когда бы тот ни загрузился.
    """
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('r9','v.MP4','','video','done',"
        "datetime('now'), datetime('now'))")
    conn.execute(
        "INSERT INTO manual_observations (id, report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES (9,'r9','t',1.0,"
        "'[0.1,0.1,0.2,0.2]','метка', datetime('now'))")
    conn.commit()
    conn.close()

    html = _page(client, "/finding/9/")
    img = re.search(r"<img id=\"shot\"[^>]*>", html).group(0)
    assert "shotMissing()" not in img, (
        "inline onerror зовёт функцию, объявленную ниже по странице: "
        "при раннем отказе вызов упадёт и обработчик не сработает")
    assert "dataset.failed" in img, "нет пометки об отказе"

    # скрипт обязан разобрать и отказ, случившийся ДО его загрузки
    assert "dataset.failed === '1'" in html, "скрипт не читает пометку"
    assert "naturalWidth === 0" in html, (
        "не проверяется уже загруженная пустышка -- второй признак "
        "раннего отказа")
    assert "addEventListener('error', shotMissing)" in html, (
        "нет подписки на последующие отказы (повторные попытки)")


def test_wait_block_is_not_inside_the_zoom_layer(client):
    """СТРАЖ ВЁРСТКИ. Блок ожидания -- сосед .zoom, а не его ребёнок.

    У .zoom нет собственных размеров: он подстраивается под картинку. Пока
    та не загрузилась, он схлопывается почти в ноль, и вложенный блок с
    inset:0 получал коробочку в углу -- кот в неё не помещался, а текст
    обрезался. Именно так это и выглядело в бою.

    Вторая причина: на .zoom висит transform масштабирования и
    панорамирования. Изнутри спиннер уезжал бы вместе с кадром.
    """
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('r7','v.MP4','','video','done',"
        "datetime('now'), datetime('now'))")
    conn.execute(
        "INSERT INTO manual_observations (id, report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES (7,'r7','t',1.0,"
        "'[0.1,0.1,0.2,0.2]','метка', datetime('now'))")
    conn.commit()
    conn.close()

    html = _page(client, "/finding/7/")
    zoom = html.index('<div class="zoom"')
    zoom_end = html.index("</div>", zoom)
    wait = html.index('id="shotwait"')
    assert wait > zoom_end, (
        "блок ожидания лежит внутри .zoom -- он схлопнется вместе с ним")
    view = html.index('<div class="view"')
    assert view < wait, "блок ожидания оказался за пределами области просмотра"


def test_hidden_attribute_actually_hides_the_wait_block(client):
    """СТРАЖ КАСКАДА. У .shot-wait обязано быть правило для [hidden].

    Блок прячется атрибутом hidden, который работает браузерным правилом
    [hidden]{display:none}. Любой АВТОРСКИЙ display его перебивает --
    авторские стили сильнее браузерных независимо от специфичности. Без
    явного .shot-wait[hidden]{display:none} блок с непрозрачным фоном
    висел поверх кадра ВСЕГДА: фото загружалось и было закрыто спиннером,
    который «бесконечно готовил» давно готовый кадр.

    Ошибку не видно ни в разметке, ни в логике -- только в каскаде.
    """
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('r8','v.MP4','','video','done',"
        "datetime('now'), datetime('now'))")
    conn.execute(
        "INSERT INTO manual_observations (id, report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES (8,'r8','t',1.0,"
        "'[0.1,0.1,0.2,0.2]','метка', datetime('now'))")
    conn.commit()
    conn.close()

    html = _page(client, "/finding/8/")
    assert 'id="shotwait" hidden' in html, "блок должен стартовать скрытым"
    rule = re.search(r"\.shot-wait\[hidden\]\s*\{[^}]*\}", html)
    assert rule, (
        "нет правила .shot-wait[hidden] -- display:flex перебьёт атрибут "
        "hidden, и спиннер закроет готовый кадр")
    assert "display:none" in rule.group(0).replace(" ", "")
    # правило обязано идти ДО общего .shot-wait, иначе при равной
    # специфичности победит последнее... но [hidden] специфичнее, поэтому
    # достаточно самого факта; проверяем, что общий блок тоже на месте
    assert re.search(r"\.shot-wait\s*\{[^}]*display:flex", html), \
        "пропал основной стиль блока ожидания"
