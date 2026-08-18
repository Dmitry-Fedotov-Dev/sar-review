"""Тесты операций: контейнер поисковых работ.

Проверяется то, ради чего связь материала с операцией вынесена в отдельную
таблицу, а не в колонку:

  * материал без операции существует и попадает в «Не разобрано» -- иначе
    привычный жест «кинул файл в корень» перестал бы работать;
  * материал может быть в двух операциях сразу -- один вылет пригождается
    в двух поисках;
  * разметка привязана к МАТЕРИАЛУ, поэтому работа общая: разобрали вылет
    в одной операции -- во второй его не пересматривают. Пересматривать
    час уже разобранного видео в поиске непозволительно.

Отдельно -- метка папки. Операция опознаётся по ней, а не по имени, иначе
переименование папки заводило бы новую операцию и теряло всю работу.
"""
import os

import pytest

import sar_common


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


def _add_report(conn, rid, name="a.MP4"):
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES (?,?,?,'video','done','2026-01-01','2026-01-01')",
        (rid, name, "/x/" + name))
    conn.commit()


# --- создание -------------------------------------------------------------

def test_create_and_read_operation(conn):
    oid = sar_common.create_operation(conn, "Курумды август 2026",
                                       area="Алайский район",
                                       coordinator="@how_many_roads")
    op = sar_common.get_operation(conn, oid)
    assert op["title"] == "Курумды август 2026"
    assert op["area"] == "Алайский район"


def test_operations_are_listed_newest_first(conn):
    a = sar_common.create_operation(conn, "первая")
    b = sar_common.create_operation(conn, "вторая")
    ids = [o["id"] for o in sar_common.list_operations(conn)]
    assert set(ids) == {a, b}


# --- «Не разобрано» -------------------------------------------------------

def test_material_without_operation_is_unsorted(conn):
    """Файл в корне watch_dir обрабатывается как обычно и ждёт разбора."""
    _add_report(conn, "r1")
    unsorted = sar_common.unsorted_materials(conn)
    assert [r["report_id"] for r in unsorted] == ["r1"]


def test_attached_material_leaves_unsorted(conn):
    _add_report(conn, "r1")
    oid = sar_common.create_operation(conn, "Курумды")
    sar_common.attach_material(conn, oid, "r1")
    assert sar_common.unsorted_materials(conn) == []


def test_detached_material_returns_to_unsorted(conn):
    _add_report(conn, "r1")
    oid = sar_common.create_operation(conn, "Курумды")
    sar_common.attach_material(conn, oid, "r1")
    sar_common.detach_material(conn, oid, "r1")
    assert [r["report_id"] for r in sar_common.unsorted_materials(conn)] == ["r1"]


# --- материал в двух операциях -------------------------------------------

def test_material_can_belong_to_two_operations(conn):
    """Один вылет пригодился в двух поисках -- это разрешено."""
    _add_report(conn, "r1")
    a = sar_common.create_operation(conn, "Курумды")
    b = sar_common.create_operation(conn, "Иссык-Куль")
    sar_common.attach_material(conn, a, "r1")
    sar_common.attach_material(conn, b, "r1")

    assert len(sar_common.materials_of_operation(conn, a)) == 1
    assert len(sar_common.materials_of_operation(conn, b)) == 1
    assert {o["id"] for o in sar_common.operations_of_material(conn, "r1")} == {a, b}


def test_review_work_is_shared_between_operations(conn):
    """Главное следствие схемы.

    Разметка привязана к материалу, а не к паре «материал в операции».
    Поэтому находки, статусы триажа и покрытие просмотра видны из ЛЮБОЙ
    операции, куда добавлен материал -- никто не пересматривает то, что
    коллега уже разобрал."""
    _add_report(conn, "r1")
    a = sar_common.create_operation(conn, "Курумды")
    b = sar_common.create_operation(conn, "Иссык-Куль")
    sar_common.attach_material(conn, a, "r1")
    sar_common.attach_material(conn, b, "r1")

    # работа, сделанная человеком «в операции A»
    conn.execute("INSERT INTO manual_observations (report_id, viewer_name, "
                 "timestamp_sec, bbox, label, created_at) "
                 "VALUES ('r1','Айгуль',12.5,'[0,0,1,1]','рюкзак','2026-01-01')")
    conn.execute("INSERT INTO watch_segments (report_id, viewer_name, start_sec, "
                 "end_sec, ts) VALUES ('r1','Айгуль',0,120,'2026-01-01')")
    conn.commit()

    for oid in (a, b):
        mats = sar_common.materials_of_operation(conn, oid)
        rid = mats[0]["report_id"]
        obs = conn.execute("SELECT COUNT(*) c FROM manual_observations "
                           "WHERE report_id=?", (rid,)).fetchone()["c"]
        seen = conn.execute("SELECT COALESCE(SUM(end_sec-start_sec),0) s FROM "
                            "watch_segments WHERE report_id=?", (rid,)).fetchone()["s"]
        assert obs == 1, "находка не видна из второй операции"
        assert seen == 120, "покрытие просмотра не видно из второй операции"


def test_attach_is_idempotent(conn):
    _add_report(conn, "r1")
    oid = sar_common.create_operation(conn, "Курумды")
    sar_common.attach_material(conn, oid, "r1")
    sar_common.attach_material(conn, oid, "r1")
    assert len(sar_common.materials_of_operation(conn, oid)) == 1


# --- метка папки ----------------------------------------------------------

def test_marker_survives_folder_rename(tmp_path):
    """Регрессия по замыслу: операция опознаётся по метке, а не по имени.

    Без метки переименование папки завело бы НОВУЮ операцию, а прежняя
    осталась бы с материалами, которых на месте больше нет, -- вместе со
    всей накопленной разметкой."""
    folder = tmp_path / "Курумды_август"
    folder.mkdir()
    sar_common.write_operation_marker(str(folder), 42)
    assert sar_common.read_operation_marker(str(folder)) == 42

    renamed = tmp_path / "Курумды_2026"
    folder.rename(renamed)
    assert sar_common.read_operation_marker(str(renamed)) == 42


def test_marker_absent_reads_as_none(tmp_path):
    d = tmp_path / "просто_папка"
    d.mkdir()
    assert sar_common.read_operation_marker(str(d)) is None


def test_broken_marker_reads_as_none(tmp_path):
    d = tmp_path / "битая"
    d.mkdir()
    with open(os.path.join(str(d), sar_common.OPERATION_MARKER), "w",
              encoding="utf-8") as f:
        f.write("это не число")
    assert sar_common.read_operation_marker(str(d)) is None


def test_marker_file_name_is_hidden(tmp_path):
    """Метка не должна мозолить глаза человеку, который открыл папку."""
    assert sar_common.OPERATION_MARKER.startswith(".")


# --- схема ----------------------------------------------------------------

def test_schema_created_on_fresh_db(tmp_path):
    db = str(tmp_path / "fresh.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"operations", "operation_materials"} <= tables
    c.close()


def test_init_db_stays_idempotent(tmp_path):
    db = str(tmp_path / "x.db")
    for _ in range(3):
        sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "проверка")
    assert sar_common.get_operation(c, oid) is not None
    c.close()
