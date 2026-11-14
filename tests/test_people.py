"""Люди и доступ без Telegram: sar_common.person_*, sar_people.py, /api/admin/people.

Зачем. Роль даёт только вход по личной ссылке, а ссылки раньше выдавал
только бот. Установка без бота (первый деплой через cd) оказалась пустой и
запертой: администратора нет, создать операцию некому. Сторожим:

- первый администратор заводится с консоли, по ссылке входит С РОЛЬЮ;
- права выдаются только явно: имя «админ» при входе по паролю ничего не даёт;
- последнего администратора не разжаловать и не отключить -- иначе снова
  заперто;
- ключ входа не попадает в список людей -- только в ответ на выдачу;
- люди без Telegram (chat_id < 0) не получают рассылок и уведомлений бота;
- бот, CLI и веб -- одна логика (бот теперь обёртка над sar_common).
"""
import os
import sqlite3

import pytest

import sar_common
import sar_people
import sar_server


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "sar_data.db")
    sar_common.init_db(path)
    conn = sar_common.get_db_connection(path)
    yield conn
    conn.close()


# --- общий слой --------------------------------------------------------------

def test_local_person_gets_negative_id_and_explicit_role(db):
    a = sar_common.person_create_local(db, "Дмитрий", sar_common.ROLE_ADMIN)
    b = sar_common.person_create_local(db, "Аня")
    assert a["chat_id"] < 0 and b["chat_id"] < a["chat_id"]
    assert a["status"] == "approved" and a["role"] == sar_common.ROLE_ADMIN
    assert b["role"] == sar_common.DEFAULT_ROLE, "без явной роли -- обычный участник"
    assert sar_common.is_local_person(a["chat_id"])
    assert not sar_common.is_local_person(123456)


def test_negative_ids_do_not_collide_with_telegram(db):
    sar_common.person_upsert_request(db, 555, "nick", "Ник")
    p = sar_common.person_create_local(db, "Без телеграма")
    assert p["chat_id"] == -1


def test_duplicate_name_is_refused(db):
    sar_common.person_create_local(db, "Саша")
    with pytest.raises(sar_common.PersonError, match="уже есть"):
        sar_common.person_create_local(db, "саша")
    sar_common.person_upsert_request(db, 777, "pilot", "Пилот")
    with pytest.raises(sar_common.PersonError):
        sar_common.person_create_local(db, "@pilot")


def test_find_by_id_nick_and_name(db):
    sar_common.person_upsert_request(db, 42, "Nick", "Коля")
    p = sar_common.person_create_local(db, "Дмитрий")
    assert sar_common.person_find(db, "42")["chat_id"] == 42
    assert sar_common.person_find(db, "@nick")["chat_id"] == 42
    assert sar_common.person_find(db, str(p["chat_id"]))["chat_id"] == p["chat_id"]
    assert sar_common.person_find(db, "дмитрий")["chat_id"] == p["chat_id"]
    assert sar_common.person_find(db, "никто") is None


def test_ambiguous_name_is_an_error_not_a_guess(db):
    sar_common.person_upsert_request(db, 1, None, "Саша")
    sar_common.person_upsert_request(db, 2, None, "Саша")
    with pytest.raises(sar_common.PersonError, match="несколько"):
        sar_common.person_find(db, "Саша")


def test_link_logs_in_with_the_role_and_revoke_stops_it(db):
    p = sar_common.person_create_local(db, "Дмитрий", sar_common.ROLE_ADMIN)
    sar_common.person_create_local(db, "Второй админ", sar_common.ROLE_ADMIN)
    token = sar_common.person_ensure_token(db, p["chat_id"])
    assert sar_common.person_ensure_token(db, p["chat_id"]) == token, "ссылка стабильна"
    who = sar_common.find_person_by_token(db, token)
    assert who["role"] == sar_common.ROLE_ADMIN
    sar_common.person_revoke(db, p["chat_id"])
    assert sar_common.find_person_by_token(db, token) is None


def test_new_token_kills_the_old_link(db):
    p = sar_common.person_create_local(db, "Аня")
    old = sar_common.person_ensure_token(db, p["chat_id"])
    new = sar_common.person_new_token(db, p["chat_id"])
    assert new != old
    assert sar_common.find_person_by_token(db, old) is None
    assert sar_common.find_person_by_token(db, new) is not None


