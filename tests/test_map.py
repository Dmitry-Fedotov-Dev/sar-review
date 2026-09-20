"""Карта находок на странице операции.

Карту не делали до сих пор не из-за карты, а из-за КАЧЕСТВА КООРДИНАТ.
На боевых данных операции из 32 пометок только у 8 посчитана точка
объекта, у 7 известна лишь позиция дрона, у 17 координат нет вовсе. А
дрон и объект расходятся на материале этой операции до 653 метров -- это
соседнее ущелье.

Отсюда всё, что здесь проверяется: карта обязана РАЗДЕЛЯТЬ два разных
вида точек и обязана говорить, сколько находок на неё не попало. Карта,
молча скрывающая половину пометок, хуже отсутствия карты: по ней делают
вывод, что искать больше негде.

Плюс две вещи, добавленные вместе с ней: отметка, которую человек ставит
прямо на карте (знание человека, а не расчёт платформы), и трек дрона по
телеметрии -- географическая версия покрытия.
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
    data = tmp_path / "data"
    data.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    conn = sar_common.get_db_connection(db)
    op = sar_common.create_operation(conn, "Курумды", folder="Курумды")
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('r1','v1.MP4','','video','done',"
        "datetime('now'), datetime('now'))")
    conn.commit()
    sar_common.attach_material(conn, op, "r1")

    def mark(oid, lat, lon, est_lat, est_lon, label, dist=None):
        conn.execute(
            "INSERT INTO manual_observations (report_id, viewer_name, "
            "timestamp_sec, bbox, label, lat, lon, est_lat, est_lon, "
            "est_distance_m, created_at) VALUES "
            "('r1','Иван',12,'[0,0,1,1]',?,?,?,?,?,?,datetime('now'))",
            (label, lat, lon, est_lat, est_lon, dist))

    # 1 -- посчитана точка объекта, известна и позиция дрона
    mark(op, 39.4800, 73.5900, 39.4850, 73.5950, "рюкзак", 653.0)
    # 2 -- только дрон
    mark(op, 39.4810, 73.5910, None, None, "щель")
    # 3 -- координат нет вовсе
    mark(op, None, None, None, None, "без координат")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "Иван"
    return c, db


def payload(client, path="/api/operations/1/findings-map"):
    r = client.get(path)
    assert r.status_code == 200, r.data[:200]
    return r.get_json()


# --- разделение видов точек ------------------------------------------------

def test_object_point_and_drone_point_are_distinguished(env):
    """ГЛАВНОЕ. Подписать позицию дрона точкой находки -- увести группу в
    соседнее ущелье."""
    client, _ = env
    d = payload(client)
    kinds = sorted(p["estimated"] for p in d["points"])
    assert kinds == [False, True]
    assert d["estimated"] == 1
    assert d["drone_only"] == 1


def test_estimated_point_uses_the_computed_coordinates(env):
    client, _ = env
    p = [x for x in payload(client)["points"] if x["estimated"]][0]
    assert p["lat"] == 39.4850 and p["lon"] == 73.5950


def test_drone_only_point_uses_the_drone_coordinates(env):
    client, _ = env
    p = [x for x in payload(client)["points"] if not x["estimated"]][0]
    assert p["lat"] == 39.4810 and p["lon"] == 73.5910


def test_estimated_point_carries_the_drone_position_too(env):
    """Линия между ними показывает разнос глазом, а не подписью."""
    client, _ = env
    p = [x for x in payload(client)["points"] if x["estimated"]][0]
    assert p["drone"] == {"lat": 39.4800, "lon": 73.5900}


def test_drone_only_point_has_no_pair(env):
    client, _ = env
    p = [x for x in payload(client)["points"] if not x["estimated"]][0]
    assert p["drone"] is None


# --- честность про пропущенные ---------------------------------------------

def test_findings_without_coordinates_are_counted(env):
    """САМОЕ ВАЖНОЕ ЧИСЛО на этой вкладке. Без него пустое место на карте
    читается как «там не искали»."""
    client, _ = env
    d = payload(client)
    assert d["total"] == 3
    assert len(d["points"]) == 2
    assert d["without_coords"] == 1


def test_missing_coordinates_do_not_break_the_answer(env):
    client, _ = env
    assert payload(client)["points"], "пометка без координат уронила выдачу"


def test_half_a_coordinate_is_not_a_point(env, tmp_path):
    """Одна из пары пустая -- не годится ни для карты, ни для навигатора.
    Отбор в SQL этого не ловит: там проверяются РАЗНЫЕ пары полей."""
    client, db = env
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, "
        "timestamp_sec, bbox, label, lat, lon, created_at) VALUES "
        "('r1','Иван',5,'[0,0,1,1]','половина',39.5,NULL,datetime('now'))")
    conn.commit()
    conn.close()
    d = payload(client)
    assert len(d["points"]) == 2, "половинчатая координата попала на карту"
    assert d["without_coords"] == 2


# --- отметка, поставленная человеком ---------------------------------------

def test_mark_is_created_and_returned(env):
    client, _ = env
    r = client.post("/api/operations/1/map-marks",
                    json={"lat": 39.49, "lon": 73.6, "label": "сюда идёт группа"})
    assert r.status_code == 200 and r.get_json()["ok"]
    marks = payload(client)["marks"]
    assert len(marks) == 1
    assert marks[0]["label"] == "сюда идёт группа"
    assert marks[0]["viewer_name"] == "Иван"


def test_mark_is_separate_from_findings(env):
    """Отметка человека -- не расчёт платформы. Смешать их значит выдать
    чужое наблюдение за вычисление по телеметрии."""
    client, _ = env
    client.post("/api/operations/1/map-marks", json={"lat": 39.49, "lon": 73.6})
    d = payload(client)
    assert len(d["points"]) == 2, "отметка человека попала в находки"
    assert len(d["marks"]) == 1
    assert d["total"] == 3, "отметка человека посчиталась как пометка"


@pytest.mark.parametrize("bad", [
    {}, {"lat": 39.5}, {"lat": "юг", "lon": 73.6},
    {"lat": 200, "lon": 73.6}, {"lat": 39.5, "lon": 400},
])
def test_bad_coordinates_are_refused(env, bad):
    client, _ = env
    assert client.post("/api/operations/1/map-marks", json=bad).status_code == 400


def test_mark_on_unknown_operation_is_404(env):
    client, _ = env
    r = client.post("/api/operations/777/map-marks", json={"lat": 39.5, "lon": 73.6})
    assert r.status_code == 404


def test_own_mark_can_be_removed(env):
    client, _ = env
    mid = client.post("/api/operations/1/map-marks",
                      json={"lat": 39.49, "lon": 73.6}).get_json()["id"]
    assert client.delete("/api/operations/1/map-marks/%d" % mid).status_code == 200
    assert payload(client)["marks"] == []


def test_foreign_mark_is_protected(env):
    """Отметка координатора «сюда идёт группа» -- это указание. Стирать
    его посторонний не должен."""
    client, db = env
    conn = sar_common.get_db_connection(db)
    conn.execute("INSERT INTO map_marks (operation_id, lat, lon, label, "
                 "viewer_name, created_at) VALUES (1,39.5,73.6,'чужая',"
                 "'Координатор',datetime('now'))")
    conn.commit()
    mid = conn.execute("SELECT id FROM map_marks").fetchone()[0]
    conn.close()
    assert client.delete("/api/operations/1/map-marks/%d" % mid).status_code == 403


def test_moderator_can_remove_a_foreign_mark(env, monkeypatch):
    client, db = env
    conn = sar_common.get_db_connection(db)
    conn.execute("INSERT INTO map_marks (operation_id, lat, lon, "
                 "viewer_name, created_at) VALUES (1,39.5,73.6,'Другой',"
                 "datetime('now'))")
    conn.commit()
    mid = conn.execute("SELECT id FROM map_marks").fetchone()[0]
    conn.close()
    monkeypatch.setattr(sar_server, "is_moderator", lambda: True)
    assert client.delete("/api/operations/1/map-marks/%d" % mid).status_code == 200


def test_removing_a_missing_mark_is_404(env):
    client, _ = env
    assert client.delete("/api/operations/1/map-marks/999").status_code == 404


# --- треки дрона -----------------------------------------------------------

def put_track(db, report_id, points, raw=0, last_sec=None):
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT OR REPLACE INTO telemetry_tracks (report_id, points, "
        "raw_points, last_sec, parsed_at) VALUES (?,?,?,?,datetime('now'))",
        (report_id, json.dumps(points), raw, last_sec))
    conn.commit()
    conn.close()


def test_server_reads_the_stored_track(env):
    """Сервер ТОЛЬКО читает. Разбор SRT -- работа воркера."""
    client, db = env
    put_track(db, "r1", [[39.4, 73.5], [39.5, 73.6]], raw=12000, last_sec=137)
    d = client_get(env, "/api/operations/1/tracks")
    assert len(d["tracks"]) == 1
    assert d["tracks"][0]["points"] == [[39.4, 73.5], [39.5, 73.6]]
    assert d["tracks"][0]["raw_points"] == 12000


def test_server_does_not_parse_telemetry():
    """Страж. Разбор в веб-слое означал 114 файлов на каждый холодный
    запрос (2,9 с) и кеш в памяти, умирающий при перезапуске."""
    import inspect
    src = inspect.getsource(sar_server.api_operation_tracks)
    for forbidden in ("parse_srt", "get_telemetry_for_report", "_srt_for"):
        assert forbidden not in src, "сервер снова разбирает телеметрию (%s)" % forbidden


def test_parsed_but_empty_is_not_pending(env):
    """«Разобрали, телеметрии нет» и «ещё не разбирали» -- разные вещи:
    первое не изменится никогда, второе пройдёт само."""
    client, db = env
    put_track(db, "r1", [])
    d = client_get(env, "/api/operations/1/tracks")
    assert d["without_telemetry"] == 1
    assert d["pending"] == 0


def test_not_yet_parsed_is_pending(env):
    client, _ = env
    d = client_get(env, "/api/operations/1/tracks")
    assert d["pending"] == 1
    assert d["without_telemetry"] == 0


def test_broken_stored_points_do_not_kill_the_answer(env, db=None):
    client, db = env
    conn = sar_common.get_db_connection(db)
    conn.execute("INSERT INTO telemetry_tracks (report_id, points, "
                 "raw_points, parsed_at) VALUES ('r1','не json',0,"
                 "datetime('now'))")
    conn.commit()
    conn.close()
    d = client_get(env, "/api/operations/1/tracks")
    assert d["without_telemetry"] == 1


# --- разбор в воркере ------------------------------------------------------

@pytest.mark.parametrize("n", [50000, 4182, 1628, 486, 401])
def test_thinning_gives_exactly_the_cap(n):
    """В SRT по точке на кадр -- десятки тысяч на видео.

    Потолок обязан БЫТЬ потолком: деление нацело давало 419 точек при
    пределе 400, а округление вверх теряло половину на треках чуть длиннее
    предела (486 -> 243). Ровное распределение даёт ровно предел.
    """
    import sar_worker
    pts = [[39.0 + i * 1e-5, 73.0 + i * 1e-5] for i in range(n)]
    assert len(sar_worker._thin_track(pts)) == sar_worker.TRACK_MAX_POINTS


def test_thinning_keeps_the_first_point():
    import sar_worker
    pts = [[39.0 + i, 73.0] for i in range(5000)]
    assert sar_worker._thin_track(pts)[0] == pts[0]


def test_thinning_keeps_the_last_point():
    """Обрубив конец, мы покажем, что дрон не долетел туда, куда долетел."""
    import sar_worker
    pts = [[39.0 + i, 73.0] for i in range(1001)]
    assert sar_worker._thin_track(pts)[-1] == [39.0 + 1000, 73.0]


def test_short_track_is_left_alone():
    import sar_worker
    pts = [[39.0, 73.0], [39.1, 73.1]]
    assert sar_worker._thin_track(pts) == pts


def client_get(env, path):
    client, _ = env
    r = client.get(path)
    assert r.status_code == 200, r.data[:200]
    return r.get_json()


# --- лицензионные обязательства --------------------------------------------

def test_openstreetmap_attribution_is_present(env):
    """Данные OSM под ODbL: видимое указание авторства обязательно, и
    убирать его нельзя. См. THIRD-PARTY.md."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "OpenStreetMap" in html
    assert "openstreetmap.org/copyright" in html


