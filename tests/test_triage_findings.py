"""Отметки триажа во вкладке находок.

Найдено при проверке превью: в списке находок операции были ТОЛЬКО ручные
пометки. Отметок триажа не было ни одной, хотя в базе их 47.

Причина -- опечатка в запросе: "ORDER BY p.updated_at", тогда как столбец
в detection_priorities называется set_at. Запрос падал всегда, а обёртка
try/except Exception: pass это молча съедала. Люди ставили «точно человек»
и «предположительно человек», а список находок делал вид, что таких
отметок нет.

Это не косметика: триаж -- то, чем человек подтверждает находку, и именно
он должен идти в отчёт заказчику.
"""
import pytest

import sar_common


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    sar_common.init_db(path)
    conn = sar_common.get_db_connection(path)
    op = sar_common.create_operation(conn, "Операция", "")
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "                      created_at, updated_at) "
        "VALUES ('r1', 'DJI_1.MP4', '/x/DJI_1.MP4', 'video', 'done', "
        "        '2026-08-15T10:00', '2026-08-15T10:00')")
    sar_common.attach_material(conn, op, "r1")
    conn.execute(
        "INSERT INTO manual_observations "
        "(id, report_id, viewer_name, timestamp_sec, bbox, label, created_at) "
        "VALUES (5, 'r1', 'Айгуль', 286.6, '[0.1,0.1,0.2,0.2]', "
        "'резко чёрное', '2026-08-16T13:43')")
    conn.commit()
    return conn, op


def add_triage(conn, kind, ref_key, priority, who="Айгуль"):
    conn.execute(
        "INSERT INTO detection_priorities "
        "(report_id, kind, ref_key, priority, set_by, set_at) "
        "VALUES ('r1', ?, ?, ?, ?, '2026-08-16T14:00')",
        (kind, ref_key, priority, who))
    conn.commit()


# --- собственно баг -------------------------------------------------------

def test_triage_marks_appear_among_findings(db):
    """Регрессия. До правки здесь было пусто -- всегда."""
    db, op = db
    add_triage(db, "manual", "5", "confirmed_person")
    kinds = [f["kind"] for f in sar_common.operation_findings(db, op)]
    assert "triage" in kinds, (
        "отметок триажа нет в находках -- запрос снова падает молча")


def test_query_error_is_not_swallowed(db):
    """Раньше любая ошибка запроса превращалась в пустой список, и баг
    прожил незамеченным. Ошибка обязана быть видна.

    Подменить метод у sqlite3.Connection нельзя, он только для чтения,
    поэтому подставляем обёртку -- она ведёт себя как соединение, но на
    запросе триажа падает так же, как падал настоящий из-за опечатки.
    """
    db, op = db
    add_triage(db, "manual", "5", "confirmed_person")

    class Broken:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "detection_priorities" in sql and "ORDER BY" in sql:
                raise sar_common.sqlite3.OperationalError(
                    "no such column: выдумка")
            return self._real.execute(sql, *a, **kw)

    with pytest.raises(sar_common.sqlite3.OperationalError):
        sar_common.operation_findings(Broken(db), op)


def test_missing_table_is_still_tolerated(db):
    """База от старой версии без detection_priorities -- законная ситуация,
    и она не должна ронять список находок."""
    db, op = db
    db.execute("DROP TABLE detection_priorities")
    db.commit()
    findings = sar_common.operation_findings(db, op)
    assert [f["kind"] for f in findings] == ["manual"]


# --- что именно показывается ---------------------------------------------

def test_target_kind_is_preserved(db):
    """Без него не понять, откуда брать картинку: у ручной пометки это
    вырезанный кадр, у сцены модели -- готовый кроп отчёта."""
    db, op = db
    add_triage(db, "ai_scene", "person:model:0:120:360", "likely_object")
    t = next(f for f in sar_common.operation_findings(db, op)
             if f["kind"] == "triage")
    assert t["target_kind"] == "ai_scene"


def test_triage_on_a_manual_mark_gets_its_timecode(db):
    """Иначе находка открывалась бы в начале видео, а не там, где её нашли,
    и список находок снова стал бы просто перечнем."""
    db, op = db
    add_triage(db, "manual", "5", "confirmed_person")
    t = next(f for f in sar_common.operation_findings(db, op)
             if f["kind"] == "triage")
    assert t["obs_seconds"] == pytest.approx(286.6)
    assert t["obs_label"] == "резко чёрное"


def test_triage_on_a_model_scene_has_no_timecode(db):
    """У ключа сцены таймкода нет -- честно оставляем пустым, а не
    подставляем ноль, который выглядел бы как «в начале видео»."""
    db, op = db
    add_triage(db, "ai_scene", "person:model:0:120:360", "likely_object")
    t = next(f for f in sar_common.operation_findings(db, op)
             if f["kind"] == "triage")
    assert t["obs_seconds"] is None


def test_triage_of_another_operation_is_not_included(db):
    db, op = db
    add_triage(db, "manual", "5", "confirmed_person")
    other = sar_common.create_operation(db, "Другая", "")
    db.commit()
    assert sar_common.operation_findings(db, other) == []


# --- подпись человеческими словами ---------------------------------------

def test_priority_key_is_never_shown_raw():
    import sar_server
    label = sar_server._finding_label({"priority": "confirmed_person"})
    assert "confirmed_person" not in label, "служебный ключ показан человеку"
    assert "человек" in label


def test_priority_and_manual_label_are_shown_together():
    import sar_server
    label = sar_server._finding_label(
        {"priority": "confirmed_person", "obs_label": "резко чёрное"})
    assert "человек" in label and "резко чёрное" in label


def test_manual_label_wins_when_present():
    import sar_server
    assert sar_server._finding_label(
        {"label": "верёвка", "priority": "rejected"}) == "верёвка"


def test_unknown_priority_does_not_break_the_row():
    import sar_server
    assert sar_server._finding_label({"priority": "выдумка"}) == "выдумка"
    assert sar_server._finding_label({}) == ""
