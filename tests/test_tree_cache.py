"""Кэш списка материалов.

/api/tree -- самый дорогой запрос платформы, и его дёргает каждая открытая
вкладка раз в 5 секунд, включая забытые. Замерено нагрузочным тестом: при
восьми одновременных он отвечает 1.5 секунды против десятков миллисекунд у
всего остального, а пропускная способность платформы падает со 105 до 59
запросов в секунду. В ночь пика операции восемь человек одновременно как
раз и были -- то есть список тормозил ровно тогда, когда работы было
больше всего.

Главный риск такого кэша -- отдать одному человеку данные другого. Он
безопасен ровно до тех пор, пока в ответе нет ничего, зависящего от
конкретного зрителя. Это здесь и проверяется, а не только «стало быстрее».
"""
import time

import pytest

import sar_common
import sar_server


@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "DJI_A.MP4").write_bytes(b"x")
    (watch / "DJI_B.MP4").write_bytes(b"x")
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(watch), raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    sar_server._tree_cache_clear()
    c = sar_server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "Первый"
    c.watch = watch
    return c


@pytest.fixture
def counted_scan(monkeypatch):
    """Считает, сколько раз реально обошли диск -- именно это и дорого."""
    calls = []
    real = sar_common.scan_all_materials

    def spy(watch_dir, *a, **kw):
        calls.append(watch_dir)
        return real(watch_dir, *a, **kw)

    monkeypatch.setattr(sar_common, "scan_all_materials", spy)
    return calls


# --- кэш работает ---------------------------------------------------------

def test_repeated_requests_do_not_rescan(client, counted_scan):
    for _ in range(5):
        assert client.get("/api/tree").status_code == 200
    assert len(counted_scan) == 1, (
        f"диск обошли {len(counted_scan)} раз вместо одного -- кэш не работает")


def test_answer_is_the_same_from_cache(client):
    first = client.get("/api/tree").get_json()
    second = client.get("/api/tree").get_json()
    assert first == second


def test_cache_expires(client, counted_scan, monkeypatch):
    client.get("/api/tree")
    assert len(counted_scan) == 1
    # сдвигаем время вместо ожидания: тест не должен спать
    real_time = time.time
    monkeypatch.setattr(sar_server.time, "time",
                        lambda: real_time() + sar_server.TREE_CACHE_TTL_SEC + 1)
    client.get("/api/tree")
    assert len(counted_scan) == 2, "кэш не протухает -- список застынет навсегда"


def test_ttl_is_short_enough_to_stay_fresh():
    """Список опрашивается клиентом раз в 5 секунд. Срок жизни кэша должен
    быть заметно меньше, иначе задержка станет ощутимой."""
    assert 0 < sar_server.TREE_CACHE_TTL_SEC <= 5


# --- разные операции не путаются -----------------------------------------

def test_operations_are_cached_separately(client):
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    op = sar_common.create_operation(conn, "Тестовая", "")
    conn.commit()
    conn.close()

    all_items = client.get("/api/tree").get_json()
    one_op = client.get(f"/api/tree?op={op}").get_json()
    unsorted = client.get("/api/tree?op=unsorted").get_json()

    assert all_items["op"] is None
    assert one_op["op"] == str(op)
    assert unsorted["operation"] == "Не разобрано"
    assert len(one_op["items"]) == 0, "в пустой операции материалов быть не должно"
    assert len(all_items["items"]) == 2


def test_missing_operation_is_not_cached_as_a_valid_answer(client):
    r = client.get("/api/tree?op=99999")
    assert r.status_code == 404
    # и не должен попасть в кэш под ключом, который потом отдастся как 200
    r2 = client.get("/api/tree?op=99999")
    assert r2.status_code == 404


# --- главное: кэш общий, значит в ответе не должно быть личного -----------

def test_answer_does_not_depend_on_who_asks(client):
    """Условие, при котором один кэш на всех вообще допустим.

    Если это сломается, кэш начнёт отдавать одному человеку данные другого.
    Тогда его надо будет либо разделить по зрителю, либо убрать.
    """
    first = client.get("/api/tree").get_json()

    other = sar_server.app.test_client()
    with other.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "Второй"
    sar_server._tree_cache_clear()
    second = other.get("/api/tree").get_json()

    assert first == second, (
        "ответ зависит от того, кто спрашивает -- общий кэш небезопасен")


def test_no_per_viewer_fields_in_items(client):
    """Прямая проверка на поля, которые выдали бы личные данные."""
    items = client.get("/api/tree").get_json()["items"]
    assert items
    forbidden = {"viewer_name", "my_progress", "watched_by_me", "is_mine",
                 "viewer", "author", "session"}
    for it in items:
        leaked = forbidden & set(it)
        assert not leaked, (
            f"в ответе личные поля {leaked} -- общий кэш раздаст их всем")


def test_cache_does_not_leak_between_data_sets(tmp_path, monkeypatch, client):
    """Кэш обязан помнить, ДЛЯ КАКОГО набора данных посчитан ответ.

    Сначала ключом был только фильтр по операции: в бою папка и база не
    меняются, и казалось, что этого достаточно. Оказалось нет -- восемь
    существующих тестов, у каждого своя временная папка, начали получать
    чужие списки файлов. Кэш не должен опираться на то, что окружение
    «обычно не меняется».
    """
    first = client.get("/api/tree").get_json()
    assert len(first["items"]) == 2

    other_watch = tmp_path / "другая"
    other_watch.mkdir()
    (other_watch / "DJI_X.MP4").write_bytes(b"x")
    other_db = str(tmp_path / "другая.db")
    sar_common.init_db(other_db)
    monkeypatch.setitem(sar_server.SERVER_CFG, "watch_dir", str(other_watch))
    monkeypatch.setattr(sar_server, "DB_PATH", other_db)

    second = client.get("/api/tree").get_json()
    names = [i["name"] for i in second["items"]]
    assert names == ["DJI_X.MP4"], (
        f"кэш отдал список от другой папки: {names}")


# --- новые файлы всё же появляются ---------------------------------------

def test_new_file_shows_up_after_the_cache_expires(client, monkeypatch):
    assert len(client.get("/api/tree").get_json()["items"]) == 2
    (client.watch / "DJI_C.MP4").write_bytes(b"x")

    real_time = time.time
    monkeypatch.setattr(sar_server.time, "time",
                        lambda: real_time() + sar_server.TREE_CACHE_TTL_SEC + 1)
    assert len(client.get("/api/tree").get_json()["items"]) == 3, (
        "новый файл не появился даже после протухания кэша")