def test_leaflet_is_served_locally_not_from_cdn(env):
    """Платформа обязана работать в поле без интернета. Ссылка на CDN
    означала бы пустую страницу там, где карта нужнее всего."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "/static/leaflet/leaflet.js" in html
    for cdn in ("unpkg.com", "cdnjs", "jsdelivr"):
        assert cdn not in html, "Leaflet тянется с %s" % cdn


def test_leaflet_license_is_shipped():
    """BSD-2 требует сохранять текст лицензии при распространении."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    lic = root / "static" / "leaflet" / "LICENSE"
    assert lic.exists(), "лицензия Leaflet не приложена"
    assert "BSD 2-Clause" in lic.read_text(encoding="utf-8")


def test_third_party_notice_lists_leaflet():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    notice = (root / "THIRD-PARTY.md").read_text(encoding="utf-8")
    assert "Leaflet" in notice and "BSD-2" in notice
    assert "OpenStreetMap" in notice


# --- ensure_telemetry_tracks: разбор и запись ------------------------------

@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    import sar_worker
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    for i in (1, 2):
        conn.execute(
            "INSERT INTO reports (report_id, rel_path, abs_path, kind, status,"
            " created_at, updated_at) VALUES (?,?,'','video','done',"
            "datetime('now'), datetime('now'))", ("r%d" % i, "v%d.MP4" % i))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_worker, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_worker, "WATCH_DIR", str(watch), raising=False)
    monkeypatch.setattr(sar_worker, "CFG", {}, raising=False)
    monkeypatch.setattr(sar_worker, "_failures", {}, raising=False)
    monkeypatch.setattr(sar_worker, "_telemetry_index", {}, raising=False)
    return sar_worker, db, watch


