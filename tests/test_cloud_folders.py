"""Структура папок облака сохраняется в дереве операции.

В облаке материал разложен по-своему: на боевом подключении это папки по
датам съёмки ("2026 08 11", "2026 08 14/Helicopter/Saykal"), а папка
операции на диске называется иначе. Путь такого файла не начинается с имени
папки операции -- и без отдельной обработки все 150 файлов сваливаются в
«вне папки операции» одной плоской кучей.

То есть раскладка, которую человек сделал в облаке, пропадала бы ровно
там, где он её ищет. Поэтому у каждого подключения свой корень в дереве, а
под ним -- его собственная структура, как есть.

Хранимый rel_path при этом НЕ меняется: по нему ищется совпадение
материала, и трогать его значит заводить дубли.
"""
import pytest

import sar_common


@pytest.fixture
def env(tmp_path):
    watch = tmp_path / "watch"
    (watch / "Курумды").mkdir(parents=True)
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    op = sar_common.create_operation(conn, "Курумды август", folder="Курумды")
    acc = sar_common.add_cloud_account(conn, provider="google", token="ya29.t",
                                        label="Диск операции")
    yield conn, str(watch), op, acc
    conn.close()


def add_local(conn, op, rel, rid):
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES (?,?,'','video','done', "
        "datetime('now'), datetime('now'))", (rid, rel))
    conn.commit()
    sar_common.attach_material(conn, op, rid)


def add_cloud(conn, op, acc, rel, rid):
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_account_id, cloud_file_id, cloud_size, created_at, updated_at) "
        "VALUES (?,?,'','video','idle',?,?,100,datetime('now'),datetime('now'))",
        (rid, rel, acc, "f" + rid))
    conn.commit()
    sar_common.attach_material(conn, op, rid)


# --- структура ------------------------------------------------------------

def test_cloud_gets_its_own_root_folder(env):
    """Иначе облачный материал не вписывается в дерево операции вообще:
    его путь начинается не с имени папки операции."""
    conn, watch, op, acc = env
    add_cloud(conn, op, acc, "2026 08 11/DJI_1.MP4", "c1")
    view = sar_common.browse_operation(conn, watch, op)
    names = [f["name"] for f in view["folders"]]
    assert "Диск операции" in names, names


def test_cloud_root_is_named_after_the_connection(env):
    """Подключений может быть несколько, и человек должен понимать, из
    какого хранилища что пришло."""
    conn, watch, op, acc = env
    sar_common.update_cloud_account(conn, acc, label="Съёмка 15 августа")
    add_cloud(conn, op, acc, "2026 08 15/DJI_9.MP4", "c9")
    names = [f["name"] for f in
             sar_common.browse_operation(conn, watch, op)["folders"]]
    assert "Съёмка 15 августа" in names


def test_provider_name_is_used_when_there_is_no_label(env):
    conn, watch, op, acc = env
    sar_common.update_cloud_account(conn, acc, label=None)
    add_cloud(conn, op, acc, "2026 08 11/DJI_1.MP4", "c1")
    names = [f["name"] for f in
             sar_common.browse_operation(conn, watch, op)["folders"]]
    assert "Google Диск" in names, names


def test_inner_structure_is_preserved(env):
    """Ради этого всё и делается: раскладка в облаке должна доехать до
    интерфейса как есть."""
    conn, watch, op, acc = env
    add_cloud(conn, op, acc, "2026 08 14/Helicopter/Saykal/C0004.MP4", "c1")
    inner = sar_common.browse_operation(conn, watch, op, "Диск операции")
    assert [f["name"] for f in inner["folders"]] == ["2026 08 14"]

    deeper = sar_common.browse_operation(
        conn, watch, op, "Диск операции/2026 08 14/Helicopter")
    assert [f["name"] for f in deeper["folders"]] == ["Saykal"]

    leaf = sar_common.browse_operation(
        conn, watch, op, "Диск операции/2026 08 14/Helicopter/Saykal")
    assert [f["rel_path"] for f in leaf["files"]] == [
        "2026 08 14/Helicopter/Saykal/C0004.MP4"]


def test_stored_path_is_not_rewritten(env):
    """Отображаемый путь и хранимый -- разные вещи. Переписать хранимый
    значит потерять совпадение материала и завести дубль."""
    conn, watch, op, acc = env
    add_cloud(conn, op, acc, "2026 08 11/DJI_1.MP4", "c1")
    sar_common.browse_operation(conn, watch, op)
    row = conn.execute("SELECT rel_path FROM reports WHERE report_id='c1'").fetchone()
    assert row["rel_path"] == "2026 08 11/DJI_1.MP4"


def test_local_and_cloud_live_side_by_side(env):
    conn, watch, op, acc = env
    add_local(conn, op, "Курумды/DJI_L.MP4", "l1")
    add_cloud(conn, op, acc, "2026 08 11/DJI_C.MP4", "c1")
    view = sar_common.browse_operation(conn, watch, op)
    assert [f["rel_path"] for f in view["files"]] == ["Курумды/DJI_L.MP4"]
    assert "Диск операции" in [f["name"] for f in view["folders"]]


