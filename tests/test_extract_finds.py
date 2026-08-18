"""Тесты нарезки кадров по реестру находок.

Работаем с НАСТОЯЩИМИ видеофайлами (cv2 их и пишет, и читает), а не с
моками: весь смысл скрипта в том, попадёт ли он в нужную секунду реального
файла, и мок именно это и спрятал бы.

Отдельно проверяем разбор таймкодов на числах с известным ответом. Ошибка
здесь тише всего и дороже всех: скрипт отработает без единой жалобы и
принесёт кадры не из того места, а на глаз снежный склон в 1:25 и в 12:53
неотличимы.
"""
import json
import os

import cv2
import numpy as np
import pytest

import sar_extract_finds as ef


# --- разбор таймкодов ----------------------------------------------------

@pytest.mark.parametrize("tc,sec", [
    ("0:15", 15.0),
    ("3:26", 206.0),
    ("1:25.3", 85.3),        # восстановленная десятичная доля
    ("1:02:03", 3723.0),     # часы
    ("95", 95.0),            # просто секунды
    ("0:45.3", 45.3),
])
def test_parse_timecode(tc, sec):
    assert ef.parse_tc(tc) == pytest.approx(sec)


def test_parse_timecode_rejects_garbage():
    with pytest.raises(ValueError):
        ef.parse_tc("три минуты")


def test_parse_timecode_none_is_none():
    assert ef.parse_tc(None) is None


# --- имена папок ---------------------------------------------------------

def test_slug_keeps_cyrillic_readable():
    """Имя папки должно оставаться читаемым: по нему человек ищет находку."""
    s = ef.slug("Синий рюкзак Николая")
    assert "Синий" in s and "рюкзак" in s
    assert "/" not in s and "\\" not in s and " " not in s


def test_slug_never_empty():
    assert ef.slug("???") != ""


# --- поиск исходников ----------------------------------------------------

