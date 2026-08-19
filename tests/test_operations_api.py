"""Тесты создания операций через интерфейс.

До этого операцию можно было завести только скриптом миграции -- то есть
пользователь не мог создать поиск вообще никак. Проверяется весь путь:
права, проверка ввода, создание папки с меткой и сводка для карточки.

Отдельное внимание имени папки. Человек ищет её в проводнике по тому же
слову, что видит в интерфейсе, поэтому папка называется как операция. Но
«Поиск 12/08» с косой чертой создал бы ВЛОЖЕННУЮ папку вместо одной, а
имя с двоеточием на Windows не создалось бы вовсе.
"""
import json
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
    return sar_server.app.test_client(), str(watch), db


def _login(client, role=sar_common.ROLE_MODERATOR):
    with client.session_transaction() as s:
        s["authed"] = True
        s["verified"] = True
        s["viewer_name"] = "координатор"
        s["role"] = role


# --- права ----------------------------------------------------------------

def test_viewer_cannot_create_operation(env):
    client, _, _ = env
    _login(client, sar_common.ROLE_VIEWER)
    r = client.post("/api/operations", json={"title": "Новый поиск"})
    assert r.status_code == 403


def test_anonymous_cannot_create_operation(env):
    client, _, _ = env
    with client.session_transaction() as s:
        s["authed"] = True          # вошёл по общему паролю, роли нет
        s["viewer_name"] = "аноним"
    r = client.post("/api/operations", json={"title": "Новый поиск"})
    assert r.status_code == 403


def test_moderator_can_create_operation(env):
    client, _, _ = env
    _login(client)
    r = client.post("/api/operations", json={"title": "Иссык-Куль сентябрь"})
    assert r.status_code == 200 and r.get_json()["ok"]


def test_admin_can_create_operation(env):
    client, _, _ = env
    _login(client, sar_common.ROLE_ADMIN)
    assert client.post("/api/operations", json={"title": "Поиск"}).status_code == 200


# --- проверка ввода -------------------------------------------------------

def test_empty_title_is_rejected(env):
    client, _, _ = env
    _login(client)
    r = client.post("/api/operations", json={"title": "   "})
    assert r.status_code == 400
    assert "название" in r.get_json()["error"]


def test_overlong_title_is_rejected(env):
    client, _, _ = env
    _login(client)
    r = client.post("/api/operations", json={"title": "я" * 200})
    assert r.status_code == 400


# --- папка и метка --------------------------------------------------------

def test_folder_is_created_with_marker(env):
    client, watch, db = env
    _login(client)
    r = client.post("/api/operations", json={"title": "Иссык-Куль сентябрь"})
    body = r.get_json()
    folder = os.path.join(watch, body["folder"])
    assert os.path.isdir(folder), "папка операции не создана"
    assert sar_common.read_operation_marker(folder) == body["id"]


def test_folder_is_found_by_the_scanner(env):
    client, watch, _ = env
    _login(client)
    client.post("/api/operations", json={"title": "Иссык-Куль"})
    ops = sar_common.operation_folders(watch)
    assert [name for _, name, _ in ops] == ["Иссык-Куль"]


@pytest.mark.parametrize("title,expect", [
    ("Поиск 12/08", "Поиск 12_08"),          # косая создала бы вложенную папку
    ("Курумды: август", "Курумды_ август"),   # двоеточие запрещено в Windows
    ("  лишние   пробелы  ", "лишние пробелы"),
    ("точка в конце.", "точка в конце"),      # хвостовая точка недопустима
])
def test_folder_name_is_safe(title, expect):
    assert sar_common.folder_name_for(title) == expect


def test_folder_name_never_empty():
    assert sar_common.folder_name_for("///") != ""


def test_two_operations_do_not_share_a_folder(env):
    """Одинаковые названия -- разные операции, но папка одна. Метка при
    этом укажет на последнюю: случай редкий, но молчать о нём нельзя."""
    client, watch, db = env
    _login(client)
    a = client.post("/api/operations", json={"title": "Поиск"}).get_json()
    b = client.post("/api/operations", json={"title": "Поиск"}).get_json()
    assert a["id"] != b["id"]
    marker = sar_common.read_operation_marker(os.path.join(watch, "Поиск"))
    assert marker == b["id"]


# --- список и сводка ------------------------------------------------------

def test_list_returns_operations_with_summary(env):
    client, _, db = env
    _login(client)
    client.post("/api/operations", json={"title": "Курумды", "area": "Алай"})
    body = client.get("/api/operations").get_json()
    assert len(body["operations"]) == 1
    op = body["operations"][0]
    assert op["title"] == "Курумды" and op["area"] == "Алай"
    for key in ("materials", "footage_sec", "watched_sec", "marks"):
        assert key in op


