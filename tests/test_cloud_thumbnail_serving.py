"""Отдача превью облачного материала.

Превью для 148 файлов из 150 были СДЕЛАНЫ и лежали на диске, а страница
операции показывала пустые квадраты. Причина: эндпоинт проверял имя файла
по обходу наблюдаемой папки --

    found = {name: kind for name, _abs, kind in scan_all_materials(watch_dir)}
    if found.get(filename) not in ("video", "photo"):
        return "", 404

-- а облачного материала на диске нет по определению. Значит 404 на каждое
облачное превью, при готовом JPEG рядом.

Это уже третий случай одного и того же: проверка «настоящий ли это
материал» задаётся ДИСКУ, и облако её не проходит. Первый был с отдачей
видео (плеер возвращал 404 при готовой лёгкой копии), второй -- с
`find_material_file`.
"""
import os

import pytest

import sar_common
import sar_server


@pytest.fixture
def env(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('c1','2026 08 11/DJI_0001.MP4','','video','idle','f1',"
        "datetime('now'), datetime('now'))")
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('c2','2026 08 11/снимок.JPG','','photo','idle','f2',"
        "datetime('now'), datetime('now'))")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "tester"
    return c, str(data)


def put_thumb(data, rel):
    p = sar_common.get_thumbnail_path(data, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0jpegdata")
    return p


# --- главное ---------------------------------------------------------------

def test_cloud_video_thumbnail_is_served(env):
    """Ради этого всё: JPEG готов, файла на диске нет -- превью обязано
    отдаться."""
    client, data = env
    put_thumb(data, "2026 08 11/DJI_0001.MP4")
    r = client.get("/api/thumbnail/2026 08 11/DJI_0001.MP4")
    assert r.status_code == 200
    assert r.data.startswith(b"\xff\xd8")


def test_cloud_photo_thumbnail_is_served(env):
    client, data = env
    put_thumb(data, "2026 08 11/снимок.JPG")
    assert client.get("/api/thumbnail/2026 08 11/снимок.JPG").status_code == 200


def test_missing_thumbnail_is_still_404(env):
    """Воркер ещё не дошёл до файла -- это не ошибка, просто нечего
    отдавать."""
    client, _ = env
    assert client.get("/api/thumbnail/2026 08 11/DJI_0001.MP4").status_code == 404


# --- защита не ослабла -----------------------------------------------------

def test_unknown_name_is_refused(env):
    """Имя, которого нет ни на диске, ни в базе, принимать нельзя."""
    client, data = env
    put_thumb(data, "чужое.MP4")
    assert client.get("/api/thumbnail/чужое.MP4").status_code == 404


def test_path_traversal_is_refused(env):
    """Сверка идёт с ТОЧНЫМ rel_path из базы, а не склейкой с каталогом --
    подставить путь наружу нечем."""
    client, _ = env
    for evil in ("../../../../etc/passwd",
                 "..%2f..%2fsar_config.json",
                 "2026 08 11/../../sar_config.json"):
        r = client.get("/api/thumbnail/" + evil)
        assert r.status_code in (301, 308, 400, 404), evil
        assert b"shared_password" not in r.data


def test_non_material_record_is_refused(env, tmp_path):
    """В reports лежат не только видео и фото."""
    client, data = env
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('c3','служебное.txt','','other','idle','f3',"
        "datetime('now'), datetime('now'))")
    conn.commit()
    conn.close()
    put_thumb(data, "служебное.txt")
    assert client.get("/api/thumbnail/служебное.txt").status_code == 404


def test_login_is_still_required(env):
    client, data = env
    put_thumb(data, "2026 08 11/DJI_0001.MP4")
    with client.session_transaction() as s:
        s.clear()
    r = client.get("/api/thumbnail/2026 08 11/DJI_0001.MP4")
    assert r.status_code in (302, 401, 403), "превью отдаётся без входа"


# --- страж -----------------------------------------------------------------

def test_disk_scan_is_not_the_only_source():
    """Тот же вопрос «настоящий ли это материал» уже трижды задавался
    диску, и трижды облако его не проходило. Страж на четвёртый раз."""
    import inspect
    src = inspect.getsource(sar_server.api_thumbnail)
    assert "FROM reports" in src, (
        "проверка имени снова опирается только на обход папки")
