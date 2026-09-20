"""Страница настроек: права, зажим значений, связь с реестром.

Зачем страница. Материал уезжает в облако, и каждое чтение файла становится
скачиванием. Ограничители, зашитые в код, нельзя подстроить под конкретный
канал, не правя файл и не перезапуская оба процесса.

Чем опасна такая страница. Это форма, которая меняет поведение того, что
качает и пишет на диск. Три вещи должны быть верны всегда:

  * менять может только администратор -- иначе любой участник операции
    выставит "50 загрузок" и положит канал всей группе;
  * опасное значение не сохраняется даже на секунду -- его мог бы успеть
    прочитать воркер;
  * человек видит то, что РЕАЛЬНО сохранилось. Показать введённое, а
    сохранить зажатое -- молчаливое расхождение: настройка выглядит
    применённой, а работает другая.
"""
import re
import sqlite3

import pytest

import sar_common
import sar_server


@pytest.fixture
def env(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", "/no/such/config/dir")
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    return sar_server.app.test_client()


def _login(client, role=sar_common.ROLE_ADMIN, name="админ"):
    with client.session_transaction() as s:
        s["authed"] = True
        s["verified"] = True
        s["viewer_name"] = name
        s["role"] = role


# --- права ----------------------------------------------------------------

def test_participant_cannot_open_the_page(env):
    _login(env, sar_common.ROLE_VIEWER)
    assert env.get("/admin").status_code == 403


def test_moderator_cannot_open_the_page(env):
    """Модератор правит обсуждения, а не расход канала операции."""
    _login(env, sar_common.ROLE_MODERATOR)
    assert env.get("/admin").status_code == 403


def test_participant_cannot_change_settings_through_the_api(env):
    """Страницу можно и не открывать -- запрос шлётся напрямую."""
    _login(env, sar_common.ROLE_VIEWER)
    r = env.post("/api/admin/settings", json={"downloads_in_flight": 4})
    assert r.status_code == 403


def test_anonymous_with_shared_password_cannot_change_settings(env):
    """Общий пароль знают все участники поиска -- это не админ."""
    with env.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "аноним"
    assert env.post("/api/admin/settings", json={}).status_code == 403


def test_admin_can_open_the_page(env):
    _login(env)
    assert env.get("/admin").status_code == 200


# --- чтение и запись ------------------------------------------------------

def test_settings_round_trip(env):
    _login(env)
    env.post("/api/admin/settings", json={"downloads_in_flight": 3})
    d = env.get("/api/admin/settings").get_json()
    assert d["values"]["downloads_in_flight"] == 3


def test_response_shows_what_was_actually_saved(env):
    """Ввели 50 -- в ответе обязано быть 4, иначе человек уверен, что
    поставил 50, а работает 4."""
    _login(env)
    d = env.post("/api/admin/settings",
                 json={"downloads_in_flight": 50}).get_json()
    assert d["values"]["downloads_in_flight"] == 4


def test_dangerous_value_never_reaches_the_database(env, tmp_path):
    """Даже на мгновение: между записью и исправлением его мог бы
    прочитать воркер -- он перечитывает настройки каждый проход."""
    _login(env)
    env.post("/api/admin/settings", json={"downloads_in_flight": 999})
    conn = sqlite3.connect(sar_server.DB_PATH)
    raw = conn.execute(
        "SELECT value FROM settings WHERE key='downloads_in_flight'").fetchone()[0]
    conn.close()
    assert int(raw) == 4


def test_boolean_off_survives(env):
    """Хранится текстом: наивное чтение превратило бы выключенное в
    включённое, и подключение большого хранилища запустило бы обработку
    всего подряд."""
    _login(env)
    env.post("/api/admin/settings", json={"auto_process": False})
    assert env.get("/api/admin/settings").get_json()["values"]["auto_process"] is False


def test_unknown_key_does_not_block_the_rest(env):
    """Старая вкладка, открытая до обновления, пришлёт лишнее поле."""
    _login(env)
    r = env.post("/api/admin/settings",
                 json={"downloads_in_flight": 2, "древний_ключ": 5})
    assert r.status_code == 200
    assert r.get_json()["values"]["downloads_in_flight"] == 2


def test_author_is_recorded(env):
    _login(env, name="Дмитрий")
    env.post("/api/admin/settings", json={"staging_cap_gb": 6})
    d = env.get("/api/admin/settings").get_json()
    assert d["schema"]["staging_cap_gb"]["set_by"] == "Дмитрий"


# --- форма строится из реестра -------------------------------------------

def test_form_is_built_from_the_registry_not_a_second_list(env):
    """Второй список полей во фронтенде разошёлся бы с реестром: добавили
    настройку в код -- в форме её нет, и никто не заметит."""
    _login(env)
    d = env.get("/api/admin/settings").get_json()
    assert set(d["schema"]) == set(sar_common.SETTINGS_SCHEMA)
    page = env.get("/admin").get_data(as_text=True)
    for key in sar_common.SETTINGS_SCHEMA:
        assert key not in page, (
            f"имя настройки {key} зашито в разметку -- форма перестала "
            f"строиться из реестра")


def test_every_setting_reaches_the_form_with_its_bounds(env):
    _login(env)
    d = env.get("/api/admin/settings").get_json()
    for key, spec in d["schema"].items():
        assert spec["label"] and spec["help"]
        if spec["type"] in ("int", "float"):
            assert "min" in spec and "max" in spec
        if spec["type"] == "choice":
            assert spec.get("options"), f"{key}: выбор без вариантов"


# --- сама страница --------------------------------------------------------

def test_page_has_no_unreplaced_placeholders(env):
    """Классическая поломка шаблона: .format() съедает одинарные скобки,
    и в браузер уезжает кусок кода вместо разметки."""
    _login(env)
    page = env.get("/admin").get_data(as_text=True)
    assert "{viewer_name}" not in page
    assert "${{" not in page, "двойные скобки шаблона утекли в готовый HTML"


def test_embedded_script_is_structurally_balanced(env):
    """Замена для `node --check`: node в этой системе не установлен,
    поэтому проверяем то, что можно проверить без него -- баланс скобок
    в готовом (уже отформатированном) скрипте. Это ловит именно ту
    поломку, которая случается с этими шаблонами: лишняя или съеденная
    скобка после .format().
    """
    _login(env)
    page = env.get("/admin").get_data(as_text=True)
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    # строки и шаблонные литералы выкидываем -- в них скобки не считаются
    body = re.sub(r"`(?:\\.|[^`\\])*`", "``", script)
    body = re.sub(r"'(?:\\.|[^'\\])*'", "''", body)
    body = re.sub(r'"(?:\\.|[^"\\])*"', '""', body)
    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        assert body.count(opening) == body.count(closing), (
            f"скобки {opening}{closing} не сбалансированы в скрипте админки")


def test_page_links_back_to_the_operations(env):
    """Тупик без выхода -- отдельная категория жалоб в этом проекте."""
    _login(env)
    assert 'href="/"' in env.get("/admin").get_data(as_text=True)


# --- подключение облачных хранилищ ----------------------------------------
#
# Здесь хранятся ТОКЕНЫ ДОСТУПА -- секреты того же уровня, что общий пароль
# платформы и токен бота. Главная проверка: наружу они не выходят никогда.

def test_cloud_list_requires_admin(env):
    _login(env, sar_common.ROLE_VIEWER)
    assert env.get("/api/admin/cloud").status_code == 403


def test_connecting_requires_admin(env):
    _login(env, sar_common.ROLE_VIEWER)
    r = env.post("/api/admin/cloud",
                 json={"provider": "google", "token": "секрет"})
    assert r.status_code == 403


def test_unknown_provider_is_refused(env):
    _login(env)
    r = env.post("/api/admin/cloud", json={"provider": "dropbox", "token": "x"})
    assert r.status_code == 400


def test_empty_token_is_refused_with_a_clear_reason(env):
    _login(env)
    r = env.post("/api/admin/cloud", json={"provider": "google", "token": "  "})
    assert r.status_code == 400
    assert "токен" in r.get_json()["error"].lower()


def test_connection_is_verified_before_being_saved(env, monkeypatch):
    """Подключение, которое не работает, не должно попадать в список
    исправных: человек уйдёт уверенный, что диск подключён, а выяснится
    это при первой обработке -- то есть посреди операции."""
    import sar_cloud

    def boom(self, folder):
        raise sar_cloud.AuthExpired("нет доступа")

    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", boom)
    _login(env)
    r = env.post("/api/admin/cloud",
                 json={"provider": "google", "token": "ya29.rejected"})
    assert r.status_code == 400
    assert env.get("/api/admin/cloud").get_json()["accounts"] == []


def test_bad_token_message_says_what_to_check(env, monkeypatch):
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder",
                        lambda self, f: (_ for _ in ()).throw(
                            sar_cloud.AuthExpired("x")))
    _login(env)
    err = env.post("/api/admin/cloud",
                   json={"provider": "google", "token": "t"}).get_json()["error"]
    assert "истёк" in err or "чтения" in err


