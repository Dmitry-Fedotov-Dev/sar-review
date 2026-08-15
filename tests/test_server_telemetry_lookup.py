"""Тест интеграции резервного поиска телеметрии внутри sar_server.py
(get_telemetry_for_report) -- не только чистая функция в sar_common.py, но
и её реальное использование сервером для GPS в ручном плеере, когда рядом
с видео нет videoname.srt."""
import sar_common
import sar_server


def test_get_telemetry_for_report_falls_back_to_telemetry_dir(tmp_path, monkeypatch):
    # видео без .srt рядом -- имитирует ситуацию "телеметрия сложена в
    # отдельную папку telemetry/", а не рядом с самим видео
    video_path = tmp_path / "DJI_20260812140054_0003_Z.MP4"
    video_path.write_bytes(b"fake video content")

    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    srt_path = telemetry_dir / "DJI_20260812140054_0003_Z.SRT"
    srt_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "[latitude: 39.480800] [longitude: 73.592544] [rel_alt: 1162.281]\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sar_server, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX",
                         sar_common.build_telemetry_index(telemetry_dir))
    monkeypatch.setattr(sar_server, "_telemetry_cache", {})

    report = {"report_id": "rep1", "abs_path": str(video_path)}
    entries = sar_server.get_telemetry_for_report(report)

    assert len(entries) == 1
    _, _, data = entries[0]
    assert data["lat"] == 39.4808
    assert data["lon"] == 73.592544

    # второй вызов должен идти из кэша (без повторного чтения индекса/файла) --
    # проверяем, что кэш реально заполнился под тем же report_id
    assert "rep1" in sar_server._telemetry_cache


def test_get_telemetry_for_report_no_telemetry_anywhere_returns_empty(tmp_path, monkeypatch):
    video_path = tmp_path / "no_telemetry_video.MP4"
    video_path.write_bytes(b"fake")

    monkeypatch.setattr(sar_server, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX", {"by_stem": {}, "by_timestamp": []})
    monkeypatch.setattr(sar_server, "_telemetry_cache", {})

    report = {"report_id": "rep2", "abs_path": str(video_path)}
    assert sar_server.get_telemetry_for_report(report) == []
