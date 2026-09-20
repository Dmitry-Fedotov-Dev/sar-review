"""Сорвавшаяся генерация превью не должна повторяться вечно.

_ensure_thumbnail и _ensure_duration вызываются для КАЖДОГО файла на
КАЖДОМ проходе наблюдения, а проход идёт раз в 15 секунд. Признак "надо
сделать" -- отсутствие файла превью на диске. Значит неудачная попытка
повторяется на следующем проходе. И на следующем. Вечно: 240 попыток в
час на один битый файл.

На локальном диске это почти безобидно -- лишнее чтение. Но материал
переезжает в облако, и там каждая попытка станет СКАЧИВАНИЕМ: один
проблемный файл будет молча выжигать квоту и канал, мешая всем остальным.
Причём молча -- именно та категория отказов, на которой этот проект уже
обжигался: платформа не падает, просто всё почему-то медленно.

Проверка нужна ДО переезда, а не после.
"""
import os

import pytest

import sar_worker as w


@pytest.fixture(autouse=True)
def clean_state():
    w._failures.clear()
    yield
    w._failures.clear()


@pytest.fixture
def clock(monkeypatch):
    """Управляемое время: тест не должен ничего ждать по-настоящему."""
    state = {"t": 1000.0}
    monkeypatch.setattr(w.time, "time", lambda: state["t"])
    return state


# --- базовое поведение ----------------------------------------------------

def test_first_attempt_is_always_allowed():
    assert w._may_try("thumb:новый.mp4")


def test_retry_is_blocked_right_after_a_failure(clock):
    w._note_failure("thumb:x", "превью")
    assert not w._may_try("thumb:x"), (
        "повтор разрешён сразу -- это и есть шторм раз в 15 секунд")


def test_retry_is_allowed_after_the_delay(clock):
    w._note_failure("thumb:x", "превью")
    clock["t"] += w.FAILURE_FIRST_DELAY_SEC + 1
    assert w._may_try("thumb:x")


def test_delay_grows_with_each_failure(clock):
    """Иначе битый файл всё равно долбится, просто чуть реже."""
    delays = []
    for _ in range(4):
        before = clock["t"]
        w._note_failure("thumb:x", "превью")
        _, not_before = w._failures["thumb:x"]
        delays.append(not_before - before)
    assert delays == sorted(delays), delays
    assert delays[-1] > delays[0] * 3


def test_delay_is_capped(clock):
    """Без потолка задержка уходит в годы, и файл не починится даже когда
    причина уже устранена."""
    for _ in range(20):
        w._note_failure("thumb:x", "превью")
    _, not_before = w._failures["thumb:x"]
    assert not_before - clock["t"] <= w.FAILURE_MAX_DELAY_SEC


def test_gives_up_after_a_number_of_attempts(clock):
    for _ in range(w.FAILURE_GIVE_UP_AFTER):
        w._note_failure("thumb:x", "превью")
    clock["t"] += 10 ** 6          # сколько угодно времени спустя
    assert not w._may_try("thumb:x")


def test_success_clears_the_record(clock):
    """Файл дописался/сеть вернулась -- счётчик обнуляется, иначе через
    неделю нормальной работы мы всё равно перестанем его обновлять."""
    w._note_failure("thumb:x", "превью")
    w._note_success("thumb:x")
    assert w._may_try("thumb:x")


def test_failures_are_tracked_per_file(clock):
    """Один битый файл не должен блокировать остальные."""
    w._note_failure("thumb:битый.mp4", "превью")
    assert w._may_try("thumb:нормальный.mp4")


def test_giving_up_is_announced_exactly_once(clock, capsys):
    """Молчать нельзя -- файл останется без превью, и это должно быть
    видно. Но и писать в лог на каждой попытке тоже нельзя."""
    for _ in range(w.FAILURE_GIVE_UP_AFTER + 3):
        w._note_failure("thumb:x", "превью")
    out = capsys.readouterr().out
    assert out.count("больше не пробую") == 1, out


# --- связь с настоящими вызовами ------------------------------------------

def test_thumbnail_failure_is_recorded(tmp_path, monkeypatch, clock):
    """Проверка не условия, а проводки: реальный _ensure_thumbnail при
    неудаче обязан отметить её, иначе весь backoff бесполезен."""
    monkeypatch.setattr(w, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(w, "_generate_thumbnail", lambda *a, **k: False)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")

    w._ensure_thumbnail("video.mp4", str(src), kind="video")
    assert "thumb:video.mp4" in w._failures, "неудача не записана"

    # второй проход не должен даже пытаться
    calls = []
    monkeypatch.setattr(w, "_generate_thumbnail",
                        lambda *a, **k: calls.append(1) or False)
    w._ensure_thumbnail("video.mp4", str(src), kind="video")
    assert calls == [], "повторная попытка сразу же -- backoff не работает"


def test_thumbnail_success_leaves_no_record(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(w, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(w, "_generate_thumbnail", lambda *a, **k: True)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    w._ensure_thumbnail("video.mp4", str(src), kind="video")
    assert "thumb:video.mp4" not in w._failures


# --- бюджет на проход -----------------------------------------------------
#
# Отдельно от backoff: тот спасает от ПОВТОРОВ после неудачи, а этот -- от
# первого массового прохода. Подключили облачную папку с 56 файлами --
# и платформа полезет читать все 56 сразу, 21,3 ГБ, без единого запроса
# от человека.

def test_budget_counts_only_real_file_reads(tmp_path, monkeypatch, clock):
    """Дешёвая проверка «превью уже есть» бюджет тратить не должна --
    иначе за проход мы осмотрим три файла вместо всей папки."""
    monkeypatch.setattr(w, "DATA_DIR", str(tmp_path), raising=False)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    thumb = w.sar_common.get_thumbnail_path(str(tmp_path), "video.mp4")
    os.makedirs(os.path.dirname(thumb), exist_ok=True)
    with open(thumb, "wb") as f:
        f.write(b"t")
    # Состариваем ИСХОДНИК, а не превью: признак «надо перегенерировать» --
    # это исходник новее превью. (В первой версии теста время 2001 года
    # было поставлено превью и подписано «заведомо новее» -- тест честно
    # упал, потому что проверял ровно обратное тому, что задумано.)
    os.utime(src, (10 ** 9, 10 ** 9))

    before = w._touches_used
    w._ensure_thumbnail("video.mp4", str(src), kind="video")
    assert w._touches_used == before, "готовое превью потратило бюджет"


def test_budget_is_spent_on_a_real_generation(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(w, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(w, "_generate_thumbnail", lambda *a, **k: True)
    src = tmp_path / "other.mp4"
    src.write_bytes(b"x")
    before = w._touches_used
    w._ensure_thumbnail("other.mp4", str(src), kind="video")
    assert w._touches_used == before + 1


def test_budget_default_is_small_enough_to_matter():
    """Смысл ограничения в том, чтобы подключение большой папки не
    превращалось в лавину скачиваний. Значение живёт в реестре настроек --
    второго умолчания в воркере быть не должно, два значения одного смысла
    рано или поздно разойдутся."""
    import sar_common
    spec = sar_common.SETTINGS_SCHEMA["material_touches_per_pass"]
    assert 1 <= spec["default"] <= 10
    assert not hasattr(w, "MATERIAL_TOUCHES_PER_PASS"), (
        "в воркере снова заведено второе умолчание")
