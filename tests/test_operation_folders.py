"""Тесты структуры папок внутри операции.

Заказчик раскладывает материал так же, как у себя в облаке: по дням, бортам,
вылетам. Скачанную с Google или Яндекс Диска папку должно быть достаточно
распаковать внутрь операции, и она отобразится один в один.

Отсюда три требования, каждое из которых здесь проверяется:

  * обход рекурсивный, но НЕ заходит в служебные каталоги -- иначе человек
    увидит десятки тысяч кропов из sar_data вместо своих папок;
  * путь материала -- относительный, а не одно имя. Одноимённые
    DJI_0001.MP4 из «борт 1» и «борт 2» это РАЗНЫЕ материалы, а запись
    ищется по rel_path: плоское имя схлопнуло бы их в одну и потеряло
    второй файл вместе со всей его разметкой;
  * пустые папки видны. Человек ожидает свою структуру целиком, а не только
    те ветки, где нашлась съёмка.
"""
import os

import pytest

import sar_common


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 16)


@pytest.fixture
def watch(tmp_path):
    """Папка операции с типичной структурой облачной выгрузки."""
    w = tmp_path / "watch"
    op = w / "Курумды август 2026"
    op.mkdir(parents=True)
    sar_common.write_operation_marker(str(op), 1)

    _touch(str(op / "11.08 первый день" / "борт 1" / "DJI_0001.MP4"))
    _touch(str(op / "11.08 первый день" / "борт 1" / "DJI_0001.SRT"))
    _touch(str(op / "11.08 первый день" / "борт 2" / "DJI_0001.MP4"))
    _touch(str(op / "12.08" / "DJI_0007.JPG"))
    _touch(str(op / "вертолёт" / "C0049.MP4"))
    (op / "пустая папка").mkdir()
    (op / "12.08" / "тоже пустая").mkdir()
    return w


# --- рекурсия и относительные пути ---------------------------------------

def test_finds_files_in_nested_folders(watch):
    files, _ = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    rels = sorted(f[0] for f in files)
    assert "Курумды август 2026/11.08 первый день/борт 1/DJI_0001.MP4" in rels
    assert "Курумды август 2026/вертолёт/C0049.MP4" in rels
    assert len(rels) == 4, rels          # 3 видео + 1 фото, SRT не медиа


def test_same_named_files_in_different_folders_stay_separate(watch):
    """Главный риск рекурсии.

    Запись материала ищется по rel_path. Если бы путь был плоским именем,
    два DJI_0001.MP4 из «борт 1» и «борт 2» стали бы одной записью -- и
    второй файл потерялся бы вместе со всей разметкой по нему."""
    files, _ = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    same = sorted(f[0] for f in files if f[0].endswith("DJI_0001.MP4"))
    assert len(same) == 2, same
    assert same[0] != same[1]
    assert len(set(same)) == 2


def test_photos_are_taken_too(watch):
    files, _ = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    kinds = {f[0]: f[2] for f in files}
    photo = [k for k, v in kinds.items() if v == "photo"]
    assert len(photo) == 1 and photo[0].endswith("DJI_0007.JPG")


def test_paths_use_forward_slashes(watch):
    """Путь хранится в базе и попадает в URL -- разделитель должен быть
    один и тот же на Windows и на Linux."""
    files, dirs = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    for rel, _, _ in files:
        assert "\\" not in rel, rel
    for d in dirs:
        assert "\\" not in d, d


# --- пустые папки ---------------------------------------------------------

def test_empty_folders_are_reported(watch):
    _, dirs = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    assert "Курумды август 2026/пустая папка" in dirs
    assert "Курумды август 2026/12.08/тоже пустая" in dirs


def test_all_folders_are_reported_not_only_empty(watch):
    _, dirs = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    assert "Курумды август 2026/11.08 первый день" in dirs
    assert "Курумды август 2026/11.08 первый день/борт 1" in dirs


# --- служебные каталоги ---------------------------------------------------

