"""Плеер обязан работать, когда оригинала нет на месте.

Материал уезжает в облако. Оригинал там нужен ровно дважды: при обработке
детектором и при сборке лёгкой копии. Дальше зрители смотрят КОПИЮ --
плеер и так отдаёт её по умолчанию, оригинал только по ?original=1.

Раньше наличие оригинала проверялось ПЕРВЫМ, до подстановки копии. Пока
всё лежало на локальном диске, это было незаметно. С материалом в облаке
плеер отдавал бы 404, имея готовую копию под рукой: человек видит
"видео не найдено" при полностью рабочем материале, и понять, почему,
невозможно.
"""
import os
import sqlite3

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
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "fps, file_ctime, created_at, updated_at) VALUES "
        "('rep1', 'video.mp4', '/gone/video.mp4', 'video', 'done', 30.0, 0.0, "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", "/no/such/config/dir")
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    client = sar_server.app.test_client()
    with client.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "tester"
    return client, watch, data


def _make_proxy(data, rel="video.mp4"):
    p = sar_common.proxy_video_path(str(data), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"proxy-bytes")
    return p


# --- главное --------------------------------------------------------------

def test_proxy_is_served_when_original_is_absent(env):
    """Ровно ситуация «материал в облаке»: оригинала локально нет."""
    client, watch, data = env
    _make_proxy(data)
    r = client.get("/report/rep1/video")
    assert r.status_code == 200
    assert r.data == b"proxy-bytes"


def test_original_still_wins_when_asked_and_present(env):
    client, watch, data = env
    _make_proxy(data)
    (watch / "video.mp4").write_bytes(b"original-bytes")
    r = client.get("/report/rep1/video?original=1")
    assert r.status_code == 200
    assert r.data == b"original-bytes"


def test_asking_for_absent_original_explains_what_to_do(env):
    """Отказ должен подсказывать выход, а не просто говорить «нет».
    Копия-то есть, человеку достаточно снять галочку."""
    client, watch, data = env
    _make_proxy(data)
    r = client.get("/report/rep1/video?original=1")
    assert r.status_code == 404
    body = r.get_data(as_text=True)
    assert "оригинал" in body.lower()
    assert "копи" in body.lower(), "не сказано, что делать"


def test_no_proxy_and_no_original_is_still_an_honest_404(env):
    """Нечего отдавать -- так и говорим. Молча отдать пустоту нельзя."""
    client, _, _ = env
    r = client.get("/report/rep1/video")
    assert r.status_code == 404


def test_original_is_preferred_over_proxy_only_on_request(env):
    """Без ?original=1 копия побеждает, даже когда оригинал на месте:
    иначе каждый зритель тянет 30 Мбит/с через один канал наружу."""
    client, watch, data = env
    _make_proxy(data)
    (watch / "video.mp4").write_bytes(b"original-bytes")
    r = client.get("/report/rep1/video")
    assert r.data == b"proxy-bytes"
