"""Операции: создание без дублей, правка, удаление, загрузка материалов, права.

Повод (14.11.2026): двойное нажатие «Создать» завело две одинаковые
операции. Кнопка не блокировалась, сервер не проверял, а две операции с
одним названием -- это ещё и одна папка на диске, где метка второй
затирает первую. Плюс не было ни удаления (дубль не убрать), ни загрузки
материалов на странице операции (только в старом интерфейсе).

Сторожим:
- дубль не создаётся ни последовательно, ни ОДНОВРЕМЕННО (два потока);
- регистр и пробелы не делают название «другим», кириллица тоже;
- удаление не теряет работу людей: материалы и их пометки остаются,
  отметки на карте -- отказ; метка папки убирается;
- права: создать/изменить -- модератор, удалить и загрузить -- админ;
  аноним по общему паролю -- ничего;
- загрузка кладёт файл в папку операции так, что воркер его к ней отнесёт.
"""
import io
import os
import threading

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
                        {"watch_dir": str(watch), "shared_password": "pw"}, raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", str(tmp_path / "noconf"))
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    return {"client": sar_server.app.test_client(), "db": db, "watch": str(watch)}


def _as(client, role, verified=True):
    with client.session_transaction() as s:
        s.update(authed=True, verified=verified, viewer_name="x", role=role)


def _conn(env):
    return sar_common.get_db_connection(env["db"])


# --- общий слой --------------------------------------------------------------

def test_duplicate_title_is_refused_with_existing_id(env):
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Курумды, август")
    for same in ("Курумды, август", "  курумды,   АВГУСТ "):
        with pytest.raises(sar_common.OperationError) as e:
            sar_common.create_operation_with_folder(c, env["watch"], same)
        assert e.value.existing_id == op
    assert c.execute("SELECT COUNT(*) n FROM operations").fetchone()["n"] == 1


def test_simultaneous_creation_gives_one_operation(env):
    """Двойной клик на сервере: два запроса почти одновременно, у каждого
    своё соединение. Без блокировки записи оба видели бы пустую таблицу."""
    results, barrier = [], threading.Barrier(8)

    def worker():
        c = sar_common.get_db_connection(env["db"])
        barrier.wait()
        try:
            results.append(sar_common.create_operation_with_folder(c, env["watch"], "Дубль")[0])
        except sar_common.OperationError as e:
            results.append(("dup", e.existing_id))
        finally:
            c.close()

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = _conn(env)
    assert c.execute("SELECT COUNT(*) n FROM operations").fetchone()["n"] == 1
    created = [r for r in results if not isinstance(r, tuple)]
    assert len(created) == 1
    assert all(r[1] == created[0] for r in results if isinstance(r, tuple))


def test_different_titles_with_same_folder_name_get_separate_folders(env):
    c = _conn(env)
    a, fa = sar_common.create_operation_with_folder(c, env["watch"], "Поиск 12/08")
    b, fb = sar_common.create_operation_with_folder(c, env["watch"], "Поиск 12_08")
    assert fa != fb
    assert sar_common.read_operation_marker(os.path.join(env["watch"], fa)) == a
    assert sar_common.read_operation_marker(os.path.join(env["watch"], fb)) == b


def test_update_checks_uniqueness_but_allows_own_title(env):
    c = _conn(env)
    a, folder = sar_common.create_operation_with_folder(c, env["watch"], "Первая")
    sar_common.create_operation_with_folder(c, env["watch"], "Вторая")
    with pytest.raises(sar_common.OperationError):
        sar_common.update_operation(c, a, title="вторая")
    op = sar_common.update_operation(c, a, title="ПЕРВАЯ", area="Алай")
    assert op["title"] == "ПЕРВАЯ" and op["area"] == "Алай"
    assert op["folder"] == folder, "папка при переименовании не трогается"


def test_delete_keeps_materials_and_human_work(env):
    c = _conn(env)
    op, folder = sar_common.create_operation_with_folder(c, env["watch"], "Удаляемая")
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, created_at, "
              "updated_at) VALUES ('r1', ?, '', 'video', 'done', 'x', 'x')", (folder + "/a.mp4",))
    c.execute("INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, "
              "label, created_at) VALUES ('r1', 'Аня', 5, '[0,0,1,1]', 'рюкзак', 'x')")
    sar_common.attach_material(c, op, "r1")
    res = sar_common.delete_operation(c, env["watch"], op)
    assert res["detached"] == 1
    assert sar_common.get_operation(c, op) is None
    assert c.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"] == 1
    assert c.execute("SELECT COUNT(*) n FROM manual_observations").fetchone()["n"] == 1
    assert os.path.isdir(os.path.join(env["watch"], folder)), "папка с файлами остаётся"
    assert sar_common.read_operation_marker(os.path.join(env["watch"], folder)) is None
    # воркер больше не привяжет туда материал
    assert sar_common.attach_by_folder(c, env["watch"], "r1", folder + "/a.mp4") is None


