"""Превью материалов на странице операции.

Раньше превью там не было вовсе: в строке стоял символ ▭ шириной 19px --
на экране он выглядел как пустой квадратик, и по списку из десятков
DJI_2026081213xxxx_000x_Z.MP4 отличить одно видео от другого было нельзя.
Имена у файлов с дрона неразличимы по смыслу, поэтому единственное, за
что цепляется глаз -- сам кадр.

Требования, из которых сделан размер:
  * превью должно быть достаточно крупным, чтобы человек узнал и запомнил
    видео;
  * но не громоздким: на экране 1920x1080 должно помещаться не меньше
    десяти строк;
  * квадратное.
"""
import io
import os
import re

import pytest

import sar_common
import sar_server


CARD = sar_server.OPERATION_CARD_HTML


# --- превью вообще есть ---------------------------------------------------

def test_file_row_shows_a_real_preview_not_a_glyph():
    """Регрессия: строка файла рисовала символ ▭ вместо кадра."""
    assert "/api/thumbnail/" in CARD, "строка файла не запрашивает превью"
    # класс дополнен spin -- спиннером ожидания кадра (см. test_spinner.py)
    assert 'class="thumb spin"' in CARD


def test_preview_is_requested_by_full_path():
    """В разных папках операции лежат файлы с одинаковыми именами. По
    короткому имени превью досталось бы не тому файлу."""
    assert "f.rel_path" in CARD, "превью просится по имени, а не по пути"


# --- размер и форма -------------------------------------------------------

def test_preview_is_square():
    """Кадр с дрона -- местность; квадратный кроп оставляет центр кадра,
    по которому видео и узнают. Плюс одинаковая ширина держит имена в
    одной колонке."""
    m = re.search(r"\.row \.ic,\.row \.thumb\{\{([^}]*)\}\}", CARD)
    assert m, "не найдено правило размера превью"
    rule = m.group(1)
    assert "width:var(--thumb)" in rule and "height:var(--thumb)" in rule, (
        "ширина и высота превью различаются -- оно не квадратное")


def test_preview_size_leaves_room_for_ten_rows():
    """Прямая проверка требования «не меньше 10 строк на 1920x1080».

    Считаем так же, как это выглядит на экране: высота строки -- это
    превью плюс вертикальные отступы плюс разделитель.
    """
    m = re.search(r"--thumb:clamp\((\d+)px,([\d.]+)vh,(\d+)px\)", CARD)
    assert m, "размер превью задан жёстко -- он не подстроится под экран"
    lo, vh, hi = int(m.group(1)), float(m.group(2)), int(m.group(3))

    thumb = min(max(lo, 1080 * vh / 100), hi)
    pad = re.search(r"\.row\{\{[^}]*padding:(\d+)px", CARD)
    assert pad, "не найден отступ строки"
    row_h = thumb + 2 * int(pad.group(1)) + 1        # +1 -- разделитель

    # Шапка страницы операции: заголовок, район, сводка, полоса покрытия,
    # вкладки, крошки. Плюс рамка браузера. Замерено по реальному экрану.
    chrome_and_header = 110 + 300
    visible = (1080 - chrome_and_header) / row_h
    assert visible >= 10, (
        f"превью {thumb:.0f}px даёт строку {row_h:.0f}px -- на экране "
        f"поместится лишь {visible:.1f} строк, а просили не меньше 10")


def test_preview_is_big_enough_to_recognise():
    """Обратная сторона: слишком мелкое превью бессмысленно -- ради
    узнаваемости кадра всё и делалось."""
    m = re.search(r"--thumb:clamp\((\d+)px,([\d.]+)vh,(\d+)px\)", CARD)
    thumb = min(max(int(m.group(1)), 1080 * float(m.group(2)) / 100),
                int(m.group(3)))
    assert thumb >= 50, f"на 1080p превью всего {thumb:.0f}px -- кадр не узнать"


def test_folders_line_up_with_files():
    """Значок папки того же размера, что превью: иначе имена папок и имена
    файлов встанут в разные колонки."""
    m = re.search(r"\.row \.ic,\.row \.thumb\{\{", CARD)
    assert m, "значок папки не приведён к размеру превью"


# --- отсутствующее превью -------------------------------------------------

def test_missing_preview_does_not_leave_a_hole():
    """Превью может законно не быть: файл только положили, воркер до него
    не дошёл. Строка при этом не должна ни прыгать, ни показывать
    сломанную картинку."""
    # onerror теперь ещё и снимает спиннер: крутилка над тем, что уже не
    # загрузится, обещала бы несбыточное (страж -- test_spinner.py).
    assert "onerror=" in CARD and "this.remove()" in CARD, (
        "нет запасного пути: браузер покажет значок битой картинки")
    assert 'class="fb"' in CARD, "нет значка под картинкой"


def test_preview_is_lazy():
    """В операции под сотню материалов -- грузить все кадры сразу значит
    сотню запросов на открытие страницы."""
    assert 'loading="lazy"' in CARD


# --- превью реально отдаётся ----------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    (watch / "Операция").mkdir(parents=True)
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

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
    c.watch = watch
    c.data_dir = data_dir
    return c


def test_thumbnail_is_served_for_a_material_in_a_subfolder(client):
    """Сквозная проверка того самого пути: материал лежит в папке операции,
    и превью для него обязано отдаваться по полному пути.

    Папке нужен маркер операции: без него сканер внутрь не заходит -- и
    это правильно, имя файла для превью проверяется по реальному списку
    материалов, а не просто склеивается с диском (защита от выхода за
    пределы папки)."""
    rel = "Операция/DJI_TEST.MP4"
    sar_common.write_operation_marker(str(client.watch / "Операция"), 1)
    (client.watch / "Операция" / "DJI_TEST.MP4").write_bytes(b"x")

    thumb_path = sar_common.get_thumbnail_path(client.data_dir, rel)
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    with io.open(thumb_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0jpeg")

    r = client.get("/api/thumbnail/" + rel)
    assert r.status_code == 200, (
        "превью для материала во вложенной папке не отдаётся -- "
        "именно так материалы и разложены в операции")
    assert r.headers["Content-Type"].startswith("image/")


def test_missing_thumbnail_is_not_an_error_page(client):
    """Файл есть, превью ещё нет. Ответ должен быть понятным, а не 500."""
    (client.watch / "Операция" / "DJI_NOTHUMB.MP4").write_bytes(b"x")
    r = client.get("/api/thumbnail/Операция/DJI_NOTHUMB.MP4")
    assert r.status_code in (204, 404), r.status_code
