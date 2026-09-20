"""Превью облачного материала БЕЗ скачивания файла целиком.

Облачная запись без превью -- пустой квадрат в списке. На подключённом
хранилище так выглядели все 145 записей, которые ещё не готовили, и
страница переставала быть обозримой глазами -- а смотрят на неё именно
за этим.

Качать ради одного кадра сотни мегабайт незачем. ffmpeg читает по HTTP и
берёт только нужное: индекс (у DJI он в КОНЦЕ файла) и начало первого
кадра. Замерено на боевом подключении 17.09.2026:

    видео  2684 МБ исходник  ->  4,8 МБ по сети, 6 с
    снимок    7 МБ исходник  ->  8,1 МБ по сети, 3 с

Отдельного пути для снимков не понадобилось: ffmpeg читает удалённый
JPEG так же, как видео.
"""
import os
import subprocess

import pytest

import sar_worker


@pytest.fixture
def seen(monkeypatch):
    """Перехватывает вызов ffmpeg, не запуская его."""
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "kw": kw})
        # ffmpeg пишет во временный путь -- последний аргумент
        with open(cmd[-1], "wb") as f:
            f.write(b"\xff\xd8jpeg")
        return Result()

    monkeypatch.setattr(sar_worker.subprocess, "run", fake_run)
    monkeypatch.setattr(sar_worker, "_ffmpeg_available", lambda: True)
    return calls


# --- как зовём ffmpeg ------------------------------------------------------

def test_thumbnail_is_made_without_downloading(seen, tmp_path):
    out = str(tmp_path / "t.jpg")
    assert sar_worker._generate_thumbnail_remote(
        "https://x/f?alt=media", {"Authorization": "Bearer T"}, out)
    assert os.path.exists(out)
    cmd = seen[0]["cmd"]
    assert "https://x/f?alt=media" in cmd
    assert "-frames:v" in cmd and "1" in cmd


def test_auth_header_is_passed(seen, tmp_path):
    """Без заголовка Google отвечает отказом, а cv2 передать его не умеет --
    ради этого здесь ffmpeg, а не привычный VideoCapture."""
    sar_worker._generate_thumbnail_remote(
        "https://x", {"Authorization": "Bearer СЕКРЕТ"}, str(tmp_path / "t.jpg"))
    cmd = seen[0]["cmd"]
    i = cmd.index("-headers")
    assert "Authorization: Bearer СЕКРЕТ" in cmd[i + 1]


def test_headers_end_with_crlf(seen, tmp_path):
    """ffmpeg ждёт заголовки одной строкой через CRLF, и завершающий
    перевод обязателен -- без него последний заголовок молча не доедет."""
    sar_worker._generate_thumbnail_remote(
        "https://x", {"Authorization": "Bearer T"}, str(tmp_path / "t.jpg"))
    blob = seen[0]["cmd"][seen[0]["cmd"].index("-headers") + 1]
    assert blob.endswith("\r\n")


def test_several_headers_are_separated(seen, tmp_path):
    sar_worker._generate_thumbnail_remote(
        "https://x", {"A": "1", "B": "2"}, str(tmp_path / "t.jpg"))
    blob = seen[0]["cmd"][seen[0]["cmd"].index("-headers") + 1]
    assert blob == "A: 1\r\nB: 2\r\n"


def test_no_headers_means_no_flag(seen, tmp_path):
    """Яндекс отдаёт разовую ссылку с авторизацией внутри -- пустой
    -headers там был бы лишним."""
    sar_worker._generate_thumbnail_remote("https://x", {}, str(tmp_path / "t.jpg"))
    assert "-headers" not in seen[0]["cmd"]


def test_call_has_a_timeout(seen, tmp_path):
    """Внешняя команда без предела однажды подвесила сторож туннеля на
    восемь суток. Здесь читается сеть -- тем более."""
    sar_worker._generate_thumbnail_remote("https://x", {}, str(tmp_path / "t.jpg"))
    assert seen[0]["kw"].get("timeout")


# --- публикация ------------------------------------------------------------

def test_publication_is_atomic(seen, tmp_path):
    """Оборванная запись оставила бы файл нормального вида, но битый, и он
    считался бы готовым превью -- повторить было бы уже некому."""
    out = str(tmp_path / "t.jpg")
    sar_worker._generate_thumbnail_remote("https://x", {}, out)
    assert seen[0]["cmd"][-1] != out, "ffmpeg пишет сразу в конечный путь"
    assert seen[0]["cmd"][-1].startswith(out)
    assert not os.path.exists(seen[0]["cmd"][-1]), "временный файл остался"


