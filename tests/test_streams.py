"""Вкладка «Стримы»: живые трансляции с дрона.

Ключевое архитектурное решение: платформа НЕ принимает и НЕ перекодирует
видео. Этим занимается медиасервер, а сюда приходит только регистрация
потока и heartbeat; зрителю отдаётся ссылка. Причина -- перекодирование
нескольких потоков на том же процессоре, где идёт разбор записей, положило
бы и то, и другое.

Детекции по потоку тоже считаются не здесь, а на стороне вещающего клиента,
и присылаются готовыми (см. api_stream_detections)."""
import json
import sqlite3
from datetime import datetime, timedelta

import sar_common
import sar_server


def _db(tmp_path):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    return db_path


def _client(db_path, monkeypatch, viewer="tester", stream_server="http://media:8888"):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG", {
        "watch_dir": ".", "shared_password": "pw",
        "stream_server_url": stream_server,
        "stream_playback_template": "{server}/{key}/index.m3u8",
        "stream_offline_after_sec": 30,
    }, raising=False)
    sar_server.app.secret_key = "test-secret"
    sar_server.app.testing = True
    client = sar_server.app.test_client()
    with client.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = viewer
    return client


def _broadcaster(db_path, monkeypatch, **kw):
    """Клиент с правом объявлять трансляции."""
    return _client(db_path, monkeypatch, viewer=sar_server.UPLOADER_NAME, **kw)


# --- объявление потока ---

def test_announce_creates_live_stream(tmp_path, monkeypatch):
    db = _db(tmp_path)
    client = _broadcaster(db, monkeypatch)
    resp = client.post("/api/streams/bort2/announce",
                        json={"title": "Борт 2, южный склон", "source": "DJI M30T"})
    assert resp.status_code == 200
    assert resp.get_json()["playback_url"] == "http://media:8888/bort2/index.m3u8"

    items = client.get("/api/streams").get_json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "live"
    assert items[0]["title"] == "Борт 2, южный склон"


def test_ordinary_viewer_cannot_announce(tmp_path, monkeypatch):
    """Объявить поток -- значит показать его команде как источник, которому
    можно доверять."""
    db = _db(tmp_path)
    client = _client(db, monkeypatch, viewer="Случайный")
    assert client.post("/api/streams/x/announce", json={"title": "x"}).status_code == 403


