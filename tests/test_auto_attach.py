"""Файл в папке операции попадает в неё сам.

Обнаружено на живой платформе: распакованный в папку операции архив дал 17
записей о материалах, и НИ ОДНА не была привязана к операции -- все висели
в «Не разобрано». То есть папка операции не работала как папка операции:
сканирование её узнавало, а принадлежность не проставлялась.

Привязка делается по метке .sar_operation в папке верхнего уровня и
вызывается на каждом проходе наблюдателя, а не только при создании записи:
операцию могли завести уже ПОСЛЕ того, как файлы появились.
"""
import os

import pytest

import sar_common


@pytest.fixture
def env(tmp_path):
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Курумды", folder="Курумды")
    c.close()
    op_dir = watch / "Курумды"
    op_dir.mkdir()
    sar_common.write_operation_marker(str(op_dir), oid)
    return str(watch), db, oid


def _report(db, rid, rel):
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "created_at, updated_at) VALUES (?,?,?,'video','idle','x','x')",
              (rid, rel, "/x/" + rel))
    c.commit()
    c.close()


# --- определение операции по пути -----------------------------------------

def test_file_in_operation_folder_belongs_to_it(env):
    watch, _, oid = env
    assert sar_common.operation_for_path(watch, "Курумды/a.MP4") == oid


def test_file_in_nested_folder_belongs_too(env):
    """Распакованная папка из облака кладёт файлы вглубь -- принадлежность
    определяет папка ВЕРХНЕГО уровня."""
    watch, _, oid = env
    assert sar_common.operation_for_path(
        watch, "Курумды/Drone part3/11.08/a.MP4") == oid


def test_file_in_root_belongs_to_nothing(env):
    watch, _, _ = env
    assert sar_common.operation_for_path(watch, "a.MP4") is None


def test_file_in_plain_folder_belongs_to_nothing(env):
    """Папка без метки -- просто папка, а не операция."""
    watch, _, _ = env
    os.makedirs(os.path.join(watch, "просто папка"), exist_ok=True)
    assert sar_common.operation_for_path(watch, "просто папка/a.MP4") is None


def test_service_folder_is_never_an_operation(env):
    watch, _, _ = env
    assert sar_common.operation_for_path(watch, "sar_data/reports/x.MP4") is None


# --- собственно привязка ---------------------------------------------------

def test_material_is_attached_automatically(env):
    watch, db, oid = env
    _report(db, "r1", "Курумды/Drone part3/a.MP4")
    c = sar_common.get_db_connection(db)
    assert sar_common.attach_by_folder(c, watch, "r1", "Курумды/Drone part3/a.MP4") == oid
    assert [r["report_id"] for r in sar_common.materials_of_operation(c, oid)] == ["r1"]
    assert sar_common.unsorted_materials(c) == []
    c.close()


def test_attach_is_idempotent(env):
    """Вызывается на каждом проходе наблюдателя -- дублей быть не должно."""
    watch, db, oid = env
    _report(db, "r1", "Курумды/a.MP4")
    c = sar_common.get_db_connection(db)
    for _ in range(3):
        sar_common.attach_by_folder(c, watch, "r1", "Курумды/a.MP4")
    n = c.execute("SELECT COUNT(*) c FROM operation_materials").fetchone()["c"]
    c.close()
    assert n == 1


def test_attach_does_not_replace_manual_link(env):
    """Связи складываются: материал, добавленный руками в другую операцию,
    не должен от неё отвязаться."""
    watch, db, oid = env
    _report(db, "r1", "Курумды/a.MP4")
    c = sar_common.get_db_connection(db)
    other = sar_common.create_operation(c, "Другая")
    sar_common.attach_material(c, other, "r1")
    sar_common.attach_by_folder(c, watch, "r1", "Курумды/a.MP4")
    ops = {o["id"] for o in sar_common.operations_of_material(c, "r1")}
    c.close()
    assert ops == {oid, other}


def test_file_outside_any_operation_stays_unsorted(env):
    watch, db, _ = env
    _report(db, "r2", "одинокий.MP4")
    c = sar_common.get_db_connection(db)
    assert sar_common.attach_by_folder(c, watch, "r2", "одинокий.MP4") is None
    assert [r["report_id"] for r in sar_common.unsorted_materials(c)] == ["r2"]
    c.close()


def test_stale_marker_does_not_attach(env):
    """Метка осталась от удалённой операции -- привязывать некуда."""
    watch, db, _ = env
    ghost = os.path.join(watch, "призрак")
    os.makedirs(ghost, exist_ok=True)
    sar_common.write_operation_marker(ghost, 9999)
    _report(db, "r3", "призрак/a.MP4")
    c = sar_common.get_db_connection(db)
    assert sar_common.attach_by_folder(c, watch, "r3", "призрак/a.MP4") is None
    c.close()


def test_operation_created_after_the_files(env):
    """Частый порядок: сначала распаковали, потом завели операцию.
    Привязка на каждом проходе это чинит без ручного вмешательства."""
    watch, db, _ = env
    late = os.path.join(watch, "Иссык-Куль")
    os.makedirs(late, exist_ok=True)
    _report(db, "r4", "Иссык-Куль/a.MP4")

    c = sar_common.get_db_connection(db)
    assert sar_common.attach_by_folder(c, watch, "r4", "Иссык-Куль/a.MP4") is None
    new_id = sar_common.create_operation(c, "Иссык-Куль", folder="Иссык-Куль")
    c.close()
    sar_common.write_operation_marker(late, new_id)

    c = sar_common.get_db_connection(db)
    assert sar_common.attach_by_folder(c, watch, "r4", "Иссык-Куль/a.MP4") == new_id
    c.close()