def _make_video(path, seconds=6, fps=10, w=64, h=48):
    """Настоящий mp4: в каждом кадре вписан его номер, чтобы можно было
    проверить, ИЗ КАКОГО МЕСТА видео взят кадр, а не только сколько их."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    for i in range(seconds * fps):
        frame = np.full((h, w, 3), i % 250, np.uint8)
        vw.write(frame)
    vw.release()
    return path


def test_index_finds_video_regardless_of_extension_case(tmp_path):
    _make_video(tmp_path / "DJI_TEST_0001_Z.MP4")
    idx = ef.index_sources([str(tmp_path)])
    assert "dji_test_0001_z" in idx


def test_index_searches_subfolders(tmp_path):
    sub = tmp_path / "полёты 13.08"
    sub.mkdir()
    _make_video(sub / "C0049.MP4")
    idx = ef.index_sources([str(tmp_path)])
    assert "c0049" in idx


def test_report_output_dirs_are_not_scanned(tmp_path):
    """sar_data содержит сотни тысяч кропов, вырезанных ИЗ этих же видео.
    Обходить её долго, а главное -- кроп не должен подменить оригинал."""
    (tmp_path / "sar_data" / "reports" / "r1").mkdir(parents=True)
    (tmp_path / "sar_data" / "reports" / "r1" / "C0049.jpg").write_bytes(b"x")
    real = _make_video(tmp_path / "C0049.MP4")
    idx = ef.index_sources([str(tmp_path)])
    assert idx["c0049"] == str(real), "нашёлся кроп вместо оригинала"


def test_skip_list_covers_dataset_output_too(tmp_path):
    (tmp_path / "sar_dataset").mkdir()
    (tmp_path / "sar_dataset" / "old.jpg").write_bytes(b"x")
    idx = ef.index_sources([str(tmp_path)])
    assert "old" not in idx


def test_resolve_matches_manifest_entry_with_extension(tmp_path):
    _make_video(tmp_path / "C0049.MP4")
    idx = ef.index_sources([str(tmp_path)])
    assert ef.resolve({"file": "C0049.MP4"}, idx) is not None
    # в реестре часть имён записана БЕЗ расширения -- должно находиться тоже
    _make_video(tmp_path / "DJI_20260813183004_0001_Z.MP4")
    idx = ef.index_sources([str(tmp_path)])
    assert ef.resolve({"file": "DJI_20260813183004_0001_Z"}, idx) is not None


def test_missing_source_resolves_to_none(tmp_path):
    idx = ef.index_sources([str(tmp_path)])
    assert ef.resolve({"file": "нет_такого.MP4"}, idx) is None


# --- собственно нарезка --------------------------------------------------

def test_extracts_expected_number_of_frames(tmp_path):
    src = str(_make_video(tmp_path / "v.MP4", seconds=6, fps=10))
    out = tmp_path / "out"
    out.mkdir()
    n, err = ef.extract_video(src, {"start": "0:02", "end": "0:04"},
                              str(out), fps=2.0, pad=0.0, quality=90)
    assert err is None
    # окно 2 с при 2 кадр/с -> 2.0, 2.5, 3.0, 3.5, 4.0 = 5 кадров
    assert n == 5
    assert len(list(out.iterdir())) == 5


def test_pad_widens_the_window(tmp_path):
    """Таймкоды из чужого разбора неточны -- запас обязан расширять окно."""
    src = str(_make_video(tmp_path / "v.MP4", seconds=6, fps=10))
    out = tmp_path / "out"
    out.mkdir()
    n0, _ = ef.extract_video(src, {"start": "0:03", "end": "0:03"},
                             str(out), fps=2.0, pad=0.0, quality=90)
    for f in out.iterdir():
        f.unlink()
    n1, _ = ef.extract_video(src, {"start": "0:03", "end": "0:03"},
                             str(out), fps=2.0, pad=1.0, quality=90)
    assert n1 > n0


def test_frame_filename_encodes_the_timecode(tmp_path):
    """По имени файла должно быть видно, с какой секунды кадр -- иначе
    размеченный кадр не связать обратно с находкой."""
    src = str(_make_video(tmp_path / "v.MP4", seconds=6, fps=10))
    out = tmp_path / "out"
    out.mkdir()
    ef.extract_video(src, {"start": "0:03", "end": "0:03"},
                     str(out), fps=1.0, pad=0.0, quality=90)
    names = [p.name for p in out.iterdir()]
    assert any("t0003.00" in n for n in names), names


def test_timecode_past_end_of_video_is_reported_not_crashed(tmp_path):
    """Таймкод может не совпасть с нашей копией файла -- это надо СКАЗАТЬ,
    а не молча выдать ноль кадров."""
    src = str(_make_video(tmp_path / "v.MP4", seconds=3, fps=10))
    out = tmp_path / "out"
    out.mkdir()
    n, err = ef.extract_video(src, {"start": "9:99", "end": "9:99"},
                              str(out), fps=1.0, pad=0.0, quality=90)
    assert n == 0
    assert err is not None and "предел" in err


def test_unreadable_file_is_reported(tmp_path):
    bad = tmp_path / "broken.MP4"
    bad.write_bytes(b"not a video")
    out = tmp_path / "out"
    out.mkdir()
    n, err = ef.extract_video(str(bad), {"start": "0:01", "end": "0:01"},
                              str(out), fps=1.0, pad=0.0, quality=90)
    assert n == 0 and err is not None


# --- пути с кириллицей (грабли Windows) ----------------------------------

def test_reads_and_writes_through_non_ascii_paths(tmp_path):
    """cv2.imread/imwrite молча не работают на путях с кириллицей в Windows --
    в проекте на это уже наступали."""
    d = tmp_path / "Съёмки полётов"
    d.mkdir()
    img = np.full((20, 30, 3), 77, np.uint8)
    p = d / "кадр.jpg"
    assert ef.imwrite_any(str(p), img)
    back = ef.imread_any(str(p))
    assert back is not None and back.shape == (20, 30, 3)


# --- сам реестр ----------------------------------------------------------

def test_manifest_is_valid_and_complete():
    with open(os.path.join(ef.HERE, "finds_manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    assert man.get("source"), "происхождение таймкодов обязано быть указано"
    for fd in man["finds"]:
        assert fd.get("file") and fd.get("item")
        assert fd.get("status") in ("confirmed", "candidate")
        if not fd.get("photo"):
            assert ef.parse_tc(fd["start"]) is not None, fd


def test_manifest_contains_all_five_confirmed_items():
    with open(os.path.join(ef.HERE, "finds_manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    items = " ".join(x["item"].lower() for x in man["finds"]
                     if x.get("status") == "confirmed")
    for thing in ("рюкзак", "спальник", "крышка", "стакан", "палка"):
        assert thing in items, f"в реестре нет подтверждённой находки: {thing}"


def test_confirmed_finds_carry_coordinates():
    """Координата -- половина ценности находки: по ней сверяют место."""
    with open(os.path.join(ef.HERE, "finds_manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    for fd in man["finds"]:
        if fd.get("status") == "confirmed":
            assert fd.get("lat") and fd.get("lon"), fd["item"]
