"""Даты записей показываются полностью и без внешних сервисов времени.

Поводом стал вопрос «в какие даты Айгуль делала записи» -- по интерфейсу
ответить было нельзя: у комментариев показывались только день и месяц без
года, а у наблюдений даты не было вовсе, только таймкод в видео и автор.
Поиски идут годами, материал по ним хранится, и «16.08 07:19» не отличить
от прошлогоднего.

Отдельное требование: часы обязаны работать в любой стране, без платных и
блокируемых сервисов и без передачи данных о пользователе. Оно выполняется
тем, что внешнего источника времени НЕТ вообще -- ни сейчас, ни раньше:
toLocaleString встроен в браузер и работает офлайн.
"""
import re

import pytest

import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML
CARD = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")


# --- полная дата ----------------------------------------------------------

def test_player_formatter_includes_the_year():
    assert "year:'numeric'" in PLAYER


def test_card_formatter_includes_the_year():
    assert "year:'numeric'" in CARD


def test_observation_shows_when_it_was_recorded():
    """Раньше у наблюдения был только таймкод в видео -- когда его сделали,
    не показывалось нигде."""
    assert "fmtStamp(o.created_at)" in PLAYER
    assert 'class="obs-when"' in PLAYER


def test_finding_shows_when_it_was_recorded():
    assert "fmtStamp(f.created_at)" in CARD


def test_comment_time_uses_the_same_formatter():
    """Два разных формата дат на одной странице путают сильнее, чем один
    неудобный."""
    assert "function fmtCommentTime(iso) {{ return fmtStamp(iso); }}" in PLAYER


def test_recording_date_is_distinct_from_video_timecode():
    """fmtTime -- место в видео, fmtStamp -- когда записали. Их легко
    перепутать, поэтому у них разные имена и разные стили."""
    assert "function fmtTime(sec)" in PLAYER
    assert "function fmtStamp(iso)" in PLAYER


# --- никаких внешних сервисов времени -------------------------------------

@pytest.mark.parametrize("banned", [
    "worldtimeapi", "timeapi.io", "time.is", "ntp", "googleapis.com/time",
])
def test_no_external_time_service(banned):
    """Требование заказчика: работать в любой стране, без блокировок и без
    передачи данных. Единственный способ гарантировать это -- не ходить за
    временем наружу вообще."""
    with open("sar_server.py", encoding="utf-8") as f:
        src = f.read().lower()
    assert banned not in src


def test_time_comes_from_the_browser():
    """toLocaleString -- встроенная функция, сеть не трогает."""
    assert "toLocaleString('ru-RU'" in PLAYER
    assert "toLocaleString('ru-RU'" in CARD


# --- устойчивость ---------------------------------------------------------

def test_formatter_survives_garbage():
    """Дата приходит из базы, где лежат записи разных версий."""
    for page in (PLAYER, CARD):
        assert "isNaN(d)" in page, "нет запасного пути для неразобранной даты"
        assert "if (!iso) return ''" in page, "пустая дата не должна ломать строку"


def test_naive_timestamps_are_not_shifted():
    """В базе время БЕЗ пояса -- это местное время операции. Пересчитывать
    его в часовой пояс зрителя нельзя: «во сколько нашли» имеет смысл
    именно по времени места работ."""
    assert "toISOString" not in PLAYER
    assert "getTimezoneOffset" not in PLAYER
