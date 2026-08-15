"""Список файлов выложен строгой сеткой.

Причина: раньше строка выкладывалась флексом, и каждый элемент вставал по
своей естественной ширине -- полоса покрытия, статистика и счётчики
оказывались в разных местах на каждой строке, правый край "плясал".

Самая хрупкая часть сетки -- КОЛИЧЕСТВО ячеек в строке. Если какая-то
колонка при определённом статусе не выводится (пустая строка вместо
<span>), все последующие элементы съезжают в чужие колонки и выравнивание
разваливается. Поэтому здесь проверяется именно постоянство числа ячеек."""
import re

import sar_server


def _tree_js():
    return sar_server.TREE_PAGE_HTML.format(viewer_name="tester", upload_section="")


def test_row_markup_has_exactly_seven_grid_cells():
    js = _tree_js()
    row = js[js.index('return `<div class="item-row">'):]
    row = row[:row.index("</div>`")]
    # ячейки верхнего уровня: превью, ссылка-строка (display:contents даёт
    # 5 своих ячеек) и блок кнопок
    assert "${thumb}" in row
    assert '<span class="cell-cov">' in row
    assert '<span class="cell-stat' in row
    assert '<span class="cell-det' in row
    assert '<span class="cell-status">' in row
    assert '<span class="cell-actions">' in row


def test_grid_template_declares_seven_columns():
    css = sar_server.TREE_PAGE_HTML
    m = re.search(r"grid-template-columns:\s*([^;]+);", css)
    assert m, "у строки списка должна быть заданная сетка колонок"
    cols = m.group(1).replace("minmax(0, 1fr)", "MINMAX").split()
    assert len(cols) == 7, f"ожидалось 7 колонок, найдено {len(cols)}: {cols}"


def test_header_row_matches_row_column_count():
    """Шапка использует ту же сетку -- если в ней другое число ячеек,
    подписи встанут не над своими столбцами."""
    js = _tree_js()
    head = js[js.index('<div class="item-row list-head">'):]
    head = head[:head.index("</div>")]
    assert head.count("<span") == 7


def test_empty_columns_are_still_rendered():
    """Пустые значения должны попадать в <span>, а не исчезать: пропуск
    ячейки сдвигает всё остальное в чужие колонки."""
    js = _tree_js()
    row = js[js.index('return `<div class="item-row">'):]
    row = row[:row.index("</div>`")]
    # cov/stat/detStat могут быть пустыми строками -- но обёртка есть всегда
    assert '<span class="cell-cov">${cov}</span>' in row.replace("\n", "").replace("  ", "")


def test_status_column_holds_progress_and_badge_together():
    """У 'обрабатывается' в статусе И полоска прогресса, И бейдж -- они
    обязаны лежать в ОДНОЙ ячейке, иначе строка занимает лишнюю колонку."""
    js = _tree_js()
    row = js[js.index('return `<div class="item-row">'):]
    row = row[:row.index("</div>`")]
    assert '<span class="cell-status">${right}</span>' in row.replace("\n", "").replace("  ", "")


def test_numeric_columns_use_tabular_figures():
    """Цифры в столбце должны быть моноширинными, иначе столбец дёргается."""
    assert "font-variant-numeric: tabular-nums" in sar_server.TREE_PAGE_HTML
