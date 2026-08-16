"""Опознание человека по персональной ссылке и роли в обсуждениях.

Личность даёт Telegram-бот: он и так знает chat_id и @username и решает, кто
получает доступ -- раньше эта личность терялась, потому что бот выдавал
ОБЩИЙ пароль. Теперь одобренному выдаётся персональная ссылка с ключом, и
сервер по ключу сажает человека в сессию уже опознанным.

Вход по общему паролю остаётся (работа в поле с чужого устройства), но такой
человек анонимен и в обсуждениях не участвует -- иначе заблокированный
просто зашёл бы по общему паролю и продолжил."""
import sqlite3

import pytest

import sar_common
import sar_server


APPROVED_CHAT = 555


def _setup(tmp_path, role=None, status="approved", token="tok-abc", username="volunteer"):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO telegram_access_requests (chat_id, username, first_name, status, "
        "requested_at, access_token, role) VALUES (?,?,?,?,?,?,?)",
        (APPROVED_CHAT, username, "Аяу", status, "2026-01-01T00:00:00", token, role))
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep1", "v.mp4", "/w/v.mp4", "video", "done", "/out", 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    return db_path


def _client(db_path, monkeypatch):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": ".", "shared_password": "obshiy"}, raising=False)
    sar_server.app.secret_key = "test-secret"
    sar_server.app.testing = True
    return sar_server.app.test_client()


# --- вход по персональной ссылке ---