def test_everyone_can_watch_the_list(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _broadcaster(db, monkeypatch).post("/api/streams/b/announce", json={"title": "Борт"})
    viewer = _client(db, monkeypatch, viewer="Волонтёр")
    assert viewer.get("/api/streams").status_code == 200
    assert len(viewer.get("/api/streams").get_json()["items"]) == 1


def test_repeat_announce_keeps_original_start_time(tmp_path, monkeypatch):
    """Переобъявление (переподключение клиента) не должно обнулять «сколько
    в эфире» -- иначе счётчик прыгает при каждом обрыве связи."""
    db = _db(tmp_path)
    client = _broadcaster(db, monkeypatch)
    client.post("/api/streams/b/announce", json={"title": "Борт"})
    first = client.get("/api/streams").get_json()["items"][0]["started_at"]
    client.post("/api/streams/b/announce", json={"title": "Борт (переподключение)"})
    again = client.get("/api/streams").get_json()["items"][0]
    assert again["started_at"] == first
    assert again["title"] == "Борт (переподключение)"


# --- жизненный цикл ---

def test_stream_goes_offline_without_heartbeat(tmp_path, monkeypatch):
    """Оборвавшаяся трансляция не должна вечно висеть как «в эфире» --
    иначе люди будут ждать картинку, которой уже нет."""
    db = _db(tmp_path)
    client = _broadcaster(db, monkeypatch)
    client.post("/api/streams/b/announce", json={"title": "Борт"})

    stale = (datetime.now() - timedelta(minutes=5)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE streams SET last_seen=? WHERE stream_key='b'", (stale,))
    conn.commit()
    conn.close()

    assert client.get("/api/streams").get_json()["items"][0]["status"] == "offline"


def test_heartbeat_keeps_stream_live(tmp_path, monkeypatch):
    db = _db(tmp_path)
    client = _broadcaster(db, monkeypatch)
    client.post("/api/streams/b/announce", json={"title": "Борт"})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE streams SET last_seen=? WHERE stream_key='b'",
                 ((datetime.now() - timedelta(minutes=5)).isoformat(),))
    conn.commit()
    conn.close()

    assert client.post("/api/streams/b/heartbeat").status_code == 200
    assert client.get("/api/streams").get_json()["items"][0]["status"] == "live"


def test_heartbeat_for_unknown_stream_is_404(tmp_path, monkeypatch):
    db = _db(tmp_path)
    assert _broadcaster(db, monkeypatch).post("/api/streams/nope/heartbeat").status_code == 404


def test_stop_marks_stream_offline(tmp_path, monkeypatch):
    db = _db(tmp_path)
    client = _broadcaster(db, monkeypatch)
    client.post("/api/streams/b/announce", json={"title": "Борт"})
    client.post("/api/streams/b/stop")
    assert client.get("/api/streams").get_json()["items"][0]["status"] == "offline"


# --- детекции на потоке ---

def test_detections_are_accepted_and_returned(tmp_path, monkeypatch):
    db = _db(tmp_path)
    client = _broadcaster(db, monkeypatch)
    client.post("/api/streams/b/announce", json={"title": "Борт"})
    resp = client.post("/api/streams/b/detections", json={"detections": [
        {"object_class": "person", "confidence": 0.7, "bbox": [0.1, 0.2, 0.3, 0.4],
         "lat": 39.48, "lon": 73.59},
    ]})
    assert resp.get_json()["saved"] == 1

    rows = client.get("/api/streams/b/detections").get_json()
    assert len(rows) == 1
    assert rows[0]["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert rows[0]["object_class"] == "person"


def test_detections_can_be_polled_incrementally(tmp_path, monkeypatch):
    """Зрителю нужно то, что в кадре сейчас, а не вся история потока."""
    db = _db(tmp_path)
    client = _broadcaster(db, monkeypatch)
    client.post("/api/streams/b/announce", json={"title": "Борт"})
    client.post("/api/streams/b/detections",
                 json={"detections": [{"object_class": "person", "bbox": [0, 0, 1, 1]}]})
    first = client.get("/api/streams/b/detections").get_json()
    last_id = first[0]["id"]
    assert client.get(f"/api/streams/b/detections?since_id={last_id}").get_json() == []


def test_ordinary_viewer_cannot_push_detections(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _broadcaster(db, monkeypatch).post("/api/streams/b/announce", json={"title": "Б"})
    viewer = _client(db, monkeypatch, viewer="Волонтёр")
    assert viewer.post("/api/streams/b/detections",
                        json={"detections": []}).status_code == 403


# --- страница ---

def test_streams_page_opens(tmp_path, monkeypatch):
    db = _db(tmp_path)
    client = _client(db, monkeypatch)
    resp = client.get("/streams")
    assert resp.status_code == 200
    assert "Стримы" in resp.get_data(as_text=True)


def test_page_shows_setup_help_when_media_server_not_configured(tmp_path, monkeypatch):
    """Пустая вкладка выглядела бы как поломка -- вместо этого объясняем,
    что настроить."""
    db = _db(tmp_path)
    client = _client(db, monkeypatch, stream_server="")
    assert client.get("/api/streams").get_json()["configured"] is False
    assert "не настроены" in sar_server.STREAMS_PAGE_HTML


def test_player_library_is_served_locally_not_from_cdn():
    """Сервис должен работать в сети операции без интернета -- внешняя
    зависимость сломала бы просмотр именно в поле."""
    html = sar_server.STREAMS_PAGE_HTML
    assert "/static/hls.min.js" in html
    assert "cdn.jsdelivr.net" not in html


def test_both_tabs_are_linked_from_each_page():
    for html in (sar_server.TREE_PAGE_HTML, sar_server.STREAMS_PAGE_HTML):
        assert 'href="/"' in html
        assert 'href="/streams"' in html