def test_last_admin_cannot_be_demoted_or_revoked(db):
    a = sar_common.person_create_local(db, "Единственный", sar_common.ROLE_ADMIN)
    with pytest.raises(sar_common.PersonError, match="последний"):
        sar_common.person_set_role(db, a["chat_id"], sar_common.ROLE_VIEWER)
    with pytest.raises(sar_common.PersonError, match="последний"):
        sar_common.person_revoke(db, a["chat_id"])
    assert sar_common.person_get(db, a["chat_id"])["role"] == sar_common.ROLE_ADMIN
    # со вторым администратором -- можно
    sar_common.person_create_local(db, "Второй", sar_common.ROLE_ADMIN)
    sar_common.person_set_role(db, a["chat_id"], sar_common.ROLE_VIEWER)


def test_unknown_role_is_refused(db):
    p = sar_common.person_create_local(db, "Аня")
    with pytest.raises(sar_common.PersonError):
        sar_common.person_set_role(db, p["chat_id"], "god")
    with pytest.raises(sar_common.PersonError):
        sar_common.person_create_local(db, "Боря", "god")


def test_public_record_has_no_token(db):
    p = sar_common.person_create_local(db, "Аня")
    token = sar_common.person_ensure_token(db, p["chat_id"])
    pub = sar_common.person_public(sar_common.person_get(db, p["chat_id"]))
    assert token not in repr(pub)
    assert pub["has_link"] is True and pub["telegram"] is False


def test_link_url():
    assert sar_common.personal_link_url("https://x.example/", "T") == "https://x.example/login?key=T"
    assert sar_common.personal_link_url("", "T", "/finding/3/") == "/login?key=T&next=%2Ffinding%2F3%2F"


def test_announcement_never_targets_local_people(db):
    import send_announcement
    sar_common.person_upsert_request(db, 10, "a", "A")
    sar_common.person_set_status(db, 10, "approved")
    sar_common.person_create_local(db, "Без телеграма")
    ids = [r["chat_id"] for r in send_announcement.recipients(db)]
    assert ids == [10]


# --- командная строка -------------------------------------------------------

@pytest.fixture
def root(tmp_path):
    (tmp_path / "sar_config.json").write_text(
        '{"server": {"watch_dir": "%s"}, "telegram_bot": {"service_url": "https://sar.example"}}'
        % str(tmp_path / "watch").replace("\\", "/"), encoding="utf-8")
    (tmp_path / "watch").mkdir()
    return str(tmp_path)


def _db_of(root):
    return sar_common.get_db_connection(os.path.join(root, "watch", "sar_data", "sar_data.db"))


def test_cli_first_admin_end_to_end(root, capsys):
    assert sar_people.main(["--root", root, "add", "Дмитрий", "--role", "admin"]) == 0
    out = capsys.readouterr().out
    assert "https://sar.example/login?key=" in out
    token = out.split("login?key=")[1].split()[0]
    conn = _db_of(root)
    assert sar_common.find_person_by_token(conn, token)["role"] == sar_common.ROLE_ADMIN
    conn.close()


def test_cli_role_link_revoke_and_errors(root, capsys):
    sar_people.main(["--root", root, "add", "Админ", "--role", "admin"])
    sar_people.main(["--root", root, "add", "Аня"])
    assert sar_people.main(["--root", root, "role", "Аня", "moderator"]) == 0
    assert sar_people.main(["--root", root, "link", "Аня"]) == 0
    assert sar_people.main(["--root", root, "list"]) == 0
    out = capsys.readouterr().out
    assert "Аня" in out and "moderator" in out
    # ключи в списке не печатаются
    conn = _db_of(root)
    for r in conn.execute("SELECT access_token FROM telegram_access_requests"):
        assert r["access_token"] not in out.split("Аня")[-1]
    conn.close()
    assert sar_people.main(["--root", root, "revoke", "Админ"]) == 1, "последний админ"
    assert "последний" in capsys.readouterr().err
    assert sar_people.main(["--root", root, "role", "Никто", "admin"]) == 1


def test_cli_approves_a_telegram_request(root, capsys):
    sar_people.main(["--root", root, "list"])  # создаёт базу
    conn = _db_of(root)
    sar_common.person_upsert_request(conn, 99, "pilot", "Пилот")
    conn.close()
    assert sar_people.main(["--root", root, "approve", "@pilot"]) == 0
    assert "login?key=" in capsys.readouterr().out
    conn = _db_of(root)
    assert sar_common.person_get(conn, 99)["status"] == "approved"
    conn.close()


