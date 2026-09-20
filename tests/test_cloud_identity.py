"""Облачный файл не должен растаскивать работу по двум записям.

В облаке материал лежит иначе, чем локально. На боевом подключении путь в
облаке -- "2026 08 11/DJI_1.MP4", а в базе -- "Курумды август 2026/DJI_1.MP4":
папки в облаке названы по датам съёмки, а операция называется иначе.

Совпадение по пути при этом не срабатывает, и тот же самый файл заводится
ВТОРОЙ записью. Вся работа по нему -- пометки, обсуждения, отметки
просмотра -- остаётся на первой, которую в списке уже не видно. Именно так
проект терял покрытие: 73 процента вместо 82.

Здесь проверяется вторая ступень сверки -- по имени файла -- и три её
исхода: пропустить, подхватить, завести новый.
"""
import os

import pytest

import sar_common


@pytest.fixture
def env(tmp_path):
    watch = tmp_path / "watch"
    (watch / "Курумды август 2026").mkdir(parents=True)
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    yield conn, str(watch)
    conn.close()


def add_report(conn, rel, rid="r1"):
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES (?,?,'','video','done', "
        "datetime('now'), datetime('now'))", (rid, rel))
    conn.commit()


# --- файл лежит локально и уже разобран -----------------------------------

def test_existing_local_file_is_skipped(env):
    """Облачная копия не нужна: запись есть, файл на месте, работа
    сделана. Вторая запись только растащила бы её по двум материалам."""
    conn, watch = env
    rel = "Курумды август 2026/DJI_1.MP4"
    add_report(conn, rel)
    open(os.path.join(watch, "Курумды август 2026", "DJI_1.MP4"), "wb").write(b"x")

    row, what = sar_common.match_existing_material(
        conn, "2026 08 11/DJI_1.MP4", watch)
    assert what == "skip"
    assert row["report_id"] == "r1"


# --- запись есть, а файла нет ---------------------------------------------

def test_missing_local_file_is_adopted(env):
    """Лучшее, что вообще может дать подключение: вернуть доступ к
    материалу, который с диска пропал, а работа по нему осталась."""
    conn, watch = env
    add_report(conn, "Курумды август 2026/DJI_2.MP4")
    row, what = sar_common.match_existing_material(
        conn, "2026 08 11/DJI_2.MP4", watch)
    assert what == "adopt"
    assert row["report_id"] == "r1"


def test_exact_path_match_is_adopted(env):
    """Структура совпала -- обычный случай, работает как раньше."""
    conn, watch = env
    rel = "Курумды август 2026/DJI_3.MP4"
    add_report(conn, rel)
    row, what = sar_common.match_existing_material(conn, rel, watch)
    assert what == "adopt" and row["report_id"] == "r1"


# --- ничего похожего ------------------------------------------------------

def test_unknown_file_is_new(env):
    conn, watch = env
    row, what = sar_common.match_existing_material(
        conn, "2026 08 11/DJI_НОВЫЙ.MP4", watch)
    assert what == "new" and row is None


def test_ambiguous_name_is_not_guessed(env):
    """Одно имя в двух операциях. Привязать работу не к тому материалу
    хуже, чем завести новую запись: в первом случае человек видит чужие
    пометки на своём видео и не понимает, откуда они."""
    conn, watch = env
    add_report(conn, "Операция А/DJI_9.MP4", rid="a")
    add_report(conn, "Операция Б/DJI_9.MP4", rid="b")
    row, what = sar_common.match_existing_material(
        conn, "2026 08 11/DJI_9.MP4", watch)
    assert what == "new", "платформа угадала, хотя угадать нельзя"


def test_partial_name_does_not_match(env):
    """DJI_1.MP4 и DJI_11.MP4 -- разные файлы."""
    conn, watch = env
    add_report(conn, "Курумды август 2026/DJI_11.MP4")
    row, what = sar_common.match_existing_material(
        conn, "2026 08 11/DJI_1.MP4", watch)
    assert what == "new"