def test_cloud_files_are_counted_in_the_folder(env):
    """Пустая на вид папка, в которой на самом деле 150 файлов, -- повод
    решить, что ничего не подключилось."""
    conn, watch, op, acc = env
    for i in range(3):
        add_cloud(conn, op, acc, "2026 08 11/DJI_%d.MP4" % i, "c%d" % i)
    folder = [f for f in sar_common.browse_operation(conn, watch, op)["folders"]
              if f["name"] == "Диск операции"][0]
    assert folder["files"] == 3


# --- отбор по источнику ---------------------------------------------------

def test_only_local_hides_the_cloud(env):
    conn, watch, op, acc = env
    add_local(conn, op, "Курумды/DJI_L.MP4", "l1")
    add_cloud(conn, op, acc, "2026 08 11/DJI_C.MP4", "c1")
    sar_common.set_setting(conn, "material_sources", "local")
    view = sar_common.browse_operation(conn, watch, op)
    assert "Диск операции" not in [f["name"] for f in view["folders"]]
    assert [f["rel_path"] for f in view["files"]] == ["Курумды/DJI_L.MP4"]


def test_only_cloud_hides_the_local(env):
    conn, watch, op, acc = env
    add_local(conn, op, "Курумды/DJI_L.MP4", "l1")
    add_cloud(conn, op, acc, "2026 08 11/DJI_C.MP4", "c1")
    sar_common.set_setting(conn, "material_sources", "cloud")
    view = sar_common.browse_operation(conn, watch, op)
    assert view["files"] == []
    assert "Диск операции" in [f["name"] for f in view["folders"]]


def test_default_shows_everything(env):
    conn, watch, op, acc = env
    add_local(conn, op, "Курумды/DJI_L.MP4", "l1")
    add_cloud(conn, op, acc, "2026 08 11/DJI_C.MP4", "c1")
    view = sar_common.browse_operation(conn, watch, op)
    assert view["files"] and view["folders"]


def test_hiding_does_not_detach_material(env):
    """Отбор -- это про показ. Материал остаётся в операции, и вернуть
    его на экран можно одним переключением."""
    conn, watch, op, acc = env
    add_cloud(conn, op, acc, "2026 08 11/DJI_C.MP4", "c1")
    sar_common.set_setting(conn, "material_sources", "local")
    sar_common.browse_operation(conn, watch, op)
    n = conn.execute("SELECT COUNT(*) c FROM operation_materials "
                     "WHERE operation_id=?", (op,)).fetchone()["c"]
    assert n == 1, "материал отвязался от операции при простом скрытии"


# --- плоский список для поиска --------------------------------------------

def test_flat_list_covers_both_sources(env):
    conn, watch, op, acc = env
    add_local(conn, op, "Курумды/DJI_L.MP4", "l1")
    add_cloud(conn, op, acc, "2026 08 14/Helicopter/Saykal/C0004.MP4", "c1")
    items = sar_common.operation_materials_flat(conn, op)
    assert {i["name"] for i in items} == {"DJI_L.MP4", "C0004.MP4"}


def test_flat_list_carries_the_display_folder(env):
    """Найти файл и не понять, в какой он папке, -- половина пользы.
    И путь обязан совпадать с тем, что показывает дерево: иначе поиск и
    навигация расходятся в том, где лежит файл."""
    conn, watch, op, acc = env
    add_cloud(conn, op, acc, "2026 08 14/Helicopter/Saykal/C0004.MP4", "c1")
    it = sar_common.operation_materials_flat(conn, op)[0]
    assert it["folder"] == "Курумды/Диск операции/2026 08 14/Helicopter/Saykal"
    # тот же путь, что и в дереве
    view = sar_common.browse_operation(
        conn, watch, op, "Диск операции/2026 08 14/Helicopter/Saykal")
    assert [f["report_id"] for f in view["files"]] == [it["report_id"]]


def test_flat_list_respects_the_source_filter(env):
    """Иначе поиск находит то, что человек намеренно скрыл."""
    conn, watch, op, acc = env
    add_local(conn, op, "Курумды/DJI_L.MP4", "l1")
    add_cloud(conn, op, acc, "2026 08 11/DJI_C.MP4", "c1")
    sar_common.set_setting(conn, "material_sources", "local")
    assert [i["name"] for i in sar_common.operation_materials_flat(conn, op)] \
        == ["DJI_L.MP4"]


def test_flat_list_marks_cloud_material(env):
    conn, watch, op, acc = env
    add_cloud(conn, op, acc, "2026 08 11/DJI_C.MP4", "c1")
    assert sar_common.operation_materials_flat(conn, op)[0]["in_cloud"] is True


def test_flat_list_is_sorted_predictably(env):
    """Порядок, меняющийся от запроса к запросу, читается как ошибка."""
    conn, watch, op, acc = env
    for i, n in enumerate(("Я.MP4", "А.MP4", "М.MP4")):
        add_cloud(conn, op, acc, "2026 08 11/" + n, "c%d" % i)
    names = [i["name"] for i in sar_common.operation_materials_flat(conn, op)]
    assert names == sorted(names, key=str.lower)


def test_missing_operation_returns_none(env):
    conn, _, _, _ = env
    assert sar_common.operation_materials_flat(conn, 999) is None