def test_personal_key_logs_in_and_identifies(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)

    resp = client.get("/login?key=tok-abc", follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as s:
        assert s["authed"] is True
        assert s["verified"] is True
        assert s["viewer_name"] == "@volunteer"
        assert s["tg_chat_id"] == APPROVED_CHAT


def test_name_is_not_typed_by_hand_for_key_login(tmp_path, monkeypatch):
    """Имя берётся из записи бота -- выдать себя за другого нельзя."""
    db = _setup(tmp_path, username=None)
    client = _client(db, monkeypatch)
    client.get("/login?key=tok-abc")
    with client.session_transaction() as s:
        assert s["viewer_name"] == "Аяу"  # first_name, раз username нет


def test_unknown_key_does_not_log_in(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    resp = client.get("/login?key=нет-такого")
    assert resp.status_code == 200
    assert "недействительна" in resp.get_data(as_text=True)
    with client.session_transaction() as s:
        assert not s.get("authed")


def test_revoked_access_stops_working_immediately(tmp_path, monkeypatch):
    """Отзыв доступа должен действовать, даже если ссылка осталась у человека."""
    db = _setup(tmp_path, status="denied")
    client = _client(db, monkeypatch)
    client.get("/login?key=tok-abc")
    with client.session_transaction() as s:
        assert not s.get("authed")


# --- права на комментирование ---

def _login_key(client):
    client.get("/login?key=tok-abc")


def _login_password(client):
    client.post("/login", data={"name": "Кто-то", "password": "obshiy"})


def test_identified_viewer_can_comment(tmp_path, monkeypatch):
    db = _setup(tmp_path, role=sar_common.ROLE_VIEWER)
    client = _client(db, monkeypatch)
    _login_key(client)
    resp = client.post("/api/report/rep1/comments",
                        json={"kind": "manual", "ref_key": "1", "text": "вижу рюкзак"})
    assert resp.status_code == 200
    assert resp.get_json()["author"] == "@volunteer"


def test_anonymous_password_login_cannot_comment(tmp_path, monkeypatch):
    """Главное свойство: иначе заблокированный обошёл бы запрет общим паролем."""
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    _login_password(client)
    resp = client.post("/api/report/rep1/comments",
                        json={"kind": "manual", "ref_key": "1", "text": "аноним"})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "not_verified"


def test_anonymous_can_still_read_comments(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    client = _client(db, monkeypatch)
    _login_password(client)
    assert client.get("/api/report/rep1/comments").status_code == 200


def test_muted_person_cannot_comment(tmp_path, monkeypatch):
    db = _setup(tmp_path, role=sar_common.ROLE_MUTED)
    client = _client(db, monkeypatch)
    _login_key(client)
    resp = client.post("/api/report/rep1/comments",
                        json={"kind": "manual", "ref_key": "1", "text": "нельзя"})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "muted"


def test_muted_person_can_still_mark_findings(tmp_path, monkeypatch):
    """Ограничение касается только обсуждений -- разметка находок остаётся:
    это основная работа, и лишать её из-за спора в комментариях нельзя."""
    db = _setup(tmp_path, role=sar_common.ROLE_MUTED)
    client = _client(db, monkeypatch)
    _login_key(client)
    resp = client.post("/api/report/rep1/priorities",
                        json={"kind": "manual", "ref_key": "1", "priority": "confirmed_person"})
    assert resp.status_code == 200


# --- модерация ---

def test_moderator_can_delete_someone_elses_comment(tmp_path, monkeypatch):
    db = _setup(tmp_path, role=sar_common.ROLE_VIEWER)
    author = _client(db, monkeypatch)
    _login_key(author)
    cid = author.post("/api/report/rep1/comments",
                       json={"kind": "manual", "ref_key": "1", "text": "спорное"}).get_json()["id"]

    # второй человек с ролью модератора
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO telegram_access_requests (chat_id, username, first_name, status, "
                 "requested_at, access_token, role) VALUES (?,?,?,?,?,?,?)",
                 (777, "mod", "Мод", "approved", "2026-01-01T00:00:00", "tok-mod",
                  sar_common.ROLE_MODERATOR))
    conn.commit()
    conn.close()

    mod = _client(db, monkeypatch)
    mod.get("/login?key=tok-mod")
    assert mod.delete(f"/api/report/rep1/comments/{cid}").status_code == 200


def test_plain_viewer_still_cannot_delete_others(tmp_path, monkeypatch):
    db = _setup(tmp_path, role=sar_common.ROLE_VIEWER)
    author = _client(db, monkeypatch)
    _login_key(author)
    cid = author.post("/api/report/rep1/comments",
                       json={"kind": "manual", "ref_key": "1", "text": "моё"}).get_json()["id"]

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO telegram_access_requests (chat_id, username, first_name, status, "
                 "requested_at, access_token, role) VALUES (?,?,?,?,?,?,?)",
                 (888, "other", "Другой", "approved", "2026-01-01T00:00:00", "tok-other",
                  sar_common.ROLE_VIEWER))
    conn.commit()
    conn.close()

    other = _client(db, monkeypatch)
    other.get("/login?key=tok-other")
    assert other.delete(f"/api/report/rep1/comments/{cid}").status_code == 403


# --- роль администратора ---

def _add_person(db, chat_id, username, token, role):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO telegram_access_requests (chat_id, username, first_name, status, "
                 "requested_at, access_token, role) VALUES (?,?,?,?,?,?,?)",
                 (chat_id, username, username, "approved", "2026-01-01T00:00:00", token, role))
    conn.commit()
    conn.close()


def test_admin_can_moderate_like_moderator(tmp_path, monkeypatch):
    db = _setup(tmp_path, role=sar_common.ROLE_VIEWER)
    author = _client(db, monkeypatch)
    _login_key(author)
    cid = author.post("/api/report/rep1/comments",
                       json={"kind": "manual", "ref_key": "1", "text": "чужое"}).get_json()["id"]

    _add_person(db, 999, "boss", "tok-admin", sar_common.ROLE_ADMIN)
    admin = _client(db, monkeypatch)
    admin.get("/login?key=tok-admin")
    assert admin.delete(f"/api/report/rep1/comments/{cid}").status_code == 200


def test_admin_can_comment(tmp_path, monkeypatch):
    db = _setup(tmp_path, role=sar_common.ROLE_ADMIN)
    client = _client(db, monkeypatch)
    _login_key(client)
    assert client.post("/api/report/rep1/comments",
                        json={"kind": "manual", "ref_key": "1", "text": "от админа"}).status_code == 200


def test_admin_can_upload(tmp_path, monkeypatch):
    """Роль admin -- честная замена трюку с именем 'uploader': её нельзя
    присвоить себе, её выдаёт координатор.
    Без файла ожидаем 400, а НЕ 403: значит проверка прав пройдена."""
    db = _setup(tmp_path, role=sar_common.ROLE_ADMIN)
    client = _client(db, monkeypatch)
    _login_key(client)
    assert client.post("/api/upload", data={}).status_code == 400


def test_plain_viewer_cannot_upload(tmp_path, monkeypatch):
    db = _setup(tmp_path, role=sar_common.ROLE_VIEWER)
    client = _client(db, monkeypatch)
    _login_key(client)
    assert client.post("/api/upload", data={}).status_code == 403


def test_moderator_is_not_automatically_an_uploader(tmp_path, monkeypatch):
    """Модерация обсуждений и загрузка файлов -- разные полномочия."""
    db = _setup(tmp_path, role=sar_common.ROLE_MODERATOR)
    client = _client(db, monkeypatch)
    _login_key(client)
    assert client.post("/api/upload", data={}).status_code == 403


def test_admin_role_is_a_superset_of_moderator():
    assert sar_common.ROLE_ADMIN in sar_common.ROLES_CAN_MODERATE
    assert sar_common.ROLE_ADMIN in sar_common.ROLES_CAN_COMMENT
    assert sar_common.ROLES_CAN_UPLOAD == {sar_common.ROLE_ADMIN}


# --- вспомогательное ---

def test_token_is_long_enough_to_resist_guessing():
    t = sar_common.generate_access_token()
    assert len(t) >= 24
    assert t != sar_common.generate_access_token()


@pytest.mark.parametrize("role", sorted(sar_common.VALID_ROLES))
def test_every_role_has_a_human_readable_label(role):
    assert role in sar_common.ROLE_LABELS