def test_file_at_the_root_is_matched_too(env):
    """Материал, положенный мимо папки операции, лежит в корне."""
    conn, watch = env
    add_report(conn, "DJI_7.MP4")
    open(os.path.join(watch, "DJI_7.MP4"), "wb").write(b"x")
    row, what = sar_common.match_existing_material(
        conn, "2026 08 11/DJI_7.MP4", watch)
    assert what == "skip" and row["report_id"] == "r1"


# --- привязка к операции --------------------------------------------------

def test_account_can_be_bound_to_an_operation(env):
    """По имени папки операцию не угадать: в облаке она называется иначе.
    Значит указывается явно."""
    conn, _ = env
    op = sar_common.create_operation(conn, "Курумды август")
    acc = sar_common.add_cloud_account(conn, provider="google", token="ya29.t")
    sar_common.update_cloud_account(conn, acc, operation_id=op)
    assert sar_common.cloud_accounts(conn)[0]["operation_id"] == op


def test_binding_is_visible_without_the_token(env):
    conn, _ = env
    op = sar_common.create_operation(conn, "Курумды август")
    acc = sar_common.add_cloud_account(conn, provider="google", token="ya29.t")
    sar_common.update_cloud_account(conn, acc, operation_id=op)
    pub = sar_common.cloud_accounts_public(conn)[0]
    assert pub["operation_id"] == op
    assert "token" not in pub


def test_recovery_message_is_printed_once(env):
    """Сообщение о возврате доступа печатается в момент события, а не на
    каждом проходе. Повтор раз в 15 секунд топит в себе весь журнал: его
    перестают читать, а вместе со спамом перестают замечать настоящее."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
           ).read_text(encoding="utf-8")
    i = src.index("вернулся доступ")
    chunk = src[max(0, i - 600):i]
    assert "cloud_file_id" in chunk, (
        "условие не смотрит, был ли доступ раньше -- значит печатает всегда")


# --- материал из облака виден в списке ------------------------------------
#
# Обход списка ходит по ЛОКАЛЬНОЙ папке и облачные записи не находит: их нет
# на диске. Без отдельного блока подключённый диск выглядит неработающим --
# файлы зарегистрированы, привязаны к операции, а в списке их нет, и понять
# почему невозможно.

@pytest.fixture
def server_env(tmp_path, monkeypatch):
    import sar_server
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    monkeypatch.setattr(sar_server, "_tree_cache", {}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", "/no/such/dir")
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "tester"
    return c, db


def add_cloud_report(db, rel, rid="c1", size=1000):
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, cloud_size, created_at, updated_at) VALUES "
        "(?,?,'','video','idle','f1',?,datetime('now'),datetime('now'))",
        (rid, rel, size))
    conn.commit()
    conn.close()


def test_cloud_material_appears_in_the_listing(server_env):
    client, db = server_env
    add_cloud_report(db, "2026 08 11/DJI_1.MP4")
    items = client.get("/api/tree").get_json()["items"]
    names = [i["name"] for i in items]
    assert "2026 08 11/DJI_1.MP4" in names, (
        "подключённый диск выглядит неработающим: файл есть в базе, "
        "но в списке его нет")


def test_cloud_material_is_marked_as_such(server_env):
    """Иначе отсутствие превью и задержка при открытии выглядят как
    неисправность."""
    client, db = server_env
    add_cloud_report(db, "2026 08 11/DJI_1.MP4")
    item = client.get("/api/tree").get_json()["items"][0]
    assert item["in_cloud"] is True
    assert item["size_bytes"] == 1000


def test_local_file_is_not_duplicated_by_its_cloud_record(server_env, tmp_path):
    """Если файл есть и там и там, в списке он должен быть ОДИН."""
    client, db = server_env
    watch = tmp_path / "watch"
    (watch / "DJI_1.MP4").write_bytes(b"x")
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('r1','DJI_1.MP4','','video','done','f1',datetime('now'),datetime('now'))")
    conn.commit()
    conn.close()
    names = [i["name"] for i in client.get("/api/tree").get_json()["items"]]
    assert names.count("DJI_1.MP4") == 1, names


def test_listing_survives_without_any_cloud(server_env):
    client, db = server_env
    assert client.get("/api/tree").status_code == 200
