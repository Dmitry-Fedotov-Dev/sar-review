"""Карточка операции: навигация по папкам как в проводнике.

Выбрана компоновка «проводник с крошками»: заходишь в папку -- видишь её
содержимое, сверху путь с возвратом на любой уровень. Так одинаково
работает на телефоне и на компьютере, и не разваливается при сотнях файлов
из пяти вылетов.

Ключевые свойства, которые здесь проверяются:

  * показывается РОВНО ОДИН уровень, а не всё дерево -- иначе список
    превращается в кашу;
  * пустые папки видны: человек ожидает свою структуру целиком, а не
    только ветки со съёмкой;
  * материал, привязанный к операции, но лежащий вне её папки, не
    теряется -- иначе он пропал бы бесследно;
  * во вкладке «Находки» только размеченное человеком: сцен модели тысячи,
    и настоящие находки в них утонули бы.
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
                        lambda w, d=None: (str(watch), str(watch), db, str(watch)))
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["verified"] = True
        s["viewer_name"] = "координатор"
        s["role"] = sar_common.ROLE_MODERATOR
    return c, str(watch), db


def _mk(db, watch, oid, rel, rid, attach=True):
    path = os.path.join(watch, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 8)
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
              "duration_sec, created_at, updated_at) "
              "VALUES (?,?,?,'video','done',100,'x','x')", (rid, rel, path))
    c.commit()
    if attach:
        sar_common.attach_material(c, oid, rid)
    c.close()


@pytest.fixture
def op(env):
    client, watch, db = env
    c = sar_common.get_db_connection(db)
    oid = sar_common.create_operation(c, "Курумды", folder="Курумды")
    c.close()
    os.makedirs(os.path.join(watch, "Курумды"), exist_ok=True)
    sar_common.write_operation_marker(os.path.join(watch, "Курумды"), oid)
    _mk(db, watch, oid, "Курумды/11.08/борт 1/a.MP4", "r1")
    _mk(db, watch, oid, "Курумды/11.08/борт 2/b.MP4", "r2")
    _mk(db, watch, oid, "Курумды/вертолёт/c.MP4", "r3")
    _mk(db, watch, oid, "Курумды/корневое.MP4", "r4")
    os.makedirs(os.path.join(watch, "Курумды", "пустая"), exist_ok=True)
    return client, watch, db, oid


# --- один уровень за раз --------------------------------------------------

def test_root_shows_only_immediate_children(op):
    client, _, _, oid = op
    d = client.get(f"/api/operations/{oid}/browse").get_json()
    assert sorted(f["name"] for f in d["folders"]) == ["11.08", "вертолёт", "пустая"]
    assert [f["name"] for f in d["files"]] == ["корневое.MP4"]


def test_nested_files_are_not_shown_at_root(op):
    """Показываем папку, а не всё дерево -- иначе список превращается в кашу."""
    client, _, _, oid = op
    d = client.get(f"/api/operations/{oid}/browse").get_json()
    names = [f["name"] for f in d["files"]]
    assert "a.MP4" not in names and "b.MP4" not in names


def test_folder_shows_its_own_children(op):
    client, _, _, oid = op
    d = client.get(f"/api/operations/{oid}/browse?path=11.08").get_json()
    assert sorted(f["name"] for f in d["folders"]) == ["борт 1", "борт 2"]
    assert d["files"] == []


def test_deep_folder_shows_files(op):
    client, _, _, oid = op
    d = client.get(f"/api/operations/{oid}/browse?path=11.08/борт 1").get_json()
    assert [f["name"] for f in d["files"]] == ["a.MP4"]
    assert d["path"] == "11.08/борт 1"


def test_folder_counts_files_inside(op):
    client, _, _, oid = op
    d = client.get(f"/api/operations/{oid}/browse").get_json()
    by = {f["name"]: f["files"] for f in d["folders"]}
    assert by["11.08"] == 2
    assert by["вертолёт"] == 1


def test_empty_folder_is_visible(op):
    """Человек ожидает увидеть свою структуру целиком."""
    client, _, _, oid = op
    d = client.get(f"/api/operations/{oid}/browse").get_json()
    empty = [f for f in d["folders"] if f["name"] == "пустая"]
    assert empty and empty[0]["files"] == 0


# --- материал вне папки операции -----------------------------------------

def test_material_outside_the_folder_is_not_lost(op):
    """Материал привязали вручную, а лежит он в другом месте -- он обязан
    быть виден, иначе пропадёт бесследно."""
    client, watch, db, oid = op
    _mk(db, watch, oid, "чужая папка/x.MP4", "r9")
    d = client.get(f"/api/operations/{oid}/browse").get_json()
    assert [f["name"] for f in d["outside"]] == ["x.MP4"]


def test_outside_shown_only_at_root(op):
    client, watch, db, oid = op
    _mk(db, watch, oid, "чужая папка/x.MP4", "r9")
    d = client.get(f"/api/operations/{oid}/browse?path=11.08").get_json()
    assert d["outside"] == []


def test_unlinked_material_is_not_shown(op):
    """Файл лежит в папке операции, но к ней не привязан -- принадлежность
    определяют связи, а не путь."""
    client, watch, db, oid = op
    _mk(db, watch, oid, "Курумды/чужой.MP4", "r8", attach=False)
    d = client.get(f"/api/operations/{oid}/browse").get_json()
    assert "чужой.MP4" not in [f["name"] for f in d["files"]]


# --- безопасность пути ----------------------------------------------------

def test_path_traversal_is_rejected(op):
    """Путь приходит из адресной строки -- выше папки операции не пускаем."""
    client, _, _, oid = op
    assert client.get(f"/api/operations/{oid}/browse?path=../..").status_code == 400


def test_unknown_operation_returns_404(op):
    client, _, _, _ = op
    assert client.get("/api/operations/999/browse").status_code == 404


# --- находки --------------------------------------------------------------

def test_findings_include_human_marks(op):
    client, _, db, oid = op
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO manual_observations (report_id, viewer_name, "
              "timestamp_sec, bbox, label, created_at) "
              "VALUES ('r1','Айгуль',12.5,'[0,0,1,1]','рюкзак','2026-01-01')")
    c.commit()
    c.close()
    f = client.get(f"/api/operations/{oid}/findings").get_json()["findings"]
    assert len(f) == 1
    assert f[0]["label"] == "рюкзак" and f[0]["file"] == "a.MP4"
    assert f[0]["kind"] == "manual"


def test_findings_span_all_materials_of_the_operation(op):
    client, _, db, oid = op
    c = sar_common.get_db_connection(db)
    for rid, lbl in (("r1", "рюкзак"), ("r3", "верёвка")):
        c.execute("INSERT INTO manual_observations (report_id, viewer_name, "
                  "timestamp_sec, bbox, label, created_at) "
                  "VALUES (?,'Айгуль',1,'[0,0,1,1]',?,'x')", (rid, lbl))
    c.commit()
    c.close()
    f = client.get(f"/api/operations/{oid}/findings").get_json()["findings"]
    assert sorted(x["label"] for x in f) == ["верёвка", "рюкзак"]


def test_findings_exclude_other_operations(op):
    client, watch, db, oid = op
    c = sar_common.get_db_connection(db)
    other = sar_common.create_operation(c, "Другая")
    c.close()
    _mk(db, watch, other, "Другая/z.MP4", "r7")
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO manual_observations (report_id, viewer_name, "
              "timestamp_sec, bbox, label, created_at) "
              "VALUES ('r7','Кто-то',1,'[0,0,1,1]','чужая','x')")
    c.commit()
    c.close()
    f = client.get(f"/api/operations/{oid}/findings").get_json()["findings"]
    assert "чужая" not in [x["label"] for x in f]


# --- страница -------------------------------------------------------------

def test_card_page_renders(op):
    client, _, _, oid = op
    r = client.get(f"/operation/{oid}/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for tab in ("Материалы", "Эфиры", "Находки", "Отчёт"):
        assert tab in html


def test_card_page_404_for_unknown_operation(op):
    client, _, _, _ = op
    assert client.get("/operation/999/").status_code == 404


def test_card_page_has_breadcrumbs(op):
    client, _, _, oid = op
    html = client.get(f"/operation/{oid}/").get_data(as_text=True)
    assert "function crumbs()" in html
    assert "class=\"crumbs\"" in html


def test_operations_list_links_to_the_card(op):
    """Раньше карточка вела на /?op= -- то есть в общий список."""
    client, _, _, _ = op
    html = client.get("/operations").get_data(as_text=True)
    assert "/operation/${o.id}/" in html
    assert 'href="/?op=' not in html


# --- поиск по всей операции -----------------------------------------------
#
# Поиск по ТЕКУЩЕЙ папке бесполезен: человек ищет файл ровно тогда, когда не
# помнит, в какой он папке. А с подключённым облаком папок стало больше:
# на боевых данных материал разложен по датам съёмки и вложенным папкам
# вроде "2026 08 14/Helicopter/Saykal".

CARD = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")


def test_search_field_exists():
    assert 'id="q"' in CARD
    assert "Поиск по всей операции" in CARD


def test_search_looks_at_the_whole_operation_not_one_folder():
    """Ключевое: список берётся отдельным запросом по всей операции, а не
    из содержимого открытой папки."""
    assert "/materials" in CARD
    assert "ensureAll" in CARD


def test_list_is_fetched_once_not_per_keystroke():
    """Поход на сервер на каждую букву -- это заметная задержка на
    медленном канале, ради которой нет никакой причины: материалов
    сотни, а не миллионы."""
    assert "if (ALL) return ALL" in CARD


def test_search_matches_path_as_well_as_name():
    """«helicopter saykal» должно находить, даже если этих слов нет в
    самом имени файла."""
    assert "it.folder" in CARD and "it.name" in CARD


def test_multiple_words_narrow_the_search():
    """Как и в списке файлов: каждое слово должно совпасть."""
    assert "parts.every" in CARD


def test_results_show_where_the_file_lies():
    """Найти файл и не понять, в какой он папке, -- половина пользы."""
    assert "hit-where" in CARD


def test_highlight_does_not_build_a_regexp_from_user_input():
    """Запрос печатает человек: точка, скобка или звёздочка в нём либо
    сломают собранную регулярку, либо заставят её совпадать не с тем."""
    i = CARD.index("function mark(")
    chunk = CARD[i:i + 1400]
    assert "new RegExp" not in chunk, (
        "подсветка снова собирает регулярку из пользовательского ввода")
    assert "indexOf" in chunk


def test_results_are_capped():
    """Двести строк разом подвешивают слабую машину, а искать среди
    двухсот результатов всё равно нельзя."""
    assert "slice(0, 60)" in CARD


def test_empty_result_explains_what_to_try():
    assert "Ничего не найдено" in CARD


def test_hotkey_checks_field_type_not_tag():
    """На этих граблях в плеере уже стояли: проверка «это input?» ломала
    горячие клавиши, стоило чекбоксу получить фокус."""
    i = CARD.index("e.key !== '/'")
    chunk = CARD[i:i + 600]
    assert "el.type" in chunk and "checkbox" in chunk


def test_escape_clears_the_search():
    assert "'Escape'" in CARD


# Проверки баланса скобок здесь НЕТ намеренно. Упрощённый разбор строк
# спотыкается о регулярку /[&<>"']/g в esc(): кавычка внутри неё
# принимается за начало строки, и счёт разъезжается ещё до конца шапки.
# Тест, дающий ложный сигнал, хуже отсутствующего -- на него перестают
# смотреть. Синтаксис скрипта проверяется запуском страницы в браузере.

def test_no_template_braces_leaked():
    assert "{{" not in CARD and "}}" not in CARD


def test_search_is_shown_on_the_materials_tab_only():
    """Поле поиска на вкладке находок или отчёта -- это поле, которое
    ничего не делает."""
    assert "classList.toggle('on', tab === 'mat')" in CARD


def test_leaving_the_tab_clears_the_query():
    """Иначе человек возвращается на материалы и видит отфильтрованный
    список, не понимая почему."""
    assert "if (tab !== 'mat' && query)" in CARD


# --- порядок и вид вкладок -------------------------------------------------

def _tabs_html(op):
    client = op[0]
    return client.get("/operation/1/").get_data(as_text=True)


def test_broadcasts_tab_is_last(op):
    """Эфиры -- заглушка. В одном ряду с работающими вкладками она обещает
    то, чего нет."""
    import re
    html = _tabs_html(op)
    order = re.findall(r'data-t="(\w+)"', html)
    assert order[-1] == "live", order
    assert order[0] == "mat"


def test_broadcasts_tab_is_dimmed_and_pushed_right(op):
    html = _tabs_html(op)
    assert 'class="tab later" data-t="live"' in html
    assert ".tab.later{margin-left:auto;color:var(--dim)}" in html


def test_selected_broadcasts_tab_is_still_readable(op):
    """Специфичность: без отдельного правила .tab.later.on выбранная
    вкладка осталась бы блёклой и читалась как неактивная."""
    html = _tabs_html(op)
    assert ".tab.later.on{color:var(--ink)}" in html
    assert html.index(".tab.later{") < html.index(".tab.later.on{"), (
        "правило для выбранной идёт раньше -- его перебьёт общее"
    )


def test_working_tabs_are_not_dimmed(op):
    """Приглушена должна быть ровно одна вкладка."""
    html = _tabs_html(op)
    assert html.count('class="tab later"') == 1
