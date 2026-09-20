"""Временная папка скачанных оригиналов.

Материал уезжает в облако, но детектор и ffmpeg читают только файл на
диске. Значит перед обработкой файл кладётся локально -- и вот тут
появляется способ уронить машину, на которой лежит вся работа поисковой
группы: скачать больше, чем есть места.

Поэтому здесь проверяется прежде всего то, чего делать НЕЛЬЗЯ:

  * выйти за потолок;
  * выбросить файл, который прямо сейчас обрабатывают;
  * молча отказать -- вызывающий тогда начнёт качать «на всякий случай».
"""
import os
import time

import pytest

import sar_staging


MB = 1024 * 1024


def make(st, rel, size_mb, age_sec=0):
    p = st.path_for(rel)
    os.makedirs(os.path.dirname(p) or st.root, exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * (size_mb * MB))
    if age_sec:
        t = time.time() - age_sec
        os.utime(p, (t, t))
    return p


@pytest.fixture
def st(tmp_path):
    return sar_staging.Staging(str(tmp_path / "staging"), cap_bytes=100 * MB)


# --- пути -----------------------------------------------------------------

def test_folder_structure_is_preserved(st):
    """Два видео с одинаковым именем в разных операциях не должны
    затирать друг друга -- на материале дрона одинаковые имена обычны."""
    a = st.path_for("Курумды 2026/DJI_0001.MP4")
    b = st.path_for("Алай 2027/DJI_0001.MP4")
    assert a != b
    assert a.endswith(os.path.join("Курумды 2026", "DJI_0001.MP4"))


def test_staging_lives_next_to_the_database_not_the_material(tmp_path):
    """watch_dir может оказаться облачной папкой; временные копии должны
    лежать на локальном диске рядом с базой."""
    d = sar_staging.staging_dir(str(tmp_path / "data"))
    assert d.startswith(str(tmp_path / "data"))


# --- потолок --------------------------------------------------------------

def test_room_is_free_when_folder_is_empty(st):
    assert st.free_space_for(50 * MB) == 0


def test_oldest_files_are_evicted_first(st):
    make(st, "старое.mp4", 40, age_sec=10_000)
    make(st, "свежее.mp4", 40, age_sec=10)
    st.free_space_for(50 * MB)
    assert not st.has("старое.mp4")
    assert st.has("свежее.mp4"), "выброшено не то -- вытеснение не по давности"


def test_touch_protects_a_file_from_eviction(st):
    """Файл читают прямо сейчас, но скачан он давно. Без отметки обращения
    его выбросят именно в этот момент."""
    make(st, "старое.mp4", 40, age_sec=10_000)
    make(st, "другое.mp4", 40, age_sec=100)
    st.touch("старое.mp4")
    st.free_space_for(50 * MB)
    assert st.has("старое.mp4")
    assert not st.has("другое.mp4")


def test_eviction_stops_as_soon_as_there_is_room(st):
    """Лишнее удаление означает лишнее скачивание потом."""
    for i in range(4):
        make(st, f"f{i}.mp4", 20, age_sec=1000 - i)
    st.free_space_for(30 * MB)
    left = len([e for e in st._entries()])
    assert left == 3, "выброшено больше, чем нужно"


# --- закреплённые ---------------------------------------------------------

def test_pinned_file_is_never_evicted(st):
    """Файл обрабатывается. Выбросив его, мы получим не ошибку, а отчёт
    по половине видео -- cap.read() просто вернёт False."""
    make(st, "в_работе.mp4", 60, age_sec=10_000)
    st.pin("в_работе.mp4")
    make(st, "прочее.mp4", 30, age_sec=10)
    st.free_space_for(35 * MB)
    assert st.has("в_работе.mp4")


def test_unpin_releases_the_file(st):
    make(st, "был_в_работе.mp4", 60, age_sec=10_000)
    st.pin("был_в_работе.mp4")
    st.unpin("был_в_работе.mp4")
    st.free_space_for(60 * MB)
    assert not st.has("был_в_работе.mp4")


def test_everything_pinned_means_loud_refusal(st):
    """Главное правило: освободить нечем -- НЕ качаем и говорим вслух.
    Молчаливый отказ привёл бы к скачиванию «на всякий случай» и
    переполнению диска, на котором лежит база операции."""
    make(st, "a.mp4", 60)
    make(st, "b.mp4", 35)
    st.pin("a.mp4")
    st.pin("b.mp4")
    with pytest.raises(sar_staging.NoRoomError) as e:
        st.free_space_for(50 * MB)
    assert "не начато" in str(e.value)


def test_file_larger_than_the_cap_is_refused_with_advice(st):
    """Отказ должен подсказывать выход, а не просто сообщать о беде."""
    with pytest.raises(sar_staging.NoRoomError) as e:
        st.free_space_for(200 * MB)
    msg = str(e.value)
    assert "больше всей временной папки" in msg
    assert "настройк" in msg.lower(), "не сказано, что делать"


def test_cap_is_never_exceeded_after_making_room(st):
    """Сквозная проверка смысла всего модуля."""
    for i in range(5):
        make(st, f"f{i}.mp4", 18, age_sec=1000 - i)
    st.free_space_for(30 * MB)
    assert st.size() + 30 * MB <= st.cap_bytes


# --- запись ---------------------------------------------------------------

def test_download_is_published_atomically(st):
    """Оборванная закачка не должна оставлять файл нормального вида, но
    неполный: детектор честно обработает половину видео."""
    tmp = st.open_for_write("op/v.mp4")
    assert tmp.endswith(".part")
    with open(tmp, "wb") as f:
        f.write(b"data")
    assert not st.has("op/v.mp4"), "недокачанный файл уже виден под именем"
    st.publish("op/v.mp4")
    assert st.has("op/v.mp4")


def test_discard_removes_the_partial_file(st):
    tmp = st.open_for_write("op/v.mp4")
    with open(tmp, "wb") as f:
        f.write(b"half")
    st.discard("op/v.mp4")
    assert not os.path.exists(tmp)


def test_publish_marks_the_file_as_just_used(st):
    """Только что скачанный файл -- самый нужный; вытеснить его первым
    значит скачать заново через минуту."""
    tmp = st.open_for_write("v.mp4")
    with open(tmp, "wb") as f:
        f.write(b"x" * MB)
    os.utime(tmp, (1, 1))          # как будто он древний
    st.publish("v.mp4")
    age = time.time() - os.path.getmtime(st.path_for("v.mp4"))
    assert age < 60


# --- обслуживание ---------------------------------------------------------

def test_empty_operation_folders_are_cleaned_up(st):
    make(st, "оп/видео.mp4", 60, age_sec=10_000)
    make(st, "другое.mp4", 30, age_sec=10)
    st.free_space_for(35 * MB)
    assert not os.path.isdir(os.path.join(st.root, "оп")), (
        "пустые папки копятся: за месяцы работы это дерево из тысяч каталогов")


def test_clear_wipes_everything(st):
    make(st, "a.mp4", 10)
    st.clear()
    assert st.size() == 0
    assert os.path.isdir(st.root)
