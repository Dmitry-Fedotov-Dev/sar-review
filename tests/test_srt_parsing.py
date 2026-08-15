"""Тесты парсинга SRT-телеметрии (sar_video_review.parse_srt_telemetry) --
включая формат с явными подписями (реальный формат DJI, встреченный в
проекте) и формат-кортеж GPS(a,b,c), плюс новые поля gimbal (yaw/pitch/roll)
и абсолютную высоту (alt_abs), и устойчивость к отсутствующим/битым файлам.
"""
from sar_video_review import parse_srt_telemetry, lookup_telemetry, _clean_raw_telemetry_block

# Реальный формат, встреченный в проекте (DJI_20260812140054_0003_Z.SRT) --
# метка времени + iso/shutter/... + lat/lon подписанные + rel_alt/abs_alt +
# gb_yaw/gb_pitch/gb_roll.
REAL_DJI_SRT = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-08-12 14:00:54.744
[iso: 100] [shutter: 1/944.10] [fnum: 3.7] [ev: 0] [color_md : default] [ae_meter_md: 1] [focal_len: 240.70] [dzoom_ratio: 1.00], [latitude: 39.480800] [longitude: 73.592544] [rel_alt: 1162.281 abs_alt: 5160.425] [gb_yaw: 178.1 gb_pitch: -56.6 gb_roll: 0.0] </font>

2
00:00:00,033 --> 00:00:00,066
<font size="28">FrameCnt: 2, DiffTime: 33ms
2026-08-12 14:00:54.779
[iso: 100] [shutter: 1/944.10] [fnum: 3.7] [ev: 0] [color_md : default] [ae_meter_md: 1] [focal_len: 240.70] [dzoom_ratio: 1.00], [latitude: 39.481200] [longitude: 73.592900] [rel_alt: 1162.300 abs_alt: 5160.440] [gb_yaw: 178.2 gb_pitch: -56.5 gb_roll: 0.1] </font>
"""

GPS_TUPLE_SRT = """1
00:00:00,000 --> 00:00:01,000
GPS(42.500000, 74.500000, 1500.0)
"""


def test_parse_real_dji_labeled_format(tmp_path):
    srt = tmp_path / "video.srt"
    srt.write_text(REAL_DJI_SRT, encoding="utf-8")
    entries = parse_srt_telemetry(str(srt))
    assert len(entries) == 2

    start, end, data = entries[0]
    assert start == 0.0
    assert abs(end - 0.033) < 1e-6
    assert data["lat"] == 39.4808
    assert data["lon"] == 73.592544
    assert data["alt"] == 1162.281       # rel_alt -- совпадает с первым найденным "alt"
    assert data["alt_abs"] == 5160.425   # abs_alt -- отдельное новое поле
    assert data["yaw"] == 178.1
    assert data["pitch"] == -56.6
    assert data["roll"] == 0.0
    assert data["focal_len"] == 240.70
    # raw_telemetry -- ВЕСЬ текст блока как есть, включая поля, которые не
    # разбираются по отдельности (iso/shutter/fnum/ev/...), без HTML-тегов
    # <font> и без служебных строк (номер блока, диапазон таймкодов)
    assert "<font" not in data["raw_telemetry"]
    assert "00:00:00,000" not in data["raw_telemetry"]
    assert "iso: 100" in data["raw_telemetry"]
    assert "shutter: 1/944.10" in data["raw_telemetry"]
    assert "latitude: 39.480800" in data["raw_telemetry"]
    assert "gb_pitch: -56.6" in data["raw_telemetry"]

    # второй блок -- координаты сдвинулись (дрон летит)
    _, _, data2 = entries[1]
    assert data2["lat"] == 39.4812


def test_lookup_telemetry_by_timestamp(tmp_path):
    srt = tmp_path / "video.srt"
    srt.write_text(REAL_DJI_SRT, encoding="utf-8")
    entries = parse_srt_telemetry(str(srt))
    got = lookup_telemetry(entries, 0.05)  # попадает во второй блок (0.033-0.066)
    assert got["lat"] == 39.4812

    # вне диапазона -- все None, но КЛЮЧИ те же, что и у реальных записей
    # (yaw/pitch/roll/alt_abs включены даже в дефолт "не нашли")
    missing = lookup_telemetry(entries, 999)
    assert missing == {"lat": None, "lon": None, "alt": None, "alt_abs": None,
                        "yaw": None, "pitch": None, "roll": None, "focal_len": None,
                        "raw_telemetry": None}


def test_parse_gps_tuple_format_lat_lon_order(tmp_path):
    srt = tmp_path / "video.srt"
    srt.write_text(GPS_TUPLE_SRT, encoding="utf-8")
    entries = parse_srt_telemetry(str(srt), gps_tuple_order="lat_lon")
    _, _, data = entries[0]
    assert data["lat"] == 42.5
    assert data["lon"] == 74.5
    assert data["alt"] == 1500.0


def test_parse_gps_tuple_format_lon_lat_order(tmp_path):
    srt = tmp_path / "video.srt"
    srt.write_text(GPS_TUPLE_SRT, encoding="utf-8")
    entries = parse_srt_telemetry(str(srt), gps_tuple_order="lon_lat")
    _, _, data = entries[0]
    # порядок в файле физически (42.5, 74.5) -- при lon_lat трактуем как (lon, lat)
    assert data["lat"] == 74.5
    assert data["lon"] == 42.5


def test_missing_srt_file_returns_empty_list_without_crashing():
    assert parse_srt_telemetry("/no/such/file.srt") == []
    assert parse_srt_telemetry(None) == []


def test_unreadable_srt_path_degrades_gracefully_instead_of_crashing(tmp_path, capsys):
    # открыть директорию как файл -- гарантированно кидает OSError на Windows/POSIX,
    # имитирует "битый"/недоступный файл телеметрии
    directory_as_path = tmp_path / "not_a_file_dir"
    directory_as_path.mkdir()
    entries = parse_srt_telemetry(str(directory_as_path))
    assert entries == []
    assert "не удалось прочитать" in capsys.readouterr().out


def test_clean_raw_telemetry_block_strips_noise_keeps_all_data():
    block = (
        "3\n"
        "00:00:00,066 --> 00:00:00,100\n"
        '<font size="28">FrameCnt: 3, DiffTime: 34ms\n'
        "2026-08-12 14:00:54.813\n"
        "[iso: 100] [shutter: 1/944.10] [fnum: 3.7] [ev: 0] [focal_len: 240.70] "
        "[latitude: 39.480800] [longitude: 73.592544] [gb_pitch: -56.6] </font>"
    )
    cleaned = _clean_raw_telemetry_block(block)
    lines = cleaned.splitlines()
    assert "<font" not in cleaned
    assert "</font>" not in cleaned
    assert lines[0] != "3"  # номер блока субтитров вырезан, а не оставлен первой строкой
    assert "-->" not in cleaned  # диапазон таймкодов вырезан
    assert "FrameCnt: 3, DiffTime: 34ms" in cleaned
    assert "2026-08-12 14:00:54.813" in cleaned
    assert "iso: 100" in cleaned
    assert "focal_len: 240.70" in cleaned
    assert "gb_pitch: -56.6" in cleaned


def test_clean_raw_telemetry_block_handles_empty_block():
    assert _clean_raw_telemetry_block("") == ""
    assert _clean_raw_telemetry_block("\n\n") == ""
