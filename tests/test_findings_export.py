"""Выгрузка находок координатору в KML и GPX.

Координатор на месте работает не в платформе, а в своей карте или в
навигаторе. Пока координаты живут только внутри системы, они бесполезны
ровно там, где нужны.

ГЛАВНОЕ, ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, -- НЕСМЕШИВАНИЕ ДВУХ РАЗНЫХ ТОЧЕК:

  * позиция ДРОНА в момент пометки -- то, что пишет телеметрия;
  * вероятная точка ОБЪЕКТА на земле -- расчёт по наклону подвеса,
    высоте и положению рамки в кадре.

На материале операции они расходятся на сотни метров: в выгрузке есть
пометка с расчётной дальностью 653 метра. Если подписать позицию дрона
словом «находка», группа пойдёт не туда -- в масштабе Алайского хребта
это соседнее ущелье. Ошибка при этом молчаливая: файл открывается,
точки на карте есть, всё выглядит правильно.
"""
import xml.etree.ElementTree as ET

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

    conn = sar_common.get_db_connection(db)
    op_id = sar_common.create_operation(conn, "Курумды август")
    assert op_id == 1, "адреса в тестах зашиты на первую операцию"
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('r1', 'DJI_0002.mp4', ?, 'video', "
        "'done', datetime('now'), datetime('now'))", (str(watch / "a.mp4"),))
    sar_common.attach_material(conn, op_id, "r1")

    def obs(oid, lat, lon, est_lat=None, est_lon=None, dist=None,
            label="находка", note=None, ts=545.0):
        conn.execute(
            "INSERT INTO manual_observations (id, report_id, viewer_name, "
            "timestamp_sec, bbox, label, note, lat, lon, est_lat, est_lon, "
            "est_distance_m, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, "
            "datetime('now'))",
            (oid, "r1", "волонтёр", ts, "[0.1,0.1,0.2,0.2]", label, note,
             lat, lon, est_lat, est_lon, dist))

    # объект посчитан: дрон в одной точке, объект в 653 метрах от неё
    obs(1, 39.480975, 73.592623, 39.486, 73.599, 653.38, label="фигура")
    # объекта нет: известна только позиция дрона
    obs(2, 39.470000, 73.580000, label="яркое пятно")
    # координат нет вовсе -- в выгрузку попадать не должна
    obs(3, None, None, label="без телеметрии")
    conn.commit()
    conn.close()

    client = sar_server.app.test_client()
    with client.session_transaction() as s:
        s["authed"] = True
        s["verified"] = True
        s["viewer_name"] = "координатор"
        s["role"] = sar_common.ROLE_VIEWER
    return client


KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}


def kml(client):
    r = client.get("/api/operations/1/findings.kml")
    assert r.status_code == 200
    return ET.fromstring(r.data)


def gpx(client):
    r = client.get("/api/operations/1/findings.gpx")
    assert r.status_code == 200
    return ET.fromstring(r.data)


# --- два вида точек не смешиваются ---------------------------------------

def test_kml_separates_object_points_from_drone_positions(env):
    """Разные папки -- в карте их видно как разные слои и можно погасить."""
    folders = kml(env).findall(".//k:Folder", KML_NS)
    names = [f.find("k:name", KML_NS).text for f in folders]
    assert len(folders) == 2, "оба вида точек свалены в одну кучу"
    assert any("объект" in n for n in names)
    assert any("дрон" in n for n in names)

    counts = {n: len(f.findall("k:Placemark", KML_NS))
              for n, f in zip(names, folders)}
    assert list(counts.values()) == [1, 1], counts


def test_drone_position_is_never_presented_as_the_object(env):
    """Самая дорогая ошибка: увести группу в соседнее ущелье."""
    for pm in kml(env).findall(".//k:Placemark", KML_NS):
        desc = pm.find("k:description", KML_NS).text
        coords = pm.find(".//k:coordinates", KML_NS).text
        if coords.startswith("73.58"):          # точка дрона
            assert "ПОЗИЦИЯ ДРОНА" in desc
            assert "ВЕРОЯТНАЯ ТОЧКА ОБЪЕКТА" not in desc


def test_object_point_uses_estimated_coordinates_not_the_drone(env):
    """Иначе разделение было бы только на словах."""
    pm = kml(env).find(".//k:Folder/k:Placemark", KML_NS)
    lon, lat, _ = pm.find(".//k:coordinates", KML_NS).text.split(",")
    assert abs(float(lat) - 39.486) < 1e-4
    assert abs(float(lon) - 73.599) < 1e-4


def test_estimate_is_labelled_as_a_calculation(env):
    """Расчёт по телеметрии -- не измерение. Читающий должен это знать
    до того, как поедет на точку."""
    text = env.get("/api/operations/1/findings.kml").data.decode()
    assert "Расчёт по телеметрии" in text
    assert "653" in text, "не показана расчётная дальность"


def test_drone_point_says_the_object_is_elsewhere(env):
    text = env.get("/api/operations/1/findings.kml").data.decode()
    assert "Объект находится в стороне" in text


# --- GPX: папок нет, значит вид точки уходит в имя ------------------------

def test_gpx_marks_drone_points_in_the_name(env):
    """На экране навигатора видно только имя точки, описание надо
    открывать отдельно -- поэтому пометка стоит прямо в имени."""
    names = [w.find("g:name", GPX_NS).text
             for w in gpx(env).findall("g:wpt", GPX_NS)]
    assert "фигура" in names
    assert "яркое пятно [дрон]" in names