def stored(db, report_id="r1"):
    conn = sar_common.get_db_connection(db)
    row = conn.execute("SELECT * FROM telemetry_tracks WHERE report_id=?",
                       (report_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def test_worker_parses_and_stores_a_track(worker_env, monkeypatch):
    sar_worker, db, watch = worker_env
    srt = watch / "v1.srt"
    srt.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sar_worker, "_srt_for_video",
                        lambda p: str(srt) if p.endswith("v1.MP4") else None)
    monkeypatch.setattr(sar_worker, "_parse_srt", lambda p, o: [
        (0.0, 1.0, {"lat": 39.4, "lon": 73.5}),
        (1.0, 2.0, {"lat": 39.5, "lon": 73.6}),
    ])
    sar_worker.ensure_telemetry_tracks()
    row = stored(db)
    assert json.loads(row["points"]) == [[39.4, 73.5], [39.5, 73.6]]
    assert row["raw_points"] == 2
    assert row["min_lat"] == 39.4 and row["max_lat"] == 39.5


def test_missing_telemetry_is_recorded_not_retried_forever(worker_env, monkeypatch):
    """БЕЗ ЭТОЙ ЗАПИСИ 95 видео без телеметрии разбирались бы заново
    каждый проход, вечно."""
    sar_worker, db, _ = worker_env
    monkeypatch.setattr(sar_worker, "_srt_for_video", lambda p: None)
    calls = []
    monkeypatch.setattr(sar_worker, "_parse_srt",
                        lambda p, o: calls.append(1) or [])
    sar_worker.ensure_telemetry_tracks()
    row = stored(db)
    assert row is not None, "«телеметрии нет» не отмечено"
    assert json.loads(row["points"]) == []
    sar_worker.ensure_telemetry_tracks()          # второй проход
    assert calls == [], "разбор повторился, хотя отмечено, что SRT нет"


def test_unchanged_srt_is_not_reparsed(worker_env, monkeypatch):
    sar_worker, db, watch = worker_env
    srt = watch / "v1.srt"
    srt.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sar_worker, "_srt_for_video",
                        lambda p: str(srt) if p.endswith("v1.MP4") else None)
    calls = []
    monkeypatch.setattr(sar_worker, "_parse_srt",
                        lambda p, o: calls.append(1) or [(0.0, 1.0, {"lat": 1, "lon": 2})])
    sar_worker.ensure_telemetry_tracks()
    sar_worker.ensure_telemetry_tracks()
    assert len(calls) == 1, "SRT разобран повторно без изменений"


