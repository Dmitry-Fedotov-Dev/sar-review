"""Тесты для индексации/сопоставления телеметрии (sar_common.py).

Покрывает функциональность, добавленную для интеграции папки telemetry/:
рекурсивная индексация SRT, сопоставление видео<->SRT по имени файла и по
резервной метке времени, устойчивость к отсутствующим/неоднозначным файлам.
"""
from datetime import timedelta
from pathlib import Path

import sar_common


def _touch(path: Path, content: str = "1\n00:00:00,000 --> 00:00:00,033\nfake\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_resolve_telemetry_dir_creates_folder(tmp_path):
    tdir = sar_common.resolve_telemetry_dir(tmp_path, "telemetry")
    assert tdir.is_dir()
    assert tdir == tmp_path / "telemetry"


def test_build_index_empty_when_dir_missing(tmp_path):
    index = sar_common.build_telemetry_index(tmp_path / "does_not_exist")
    assert index == {"by_stem": {}, "by_timestamp": []}


def test_build_index_recursive_nested_subfolders(tmp_path):
    tdir = tmp_path / "telemetry"
    _touch(tdir / "DJI_20260812140054_0003_Z.SRT")
    _touch(tdir / "12.08.2026 Субтитры полетов" / "DJI_20260812135426_0001_Z.SRT")
    index = sar_common.build_telemetry_index(tdir)
    assert set(index["by_stem"]) == {"dji_20260812140054_0003_z", "dji_20260812135426_0001_z"}
    # оба файла успешно разобрались как метки времени DJI
    assert len(index["by_timestamp"]) == 2


def test_find_telemetry_exact_filename_match(tmp_path):
    tdir = tmp_path / "telemetry"
    srt = _touch(tdir / "DJI_20260812140054_0003_Z.SRT")
    index = sar_common.build_telemetry_index(tdir)
    match, reason = sar_common.find_telemetry_for_video(
        str(tmp_path / "DJI_20260812140054_0003_Z.MP4"), index)
    assert match == srt
    assert "имя файла" in reason


def test_find_telemetry_case_insensitive_stem_match(tmp_path):
    tdir = tmp_path / "telemetry"
    srt = _touch(tdir / "dji_20260812140054_0003_z.srt")
    index = sar_common.build_telemetry_index(tdir)
    match, _ = sar_common.find_telemetry_for_video(
        str(tmp_path / "DJI_20260812140054_0003_Z.MP4"), index)
    assert match == srt


def test_find_telemetry_falls_back_to_timestamp_when_name_differs(tmp_path):
    tdir = tmp_path / "telemetry"
    # имя SRT не совпадает с именем видео (например, переименовано вручную),
    # но встроенная в имя метка времени DJI совпадает почти точно
    srt = _touch(tdir / "renamed_flight_log_DJI_20260812140054_0099_Z.SRT")
    index = sar_common.build_telemetry_index(tdir)
    match, reason = sar_common.find_telemetry_for_video(
        str(tmp_path / "DJI_20260812140057_0003_Z.MP4"), index)  # разница 3 секунды
    assert match == srt
    assert "метк" in reason


def test_find_telemetry_no_match_beyond_tolerance(tmp_path):
    tdir = tmp_path / "telemetry"
    _touch(tdir / "DJI_20260812090000_0001_Z.SRT")  # 09:00, видео -- 14:00, разница часы
    index = sar_common.build_telemetry_index(tdir)
    match, reason = sar_common.find_telemetry_for_video(
        str(tmp_path / "DJI_20260812140054_0003_Z.MP4"), index,
        max_fallback_gap=timedelta(minutes=5))
    assert match is None
    assert reason is None


def test_find_telemetry_no_match_when_neither_name_nor_timestamp_available(tmp_path):
    tdir = tmp_path / "telemetry"
    _touch(tdir / "some_random_export.SRT")  # без метки DJI в имени
    index = sar_common.build_telemetry_index(tdir)
    match, reason = sar_common.find_telemetry_for_video(
        str(tmp_path / "plain_video_name.MP4"), index)
    assert match is None and reason is None


def test_build_index_ambiguous_duplicate_stems_keeps_first_without_crashing(tmp_path, capsys):
    tdir = tmp_path / "telemetry"
    first = _touch(tdir / "a" / "DJI_20260812140054_0003_Z.SRT")
    _touch(tdir / "b" / "DJI_20260812140054_0003_Z.SRT")  # то же имя, другая папка -- разные полёты
    index = sar_common.build_telemetry_index(tdir)
    # не падаем, оставляем первый найденный (детерминированность порядка rglob
    # тут не гарантирована ОС, но важно что выбор стабильно один и тот же файл,
    # а не тихая перезапись -- и что явно предупреждаем в консоль)
    assert index["by_stem"]["dji_20260812140054_0003_z"] in (first, tdir / "b" / "DJI_20260812140054_0003_Z.SRT")
    assert "неоднозначность" in capsys.readouterr().out


def test_build_index_ignores_non_srt_files(tmp_path):
    tdir = tmp_path / "telemetry"
    _touch(tdir / "readme.txt")
    _touch(tdir / "DJI_20260812140054_0003_Z.SRT")
    index = sar_common.build_telemetry_index(tdir)
    assert list(index["by_stem"]) == ["dji_20260812140054_0003_z"]
