"""Оборванное чтение видео не должно выглядеть как законченная работа.

cap.read() возвращает False и когда видео действительно кончилось, и когда
файл прочитать не удалось: битый кадр, отвалившийся сетевой диск,
недокачанный файл. Цикл в обоих случаях просто заканчивается, и дальше
пишется нормальный на вид отчёт -- по половине видео.

Ни ошибки, ни предупреждения: статус "готово", отчёт открывается, сцены
есть. Человек видит разобранный материал и идёт дальше, а вторую половину
не смотрел никто. Для поисковой операции это самый дорогой вид отказа.

Сейчас, на локальном диске, обрыв редок. Когда материал переедет в облако,
он станет обычным делом -- поэтому проверка нужна до переезда, а не после.
"""
import pytest

import sar_video_review as v


def shortfall_is_fatal(read, total):
    """Повторяет условие из детектора: недобор больше допуска -- отказ."""
    if total <= 0:
        return False
    slack = max(v.MIN_FRAME_SLACK, int(total * v.FRAME_SHORTFALL_TOLERANCE))
    return read < total - slack


# --- когда это отказ ------------------------------------------------------

def test_half_a_video_is_an_error():
    assert shortfall_is_fatal(500, 1000)


def test_losing_a_tenth_is_an_error():
    """Десять процентов видео -- это минуты съёмки, которые никто не
    посмотрел. Молчать об этом нельзя."""
    assert shortfall_is_fatal(900, 1000)


def test_almost_nothing_read_is_an_error():
    assert shortfall_is_fatal(3, 1000)


# --- когда это НЕ отказ ---------------------------------------------------

def test_complete_read_is_fine():
    assert not shortfall_is_fatal(1000, 1000)


def test_one_frame_short_is_tolerated():
    """У части контейнеров счётчик в заголовке приблизительный. Ложная
    тревога в поле хуже отсутствующей: её быстро учатся игнорировать, и
    вместе с ней перестают замечать настоящие."""
    assert not shortfall_is_fatal(999, 1000)


def test_tolerance_scales_with_length():
    """На длинном видео один процент -- это уже десятки кадров."""
    assert not shortfall_is_fatal(9950, 10000)
    assert shortfall_is_fatal(9800, 10000)


def test_short_video_gets_absolute_slack_not_percentage():
    """На видео в 50 кадров один процент -- это ноль, то есть строгое
    равенство. Поэтому есть минимальный абсолютный запас."""
    assert not shortfall_is_fatal(49, 50)
    assert not shortfall_is_fatal(48, 50)
    assert shortfall_is_fatal(40, 50)


def test_unknown_frame_count_is_not_reported_as_failure():
    """Счётчика нет -- проверить нечем. Выдумывать тревогу там, где нет
    данных, значит приучить людей её игнорировать."""
    assert not shortfall_is_fatal(10, 0)
    assert not shortfall_is_fatal(10, -1)


# --- что именно происходит при отказе -------------------------------------

def test_error_type_exists_and_is_catchable():
    assert issubclass(v.IncompleteReadError, RuntimeError)


def test_detector_raises_instead_of_writing_a_report():
    """Ключевое: отчёт НЕ пишется. Записанный отчёт по половине видео
    выглядит готовым, и именно поэтому опасен."""
    import inspect
    src = inspect.getsource(v.process_video)
    idx = src.index("IncompleteReadError")
    tail = src[idx:]
    assert "save_outputs" not in src[:idx].split("cap.release()")[-1], (
        "отчёт пишется до проверки -- проверка бесполезна")
    assert "raise" in src[max(0, idx - 40):idx + 20]


def test_error_message_names_the_numbers():
    """Человеку нужно понять масштаб потери, а не просто увидеть слово
    «ошибка»."""
    import inspect
    src = inspect.getsource(v.process_video)
    assert "кадров из" in src


# --- настоящий путь исполнения --------------------------------------------
#
# Проверки выше повторяют условие, а не выполняют его. Этого мало: условие
# могли бы посчитать правильно и не применить. Ниже подменяется чтение
# видео так, чтобы оно "оборвалось" на середине, и запускается настоящий
# process_video.

class _StubCapture:
    """Видео, которое обещает many кадров, а отдаёт few."""

    def __init__(self, few, many):
        self.few, self.many, self.i = few, many, 0

    def isOpened(self):
        return True

    def get(self, prop):
        import cv2
        if prop == cv2.CAP_PROP_FPS:
            return 30.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return self.many
        return 0

    def read(self):
        import numpy as np
        if self.i >= self.few:
            return False, None           # ровно то, что вернёт битый файл
        self.i += 1
        return True, np.zeros((16, 16, 3), dtype=np.uint8)

    def release(self):
        pass


@pytest.fixture
def stubbed(monkeypatch):
    """Отключаем модель и телеметрию -- проверяется чтение, не детекция."""
    monkeypatch.setattr(v, "load_model", lambda *a, **k: None)
    monkeypatch.setattr(v, "detect_frame_tiled", lambda *a, **k: [])
    monkeypatch.setattr(v, "detect_color_anomalies", lambda *a, **k: [])
    monkeypatch.setattr(v, "parse_srt_telemetry", lambda *a, **k: {})
    return monkeypatch


def test_truncated_video_raises_and_writes_no_report(stubbed, tmp_path):
    """Главная проверка всего файла."""
    import cv2
    stubbed.setattr(cv2, "VideoCapture", lambda *a, **k: _StubCapture(300, 1000))
    out = tmp_path / "out"
    with pytest.raises(v.IncompleteReadError) as e:
        v.process_video("fake.mp4", None, "custom", ["person"], 0.3,
                        640, 64, 1, str(out))
    assert "300" in str(e.value) and "1000" in str(e.value)
    assert not (out / "report.html").exists(), (
        "отчёт по неполному видео записан -- он выглядит готовым")


def test_complete_video_is_processed_normally(stubbed, tmp_path):
    """Контроль: та же подмена, но видео прочитано полностью -- отчёт есть.

    Без этого теста предыдущий проходил бы и в случае, когда детектор
    сломан целиком и падает на любом видео.
    """
    import cv2
    stubbed.setattr(cv2, "VideoCapture", lambda *a, **k: _StubCapture(1000, 1000))
    out = tmp_path / "out"
    v.process_video("fake.mp4", None, "custom", ["person"], 0.3,
                    640, 64, 1, str(out))
    assert (out / "report.html").exists()


def test_unknown_frame_count_does_not_block_processing(stubbed, tmp_path):
    """Контейнер не сообщил число кадров -- работаем как раньше."""
    import cv2
    stubbed.setattr(cv2, "VideoCapture", lambda *a, **k: _StubCapture(50, 0))
    out = tmp_path / "out"
    v.process_video("fake.mp4", None, "custom", ["person"], 0.3,
                    640, 64, 1, str(out))
    assert (out / "report.html").exists()