def test_delete_refused_when_map_has_coordinator_marks(env):
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "С отметками")
    c.execute("INSERT INTO map_marks (operation_id, lat, lon, viewer_name, created_at) "
              "VALUES (?, 1, 2, 'коорд', 'x')", (op,))
    c.commit()
    with pytest.raises(sar_common.OperationError, match="отмет"):
        sar_common.delete_operation(c, env["watch"], op)
    assert sar_common.get_operation(c, op) is not None


def test_title_can_be_reused_after_delete(env):
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Повтор")
    sar_common.delete_operation(c, env["watch"], op)
    new, folder = sar_common.create_operation_with_folder(c, env["watch"], "Повтор")
    assert new != op
    assert sar_common.read_operation_marker(os.path.join(env["watch"], folder)) == new


# --- API и права -------------------------------------------------------------

def test_api_duplicate_is_409_with_link(env):
    cl = env["client"]
    _as(cl, sar_common.ROLE_MODERATOR)
    first = cl.post("/api/operations", json={"title": "Алай"}).get_json()
    r = cl.post("/api/operations", json={"title": "алай"})
    assert r.status_code == 409 and r.get_json()["existing_id"] == first["id"]


@pytest.mark.parametrize("role,verified,manage,delete,upload", [
    (sar_common.ROLE_VIEWER, False, False, False, False),     # аноним по паролю
    (sar_common.ROLE_VIEWER, True, False, False, False),
    (sar_common.ROLE_MODERATOR, True, True, False, False),
    (sar_common.ROLE_ADMIN, True, True, True, True),
])
def test_rights_match_server_checks(env, role, verified, manage, delete, upload):
    cl = env["client"]
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Права")
    _as(cl, role, verified)
    rights = cl.get(f"/api/operations/{op}/browse").get_json()["rights"]
    assert (rights["manage"], rights["delete"], rights["upload"]) == (manage, delete, upload)
    ok = lambda r: r.status_code not in (401, 403)
    assert ok(cl.patch(f"/api/operations/{op}", json={"area": "x"})) == manage
    assert ok(cl.post(f"/api/operations/{op}/upload", data={})) == upload
    assert ok(cl.post("/api/operations", json={"title": "Новая " + role + str(verified)})) == manage
    assert ok(cl.delete(f"/api/operations/{op}")) == delete


def test_upload_lands_in_operation_folder_and_is_attached_by_marker(env):
    cl = env["client"]
    c = _conn(env)
    op, folder = sar_common.create_operation_with_folder(c, env["watch"], "Загрузка")
    _as(cl, sar_common.ROLE_ADMIN)
    r = cl.post(f"/api/operations/{op}/upload", content_type="multipart/form-data", data={
        "files": [(io.BytesIO(b"v"), "DJI 0001.MP4"), (io.BytesIO(b"p"), "shot.jpg"),
                  (io.BytesIO(b"1\n"), "DJI 0001.SRT"), (io.BytesIO(b"x"), "evil.exe")]}).get_json()
    assert len(r["saved"]) == 3 and len(r["errors"]) == 1 and "evil.exe" in r["errors"][0]
    up = os.path.join(env["watch"], folder, sar_server.UPLOAD_SUBDIR)
    names = sorted(os.listdir(up))
    assert any(n.lower().endswith(".mp4") for n in names) and any(n.endswith(".jpg") for n in names)
    assert not any(n.endswith(".uploading") for n in names)
    rel = folder + "/" + sar_server.UPLOAD_SUBDIR + "/" + names[0]
    assert sar_common.operation_for_path(env["watch"], rel) == op


def test_upload_recreates_missing_folder(env):
    cl = env["client"]
    c = _conn(env)
    op = sar_common.create_operation(c, "Без папки")
    _as(cl, sar_common.ROLE_ADMIN)
    r = cl.post(f"/api/operations/{op}/upload", content_type="multipart/form-data",
                data={"files": [(io.BytesIO(b"v"), "a.mp4")]}).get_json()
    assert r["ok"] and r["folder"]
    assert sar_common.read_operation_marker(os.path.join(env["watch"], r["folder"])) == op


def test_upload_rejects_path_tricks(env):
    cl = env["client"]
    c = _conn(env)
    op, folder = sar_common.create_operation_with_folder(c, env["watch"], "Пути")
    _as(cl, sar_common.ROLE_ADMIN)
    cl.post(f"/api/operations/{op}/upload", content_type="multipart/form-data",
            data={"files": [(io.BytesIO(b"v"), "../../outside.mp4")]})
    assert not os.path.exists(os.path.join(env["watch"], "..", "outside.mp4"))
    assert not os.path.exists(os.path.join(env["watch"], "outside.mp4"))