def test_successful_connection_is_stored(env, monkeypatch):
    import sar_cloud
    monkeypatch.setattr(
        sar_cloud.GoogleDrive, "list_folder",
        lambda self, f: [sar_cloud.FileInfo("1", "DJI_1.MP4", 100)])
    _login(env)
    r = env.post("/api/admin/cloud",
                 json={"provider": "google", "token": "ya29.valid-looking",
                       "label": "Диск операции"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["found"] == 1
    assert d["accounts"][0]["label"] == "Диск операции"


def test_token_is_never_returned_to_the_browser(env, monkeypatch):
    """САМОЕ ВАЖНОЕ. Показать токен «для удобства» значит положить его в
    историю браузера, в скриншот и в пересланное сообщение."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder",
                        lambda self, f: [])
    _login(env)
    secret = "очень-секретный-токен-12345"
    post = env.post("/api/admin/cloud",
                    json={"provider": "google", "token": secret})
    assert secret not in post.get_data(as_text=True)
    listing = env.get("/api/admin/cloud")
    assert secret not in listing.get_data(as_text=True)
    for acc in listing.get_json()["accounts"]:
        assert "token" not in acc and "refresh_token" not in acc


def test_disconnect_removes_the_account(env, monkeypatch):
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda self, f: [])
    _login(env)
    acc_id = env.post("/api/admin/cloud",
                      json={"provider": "google", "token": "t"}).get_json()["id"]
    r = env.delete("/api/admin/cloud/%d" % acc_id)
    assert r.status_code == 200
    assert r.get_json()["accounts"] == []


def test_browse_reports_why_it_failed(env, monkeypatch):
    """«Почему не видно файлов» не должно выясняться чтением журнала
    воркера -- причина пишется прямо в подключение."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda self, f: [])
    _login(env)
    acc_id = env.post("/api/admin/cloud",
                      json={"provider": "google", "token": "t"}).get_json()["id"]

    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder",
                        lambda self, f: (_ for _ in ()).throw(
                            sar_cloud.CloudError("папка удалена")))
    r = env.get("/api/admin/cloud/%d/browse" % acc_id)
    assert r.status_code == 400
    acc = env.get("/api/admin/cloud").get_json()["accounts"][0]
    assert "папка удалена" in (acc["last_error"] or "")


def test_page_offers_both_providers(env):
    _login(env)
    d = env.get("/api/admin/cloud").get_json()
    assert {p["name"] for p in d["providers"]} == {"google", "yandex"}
    for p in d["providers"]:
        assert p["label"], "хранилище без человеческого названия"


# --- папка: ссылка вместо идентификатора ----------------------------------
#
# Найдено на боевом подключении. В поле «папка» естественнее всего вставить
# ССЫЛКУ -- её видно в адресной строке и её копируют. Раньше она не
# распознавалась, поле считалось пустым, и платформа молча бралась за
# КОРЕНЬ ДИСКА: в платформу поисковой операции затянуло 288 личных
# фотографий из чужих папок.

def test_folder_url_is_understood(env, monkeypatch):
    import sar_cloud
    seen = {}

    def spy(self, folder):
        seen["folder"] = folder
        return []

    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", spy)
    _login(env)
    env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t",
        "root_id": "https://drive.google.com/drive/folders/1uD9uCz?usp=drive_link"})
    assert seen["folder"] == "1uD9uCz", (
        "ссылка не разобрана -- платформа взялась бы за весь диск")