def test_gpx_also_carries_the_type_field(env):
    types = sorted(w.find("g:type", GPX_NS).text
                   for w in gpx(env).findall("g:wpt", GPX_NS))
    assert types == ["объект", "позиция дрона"]


# --- что попадает в выгрузку ---------------------------------------------

def test_findings_without_any_coordinates_are_skipped(env):
    """Точка без координат на карте бессмысленна, а в списке создаёт
    впечатление, что координата есть."""
    text = env.get("/api/operations/1/findings.kml").data.decode()
    assert "без телеметрии" not in text
    assert len(kml(env).findall(".//k:Placemark", KML_NS)) == 2


def test_description_carries_enough_to_find_the_source(env):
    """С точки на карте нужно уметь вернуться к кадру."""
    text = env.get("/api/operations/1/findings.kml").data.decode()
    assert "DJI_0002.mp4" in text
    assert "09:05" in text, "нет таймкода"
    assert "волонтёр" in text


def test_status_is_included_when_set(env):
    """Статус ставится тем же путём, что и в интерфейсе: если выгрузка
    возьмёт его из другого места, разойдётся именно так -- незаметно."""
    r = env.post("/api/report/r1/priorities",
                 json={"kind": "manual", "ref_key": "1",
                       "priority": "rejected"})
    assert r.status_code == 200 and r.get_json()["ok"]
    text = env.get("/api/operations/1/findings.kml").data.decode()
    assert sar_common.PRIORITY_LABELS["rejected"] in text


# --- сам файл -------------------------------------------------------------

def test_both_formats_are_valid_xml_and_download_as_files(env):
    for fmt in ("kml", "gpx"):
        r = env.get("/api/operations/1/findings.%s" % fmt)
        ET.fromstring(r.data)                       # упадёт, если не XML
        assert "attachment" in r.headers["Content-Disposition"]
        assert "UTF-8" in r.headers["Content-Disposition"], (
            "русское имя файла без указания кодировки превратится в мусор")
        assert "charset=utf-8" in r.headers["Content-Type"]


def test_special_characters_do_not_break_the_file(env):
    """Человек пишет в заметке что угодно, в том числе «<» и «&»."""
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute("UPDATE manual_observations SET label=?, note=? WHERE id=1",
                 ("склон <30° & лёд", "кто-то сказал \"видно\""))
    conn.commit()
    conn.close()
    root = kml(env)                                  # разбор и есть проверка
    names = [n.text for n in root.findall(".//k:Placemark/k:name", KML_NS)]
    assert "склон <30° & лёд" in names


def test_unknown_format_is_refused(env):
    assert env.get("/api/operations/1/findings.csv").status_code == 404


def test_missing_operation_is_not_an_empty_file(env):
    """Пустой валидный файл читается как «находок нет» -- это неправда."""
    assert env.get("/api/operations/777/findings.kml").status_code == 404


def test_export_requires_login(env):
    with env.session_transaction() as s:
        s.clear()
    r = env.get("/api/operations/1/findings.kml")
    assert r.status_code in (302, 401, 403), (
        "координаты находок отдаются кому угодно")


# --- кнопки в интерфейсе --------------------------------------------------

def test_buttons_are_offered_on_the_findings_tab():
    card = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")
    assert "findings.kml" in card and "findings.gpx" in card
    assert "exportButtons()" in card


def test_buttons_hidden_when_there_is_nothing_to_export():
    """Кнопка, которая отдаёт пустой файл, хуже отсутствующей."""
    card = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")
    assert "if (!withGeo) return ''" in card


def test_half_a_coordinate_does_not_break_the_whole_export(env):
    """Широта есть, долготы нет. Отбор в SQL этого не ловит -- там
    проверяются разные пары полей. Одна кривая строка не должна лишать
    координатора всех остальных точек."""
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute("UPDATE manual_observations SET est_lat=39.5, est_lon=NULL, "
                 "lon=NULL WHERE id=3")
    conn.commit()
    conn.close()
    assert len(kml(env).findall(".//k:Placemark", KML_NS)) == 2


def test_counter_and_file_cannot_disagree(env):
    """Кнопка говорит «(N)», в файле оказывается M. Такое расхождение
    молчаливое: оба числа выглядят правдоподобно. Поэтому признак
    «попадёт в выгрузку» считает сервер, а интерфейс только складывает."""
    rows = env.get("/api/operations/1/findings").get_json()["findings"]
    assert sum(1 for r in rows if r.get("exportable")) == len(
        kml(env).findall(".//k:Placemark", KML_NS))


def test_client_does_not_recompute_the_rule():
    card = sar_server.OPERATION_CARD_HTML.format(viewer_name="в")
    assert "findings.filter(f => f.exportable)" in card, (
        "интерфейс снова считает координаты по своему правилу")


def test_half_a_coordinate_is_not_exportable_in_the_list_either(env):
    """Признак в списке обязан отражать то же правило."""
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    conn.execute("UPDATE manual_observations SET lon=NULL WHERE id=2")
    conn.commit()
    conn.close()
    rows = env.get("/api/operations/1/findings").get_json()["findings"]
    by_id = {r["obs_id"]: r for r in rows if r.get("obs_id")}
    assert by_id[2]["exportable"] is False
    assert len(kml(env).findall(".//k:Placemark", KML_NS)) == 1