def test_replaced_srt_is_reparsed(worker_env, monkeypatch):
    """Человек дозалил телеметрию -- трек обязан появиться, а не остаться
    пустым навсегда."""
    import os as _os
    import time as _time
    sar_worker, db, watch = worker_env
    srt = watch / "v1.srt"
    srt.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sar_worker, "_srt_for_video",
                        lambda p: str(srt) if p.endswith("v1.MP4") else None)
    calls = []
    monkeypatch.setattr(sar_worker, "_parse_srt",
                        lambda p, o: calls.append(1) or [(0.0, 1.0, {"lat": 1, "lon": 2})])
    sar_worker.ensure_telemetry_tracks()
    t = _time.time() + 500
    _os.utime(str(srt), (t, t))
    sar_worker.ensure_telemetry_tracks()
    assert len(calls) == 2, "заменённый SRT не перечитан"


def test_broken_srt_does_not_stop_the_others(worker_env, monkeypatch, capsys):
    sar_worker, db, watch = worker_env
    for n in ("v1", "v2"):
        (watch / (n + ".srt")).write_text("x", encoding="utf-8")
    monkeypatch.setattr(sar_worker, "_srt_for_video",
                        lambda p: str(watch / (os.path.basename(p)[:2] + ".srt")))

    def flaky(path, order):
        if "v1" in path:
            raise ValueError("битый SRT")
        return [(0.0, 1.0, {"lat": 39.0, "lon": 73.0}),
                (1.0, 2.0, {"lat": 39.1, "lon": 73.1})]

    monkeypatch.setattr(sar_worker, "_parse_srt", flaky)
    sar_worker.ensure_telemetry_tracks()
    assert stored(db, "r2") is not None, "битый сосед лишил трека исправное видео"
    assert "не разобрался" in capsys.readouterr().out


