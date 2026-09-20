"""Отчёт по операции.

Главное в этом отчёте -- не найденное, а НЕ ПРОСМОТРЕННОЕ. Отчёт,
показывающий только находки, льстит: он отвечает на вопрос «что мы нашли»,
тогда как решение принимается по вопросу «куда ещё не смотрели». На боевых
данных это 80 видео из 114, которые не открывал ни один человек, при
заголовке «просмотрено 1 ч 21 мин из 1 ч 34 мин».

Второе: отчёт обязан честно отличать «ноль» от «не измеряется». Просмотр
фотографий платформа не отслеживает вовсе -- отрезки пишет только плеер
видео. Показать «просмотрено 0 из 92» значило бы соврать.
"""
import json

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

    def add(rid, rel, kind="video", dur=None):
        conn.execute(
            "INSERT INTO reports (report_id, rel_path, abs_path, kind, status,"
            " duration_sec, created_at, updated_at) VALUES "
            "(?,?,'',?,'done',?,datetime('now'),datetime('now'))",
            (rid, rel, kind, dur))
        conn.commit()
        sar_common.attach_material(conn, op, rid)

    add("v1", "оп/DJI_1.MP4", dur=100)      # смотрели двое
    add("v2", "оп/DJI_2.MP4", dur=200)      # не смотрел никто
    add("v3", "оп/DJI_3.MP4", dur=None)     # длительность неизвестна
    add("p1", "оп/снимок.JPG", kind="photo")

    def seg(rid, who, a, b, ts):
        conn.execute(
            "INSERT INTO watch_segments (report_id, viewer_name, start_sec, "
            "end_sec, ts) VALUES (?,?,?,?,?)", (rid, who, a, b, ts))

    # Иван: два захода 15-го (разрыв больше получаса) -> 2 просмотра
    seg("v1", "Иван", 0, 40, "2026-08-15T10:00:00")
    seg("v1", "Иван", 40, 50, "2026-08-15T10:05:00")
    seg("v1", "Иван", 50, 60, "2026-08-15T14:00:00")
    # Пётр: один заход 16-го
    seg("v1", "Пётр", 0, 30, "2026-08-16T09:00:00")
    # v3 смотрели, но длительность неизвестна
    seg("v3", "Иван", 0, 10, "2026-08-15T11:00:00")

    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, "
        "timestamp_sec, bbox, label, lat, lon, created_at) VALUES "
        "('v1','Иван',5,'[0,0,1,1]','рюкзак',39.5,73.6,'2026-08-15T10:03:00')")
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES "
        "('v1','Пётр',9,'[0,0,1,1]','щель','2026-08-16T09:10:00')")
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


def rep(client, qs=""):
    r = client.get("/api/operations/1/report" + qs)
    assert r.status_code == 200, r.data[:300]
    return r.get_json()


def row(d, kind, name):
    return [x for x in d["materials"][kind] if x["name"] == name][0]


# --- непросмотренное -------------------------------------------------------

def test_untouched_videos_are_counted(env):
    """САМОЕ ВАЖНОЕ ЧИСЛО отчёта."""
    client, _ = env
    c = rep(client)["coverage"]
    assert c["videos_untouched"] == 1      # v2
    assert c["videos_touched"] == 2        # v1, v3


def test_untouched_video_is_in_the_table_with_zeros(env):
    """Его не должно не быть: пустая строка в таблице -- это и есть
    сообщение «сюда не смотрели»."""
    client, _ = env
    r = row(rep(client), "video", "DJI_2.MP4")
    assert r["views"] == 0 and r["viewers"] == 0 and r["coverage_pct"] == 0.0


# --- покрытие --------------------------------------------------------------

def test_coverage_merges_overlapping_intervals(env):
    """Иван посмотрел 0-40, 40-50, 50-60 -- это 60 секунд из 100, а не
    сумма отрезков."""
    client, _ = env
    r = row(rep(client), "video", "DJI_1.MP4")
    assert r["coverage_pct"] == 60.0


def test_unknown_duration_gives_null_not_zero(env):
    """Ноль процентов читается как «не смотрели». А здесь смотрели --
    просто посчитать не из чего."""
    client, _ = env
    r = row(rep(client), "video", "DJI_3.MP4")
    assert r["coverage_pct"] is None
    assert r["views"] > 0, "просмотры есть, а процент неизвестен"


# --- просмотры считаются по людям ------------------------------------------

def test_views_are_counted_per_viewer(env):
    """По общей ленте времени двое, смотревшие одновременно, слипались в
    один просмотр -- и выходило «просмотров 6, людей 8»."""
    client, _ = env
    r = row(rep(client), "video", "DJI_1.MP4")
    assert r["viewers"] == 2
    # Иван: два захода (разрыв 4 часа), Пётр: один
    assert r["views"] == 3


def test_simultaneous_viewers_are_not_merged_into_one_view(env, db=None):
    """РЕШАЮЩИЙ СЛУЧАЙ. По общей ленте времени два человека, смотревшие
    ОДНОВРЕМЕННО, слипаются в один просмотр -- именно так и выходило
    «просмотров 6, людей 8». Разнесённые по времени зрители этого не
    показывают: у них разрыв и так больше получаса.
    """
    client, db = env
    conn = sar_common.get_db_connection(db)
    # третий человек смотрит v1 в ту же минуту, что и Иван
    conn.execute(
        "INSERT INTO watch_segments (report_id, viewer_name, start_sec, "
        "end_sec, ts) VALUES ('v1','Ольга',0,20,'2026-08-15T10:02:00')")
    conn.commit()
    conn.close()
    r = row(rep(client), "video", "DJI_1.MP4")
    assert r["viewers"] == 3
    assert r["views"] == 4, (
        "одновременные зрители посчитаны как один просмотр (%d)" % r["views"])


