"""В списке файлов показывается имя файла, а не путь целиком.

Регрессия, замеченная на живой платформе с телефона. После переезда
материалов в папки операций поле name стало содержать полный
относительный путь, и строка списка выглядела так:

    Курумды август 2026/DJI_20260812140054...

Приставка съедала всю ширину, а само имя обрезалось -- вылеты перестали
отличаться друг от друга, притом что на диске ничего не переименовывалось.

Проверяем и то, что имя укоротили, и то, что полный путь никуда не делся:
он нужен и для подсказки при наведении, и для превью, и для поиска.
"""
import re

import sar_server


LIST_JS = sar_server.TREE_PAGE_HTML


def test_list_renders_basename_not_full_path():
    """В строке выводится shortName, а не сырое it.name."""
    assert 'class="fname">${{shortName}}' in LIST_JS
    assert 'class="fname">${{it.name}}' not in LIST_JS


def test_basename_is_taken_from_the_last_path_segment():
    assert "String(it.name).split('/').pop()" in LIST_JS


def test_full_path_stays_in_the_tooltip():
    """Информация о папке не должна пропасть -- она нужна, когда в операции
    несколько вложенных папок с одинаковыми именами файлов."""
    assert 'title="${{it.name}}"' in LIST_JS


def test_thumbnail_still_uses_the_full_path():
    """Превью именуется по полному пути: одноимённые файлы из разных папок
    имеют разные картинки, и укорачивание тут сломало бы их."""
    assert "/api/thumbnail/${{encodeURIComponent(it.name)}}" in LIST_JS


def test_search_still_matches_the_full_path():
    """Поиск по «борт 1» должен находить файлы в этой папке."""
    assert "it.name" in LIST_JS
    # поиск фильтрует по name, а не по укороченному имени
    m = re.search(r"filter[^\n]*it\.name|it\.name[^\n]*toLowerCase", LIST_JS)
    assert m, "поиск перестал использовать полный путь"


def test_short_name_is_computed_before_use():
    """Порядок важен: shortName должен объявляться до строки шаблона."""
    assert LIST_JS.index("const shortName") < LIST_JS.index('class="fname">${{shortName}}')
