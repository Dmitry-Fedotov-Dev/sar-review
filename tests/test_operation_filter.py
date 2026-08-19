"""Фильтр списка материалов по операции.

Карточка операции ссылалась на /?op=<id>, а главная страница этот параметр
игнорировала -- открывался общий список всех файлов. Ссылка обманывала:
человек жал на операцию и получал то же самое, что и без неё.

Отбор идёт по СВЯЗЯМ в базе, а не по префиксу пути. Это принципиально:
материал может быть добавлен в операцию, физически лежа где угодно, и может
принадлежать двум операциям сразу -- путь на диске удобство раскладки, а не
источник истины о принадлежности.
"""
import os

import pytest

import sar_common
import sar_server


@pytest.fixture
def env(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_common, "resolve_paths",
                        lambda w: (str(watch), str(watch), db, str(watch)))
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["verified"] = True
        s["viewer_name"] = "тест"
        s["role"] = sar_common.ROLE_MODERATOR
    return c, str(watch), db


def _material(db_path, rid, rel, watch):
    """Создаёт файл на диске и запись о нём."""
    path = os.path.join(watch, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 16)
    c = sar_common.get_db_connection(db_path)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "created_at, updated_at) VALUES (?,?,?,'video','done','x','x')",
              (rid, rel, path))
    c.commit()
    c.close()


def _names(body):
    return sorted(i["name"] for i in body["items"])


# --- фильтр ---------------------------------------------------------------

def test_without_op_all_materials_are_shown(env):
    client, watch, db = env
    _material(db, "r1", "a.MP4", watch)
    _material(db, "r2", "b.MP4", watch)
    body = client.get("/api/tree").get_json()
    assert _names(body) == ["a.MP4", "b.MP4"]
    assert body["operation"] is None


def test_op_filter_shows_only_its_materials(env):
    client, watch, db = env
    _material(db, "r1", "a.MP4", watch)
    _material(db, "r2", "b.MP4", watch)
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Курумды")
    sar_common.attach_material(c, oid, "r1")
    c.close()

    body = client.get(f"/api/tree?op={oid}").get_json()
    assert _names(body) == ["a.MP4"]
    assert body["operation"] == "Курумды"


def test_material_in_two_operations_shows_in_both(env):
    """Один вылет пригодился в двух поисках -- он обязан быть виден в обоих."""
    client, watch, db = env
    _material(db, "r1", "a.MP4", watch)
    c = sar_common.get_db_connection(db)
    a = sar_common.create_operation(c, "Курумды")
    b = sar_common.create_operation(c, "Иссык-Куль")
    sar_common.attach_material(c, a, "r1")
    sar_common.attach_material(c, b, "r1")
    c.close()

    assert _names(client.get(f"/api/tree?op={a}").get_json()) == ["a.MP4"]
    assert _names(client.get(f"/api/tree?op={b}").get_json()) == ["a.MP4"]


def test_filter_uses_links_not_path_prefix(env):
    """Материал лежит В ПАПКЕ одной операции, но привязан к другой.

    Принадлежность определяют связи в базе: путь на диске -- удобство
    раскладки. Иначе перенос файла молча менял бы принадлежность."""
    client, watch, db = env
    c = sar_common.get_db_connection(db)
    a = sar_common.create_operation(c, "Курумды")
    b = sar_common.create_operation(c, "Иссык-Куль")
    c.close()
    # папка операции A -- с меткой, иначе сканер внутрь не заглянет
    folder = os.path.join(watch, "Курумды")
    os.makedirs(folder, exist_ok=True)
    sar_common.write_operation_marker(folder, a)
    _material(db, "r1", "Курумды/a.MP4", watch)

    c = sar_common.get_db_connection(db)
    sar_common.attach_material(c, b, "r1")      # привязан к ДРУГОЙ операции
    c.close()

    assert _names(client.get(f"/api/tree?op={b}").get_json()) == ["Курумды/a.MP4"]
    assert _names(client.get(f"/api/tree?op={a}").get_json()) == []


def test_unsorted_filter(env):
    client, watch, db = env
    _material(db, "r1", "a.MP4", watch)
    _material(db, "r2", "b.MP4", watch)
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Курумды")
    sar_common.attach_material(c, oid, "r1")
    c.close()

    body = client.get("/api/tree?op=unsorted").get_json()
    assert _names(body) == ["b.MP4"]
    assert body["operation"] == "Не разобрано"


def test_unknown_operation_returns_404(env):
    client, _, _ = env
    r = client.get("/api/tree?op=999")
    assert r.status_code == 404


def test_garbage_op_is_ignored_not_crashed(env):
    """Параметр приходит из адресной строки -- туда попадает что угодно."""
    client, watch, db = env
    _material(db, "r1", "a.MP4", watch)
    body = client.get("/api/tree?op=абв").get_json()
    assert _names(body) == ["a.MP4"], "мусор должен вести себя как отсутствие фильтра"


def test_empty_operation_returns_empty_list(env):
    client, _, db = env
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Пустая")
    c.close()
    body = client.get(f"/api/tree?op={oid}").get_json()
    assert body["items"] == []
    assert body["operation"] == "Пустая"


# --- страница -------------------------------------------------------------

def test_page_reads_op_from_the_address(env):
    """Ссылка должна быть поделимой: кинул коллеге /?op=3 -- он видит то же."""
    client, _, _ = env
    html = client.get("/").get_data(as_text=True)
    assert "new URLSearchParams(location.search).get('op')" in html


def test_page_passes_op_to_the_api(env):
    client, _, _ = env
    html = client.get("/").get_data(as_text=True)
    assert "'/api/tree' + (OP ? ('?op=' + encodeURIComponent(OP)) : '')" in html


def test_page_shows_which_operation_is_open(env):
    """Иначе непонятно, почему в списке часть файлов -- выглядит как пропажа."""
    client, _, _ = env
    html = client.get("/").get_data(as_text=True)
    assert 'id="page-title"' in html
    assert "'Материалы: '" in html


def test_page_has_link_back_to_operations(env):
    client, _, _ = env
    html = client.get("/").get_data(as_text=True)
    assert 'href="/operations"' in html
