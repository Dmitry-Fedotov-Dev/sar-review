"""Тумблер авто-обработки и ручной запуск модели по выбранным файлам.

Причина: на CPU минута видео считается ~30 минут. Автоматическая обработка
всего подряд забивает машину на сутки вперёд и мешает тем, кто в это же
время смотрит видео руками. При выключенной авто-обработке файлы всё равно
появляются в списке и полностью доступны для РУЧНОГО разбора -- модель
запускается только по тем, что человек выбрал сам."""
import sqlite3

import sar_common
import sar_server


QUEUE_SQL = ("SELECT rel_path FROM reports WHERE status='queued' "
             "ORDER BY CASE kind WHEN 'photo' THEN 0 ELSE 1 END, created_at")


def _insert(db_path, report_id, rel_path, status, kind="video"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (report_id, rel_path, f"/watch/{rel_path}", kind, status, "/out", 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


def _status(db_path, report_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM reports WHERE report_id=?", (report_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def _client(app, db_path, monkeypatch):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


def test_auto_process_is_on_by_default():
    assert sar_common.DEFAULT_SERVER_CONFIG["auto_process"] is True


def test_idle_files_are_not_taken_by_the_worker(tmp_path):
    """Главное свойство: пока файл 'idle', воркер его не трогает."""
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "r_idle", "untouched.mp4", "idle")
    _insert(db_path, "r_queued", "wanted.mp4", "queued")

    conn = sqlite3.connect(db_path)
    queue = [r[0] for r in conn.execute(QUEUE_SQL).fetchall()]
    conn.close()
    assert queue == ["wanted.mp4"]


def test_enqueue_moves_idle_to_queued(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "idle")
    client = _client(sar_server.app, db_path, monkeypatch)

    resp = client.post("/api/report/rep1/enqueue")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "queued"
    assert _status(db_path, "rep1") == "queued"


def test_enqueue_is_noop_when_already_processing(tmp_path, monkeypatch):
    """Нельзя сбрасывать прогресс уже идущей обработки."""
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "processing")
    client = _client(sar_server.app, db_path, monkeypatch)

    resp = client.post("/api/report/rep1/enqueue")
    assert resp.status_code == 200
    assert _status(db_path, "rep1") == "processing"


def test_enqueue_allows_retry_after_error(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "error")
    client = _client(sar_server.app, db_path, monkeypatch)

    client.post("/api/report/rep1/enqueue")
    assert _status(db_path, "rep1") == "queued"


def test_enqueue_clears_previous_error_text(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert(db_path, "rep1", "v.mp4", "error")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE reports SET error='старая ошибка' WHERE report_id='rep1'")
    conn.commit()
    conn.close()

    client = _client(sar_server.app, db_path, monkeypatch)
    client.post("/api/report/rep1/enqueue")

    conn = sqlite3.connect(db_path)
    err = conn.execute("SELECT error FROM reports WHERE report_id='rep1'").fetchone()[0]
    conn.close()
    assert err is None


def test_enqueue_404_for_unknown_report(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    client = _client(sar_server.app, db_path, monkeypatch)
    assert client.post("/api/report/nope/enqueue").status_code == 404


# --- интерфейс ---

def test_file_list_shows_run_button_for_unprocessed_files():
    html = sar_server.TREE_PAGE_HTML.replace("\n", " ")
    assert "enqueue(" in html
    assert "it.status === 'idle' || it.status === 'error'" in html


def test_idle_status_has_its_own_badge_label():
    html = sar_server.TREE_PAGE_HTML
    assert "idle:'без анализа'" in html