def test_pass_has_a_budget(worker_env, monkeypatch):
    """Разбор дешёвый, но проход не должен уходить в сотню файлов разом --
    он делит время с постановкой в очередь и отметкой живости."""
    sar_worker, db, watch = worker_env
    monkeypatch.setattr(sar_worker, "TRACKS_PER_PASS", 1)
    monkeypatch.setattr(sar_worker, "_srt_for_video", lambda p: None)
    sar_worker.ensure_telemetry_tracks()
    conn = sar_common.get_db_connection(db)
    n = conn.execute("SELECT COUNT(*) FROM telemetry_tracks").fetchone()[0]
    conn.close()
    assert n == 1


# --- превью находки в карточке на карте ------------------------------------

def test_popup_shows_the_finding_frame(env):
    """Без кадра точка на карте -- просто кружок, и чтобы понять, что там,
    надо уходить на страницу находки."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "/api/finding/${p.id}/preview" in html


def test_missing_frame_hides_the_image(env):
    """Превью может не быть (воркер не дошёл, находка старая). Значок
    битой картинки в карточке хуже, чем её отсутствие."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "onerror=" in html and "display='none'" in html


def test_hover_shows_a_tooltip_not_the_whole_card(env):
    """Раньше наведение открывало карточку целиком -- она выскакивала при
    каждом движении мыши и закрывала соседние точки. Подсказка отвечает на
    вопрос «что это», карточка -- на «расскажи подробнее»."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "bindTooltip" in html
    assert "openPopup()" not in html, "наведение всё ещё открывает карточку"


def test_tooltip_is_bound_to_findings_and_marks(env):
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "bindTooltip(tipFor(p))" in html          # находки
    assert "bindTooltip(esc(m.label" in html         # отметки человека


def test_tooltip_says_when_the_point_is_the_drone(env):
    """Иначе при наведении видно «рюкзак» над местом, где рюкзака нет:
    на этом материале дрон и объект расходятся до 653 метров."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "позиция дрона" in html


def test_card_still_opens_on_click(env):
    """Карточка с кадром и ссылкой никуда не делась -- она просто теперь
    по клику."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "bindPopup(findingPopup(p)" in html
    assert "bindPopup(markPopup(m)" in html


# --- карточка не должна вылезать за рамку карты -----------------------------

def test_popup_has_a_height_limit(env):
    """Leaflet вписывает открытую карточку, подвигая карту, -- но только
    если ей есть куда вписаться. Высокая карточка упиралась в верхний край
    и обрезалась: ровно это было видно на боевой карте."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "maxHeight" in html
    assert "autoPanPadding" in html


def test_preview_image_is_capped(env):
    """Без предела вертикальный кадр делает карточку выше карты."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "max-height:150px" in html
    assert "object-fit:cover" in html


def test_popup_options_are_shared(env):
    """Два места с разными настройками разойдутся: одна карточка будет
    вписываться, другая нет."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert html.count("POPUP_OPTS") >= 3      # объявление + два применения


def test_no_stray_concatenation_in_the_card(env):
    """В шаблонной строке перенос -- это ПЕРЕНОС, а не склейка. Написав
    «' + '» внутри бэктиков, я отправил эти символы прямо в карточку: на
    боевой карте было видно «расчётная дальность ' + '999 м»."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    i = html.index("расчётная дальность")
    chunk = html[i:i + 160]
    assert "' + '" not in chunk, chunk[:120]
    assert "${dist}" in chunk, "подстановка расстояния потерялась"


# --- подпись под картой ----------------------------------------------------

def test_leaflet_prefix_is_plain_text(env):
    """По умолчанию Leaflet 1.9 вставляет в подпись свой SVG-значок. Это
    высказывание разработчиков библиотеки, а не требование лицензии:
    BSD-2 обязывает сохранять текст лицензии при распространении, про
    интерфейс там ничего нет. Платформа поисково-спасательная, и лишних
    высказываний на её карте быть не должно."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "setPrefix" in html
    i = html.index("setPrefix")
    assert "<svg" not in html[i:i + 300]


def test_openstreetmap_attribution_is_not_removed_with_it(env):
    """Вот она как раз ОБЯЗАТЕЛЬНА по ODbL -- убирать нельзя."""
    client, _ = env
    html = client.get("/operation/1/?tab=map").get_data(as_text=True)
    assert "openstreetmap.org/copyright" in html
    assert "attributionControl: false" not in html, (
        "выключен весь блок подписей -- вместе с обязательной OSM")