def test_failure_leaves_nothing_behind(monkeypatch, tmp_path):
    class Bad:
        returncode = 1
        stdout = ""
        stderr = "Server returned 403 Forbidden"

    tmps = []

    def fake_run(cmd, **kw):
        tmps.append(cmd[-1])
        with open(cmd[-1], "wb") as f:
            f.write(b"")
        return Bad()

    monkeypatch.setattr(sar_worker.subprocess, "run", fake_run)
    monkeypatch.setattr(sar_worker, "_ffmpeg_available", lambda: True)
    out = str(tmp_path / "t.jpg")
    assert sar_worker._generate_thumbnail_remote("https://x", {}, out) is False
    assert not os.path.exists(out)
    assert not os.path.exists(tmps[0]), "недописанный файл остался на диске"


def test_empty_output_is_a_failure(monkeypatch, tmp_path):
    """ffmpeg может вернуть 0 и не записать ничего. Пустое превью хуже
    отсутствующего: повторять его никто не станет."""
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        open(cmd[-1], "wb").close()
        return Ok()

    monkeypatch.setattr(sar_worker.subprocess, "run", fake_run)
    monkeypatch.setattr(sar_worker, "_ffmpeg_available", lambda: True)
    out = str(tmp_path / "t.jpg")
    assert sar_worker._generate_thumbnail_remote("https://x", {}, out) is False


def test_timeout_is_reported_not_swallowed(monkeypatch, tmp_path, capsys):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(sar_worker.subprocess, "run", boom)
    monkeypatch.setattr(sar_worker, "_ffmpeg_available", lambda: True)
    assert sar_worker._generate_thumbnail_remote(
        "https://x", {}, str(tmp_path / "t.jpg")) is False
    assert "не уложилось" in capsys.readouterr().out


def test_no_ffmpeg_is_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(sar_worker, "_ffmpeg_available", lambda: False)
    assert sar_worker._generate_thumbnail_remote(
        "https://x", {}, str(tmp_path / "t.jpg")) is False


# --- фоновый проход --------------------------------------------------------

class FakeFetcher:
    def __init__(self):
        self.asked = []

    def stream_source(self, rel, file_id=None):
        self.asked.append(rel)
        return "https://cloud/" + (file_id or ""), {"Authorization": "Bearer T"}


@pytest.fixture
def pass_env(tmp_path, monkeypatch):
    import sar_common
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    for i, kind in enumerate(("video", "photo", "video"), start=1):
        conn.execute(
            "INSERT INTO reports (report_id, rel_path, abs_path, kind, status,"
            " cloud_file_id, created_at, updated_at) VALUES "
            "(?,?,'',?, 'idle', ?, datetime('now'), datetime('now'))",
            ("r%d" % i, "оп/файл%d.MP4" % i, kind, "f%d" % i))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_worker, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(tmp_path / "data"),
                        raising=False)
    monkeypatch.setattr(sar_worker, "WATCH_DIR", str(tmp_path / "watch"),
                        raising=False)
    monkeypatch.setattr(sar_worker, "_failures", {}, raising=False)
    made = []
    monkeypatch.setattr(sar_worker, "_generate_thumbnail_remote",
                        lambda url, h, out, **k: (made.append(out),
                                                   _touch(out))[1])
    return made


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"jpg")
    return True


def test_pass_makes_thumbnails_for_all_kinds(pass_env, monkeypatch):
    """Снимкам превью нужны так же, как видео -- отдельного пути для них
    не требуется, ffmpeg читает удалённый JPEG тем же способом."""
    f = FakeFetcher()
    monkeypatch.setattr(sar_worker, "get_fetcher", lambda: f)
    n = sar_worker._remote_thumbs_pass()
    assert n == 3
    assert len(f.asked) == 3


def test_existing_thumbnail_is_not_redone(pass_env, monkeypatch):
    import sar_common
    f = FakeFetcher()
    monkeypatch.setattr(sar_worker, "get_fetcher", lambda: f)
    _touch(sar_common.get_thumbnail_path(sar_worker.DATA_DIR, "оп/файл1.MP4"))
    sar_worker._remote_thumbs_pass()
    assert "оп/файл1.MP4" not in f.asked