# --- веб: /api/admin/people ---------------------------------------------------

@pytest.fixture
def web(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    dbp = str(tmp_path / "sar_data.db")
    sar_common.init_db(dbp)
    monkeypatch.setattr(sar_server, "DB_PATH", dbp, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"}, raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    return sar_server.app.test_client(), dbp


def _as(client, role, verified=True):
    with client.session_transaction() as s:
        s["authed"] = True
        s["verified"] = verified
        s["viewer_name"] = "x"
        s["role"] = role


@pytest.mark.parametrize("role,verified", [(sar_common.ROLE_VIEWER, True),
                                           (sar_common.ROLE_MODERATOR, True),
                                           (sar_common.ROLE_ADMIN, False)])
def test_only_verified_admin_can_manage_people(web, role, verified):
    client, _ = web
    _as(client, role, verified)
    assert client.get("/api/admin/people").status_code == 403
    assert client.post("/api/admin/people", json={"name": "a"}).status_code == 403


def test_password_login_named_admin_gets_no_rights(web):
    """Права по имени не выдаются: «админ» с общим паролем -- аноним."""
    client, _ = web
    client.post("/login", data={"name": "админ", "password": "pw"})
    assert client.get("/admin").status_code == 403
    assert client.get("/api/admin/people").status_code == 403


def test_admin_adds_person_and_link_logs_in_with_role(web):
    client, dbp = web
    _as(client, sar_common.ROLE_ADMIN)
    r = client.post("/api/admin/people", json={"name": "Аня", "role": "moderator"}).get_json()
    assert r["ok"] and r["link"].startswith("/login?key=")
    assert all("access_token" not in p and r["link"].split("=")[1] not in str(p)
               for p in r["people"]), "ключ только в поле link, не в списке"
    other = sar_server.app.test_client()
    resp = other.get(r["link"])
    assert resp.status_code in (302, 303)
    with other.session_transaction() as s:
        assert s["role"] == sar_common.ROLE_MODERATOR and s["verified"] is True


def test_admin_role_link_approve_revoke(web):
    client, dbp = web
    conn = sar_common.get_db_connection(dbp)
    me = sar_common.person_create_local(conn, "Я", sar_common.ROLE_ADMIN)
    sar_common.person_upsert_request(conn, 4242, "pilot", "Пилот")
    conn.close()
    _as(client, sar_common.ROLE_ADMIN)
    r = client.post("/api/admin/people/4242/approve").get_json()
    assert r["ok"] and r["link"].startswith("/login?key=")
    assert client.post("/api/admin/people/4242/role", json={"role": "admin"}).get_json()["ok"]
    old = r["link"]
    new = client.post("/api/admin/people/4242/link").get_json()["link"]
    assert new != old
    assert client.post("/api/admin/people/4242/revoke").get_json()["ok"]
    # последний оставшийся админ -- «Я»
    bad = client.post(f"/api/admin/people/{me['chat_id']}/role", json={"role": "viewer"})
    assert bad.status_code == 400 and "последний" in bad.get_json()["error"]
    assert client.post("/api/admin/people/999/revoke").status_code == 400
    roles = client.get("/api/admin/people").get_json()["roles"]
    assert {x["id"] for x in roles} == sar_common.VALID_ROLES


def test_admin_page_has_people_section(web):
    client, _ = web
    _as(client, sar_common.ROLE_ADMIN)
    page = client.get("/admin").get_data(as_text=True)
    assert "Люди" in page and "loadPeople()" in page and "location.origin" in page


def test_buttons_work_for_people_without_telegram(web):
    """Номер у них отрицательный; обычный <int> во Flask отвечал бы 404."""
    client, dbp = web
    _as(client, sar_common.ROLE_ADMIN)
    client.post("/api/admin/people", json={"name": "Админ", "role": "admin"})
    p = client.post("/api/admin/people", json={"name": "Аня"}).get_json()["person"]
    assert p["id"] < 0
    for action, body in (("role", {"role": "moderator"}), ("link", {}), ("revoke", {})):
        r = client.post(f"/api/admin/people/{p['id']}/{action}", json=body)
        assert r.status_code == 200, (action, r.status_code)


def test_cyrillic_names_match_regardless_of_case(db):
    sar_common.person_create_local(db, "Саша")
    assert sar_common.person_find(db, "САША") is not None
    with pytest.raises(sar_common.PersonError):
        sar_common.person_create_local(db, "сАША")