def test_connecting_to_the_whole_drive_warns(env, monkeypatch):
    """Молчать нельзя: человек уходит уверенный, что подключил нужную
    папку, а платформа забирает всё подряд."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    d = env.post("/api/admin/cloud",
                 json={"provider": "google", "token": "ya29.t"}).get_json()
    assert d["warnings"], "подключение к корню прошло молча"
    assert any("весь диск" in w.lower() for w in d["warnings"])


def test_explicit_folder_does_not_warn_about_the_folder(env, monkeypatch):
    """Предупреждений два и они независимы: про весь диск и про продление
    доступа. Указав папку, человек снимает первое, но не второе."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    d = env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t", "root_id": "1uD9uCz",
        "client_id": "cid", "client_secret": "s",
        "refresh_token": "rt"}).get_json()
    assert not any("папк" in w.lower() for w in d["warnings"])


def test_google_without_refresh_keys_warns(env, monkeypatch):
    """Токен Google живёт час. Подключение без ключей продления
    гарантированно перестанет работать -- и сказать об этом надо сразу, а
    не через час, когда человек уже ушёл заниматься другим."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    d = env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t", "root_id": "1uD9uCz"}).get_json()
    assert any("через час" in w for w in d["warnings"])


def test_yandex_is_not_warned_about_refresh(env, monkeypatch):
    """У Яндекса токен живёт около года -- пугать нечем."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.YandexDisk, "list_folder", lambda s, f: [])
    _login(env)
    d = env.post("/api/admin/cloud", json={
        "provider": "yandex", "token": "y0_t", "root_id": "disk:/Оп"}).get_json()
    assert not any("через час" in w for w in d["warnings"])


