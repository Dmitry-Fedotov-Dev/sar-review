"""Файл, из которого лёгкая копия не собирается, не должен держать очередь.

Копий делается ОДНА за проход (PROXIES_PER_PASS = 1). Файл без копии
выбирается следующим проходом снова -- и если собрать её невозможно, он
выбирается всегда. Очередь из восьмидесяти файлов встаёт намертво за
одним, и со стороны это выглядит как «платформа думает».

Ровно это и случилось 17.09.2026 на боевом подключении: в Google Диске
нашёлся MP4, залитый не до конца. Google отдаёт 593 МБ, а внутри файла
заявлено mdat на 3,44 ГБ -- то есть индекс, который DJI пишет в КОНЕЦ,
до хранилища не доехал. ffmpeg на таком падает с «moov atom not found»,
и дальше него дело не шло.

Проверка размера тут не спасает и не виновата: скачано ровно столько,
сколько облако и обещало. Неполон сам источник.
"""
import pathlib

import pytest


SRC = (pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
       ).read_text(encoding="utf-8")


def chunk():
    """Тело ensure_video_proxies."""
    i = SRC.index("def ensure_video_proxies")
    j = SRC.index("def ensure_finding_previews")
    return SRC[i:j]


def test_failed_build_is_backed_off():
    """ГЛАВНОЕ. Без паузы неудачный файл выбирается каждый проход и
    занимает единственный слот сборки."""
    body = chunk()
    assert '_may_try(build_key)' in body, (
        "перед сборкой не проверяется, не пора ли подождать")
    assert '_note_failure(build_key' in body, (
        "неудача сборки не запоминается -- пауза не начнётся")


def test_successful_build_clears_the_backoff():
    """Иначе один сбой оставит файл под паузой навсегда, хотя причина
    могла быть разовой (кончилось место, ffmpeg убили)."""
    assert "_note_success(build_key)" in chunk()


def test_backoff_key_is_per_file():
    """Общий ключ означал бы, что один битый файл ставит на паузу сборку
    копий ВООБЩЕ -- лекарство хуже болезни."""
    body = chunk()
    assert 'build_key = "proxy:" + name' in body


def test_reason_reaches_the_material_record():
    """Страница файла иначе говорит «в очереди» и молчит месяцами, а
    человек ждёт копию, которой не будет никогда. Это та самая категория
    молчаливых отказов, на которой проект уже обжигался."""
    body = chunk()
    assert "UPDATE reports SET error=?" in body
    assert "залит не" in body, "в тексте ошибки нет понятной человеку причины"


def test_reason_is_recorded_only_after_giving_up():
    """Одна неудача может быть случайной. Писать «файл битый» после
    первой -- пугать человека раньше времени."""
    body = chunk()
    i = body.index("UPDATE reports SET error=?")
    before = body[:i]
    assert "FAILURE_GIVE_UP_AFTER" in before[-400:], (
        "причина пишется без проверки, что попытки исчерпаны")


def test_failed_file_does_not_count_as_done():
    """made += 1 на неудаче означал бы, что проход считает работу
    сделанной и не возьмёт следующий файл."""
    body = chunk()
    i = body.index("if not built:")
    j = body.index("_note_success(build_key)")
    assert "made += 1" not in body[i:j]


def test_failed_file_keeps_its_request():
    """proxy_requested снимать нельзя: просьба человека в силе, и после
    перезапуска воркера попытку надо повторить -- вдруг файл дозалили."""
    body = chunk()
    i = body.index("if not built:")
    j = body.index("_note_success(build_key)")
    assert "proxy_requested=0" not in body[i:j]


@pytest.mark.parametrize("anchor", ["_may_try(build_key)", "_note_failure(build_key"])
def test_guard_sits_inside_the_loop(anchor):
    """Проверка вне цикла по файлам не сделала бы ничего."""
    body = chunk()
    i = body.index("for name, abs_path, cloud in jobs:")
    assert body.index(anchor) > i