def test_summary_counts_materials_and_work(env):
    client, _, db = env
    _login(client)
    oid = client.post("/api/operations", json={"title": "Курумды"}).get_json()["id"]
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "duration_sec, created_at, updated_at) VALUES "
              "('r1','a.MP4','/a.MP4','video','done',600,'x','x')")
    c.execute("INSERT INTO watch_segments (report_id, viewer_name, start_sec, "
              "end_sec, ts) VALUES ('r1','Айгуль',0,150,'x')")
    c.execute("INSERT INTO manual_observations (report_id, viewer_name, "
              "timestamp_sec, bbox, label, created_at) "
              "VALUES ('r1','Айгуль',5,'[0,0,1,1]','рюкзак','x')")
    c.commit()
    sar_common.attach_material(c, oid, "r1")
    c.close()

    op = client.get("/api/operations").get_json()["operations"][0]
    assert op["materials"] == 1
    assert op["footage_sec"] == 600
    assert op["watched_sec"] == 150
    assert op["marks"] == 1


def test_coverage_never_exceeds_100_percent(env):
    """Регрессия, замеченная на боевых данных.

    Сводка складывала сегменты ВСЕХ зрителей, и операция показывала
    «просмотрено 193%»: 1.6 часа съёмки против 3.0 часов суммарного
    просмотра. Такую цифру нельзя показать заказчику -- она выглядит как
    поломка, а по сути путает два разных показателя."""
    client, _, db = env
    _login(client)
    oid = client.post("/api/operations", json={"title": "Курумды"}).get_json()["id"]
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "duration_sec, created_at, updated_at) VALUES "
              "('r1','a.MP4','/a.MP4','video','done',100,'x','x')")
    # трое посмотрели ОДНО И ТО ЖЕ видео целиком
    for who in ("Айгуль", "Karpuha", "Олеся"):
        c.execute("INSERT INTO watch_segments (report_id, viewer_name, start_sec, "
                  "end_sec, ts) VALUES ('r1',?,0,100,'x')", (who,))
    c.commit()
    sar_common.attach_material(c, oid, "r1")
    c.close()

    op = client.get("/api/operations").get_json()["operations"][0]
    assert op["coverage_pct"] == 100, op["coverage_pct"]
    assert op["watched_sec"] == 100, "просмотрено должно быть длительностью видео"
    assert op["viewer_sec"] == 300, "человеко-часы должны считаться отдельно"


def test_partial_coverage_is_computed_correctly(env):
    client, _, db = env
    _login(client)
    oid = client.post("/api/operations", json={"title": "Курумды"}).get_json()["id"]
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "duration_sec, created_at, updated_at) VALUES "
              "('r1','a.MP4','/a.MP4','video','done',200,'x','x')")
    # два пересекающихся отрезка: 0-60 и 40-100 -> уникально 0-100
    c.execute("INSERT INTO watch_segments (report_id, viewer_name, start_sec, "
              "end_sec, ts) VALUES ('r1','Айгуль',0,60,'x')")
    c.execute("INSERT INTO watch_segments (report_id, viewer_name, start_sec, "
              "end_sec, ts) VALUES ('r1','Karpuha',40,100,'x')")
    c.commit()
    sar_common.attach_material(c, oid, "r1")
    c.close()

    op = client.get("/api/operations").get_json()["operations"][0]
    assert op["watched_sec"] == 100
    assert op["viewer_sec"] == 120
    assert op["coverage_pct"] == 50


@pytest.mark.parametrize("segs,expect", [
    ([(0, 10)], 10),
    ([(0, 10), (0, 10)], 10),               # полное совпадение
    ([(0, 10), (5, 15)], 15),               # пересечение
    ([(0, 10), (20, 30)], 20),              # разрыв
    ([(0, 10), (10, 20)], 20),              # стык
    ([(10, 20), (0, 10)], 20),              # порядок не важен
    ([(5, 5), (0, 10)], 10),                # пустой отрезок
    ([], 0),
])
def test_merged_length(segs, expect):
    assert sar_common.merged_length(segs) == expect


def test_list_reports_unsorted_count(env):
    client, _, db = env
    _login(client)
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "created_at, updated_at) VALUES ('r9','x.MP4','/x.MP4','video','done','x','x')")
    c.commit()
    c.close()
    assert client.get("/api/operations").get_json()["unsorted"] == 1


def test_viewer_can_read_the_list(env):
    """Смотреть операции могут все -- разграничение доступа отложено."""
    client, _, _ = env
    _login(client, sar_common.ROLE_VIEWER)
    body = client.get("/api/operations").get_json()
    assert body["can_manage"] is False
    assert "operations" in body


# --- страница -------------------------------------------------------------

def test_operations_page_requires_login(env):
    client, _, _ = env
    r = client.get("/operations", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_operations_page_renders(env):
    client, _, _ = env
    _login(client)
    html = client.get("/operations").get_data(as_text=True)
    assert "Операции" in html
    assert "Новая операция" in html


def test_page_has_no_nested_links(env):
    """Вложенные <a> внутри <a> -- невалидный HTML, браузер разбирает их
    непредсказуемо. В проекте эти грабли уже были."""
    import re
    client, _, _ = env
    _login(client)
    html = client.get("/operations").get_data(as_text=True)
    assert not re.findall(r"<a\b[^>]*>(?:(?!</a>).)*<a\b", html, re.S)


def test_create_button_hidden_from_viewer(env):
    """Кнопка показывается по can_manage из API, а не по вёрстке -- но
    сама разметка обязана уметь её прятать."""
    client, _, _ = env
    _login(client, sar_common.ROLE_VIEWER)
    html = client.get("/operations").get_data(as_text=True)
    assert 'id="new" style="display:none"' in html