def test_local_file_wins_over_the_network(pass_env, monkeypatch):
    """Файл уже рядом -- читать его по сети значит платить за то, что
    лежит на диске."""
    f = FakeFetcher()
    monkeypatch.setattr(sar_worker, "get_fetcher", lambda: f)
    monkeypatch.setattr(sar_common_find := sar_worker.sar_common,
                        "find_material_file",
                        lambda w, d, rel: "/есть/локально" if rel.endswith("1.MP4") else None)
    sar_worker._remote_thumbs_pass()
    assert "оп/файл1.MP4" not in f.asked


def test_limit_is_respected(pass_env, monkeypatch):
    """Проход не должен уходить в многочасовую работу: он фоновый и
    должен уступать место следующему."""
    f = FakeFetcher()
    monkeypatch.setattr(sar_worker, "get_fetcher", lambda: f)
    assert sar_worker._remote_thumbs_pass(limit=2) == 2


def test_failure_is_backed_off(pass_env, monkeypatch):
    """Протухший токен отказывает одинаково на каждом файле. Без паузы
    проход будет биться о него каждые 15 секунд."""
    f = FakeFetcher()
    monkeypatch.setattr(sar_worker, "get_fetcher", lambda: f)
    monkeypatch.setattr(sar_worker, "_generate_thumbnail_remote",
                        lambda *a, **k: False)
    noted = []
    monkeypatch.setattr(sar_worker, "_note_failure",
                        lambda key, what: noted.append(key) or 1)
    sar_worker._remote_thumbs_pass()
    assert len(noted) == 3
    assert all(k.startswith("rthumb:") for k in noted)


def test_no_cloud_connected_is_quiet(pass_env, monkeypatch):
    monkeypatch.setattr(sar_worker, "get_fetcher", lambda: None)
    assert sar_worker._remote_thumbs_pass() == 0


def test_only_one_background_thread(pass_env, monkeypatch):
    """Два потока делали бы одно и то же и платили бы за это дважды."""
    import threading
    started = []
    monkeypatch.setattr(sar_worker, "_rthumbs", {"thread": None},
                        raising=False)

    class Busy:
        def is_alive(self): return True

    sar_worker._rthumbs["thread"] = Busy()
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: started.append(1))
    sar_worker._start_remote_thumbs()
    assert started == []


def test_broken_pass_does_not_kill_the_thread(pass_env, monkeypatch, capsys):
    """Тихо упавший поток выглядит как «превью просто не делаются»."""
    monkeypatch.setattr(sar_worker, "_remote_thumbs_pass",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("сеть")))
    sar_worker._remote_thumbs_worker()
    assert "сорвался" in capsys.readouterr().out


# --- страж -----------------------------------------------------------------

def test_previews_run_beside_proxy_building():
    """Смысл фонового потока: превью не должны ждать окончания сборки
    копии, которая занимает минуты. Одно упирается в сеть, другое в
    процессор -- мешать друг другу им нечем."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
           ).read_text(encoding="utf-8")
    i = src.index("_start_remote_thumbs()")
    j = src.index("ensure_video_proxies(get_db())")
    assert i < j, "превью запускаются после сборки копий -- значит ждут её"


# --- пропускная способность фонового потока -------------------------------

def test_worker_keeps_going_while_there_is_work(pass_env, monkeypatch):
    """Пачка из 12 и выход означали 0,5 превью в минуту: поток ждал
    следующего прохода, а проход стоял на сборке копии (минуты). Полтораста
    записей растянулись бы на четыре часа вместо получаса."""
    calls = []

    def passes(limit=None):
        calls.append(limit)
        return 12 if len(calls) < 3 else 0

    monkeypatch.setattr(sar_worker, "_remote_thumbs_pass", passes)
    sar_worker._remote_thumbs_worker()
    assert len(calls) == 3, "поток вышел после первой пачки"


def test_worker_stops_when_nothing_left(pass_env, monkeypatch):
    """Иначе получится бесконечный цикл без работы."""
    monkeypatch.setattr(sar_worker, "_remote_thumbs_pass", lambda limit=None: 0)
    sar_worker._remote_thumbs_worker()      # просто не должен зависнуть


def test_total_is_reported_once(pass_env, monkeypatch, capsys):
    """Строка на каждую пачку утопила бы журнал."""
    seq = [12, 5, 0]
    monkeypatch.setattr(sar_worker, "_remote_thumbs_pass",
                        lambda limit=None: seq.pop(0))
    sar_worker._remote_thumbs_worker()
    out = capsys.readouterr().out
    assert out.count("сделано") == 1
    assert "17" in out