def test_page_create_button_is_guarded_against_double_click(env):
    cl = env["client"]
    _as(cl, sar_common.ROLE_MODERATOR)
    page = cl.get("/operations").get_data(as_text=True)
    assert "if (creating) return;" in page and "btn.disabled = true" in page


def test_operation_page_has_tools(env):
    cl = env["client"]
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Кнопки")
    _as(cl, sar_common.ROLE_ADMIN)
    page = cl.get(f"/operation/{op}/").get_data(as_text=True)
    for s in ("Добавить материалы", "Добавить с Google Диска", "renderTools", "doUpload"):
        assert s in page


# --- архив (скрыть) -----------------------------------------------------------

def test_archive_hides_from_list_and_comes_back(env):
    cl = env["client"]
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Закрытый поиск")
    sar_common.create_operation_with_folder(c, env["watch"], "Идущий поиск")
    _as(cl, sar_common.ROLE_MODERATOR)
    assert cl.patch(f"/api/operations/{op}", json={"archived": True}).get_json()["operation"]["archived"]
    d = cl.get("/api/operations").get_json()
    assert [o["title"] for o in d["operations"]] == ["Идущий поиск"] and d["archived_hidden"] == 1
    d = cl.get("/api/operations?archived=1").get_json()
    assert len(d["operations"]) == 2
    # страница скрытой операции работает
    assert cl.get(f"/api/operations/{op}/browse").get_json()["operation"]["archived"] is True
    cl.patch(f"/api/operations/{op}", json={"archived": False})
    assert len(cl.get("/api/operations").get_json()["operations"]) == 2


def test_archive_needs_moderator(env):
    cl = env["client"]
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Чужая")
    _as(cl, sar_common.ROLE_VIEWER)
    assert cl.patch(f"/api/operations/{op}", json={"archived": True}).status_code == 403
    assert sar_common.get_operation(c, op)["archived_at"] is None


def test_edit_description_fields(env):
    cl = env["client"]
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Описание")
    _as(cl, sar_common.ROLE_MODERATOR)
    r = cl.patch(f"/api/operations/{op}", json={"area": "Алай", "client": "МЧС"}).get_json()
    assert r["operation"]["area"] == "Алай" and r["operation"]["client"] == "МЧС"
    r = cl.patch(f"/api/operations/{op}", json={"client": ""}).get_json()
    assert r["operation"]["client"] is None
    assert cl.patch(f"/api/operations/{op}", json={"title": "  "}).status_code == 400


# --- файлы и пометки по ролям -------------------------------------------------

def _report(c, rid="r1"):
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, created_at, "
              "updated_at) VALUES (?, 'a.mp4', '', 'video', 'idle', 'x', 'x')", (rid,))
    c.commit()


def _obs(c, who):
    cur = c.execute("INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, "
                    "created_at) VALUES ('r1', ?, 1, '[0,0,1,1]', 'x')", (who,))
    c.commit()
    return cur.lastrowid


def test_anyone_deletes_own_mark_only_moderator_deletes_others(env):
    cl = env["client"]
    c = _conn(env)
    _report(c)
    mine, theirs = _obs(c, "x"), _obs(c, "Аня")
    _as(cl, sar_common.ROLE_VIEWER, verified=False)          # аноним «x»
    assert cl.delete(f"/api/report/r1/observations/{theirs}").status_code == 403
    assert cl.delete(f"/api/report/r1/observations/{mine}").status_code == 200
    assert c.execute("SELECT COUNT(*) n FROM manual_observations").fetchone()["n"] == 1
    _as(cl, sar_common.ROLE_MODERATOR)
    assert cl.delete(f"/api/report/r1/observations/{theirs}").status_code == 200
    assert cl.delete(f"/api/report/r1/observations/{theirs}").status_code == 404


def test_only_admin_starts_processing(env):
    cl = env["client"]
    c = _conn(env)
    _report(c)
    for role in (sar_common.ROLE_VIEWER, sar_common.ROLE_MODERATOR):
        _as(cl, role)
        assert cl.post("/api/report/r1/enqueue").status_code == 403
    assert c.execute("SELECT status FROM reports").fetchone()["status"] == "idle"
    _as(cl, sar_common.ROLE_ADMIN)
    assert cl.post("/api/report/r1/enqueue").get_json()["status"] == "queued"


def test_run_button_hidden_for_non_admin(env):
    cl = env["client"]
    _as(cl, sar_common.ROLE_VIEWER)
    assert "const CAN_RUN = false;" in cl.get("/").get_data(as_text=True)
    _as(cl, sar_common.ROLE_ADMIN)
    assert "const CAN_RUN = true;" in cl.get("/").get_data(as_text=True)


