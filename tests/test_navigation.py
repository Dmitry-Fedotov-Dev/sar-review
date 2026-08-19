"""Навигация: куда человек попадает и куда может вернуться.

Все три случая замечены на живой платформе:

  * ссылка из бота приводила в общий список ВСЕХ файлов, мимо операций --
    то есть мимо структуры, ради которой они и заводились;
  * плеер предлагал вернуться «к списку файлов», в ту же кучу, а не в
    операцию, из которой человек пришёл;
  * находка в списке была текстом: видно «верёвка, 66 с», а посмотреть,
    что там, нельзя.

Плюс окно создания операции не закрывалось кнопкой «Отмена», пока не
заполнено название: браузер требовал заполнить обязательное поле, потому
что кнопка внутри формы по умолчанию считается отправкой.
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
    return sar_server.app.test_client(), str(watch), db


# --- куда ведёт вход ------------------------------------------------------

def test_login_lands_on_operations(env):
    """Ссылка из бота должна приводить к операциям, а не в общую кучу."""
    client, _, _ = env
    r = client.post("/login", data={"name": "волонтёр", "password": "pw"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].rstrip("/").endswith("/operations")


def test_login_respects_explicit_next(env):
    """Переход по глубокой ссылке не должен уводить на список операций."""
    client, _, _ = env
    r = client.post("/login?next=/report/abc/player/",
                    data={"name": "в", "password": "pw"}, follow_redirects=False)
    assert "/report/abc/player/" in r.headers["Location"]


# --- крошки на страницах материала ---------------------------------------

def _material(db, watch, rel="Курумды/a.MP4", rid="r1", attach_to=None):
    path = os.path.join(watch, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 8)
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "duration_sec, out_dir, created_at, updated_at) "
              "VALUES (?,?,?,'video','done',100,?, 'x','x')",
              (rid, rel, path, os.path.dirname(path)))
    c.commit()
    if attach_to:
        sar_common.attach_material(c, attach_to, rid)
    c.close()
    return path


def test_crumbs_lead_back_to_the_operation(env):
    client, watch, db = env
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Курумды", folder="Курумды")
    c.close()
    _material(db, watch, attach_to=oid)

    c = sar_common.get_db_connection(db)
    rep = c.execute("SELECT * FROM reports WHERE report_id='r1'").fetchone()
    crumbs = sar_server.material_crumbs(c, rep)
    c.close()
    assert '/operations' in crumbs
    assert f'/operation/{oid}/' in crumbs
    assert "Курумды" in crumbs
    assert "a.MP4" in crumbs


def test_crumbs_for_material_without_operation(env):
    """Материал вне операций тоже должен куда-то возвращать."""
    client, watch, db = env
    _material(db, watch, rel="одинокий.MP4", rid="r2")
    c = sar_common.get_db_connection(db)
    rep = c.execute("SELECT * FROM reports WHERE report_id='r2'").fetchone()
    crumbs = sar_server.material_crumbs(c, rep)
    c.close()
    assert "Не разобрано" in crumbs
    assert "одинокий.MP4" in crumbs


def test_crumbs_show_filename_not_full_path(env):
    client, watch, db = env
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Курумды", folder="Курумды")
    c.close()
    _material(db, watch, rel="Курумды/11.08/борт 1/a.MP4", rid="r3", attach_to=oid)
    c = sar_common.get_db_connection(db)
    rep = c.execute("SELECT * FROM reports WHERE report_id='r3'").fetchone()
    crumbs = sar_server.material_crumbs(c, rep)
    c.close()
    assert "<span>a.MP4</span>" in crumbs


def test_player_shows_crumbs_not_flat_list_link(env):
    client, watch, db = env
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Курумды", folder="Курумды")
    c.close()
    _material(db, watch, attach_to=oid)
    with client.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "в"
    html = client.get("/report/r1/player/").get_data(as_text=True)
    assert 'class="crumbs"' in html
    assert "к списку файлов" not in html
    assert f'/operation/{oid}/' in html


# --- находки кликабельны --------------------------------------------------

def test_findings_link_to_the_player_at_the_timecode(env):
    """Список находок без переходов бесполезен: человек видит «верёвка,
    66 с» и не может посмотреть, что там на самом деле."""
    # шаблон хранится с удвоенными скобками для .format() -- проверяем
    # ОТРЕНДЕРЕННУЮ страницу, а не сырую строку
    card = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")
    assert "/player/`" in card
    assert "?t=${Math.max(0, Math.floor(f.seconds))}" in card
    assert '<a class="find"' in card


def test_finding_row_is_a_link_not_a_div():
    card = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")
    assert '<div class="find">' not in card


# --- кнопка «Отмена» ------------------------------------------------------

def test_cancel_button_is_not_a_submit():
    """Кнопка внутри формы по умолчанию считается отправкой, и браузер
    требовал заполнить название ПРЕЖДЕ чем закрыть окно."""
    html = sar_server.OPERATIONS_PAGE_HTML
    assert 'type="button" id="cancel"' in html
    assert 'class="btn ghost" value="cancel"' not in html


def test_cancel_clears_the_draft():
    """Брошенный черновик не должен всплыть при следующем открытии."""
    assert "document.getElementById('cancel').onclick" in sar_server.OPERATIONS_PAGE_HTML
    assert "['t','a','c'].forEach" in sar_server.OPERATIONS_PAGE_HTML
