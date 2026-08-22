"""Лёгкая копия видео для плеера.

Съёмка с дрона идёт на 30 Мбит/с. Чтобы смотреть её в реальном времени,
столько же нужно КАЖДОМУ зрителю, а канал наружу один -- при нескольких
зрителях видео просто не успевает грузиться. 34 файла операции весят 22.7 ГБ.

Замеры, на которых построено решение:
  * moov (индекс) лежит В КОНЦЕ файла у всех проверенных исходников, то
    есть браузер вынужден сначала тянуть хвост, прежде чем начать играть;
  * разрешение понижать НЕЛЬЗЯ: человек ищет объекты в десяток пикселей,
    720p съел бы половину линейного размера;
  * crf 26 при 1080p даёт примерно вчетверо меньший файл, и реальная
    находка -- сине-жёлтый предмет 41x32 px на осыпи -- остаётся отчётливо
    видна. Сжатие съедает мелкую фактуру камней, а находки различаются
    цветом и формой, и это кодек сохраняет.
"""
import os
import re

import pytest

import sar_common
import sar_server
import sar_worker


PLAYER = sar_server.PLAYER_PAGE_HTML


# --- где лежит копия ------------------------------------------------------

def test_proxy_path_is_unique_per_material(tmp_path):
    """Файлы с одинаковыми именами в разных папках операции -- обычное дело
    для облачных выгрузок, и копии не должны перезаписывать друг друга."""
    a = sar_common.proxy_video_path(str(tmp_path), "борт 1/DJI_0001.MP4")
    b = sar_common.proxy_video_path(str(tmp_path), "борт 2/DJI_0001.MP4")
    assert a != b
    assert a.endswith(".mp4")


def test_proxies_folder_is_not_scanned_as_material():
    """Иначе копии сами появились бы в списке материалов как новые видео,
    и воркер начал бы обрабатывать их детектором."""
    assert "proxies" in sar_common.SERVICE_DIRS


# --- как кодируем ---------------------------------------------------------

def test_resolution_is_not_reduced():
    """Главное ограничение: человек ищет объекты размером в десяток
    пикселей, и понижение разрешения -- прямая потеря того, ради чего всё
    делается."""
    import inspect
    src = inspect.getsource(sar_worker._build_proxy)
    for banned in ("-vf", "scale=", "-s "):
        assert banned not in src, f"в команду попал масштаб ({banned})"


def test_index_is_moved_to_the_front():
    """У исходников с дрона moov в конце: браузер сначала тянет хвост."""
    import inspect
    src = inspect.getsource(sar_worker._build_proxy)
    assert "+faststart" in src


def test_only_the_main_video_stream_is_taken():
    """В файлах с дрона рядом лежат служебный поток и mjpeg-превьюшка."""
    import inspect
    src = inspect.getsource(sar_worker._build_proxy)
    assert '"-map", "0:v:0"' in src


def test_encoding_does_not_starve_the_detector():
    """Детектор важнее: копия -- удобство."""
    import inspect
    src = inspect.getsource(sar_worker._build_proxy)
    assert "_low_priority_kwargs()" in src
    assert '"-threads"' in src


def test_worker_keeps_reporting_alive_while_encoding():
    """Кодирование занимает минуты, а отметка «воркер жив» ставится в конце
    прохода. Простое ожидание означало бы многоминутную паузу -- и
    мониторинг доложил бы, что воркер умер, хотя он занят делом."""
    import inspect
    src = inspect.getsource(sar_worker._build_proxy)
    assert "touch_heartbeat" in src
    assert "proc.poll()" in src


def test_unfinished_file_is_never_visible():
    """Пока идёт кодирование, обрубок не должен попасть ни серверу, ни
    следующему проходу."""
    import inspect
    src = inspect.getsource(sar_worker._build_proxy)
    assert "os.replace(tmp, out_path)" in src
    assert '".tmp.mp4"' in src


def test_one_proxy_per_pass():
    """Каждая копия занимает минуты -- проход наблюдения не должен на них
    вставать."""
    assert sar_worker.PROXIES_PER_PASS == 1


def test_missing_ffmpeg_is_not_a_crash():
    """ffmpeg есть не на каждой машине, а платформа должна работать и без
    лёгких копий."""
    import inspect
    src = inspect.getsource(sar_worker.ensure_video_proxies)
    assert "_ffmpeg_available()" in src


def test_feature_can_be_switched_off():
    import inspect
    src = inspect.getsource(sar_worker.ensure_video_proxies)
    assert 'CFG.get("proxy_video"' in src
    assert sar_common.DEFAULT_SERVER_CONFIG["proxy_video"] is True
    assert sar_common.DEFAULT_SERVER_CONFIG["proxy_crf"] == 26


# --- отдача ---------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    src = watch / "DJI_1.MP4"
    src.write_bytes(b"x" * 5000)
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "                      created_at, updated_at) "
        "VALUES ('r1', 'DJI_1.MP4', ?, 'video', 'done', "
        "        '2026-08-15T10:00', '2026-08-15T10:00')", (str(src),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", data_dir, raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "Тестовый"
    c.data_dir = data_dir
    c.src = src
    return c


def make_proxy(client, size=900):
    path = sar_common.proxy_video_path(client.data_dir, "DJI_1.MP4")
    with open(path, "wb") as f:
        f.write(b"y" * size)
    return path


def test_proxy_is_served_by_default(client):
    make_proxy(client)
    r = client.get("/report/r1/video")
    assert r.status_code == 200
    assert r.get_data() == b"y" * 900, "отдан оригинал вместо копии"


def test_original_is_available_on_request(client):
    """Сжатие, которое нельзя обойти, -- тихое ухудшение инструмента."""
    make_proxy(client)
    r = client.get("/report/r1/video?original=1")
    assert r.get_data() == b"x" * 5000, "оригинал недоступен"


def test_original_is_served_while_the_proxy_is_not_ready(client):
    """Копия делается минутами: до этого видео обязано играть как раньше."""
    r = client.get("/report/r1/video")
    assert r.get_data() == b"x" * 5000


def test_video_info_reports_both_sizes(client):
    make_proxy(client)
    info = client.get("/api/report/r1/video_info").get_json()
    assert info["proxy"] is True
    assert info["original_mb"] >= 0 and info["proxy_mb"] >= 0


def test_video_info_when_there_is_no_proxy(client):
    assert client.get("/api/report/r1/video_info").get_json()["proxy"] is False


# --- плеер честно говорит, что играет ------------------------------------

def test_player_offers_the_switch():
    assert 'id="use-original"' in PLAYER
    assert "function initVideoSource" in PLAYER


def test_switch_is_hidden_until_a_proxy_exists():
    """Пока копии нет, переключать нечего, и лишний элемент только
    запутает."""
    assert 'id="src-switch" hidden' in PLAYER
    assert "if (!info.proxy) return;" in PLAYER


def test_switch_shows_how_much_lighter_the_copy_is():
    assert "копия ${{info.proxy_mb}} МБ вместо ${{info.original_mb}} МБ" in PLAYER


def test_switching_keeps_the_position(client=None):
    """Человек смотрит конкретный момент -- переключение источника не
    должно отбрасывать его в начало."""
    body = PLAYER[PLAYER.index("cb.addEventListener('change'"):]
    body = body[:body.index("}});")]
    assert "const at = video.currentTime" in body
    assert "video.currentTime = at" in body
