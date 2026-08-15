"""Просмотр снимка должен работать ДО обработки детектором.

Из эксплуатации: "я так и не могу открыть фото с платформы". Причина
оказалась не в очереди: маршрута отдачи оригинала снимка не существовало
вообще (был только /report/<id>/video), а строка в списке файлов не
кликабельна, пока нет готового отчёта. То есть снимок в очереди нельзя было
посмотреть НИЧЕМ, хотя файл лежит на диске с первой секунды -- в отличие от
видео, где ручной плеер доступен сразу (см. test_player_accessibility.py)."""
import sqlite3

import cv2
import numpy as np

import sar_common
import sar_server


def _make_photo(path, width=80, height=60):
    cv2.imwrite(str(path), np.full((height, width, 3), 128, dtype=np.uint8))


def _setup(tmp_path, status="queued", kind="photo", exists=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    photo = tmp_path / "shot.jpg"
    if exists:
        _make_photo(photo)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep1", "shot.jpg", str(photo), kind, status, "/out", 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    return db_path


def _client(app, db_path, monkeypatch):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


# --- отдача оригинала ---

def test_photo_is_served_while_still_queued(tmp_path, monkeypatch):
    db = _setup(tmp_path, status="queued")
    client = _client(sar_server.app, db, monkeypatch)
    resp = client.get("/report/rep1/photo")
    assert resp.status_code == 200
    assert len(resp.data) > 0


def test_photo_route_rejects_video_report(tmp_path, monkeypatch):
    db = _setup(tmp_path, kind="video")
    client = _client(sar_server.app, db, monkeypatch)
    assert client.get("/report/rep1/photo").status_code == 400


def test_photo_route_404_when_file_gone(tmp_path, monkeypatch):
    db = _setup(tmp_path, exists=False)
    client = _client(sar_server.app, db, monkeypatch)
    assert client.get("/report/rep1/photo").status_code == 404


# --- страница просмотра ---

def test_viewer_opens_in_every_status(tmp_path, monkeypatch):
    for status in ("queued", "processing", "done", "error"):
        db = _setup(tmp_path / status, status=status)
        client = _client(sar_server.app, db, monkeypatch)
        resp = client.get("/report/rep1/viewer/")
        assert resp.status_code == 200, f"статус {status} должен открываться"
        assert "/report/rep1/photo" in resp.get_data(as_text=True)


def test_viewer_warns_when_not_processed_yet(tmp_path, monkeypatch):
    db = _setup(tmp_path, status="queued")
    client = _client(sar_server.app, db, monkeypatch)
    html = client.get("/report/rep1/viewer/").get_data(as_text=True)
    assert "ещё не обработан" in html
    assert "к отчёту" not in html  # отчёта ещё нет, ссылка вела бы в никуда


def test_viewer_links_to_report_when_done(tmp_path, monkeypatch):
    db = _setup(tmp_path, status="done")
    client = _client(sar_server.app, db, monkeypatch)
    html = client.get("/report/rep1/viewer/").get_data(as_text=True)
    assert "/report/rep1/" in html
    assert "обработан" in html


def test_viewer_rejects_video(tmp_path, monkeypatch):
    db = _setup(tmp_path, kind="video")
    client = _client(sar_server.app, db, monkeypatch)
    assert client.get("/report/rep1/viewer/").status_code == 400


def test_file_list_offers_view_link_for_photos():
    html = sar_server.TREE_PAGE_HTML
    assert "/viewer/" in html
    assert "🖼 смотреть" in html
