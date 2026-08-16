"""Обсуждение находки в карточке плеера.

Привязка -- та же пара (kind, ref_key), что и у статусов находок, поэтому
обсуждение переживает переобработку видео (см. sar_common.ai_scene_ref_key).

Отдельно проверяется, что все комментарии отчёта отдаются ОДНИМ запросом:
карточек на видео бывают сотни, и запрос на каждую превратил бы открытие
плеера в сотни обращений к серверу."""
import sqlite3

import sar_common
import sar_server


def _setup(tmp_path):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep1", "v.mp4", "/w/v.mp4", "video", "done", "/out", 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    return db_path


def _client(db_path, monkeypatch, viewer="tester", verified=True,
            role=sar_common.ROLE_VIEWER):
    """verified=True -- человек вошёл по персональной ссылке из бота и опознан.
    Писать в обсуждениях могут только такие (см. test_roles_and_identity.py):
    анонимный вход по общему паролю даёт только чтение и разметку находок."""
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    sar_server.app.secret_key = "test-secret"
    sar_server.app.testing = True
    client = sar_server.app.test_client()
    with client.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = viewer
        s["verified"] = verified
        s["role"] = role
    return client


def test_comment_is_saved_and_returned(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch, viewer="Аяу")

    resp = client.post("/api/report/rep1/comments", json={
        "kind": "ai_scene", "ref_key": "person:model:10:100:100", "text": "похоже на рюкзак"})
    assert resp.status_code == 200
    assert resp.get_json()["author"] == "Аяу"

    rows = client.get("/api/report/rep1/comments").get_json()
    assert len(rows) == 1
    assert rows[0]["text"] == "похоже на рюкзак"
    assert rows[0]["kind"] == "ai_scene"


def test_comments_of_whole_report_come_in_one_request(tmp_path, monkeypatch):
    """Ключевое для производительности: одна выдача на весь отчёт, а не по
    запросу на карточку."""
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    client.post("/api/report/rep1/comments",
                 json={"kind": "ai_scene", "ref_key": "a", "text": "раз"})
    client.post("/api/report/rep1/comments",
                 json={"kind": "manual", "ref_key": "7", "text": "два"})

    rows = client.get("/api/report/rep1/comments").get_json()
    assert len(rows) == 2
    assert {r["kind"] for r in rows} == {"ai_scene", "manual"}


def test_comments_keep_chronological_order(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    for t in ("первое", "второе", "третье"):
        client.post("/api/report/rep1/comments",
                     json={"kind": "manual", "ref_key": "1", "text": t})
    rows = client.get("/api/report/rep1/comments").get_json()
    assert [r["text"] for r in rows] == ["первое", "второе", "третье"]


def test_empty_comment_is_rejected(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    resp = client.post("/api/report/rep1/comments",
                        json={"kind": "manual", "ref_key": "1", "text": "   "})
    assert resp.status_code == 400
    assert client.get("/api/report/rep1/comments").get_json() == []


def test_bad_kind_is_rejected(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    resp = client.post("/api/report/rep1/comments",
                        json={"kind": "robot", "ref_key": "1", "text": "привет"})
    assert resp.status_code == 400


def test_long_comment_is_truncated_not_rejected(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    client.post("/api/report/rep1/comments",
                 json={"kind": "manual", "ref_key": "1", "text": "я" * 5000})
    rows = client.get("/api/report/rep1/comments").get_json()
    assert len(rows[0]["text"]) == sar_server.MAX_COMMENT_LEN


# --- удаление ---

def test_author_can_delete_own_comment(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch, viewer="Аяу")
    cid = client.post("/api/report/rep1/comments",
                       json={"kind": "manual", "ref_key": "1", "text": "опечатка"}).get_json()["id"]

    assert client.delete(f"/api/report/rep1/comments/{cid}").status_code == 200
    assert client.get("/api/report/rep1/comments").get_json() == []


def test_other_viewer_cannot_delete_someone_elses_comment(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    author = _client(db, monkeypatch, viewer="Аяу")
    cid = author.post("/api/report/rep1/comments",
                       json={"kind": "manual", "ref_key": "1", "text": "важно"}).get_json()["id"]

    other = _client(db, monkeypatch, viewer="Данияр")
    assert other.delete(f"/api/report/rep1/comments/{cid}").status_code == 403
    assert len(other.get("/api/report/rep1/comments").get_json()) == 1


def test_deleting_unknown_comment_is_404(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    assert client.delete("/api/report/rep1/comments/9999").status_code == 404


# --- интерфейс ---

def test_player_renders_discussion_in_both_card_types():
    html = sar_server.PLAYER_PAGE_HTML
    assert "renderComments('manual', o.id)" in html
    assert "renderComments('ai_scene', s.ref_key)" in html


def test_comment_text_and_author_are_escaped():
    """Текст пишет человек -- в HTML он обязан попадать экранированным."""
    html = sar_server.PLAYER_PAGE_HTML
    assert "escapeHtml(c.text)" in html
    assert "escapeHtml(c.author)" in html


def test_delete_button_shown_only_for_own_comments():
    html = sar_server.PLAYER_PAGE_HTML.replace("\n", " ")
    assert "c.author === VIEWER_NAME" in html


def test_comments_are_polled_like_the_rest():
    assert "setInterval(loadComments" in sar_server.PLAYER_PAGE_HTML