# --- повторная загрузка того же файла -----------------------------------------

def _upload(cl, op, files):
    return cl.post(f"/api/operations/{op}/upload", content_type="multipart/form-data",
                   data={"files": [(io.BytesIO(b), n) for n, b in files]}).get_json()


def test_same_file_twice_is_not_saved_again(env):
    """Форма не очищалась после загрузки, и повторное «Загрузить» клало тот
    же файл рядом как «имя_1» -- воркер завёл бы второй материал, и работа
    людей разошлась бы по двум записям."""
    cl = env["client"]
    c = _conn(env)
    op, folder = sar_common.create_operation_with_folder(c, env["watch"], "Повтор загрузки")
    _as(cl, sar_common.ROLE_ADMIN)
    first = _upload(cl, op, [("DJI_0001.MP4", b"video-bytes"), ("DJI_0001.SRT", b"1\n")])
    assert len(first["saved"]) == 2 and first["skipped"] == []
    again = _upload(cl, op, [("DJI_0001.MP4", b"video-bytes"), ("DJI_0001.SRT", b"1\n")])
    assert again["ok"] and again["saved"] == [] and len(again["skipped"]) == 2
    up = os.path.join(env["watch"], folder, sar_server.UPLOAD_SUBDIR)
    assert os.listdir(up) == ["DJI_0001.mp4"]
    assert not any(n.endswith(".uploading") for n in os.listdir(up))


def test_same_name_different_content_is_kept(env):
    cl = env["client"]
    c = _conn(env)
    op, folder = sar_common.create_operation_with_folder(c, env["watch"], "Тёзки")
    _as(cl, sar_common.ROLE_ADMIN)
    _upload(cl, op, [("a.mp4", b"first")])
    r = _upload(cl, op, [("a.mp4", b"second, longer")])
    assert r["saved"] == ["a_1.mp4"] and r["skipped"] == []
    # и повтор второго тоже распознаётся
    assert _upload(cl, op, [("a.mp4", b"second, longer")])["skipped"]


def test_upload_dialog_clears_and_blocks_repeat(env):
    cl = env["client"]
    c = _conn(env)
    op, _ = sar_common.create_operation_with_folder(c, env["watch"], "Окно")
    _as(cl, sar_common.ROLE_ADMIN)
    page = cl.get(f"/operation/{op}/").get_data(as_text=True)
    assert "document.getElementById('upFiles').value = '';" in page
    assert "upGo').disabled = !e.target.files.length" in page


# --- загруженное видно в списке сразу ------------------------------------------

def test_uploaded_file_is_listed_immediately(env):
    """Раньше файл появлялся только после прохода воркера (до минуты с
    облачным обходом), и человек перезагружал страницу. Теперь сервер
    регистрирует его сам -- той же функцией, что и воркер."""
    cl = env["client"]
    c = _conn(env)
    op, folder = sar_common.create_operation_with_folder(c, env["watch"], "Сразу")
    _as(cl, sar_common.ROLE_ADMIN)
    _upload(cl, op, [("DJI_0003.MP4", b"v"), ("DJI_0003.SRT", b"1\n"), ("p.jpg", b"x")])
    d = cl.get(f"/api/operations/{op}/browse?path=" + sar_server.UPLOAD_SUBDIR).get_json()
    names = sorted(f["rel_path"].rsplit("/", 1)[-1] for f in d["files"])
    assert names == ["DJI_0003.mp4", "p.jpg"], "SRT -- телеметрия, а не материал"
    kinds = {r["kind"] for r in c.execute("SELECT kind FROM reports")}
    assert kinds == {"video", "photo"}


def test_worker_does_not_duplicate_uploaded_file(env):
    """Воркер потом проходит по той же папке -- второй записи быть не должно:
    ищет по тому же rel_path, тем же register_material."""
    cl = env["client"]
    c = _conn(env)
    op, folder = sar_common.create_operation_with_folder(c, env["watch"], "Без дублей")
    _as(cl, sar_common.ROLE_ADMIN)
    _upload(cl, op, [("DJI_0004.MP4", b"v")])
    for rel, abs_path, kind in sar_common.scan_all_materials(env["watch"]):
        rid, created = sar_common.register_material(c, os.path.join(env["watch"], "r"),
                                                    rel, abs_path, kind, False)
        assert not created, rel
    assert c.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"] == 1


def test_register_material_respects_auto_process(env):
    c = _conn(env)
    p = os.path.join(env["watch"], "x.mp4")
    open(p, "wb").write(b"v")
    rid, new = sar_common.register_material(c, env["watch"], "x.mp4", p, "video", True)
    assert new and c.execute("SELECT status FROM reports").fetchone()["status"] == "queued"
    assert sar_common.register_material(c, env["watch"], "x.mp4", p, "video", True) == (rid, False)