def test_service_dirs_are_not_scanned(watch):
    """sar_data содержит сотни тысяч кропов. Заглянуть туда -- показать
    человеку мусор вместо его папок и подорожать обход на порядки."""
    op = os.path.join(str(watch), "Курумды август 2026")
    _touch(os.path.join(op, "sar_data", "reports", "r1", "frames", "f01.jpg"))
    _touch(os.path.join(op, "telemetry", "полёты", "x.MP4"))
    files, dirs = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    assert not any("sar_data" in f[0] for f in files)
    assert not any("telemetry" in f[0] for f in files)
    assert not any("sar_data" in d for d in dirs)


def test_hidden_folders_are_skipped(watch):
    op = os.path.join(str(watch), "Курумды август 2026")
    _touch(os.path.join(op, ".служебная", "x.MP4"))
    files, dirs = sar_common.scan_operation_folder(str(watch), "Курумды август 2026")
    assert not any(".служебная" in f[0] for f in files)
    assert not any(".служебная" in d for d in dirs)


def test_depth_is_limited(tmp_path):
    """Облачные выгрузки бывают вложены как угодно, а симлинк на родителя
    устроил бы бесконечный обход."""
    w = tmp_path / "watch"
    op = w / "оп"
    op.mkdir(parents=True)
    sar_common.write_operation_marker(str(op), 1)
    deep = op
    for i in range(20):
        deep = deep / f"уровень{i}"
    _touch(str(deep / "DJI_0001.MP4"))
    files, _ = sar_common.scan_operation_folder(str(w), "оп", max_depth=5)
    assert files == [], "обход ушёл глубже разрешённого"


# --- какие папки считаются операциями ------------------------------------

def test_only_marked_folders_are_operations(watch):
    (watch / "просто папка пользователя").mkdir()
    ops = sar_common.operation_folders(str(watch))
    names = [name for _, name, _ in ops]
    assert names == ["Курумды август 2026"]


def test_operation_id_comes_from_the_marker(watch):
    ops = sar_common.operation_folders(str(watch))
    assert ops[0][0] == 1


def test_service_dirs_never_look_like_operations(tmp_path):
    w = tmp_path / "watch"
    (w / "sar_data").mkdir(parents=True)
    sar_common.write_operation_marker(str(w / "sar_data"), 99)
    assert sar_common.operation_folders(str(w)) == []


# --- превью: коллизия имён -----------------------------------------------

def test_thumbnails_of_same_named_files_do_not_collide(tmp_path):
    """Регрессия по замыслу.

    Превью именовалось по имени файла. С папками внутри операции
    «борт 1/DJI_0001.MP4» и «борт 2/DJI_0001.MP4» получили бы ОДНО имя
    картинки и перезаписали друг друга -- в списке показывался бы чужой
    кадр, причём молча."""
    a = sar_common.get_thumbnail_path(str(tmp_path), "борт 1/DJI_0001.MP4")
    b = sar_common.get_thumbnail_path(str(tmp_path), "борт 2/DJI_0001.MP4")
    assert a != b


def test_thumbnail_path_is_stable(tmp_path):
    a = sar_common.get_thumbnail_path(str(tmp_path), "борт 1/DJI_0001.MP4")
    b = sar_common.get_thumbnail_path(str(tmp_path), "борт 1/DJI_0001.MP4")
    assert a == b


def test_thumbnail_name_stays_readable(tmp_path):
    """Имя должно оставаться узнаваемым: по нему разбираются вручную."""
    p = sar_common.get_thumbnail_path(str(tmp_path), "11.08/борт 1/DJI_0001.MP4")
    assert "DJI_0001" in os.path.basename(p)


def test_thumbnail_separator_does_not_matter(tmp_path):
    """Windows отдаёт путь с обратным слэшем -- превью не должно
    раздваиваться из-за разделителя."""
    a = sar_common.get_thumbnail_path(str(tmp_path), "борт 1/DJI_0001.MP4")
    b = sar_common.get_thumbnail_path(str(tmp_path), "борт 1\\DJI_0001.MP4")
    assert a == b
