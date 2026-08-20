"""Запись картинок по путям с кириллицей.

Баг прожил в проекте месяцами и молча: cv2.imwrite на Windows возвращает
False, если в пути есть не-ASCII символы, а результат никто не проверял.
Имя кропа собирается из названия класса, цветовые аномалии называются
по-русски -- значит НИ ОДИН их кроп никогда не сохранялся. В одном отчёте
из 3366 детекций отсутствовало 2194 картинки.

Заметить было тяжело именно потому, что кропы модели («person», латиница)
писались нормально: часть карточек в отчёте работала, часть показывала
битую картинку, и это выглядело как случайность.
"""
import os

import cv2
import numpy as np
import pytest

from sar_video_review import imwrite_safe


@pytest.fixture
def img():
    return np.full((24, 32, 3), 128, np.uint8)


def _read(path):
    """Чтение тем же обходным путём -- cv2.imread тоже спотыкается."""
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def test_writes_ascii_path(tmp_path, img):
    p = str(tmp_path / "f0000142_person_c0.71.jpg")
    assert imwrite_safe(p, img)
    assert _read(p) is not None


def test_writes_cyrillic_filename(tmp_path, img):
    """Тот самый случай: имя класса «цвет: оранжевый/красный (снаряжение)»
    превращается в имя файла с русскими буквами."""
    p = str(tmp_path / "f0003490_цвет_оранжевый_красный_снаряже_c0.58.jpg")
    assert imwrite_safe(p, img), "кроп с кириллицей не записался"
    assert _read(p) is not None


def test_writes_into_cyrillic_folder(tmp_path, img):
    """Материалы теперь лежат в папке операции -- «Курумды август 2026»."""
    d = tmp_path / "Курумды август 2026" / "crops"
    d.mkdir(parents=True)
    p = str(d / "f0000001_цвет_жёлтый_c0.42.jpg")
    assert imwrite_safe(p, img)
    assert _read(p) is not None


def test_file_appears_under_the_exact_name(tmp_path, img):
    """Главная проверка, и первая её версия была НЕВЕРНОЙ.

    Сначала тест смотрел только на возвращаемое значение -- и прошёл, хотя
    баг на месте: cv2.imwrite возвращает True, файл создаёт, но имя пишет
    искажённым. На диске лежало «С†РІРµС‚_...» -- байты UTF-8, прочитанные
    как CP1251, -- а detections.json помнил правильное «цвет_...». Ссылки
    указывали в пустоту при формально успешной записи.

    Поэтому проверяем не код возврата, а наличие файла ИМЕННО ПОД ТЕМ
    ИМЕНЕМ, которое запрашивали."""
    name = "f0003490_цвет_оранжевый_красный_снаряже_c0.58.jpg"
    p = tmp_path / name
    assert imwrite_safe(str(p), img)
    assert p.exists(), "файл записан не под тем именем"
    assert name in os.listdir(str(tmp_path)), "на диске искажённое имя"


def test_plain_imwrite_mangles_cyrillic_names(tmp_path, img):
    """Подтверждаем природу бага, а не верим на слово.

    Если OpenCV однажды это починит и тест начнёт падать -- значит обход
    больше не нужен, и это тоже полезно знать."""
    name = "кириллица.jpg"
    d = tmp_path / "plain"
    d.mkdir()
    cv2.imwrite(str(d / name), img)
    listed = os.listdir(str(d))
    if listed == [name]:
        pytest.skip("на этой платформе cv2.imwrite пишет кириллицу верно")
    assert listed and listed[0] != name, "ожидалось искажение имени"


def test_returns_false_on_unwritable_path(img):
    """Провал должен быть ВИДЕН вызывающему -- молчаливый False и есть
    причина, по которой баг прожил так долго."""
    assert imwrite_safe("/нет/такой/папки/x.jpg", img) is False


def test_respects_quality_parameter(tmp_path, img):
    noisy = np.random.randint(0, 255, (200, 200, 3), dtype=np.uint8)
    lo = str(tmp_path / "низкое.jpg")
    hi = str(tmp_path / "высокое.jpg")
    imwrite_safe(lo, noisy, [cv2.IMWRITE_JPEG_QUALITY, 20])
    imwrite_safe(hi, noisy, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert os.path.getsize(lo) < os.path.getsize(hi)


def test_detector_uses_the_safe_writer():
    """Прямой cv2.imwrite не должен вернуться в детектор незамеченным."""
    with open("sar_video_review.py", encoding="utf-8") as f:
        src = f.read()
    # в самой обёртке imencode есть, а вызовов imwrite быть не должно
    assert "cv2.imwrite(" not in src


def test_write_failures_are_reported():
    """Тихий провал -- корень всей истории. Он обязан попасть в лог."""
    with open("sar_video_review.py", encoding="utf-8") as f:
        src = f.read()
    assert src.count("не удалось сохранить") >= 2