def test_views_are_never_fewer_than_viewers(env):
    client, _ = env
    for r in rep(client)["materials"]["video"]:
        assert r["views"] >= r["viewers"], r["name"]


def test_close_segments_are_one_view(env):
    """Отрезки одного захода не должны считаться отдельными просмотрами:
    на боевых данных один человек за заход даёт их сотни."""
    client, _ = env
    r = row(rep(client), "video", "DJI_1.MP4")
    assert r["views"] < 4, "отрезки посчитаны как отдельные просмотры"


# --- период ----------------------------------------------------------------

def test_period_filters_work_not_shooting_date(env):
    """«Отчёт за 15 августа» -- это что сделали 15-го."""
    client, _ = env
    d = rep(client, "?from=2026-08-16&to=2026-08-16")
    r = row(d, "video", "DJI_1.MP4")
    assert r["viewers"] == 1        # только Пётр
    assert d["findings"]["total"] == 1


def test_period_end_includes_the_whole_day(env):
    """Человек, выбравший «по 15 августа», имеет в виду 15-е целиком, а не
    полночь на его начало."""
    client, _ = env
    d = rep(client, "?from=2026-08-15&to=2026-08-15")
    assert d["findings"]["total"] == 1, "пометка 15-го в 10:03 не попала"
    assert row(d, "video", "DJI_1.MP4")["viewers"] == 1


def test_empty_period_is_the_whole_operation(env):
    client, _ = env
    d = rep(client)
    assert d["period"]["full"] is True
    assert d["findings"]["total"] == 2


def test_period_outside_data_is_empty_not_broken(env):
    client, _ = env
    d = rep(client, "?from=2027-01-01&to=2027-01-02")
    assert d["coverage"]["videos_touched"] == 0
    assert d["findings"]["total"] == 0
    assert len(d["materials"]["video"]) == 3, "материалы пропали из таблицы"


def test_broken_date_does_not_crash(env):
    client, _ = env
    assert rep(client, "?from=не-дата&to=тоже")["operation"]["id"] == 1


# --- фотографии ------------------------------------------------------------

def test_photo_tracking_absence_is_reported_as_such(env):
    """Показать «просмотрено 0 из 92» значило бы соврать: это не ноль, а
    отсутствие измерения -- отрезки пишет только плеер видео."""
    client, _ = env
    c = rep(client)["coverage"]
    assert c["photos_tracked"] is False
    assert c["photos_total"] == 1
    assert "photos_untouched" not in c, "выдуманное число про фото вернулось"


def test_photos_are_a_separate_table(env):
    client, _ = env
    d = rep(client)
    assert [x["name"] for x in d["materials"]["photo"]] == ["снимок.JPG"]
    assert all(x["name"] != "снимок.JPG" for x in d["materials"]["video"])


# --- люди ------------------------------------------------------------------

def test_people_are_ranked_by_contribution(env):
    client, _ = env
    people = rep(client)["people"]
    assert [p["name"] for p in people] == ["Иван", "Пётр"]
    assert people[0]["marks"] == 1
    assert people[0]["materials"] == 2


# --- обезличивание ---------------------------------------------------------

def test_anonymization_replaces_every_name(env):
    """Псевдоним раздаётся при СБОРКЕ данных, а не при отрисовке: фильтр,
    который надо не забыть применить в каждом месте, однажды забудут."""
    client, _ = env
    d = rep(client, "?anon=1")
    assert d["anonymized"] is True
    blob = json.dumps(d, ensure_ascii=False)
    assert "Иван" not in blob and "Пётр" not in blob


def test_aliases_follow_contribution(env):
    """«Волонтёр А» -- всегда тот, кто сделал больше всех: структурный
    вывод сохраняется, имя нет."""
    client, _ = env
    people = rep(client, "?anon=1")["people"]
    assert people[0]["name"] == "волонтёр А"
    assert people[1]["name"] == "волонтёр Б"
    assert people[0]["seconds"] > people[1]["seconds"]


def test_alias_is_stable_within_a_report(env):
    """Читатель должен уметь проследить «волонтёр Б сделал то и это»."""
    client, _ = env
    d = rep(client, "?anon=1")
    a = rep(client, "?anon=1")
    assert [p["name"] for p in d["people"]] == [p["name"] for p in a["people"]]


def test_names_are_intact_without_the_flag(env):
    client, _ = env
    blob = json.dumps(rep(client), ensure_ascii=False)
    assert "Иван" in blob


def test_mark_author_without_views_still_gets_an_alias(env):
    """Человек, который ничего не смотрел, но ставил пометки, иначе
    остался бы в отчёте под своим именем."""
    client, db = env
    conn = sar_common.get_db_connection(db)
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES "
        "('v2','Тихий',1,'[0,0,1,1]','след',datetime('now'))")
    conn.commit()
    conn.close()
    blob = json.dumps(rep(client, "?anon=1"), ensure_ascii=False)
    assert "Тихий" not in blob


# --- второй проход ---------------------------------------------------------

def test_second_pass_histogram(env):
    """Учёт второго прохода: важно не сколько посмотрели, а сколько
    посмотрели ДВАЖДЫ."""
    client, _ = env
    hist = {x["viewers"]: x["materials"] for x in rep(client)["second_pass"]}
    assert hist[2] == 1        # v1 смотрели двое
    assert hist[0] == 2        # v2 и снимок


# --- страж -----------------------------------------------------------------

def test_report_does_not_invent_photo_coverage():
    import inspect
    src = inspect.getsource(sar_server.api_operation_report)
    assert "photos_untouched" not in src
