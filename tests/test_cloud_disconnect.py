"""Отключение хранилища не должно ломать показ материала.

Записи материала ссылаются на подключение. Удалив его и не прибрав за
собой, мы оставляем ссылки на несуществующее: корня в дереве у таких
файлов больше нет, и все они проваливаются в «вне папки операции» плоской
кучей. Именно так диск «пропадал» со страницы операции после
переподключения -- при том что файлы никуда не девались.

Второе правило, важнее первого: РАБОТУ ЛЮДЕЙ НЕ ТЕРЯЕМ. Пометки,
обсуждения и отметки просмотра переживают отключение диска, даже если сам
файл стал недоступен.
"""
import pytest

import sar_common


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


def add_account(conn, label="Диск"):
    return sar_common.add_cloud_account(conn, provider="google",
                                         token="ya29.t", label=label)


def add_cloud_report(conn, acc, rid, rel):
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_account_id, cloud_file_id, created_at, updated_at) VALUES "
        "(?,?,'','video','idle',?,?,datetime('now'),datetime('now'))",
        (rid, rel, acc, "f" + rid))
    conn.commit()
    return rid


# --- уборка ---------------------------------------------------------------

def test_untouched_material_is_removed(conn):
    """Без хранилища это пустые ссылки: файла нет, работы по нему нет."""
    acc = add_account(conn)
    add_cloud_report(conn, acc, "c1", "оп/a.mp4")
    res = sar_common.delete_cloud_account(conn, acc)
    assert res["removed"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"] == 0


def test_material_with_human_work_survives(conn):
    """САМОЕ ВАЖНОЕ. Пометка волонтёра дороже любой автоматики: файл можно
    скачать заново, а разметку -- нет."""
    acc = add_account(conn)
    add_cloud_report(conn, acc, "c1", "оп/a.mp4")
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES "
        "('c1','волонтёр',10,'[0,0,1,1]','находка',datetime('now'))")
    conn.commit()

    res = sar_common.delete_cloud_account(conn, acc)
    assert res["kept"] == 1 and res["removed"] == 0
    row = conn.execute("SELECT cloud_account_id, cloud_file_id FROM reports "
                       "WHERE report_id='c1'").fetchone()
    assert row is not None, "запись с пометкой удалена вместе с диском"
    assert row["cloud_account_id"] is None
    assert conn.execute("SELECT COUNT(*) n FROM manual_observations"
                        ).fetchone()["n"] == 1


def test_watched_material_survives(conn):
    """Отметки просмотра -- тоже работа: по ним считается покрытие."""
    acc = add_account(conn)
    add_cloud_report(conn, acc, "c1", "оп/a.mp4")
    conn.execute("INSERT INTO watch_segments (report_id, viewer_name, "
                 "start_sec, end_sec, ts) VALUES ('c1','в',0,10,datetime('now'))")
    conn.commit()
    assert sar_common.delete_cloud_account(conn, acc)["kept"] == 1


def test_discussed_material_survives(conn):
    acc = add_account(conn)
    add_cloud_report(conn, acc, "c1", "оп/a.mp4")
    conn.execute("INSERT INTO detection_comments (report_id, kind, ref_key, "
                 "author, text, created_at) VALUES "
                 "('c1','manual','1','в','посмотрите сюда',datetime('now'))")
    conn.commit()
    assert sar_common.delete_cloud_account(conn, acc)["kept"] == 1


def test_triaged_material_survives(conn):
    acc = add_account(conn)
    add_cloud_report(conn, acc, "c1", "оп/a.mp4")
    conn.execute("INSERT INTO detection_priorities (report_id, kind, ref_key, "
                 "priority, set_by, set_at) VALUES "
                 "('c1','manual','1','rejected','в',datetime('now'))")
    conn.commit()
    assert sar_common.delete_cloud_account(conn, acc)["kept"] == 1


def test_other_accounts_are_not_touched(conn):
    """Отключая один диск, второй трогать нельзя."""
    a1, a2 = add_account(conn, "Первый"), add_account(conn, "Второй")
    add_cloud_report(conn, a1, "c1", "оп/a.mp4")
    add_cloud_report(conn, a2, "c2", "оп/b.mp4")
    sar_common.delete_cloud_account(conn, a1)
    left = [r["report_id"] for r in conn.execute("SELECT report_id FROM reports")]
    assert left == ["c2"]
    assert len(sar_common.cloud_accounts_public(conn)) == 1


def test_operation_links_are_cleaned_too(conn):
    """Связь, ведущая на удалённую запись, ломает счётчики операции."""
    acc = add_account(conn)
    op = sar_common.create_operation(conn, "Операция")
    add_cloud_report(conn, acc, "c1", "оп/a.mp4")
    sar_common.attach_material(conn, op, "c1")
    sar_common.delete_cloud_account(conn, acc)
    n = conn.execute("SELECT COUNT(*) n FROM operation_materials").fetchone()["n"]
    assert n == 0


def test_tree_is_clean_after_disconnect(conn, tmp_path):
    """Сквозная проверка смысла: после отключения в дереве не остаётся
    осиротевшей кучи."""
    watch = tmp_path / "watch"
    (watch / "Оп").mkdir(parents=True)
    acc = add_account(conn)
    op = sar_common.create_operation(conn, "Операция", folder="Оп")
    for i in range(5):
        add_cloud_report(conn, acc, "c%d" % i, "2026 08 11/v%d.mp4" % i)
        sar_common.attach_material(conn, op, "c%d" % i)

    before = sar_common.browse_operation(conn, str(watch), op)
    assert len(before["folders"]) == 1, "до отключения был корень подключения"

    sar_common.delete_cloud_account(conn, acc)
    after = sar_common.browse_operation(conn, str(watch), op)
    assert after["folders"] == []
    assert after["outside"] == [], (
        "материал остался висеть «вне папки операции» -- именно так диск "
        "и пропадал со страницы")