def test_refresh_keys_are_stored(env, monkeypatch):
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t", "client_id": "cid",
        "client_secret": "sec", "refresh_token": "rt"})
    acc = env.get("/api/admin/cloud").get_json()["accounts"][0]
    assert acc["can_refresh"] is True


def test_refresh_secrets_never_reach_the_browser(env, monkeypatch):
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    post = env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t", "client_id": "cid",
        "client_secret": "ОЧЕНЬ-СЕКРЕТНО", "refresh_token": "СЕКРЕТНЫЙ-RT"})
    listing = env.get("/api/admin/cloud")
    for body in (post.get_data(as_text=True), listing.get_data(as_text=True)):
        assert "ОЧЕНЬ-СЕКРЕТНО" not in body
        assert "СЕКРЕТНЫЙ-RT" not in body


def test_link_to_a_file_is_refused_with_advice(env, monkeypatch):
    """Ссылка на файл -- другая сущность. Молча подставлять её как папку
    значит обещать то, чего не будет."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    r = env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t",
        "root_id": "https://drive.google.com/file/d/abc/view"})
    assert r.status_code == 400
    assert "не на папку" in r.get_json()["error"]


def test_changing_folder_also_accepts_a_link(env, monkeypatch):
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    acc = env.post("/api/admin/cloud",
                   json={"provider": "google", "token": "ya29.t"}).get_json()["id"]
    # Идентификатор папки Google -- латиница, цифры, дефис и подчёркивание.
    env.post("/api/admin/cloud/%d" % acc, json={
        "root_id": "https://drive.google.com/drive/folders/1aB-cD_2eF"})
    a = env.get("/api/admin/cloud").get_json()["accounts"][0]
    assert a["root_id"] == "1aB-cD_2eF"


def test_folder_link_pasted_into_the_name_field_is_understood(env, monkeypatch):
    """Так вставляли дважды: поле названия первое текстовое в форме, и рука
    идёт туда. Последствие тяжёлое -- поле папки остаётся пустым, платформа
    берёт весь диск и затягивает личные файлы. Раз люди так делают, надо
    это понимать, а не считать их ошибкой."""
    import sar_cloud
    seen = {}

    def spy(self, folder):
        seen["folder"] = folder
        return []

    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", spy)
    _login(env)
    d = env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t",
        "label": "https://drive.google.com/drive/folders/1uD9uCz?usp=drive_link",
    }).get_json()
    assert seen["folder"] == "1uD9uCz", "ссылка из поля названия не подхвачена"
    assert not (d["accounts"][0]["label"] or "").startswith("http"), (
        "ссылка осталась названием подключения")


def test_explicit_folder_wins_over_a_link_in_the_name(env, monkeypatch):
    """Если папка указана явно, название трогать не надо."""
    import sar_cloud
    seen = {}
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder",
                        lambda s, f: seen.setdefault("folder", f) or [])
    _login(env)
    env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t",
        "label": "http://пример", "root_id": "1aB"})
    assert seen["folder"] == "1aB"


def test_connecting_without_an_operation_warns(env, monkeypatch):
    """Материал попадёт в «Не разобрано», а человек будет искать его в
    операции и не найдёт."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    d = env.post("/api/admin/cloud", json={
        "provider": "google", "token": "ya29.t", "root_id": "1aB",
        "client_id": "c", "client_secret": "s", "refresh_token": "r"}).get_json()
    assert any("Операция не выбрана" in w for w in d["warnings"])


