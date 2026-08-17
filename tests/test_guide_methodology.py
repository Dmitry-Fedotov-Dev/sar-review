"""Гид волонтёра содержит методику отсмотра, а не только описание кнопок.

Методику продиктовал координатор поисков -- это не рекомендации "для полноты",
а то, ради чего гид вообще читают: человек, севший смотреть видео без неё,
просматривает центр кадра на нормальной скорости и не видит ничего, потому
что находки лежат у краёв и живут два кадра.

Тесты держат конкретные пункты, которые легко потерять при следующей правке
вёрстки: темп, сетку, смены, формат заявки, пять признаков и -- главное --
установку "ищем не снег и не камень" вместо интуитивного "ищем яркое".

Проверяем сам GUIDE_PAGE_HTML: он отдаётся как есть, без .format(), поэтому
строка в тесте и строка на странице совпадают буквально.
"""
import re

import sar_server


HTML = sar_server.GUIDE_PAGE_HTML


def _text():
    """Страница без тегов -- чтобы <strong> внутри фразы не ломал поиск."""
    return re.sub(r"<[^>]+>", " ", HTML)


# --- 1. Как смотреть ---------------------------------------------------

def test_pacing_rule_is_stated():
    assert "0.5" in HTML, "темп просмотра -- не быстрее 0.5x"
    assert "покадрово" in HTML


def test_mental_grid_is_stated():
    txt = _text()
    assert "6 или 9 сектор" in txt
    assert "у краёв" in txt, "нужно объяснить, ЗАЧЕМ сетка"


def test_shift_length_is_stated():
    txt = _text()
    assert "20" in txt and "30 минут" in txt
    assert "40 минут" in txt, "предел, после которого человек перестаёт видеть"


def test_clean_and_poorly_viewed_are_separate_statuses():
    """Различие критично: по 'просмотрено плохо' планируют повторный облёт."""
    txt = _text()
    assert "просмотрено чисто" in txt
    assert "просмотрено плохо" in txt


def test_candidate_report_format_lists_all_seven_fields():
    txt = _text()
    for field in ["имя файла", "таймкод", "координаты из телеметрии",
                  "скриншот", "короткое описание", "уверенность", "повторный облёт"]:
        assert field in txt, f"в формате заявки нет поля: {field}"


def test_doubtful_findings_are_always_recorded():
    assert "Сомнительное фиксируем всегда" in _text()


# --- 2. Куда смотреть --------------------------------------------------

def test_priority_terrain_is_listed():
    txt = _text()
    for term in ["ергшрунд", "анклюфт", "мульд", "улуар",
                 "онусы выноса", "рещины ледника", "ромоины"]:
        assert term in txt, f"не указан приоритетный рельеф: {term}"


def test_light_gear_drifts_further_than_the_body():
    """Практический вывод, без него конус выноса смотрят не целиком."""
    txt = _text()
    assert "ниже и глубже" in txt
    assert "дальше и вбок" in txt


def test_deep_cracks_are_flagged_for_reshoot():
    assert "сбоку" in _text(), "трещины сверху не читаются -- нужна пересъёмка"


# --- 3. Признаки -------------------------------------------------------

def test_all_five_detection_cues_are_present():
    txt = _text()
    for cue in ["Геометрия", "Блик", "Тень", "Тёмное пятно на снегу", "Изменение и движение"]:
        assert cue in txt, f"нет признака: {cue}"


def test_dark_spot_is_marked_as_the_strongest_signal():
    assert "сильнее любого яркого цвета" in _text()


def test_color_is_explicitly_called_unreliable():
    assert "ненадёжен" in _text()


# --- 4. Что ищем -------------------------------------------------------

def test_search_targets_include_traces_not_just_objects():
    """Следы и изменения рельефа важнее самих предметов: их видно крупнее."""
    txt = _text()
    for target in ["снежная пещера", "орозда скольжения", "ошек и ледоруба",
                   "ивуачный мусор", "вежий лежит поверх снега", "нежные комья"]:
        assert target in txt, f"нет цели поиска: {target}"


# --- 5. Цвет -----------------------------------------------------------

def test_the_core_instruction_is_not_look_for_bright():
    """Самый важный абзац гида: 'ищем яркое' -- вредная установка, из-за неё
    тёмная одежда в тени проходит мимо взгляда."""
    txt = _text()
    assert "не снег и не камень" in txt
    assert "достоверно неизвестны" in txt, "цвета курток нам неизвестны"


def test_shadow_shifts_colors_to_blue():
    txt = _text()
    assert "уходит в синий" in txt
    assert "насыщенность" in txt, "нужен практический приём, а не только предупреждение"


def test_known_false_positives_are_listed():
    txt = _text()
    for fp in ["ишайники", "расная водоросль", "христые", "амня-останца"]:
        assert fp in txt, f"не предупредили о ложном срабатывании: {fp}"


# --- вёрстка -----------------------------------------------------------

def test_methodology_sections_are_linked_from_the_contents():
    for anchor in ["#how", "#where", "#signs", "#what", "#color"]:
        assert f'href="{anchor}"' in HTML, f"раздел {anchor} не попал в оглавление"
        assert f'id="{anchor[1:]}"' in HTML, f"нет самого раздела {anchor}"


def test_methodology_comes_before_the_ui_instructions():
    """Порядок содержательный: как смотреть -- важнее, чем куда нажимать."""
    assert HTML.index('id="how"') < HTML.index('id="login"')
    assert HTML.index('id="color"') < HTML.index('id="list"')


def test_section_numbers_are_unique_and_sequential():
    nums = re.findall(r'<span class="step-num">(\d+)</span>', HTML)
    assert nums == [f"{i:02d}" for i in range(1, len(nums) + 1)], nums


def test_guide_stays_self_contained():
    assert "https://fonts" not in HTML
    assert "<script src=" not in HTML