def test_disconnect_reports_what_it_cleaned(env, monkeypatch):
    """Молчаливое исчезновение сотни записей выглядит как потеря данных."""
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    acc = env.post("/api/admin/cloud",
                   json={"provider": "google", "token": "ya29.t"}).get_json()["id"]
    d = env.delete("/api/admin/cloud/%d" % acc).get_json()
    assert "cleanup" in d and "removed" in d["cleanup"]


# --- правка подключения ---------------------------------------------------

def _connected(env, monkeypatch, op_id=None):
    import sar_cloud
    monkeypatch.setattr(sar_cloud.GoogleDrive, "list_folder", lambda s, f: [])
    _login(env)
    body = {"provider": "google", "token": "ya29.t", "root_id": "1aB"}
    if op_id:
        body["operation_id"] = op_id
    return env.post("/api/admin/cloud", json=body).get_json()["id"]


def test_operation_can_be_changed_after_connecting(env, monkeypatch):
    """Раньше единственным способом было отключить и подключить заново --
    а отключение убирает записи материала, то есть за смену операции
    платили полным пересканом."""
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    op1 = sar_common.create_operation(conn, "Первая")
    op2 = sar_common.create_operation(conn, "Вторая")
    conn.close()
    acc = _connected(env, monkeypatch, op_id=op1)

    env.post("/api/admin/cloud/%d" % acc, json={"operation_id": op2})
    a = env.get("/api/admin/cloud").get_json()["accounts"][0]
    assert a["operation_id"] == op2


def test_changing_operation_moves_the_material(env, monkeypatch):
    """Иначе одна папка оказывается разложена по двум операциям, и понять
    это по интерфейсу невозможно."""
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    op1 = sar_common.create_operation(conn, "Первая")
    op2 = sar_common.create_operation(conn, "Вторая")
    conn.close()
    acc = _connected(env, monkeypatch, op_id=op1)

    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_account_id, cloud_file_id, created_at, updated_at) VALUES "
        "('c1','a.mp4','','video','idle',?,'f1',datetime('now'),datetime('now'))",
        (acc,))
    conn.commit()
    sar_common.attach_material(conn, op1, "c1")
    conn.close()

    env.post("/api/admin/cloud/%d" % acc, json={"operation_id": op2})

    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    ops = [o["id"] for o in sar_common.operations_of_material(conn, "c1")]
    conn.close()
    assert ops == [op2], f"материал остался в старой операции: {ops}"


def test_folder_can_be_changed_after_connecting(env, monkeypatch):
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    op = sar_common.create_operation(conn, "Оп")
    conn.close()
    acc = _connected(env, monkeypatch, op_id=op)
    env.post("/api/admin/cloud/%d" % acc,
             json={"root_id": "https://drive.google.com/drive/folders/НОВ-1aB"})
    a = env.get("/api/admin/cloud").get_json()["accounts"][0]
    assert a["root_id"] == "1aB" or a["root_id"].endswith("1aB")


def test_admin_page_offers_editing(env):
    _login(env)
    page = env.get("/admin").get_data(as_text=True)
    assert "saveCloud" in page and "opOptions" in page
