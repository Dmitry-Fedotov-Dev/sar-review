"""Тесты /api/tree -- список файлов на главной странице.

Регрессия, пойманная на реальных данных (см. tests/test_worker_resilience.py
для той же истории на стороне watcher_loop): раньше report_id для поиска
записи в БД пересчитывался заново из ТЕКУЩЕГО ctime файла. Если ctime успел
"уехать" (типично для больших/ещё дописывающихся файлов), пересчитанный
report_id не совпадал с тем, под которым воркер реально ведёт обработку --
список файлов показывал "в очереди"/0% и ссылку в никуда, хотя обработка
реально шла (или уже завершилась) под другим report_id. Теперь ищем
существующую запись по имени файла (rel_path), а не пересчитываем id."""
import sqlite3

import sar_common
import sar_server


def _client(app, db_path, watch_dir, monkeypatch):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": str(watch_dir)}, raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


def _insert_report(db_path, report_id, rel_path, status, progress_pct=0.0, updated_at="2026-01-01T00:00:00"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, progress_pct, "
        "out_dir, file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (report_id, rel_path, f"/watch/{rel_path}", "video", status, progress_pct,
         "/out", 0.0, updated_at, updated_at))
    conn.commit()
    conn.close()


def test_api_tree_finds_existing_report_even_when_current_ctime_would_compute_different_id(tmp_path, monkeypatch):
    """Основной регрессионный сценарий: воркер завёл запись под report_id,
    вычисленным из СТАРОГО ctime. Файл на диске сейчас имеет ДРУГОЙ ctime
    (типично для видео, которое ещё дописывалось при первом скане воркера).
    /api/tree должен всё равно найти существующую запись (по rel_path), а
    не показать её как "в очереди" с несуществующим свежепосчитанным id."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    video_path = watch_dir / "DJI_test.MP4"
    video_path.write_bytes(b"fake video content")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    # report_id намеренно НЕ совпадает с тем, что make_report_id() посчитал
    # бы сейчас для video_path -- имитирует "уехавший" ctime
    _insert_report(db_path, "DJI_test__STALE_CTIME_ID__abc123", "DJI_test.MP4",
                    status="processing", progress_pct=42.0)

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    resp = client.get("/api/tree")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 1
    item = items[0]

    assert item["report_id"] == "DJI_test__STALE_CTIME_ID__abc123"
    assert item["status"] == "processing"
    assert item["progress_pct"] == 42.0


def test_api_tree_picks_most_recently_updated_row_when_duplicates_exist(tmp_path, monkeypatch):
    """Если дубли уже накопились (данные до фикса) -- берём самую свежую
    запись по updated_at, а не первую попавшуюся."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "DJI_dup.MP4").write_bytes(b"fake")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    _insert_report(db_path, "DJI_dup__OLD__aaa", "DJI_dup.MP4", status="error",
                    updated_at="2026-01-01T00:00:00")
    _insert_report(db_path, "DJI_dup__NEW__bbb", "DJI_dup.MP4", status="done",
                    progress_pct=100.0, updated_at="2026-01-02T00:00:00")

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    resp = client.get("/api/tree")
    item = resp.get_json()["items"][0]

    assert item["report_id"] == "DJI_dup__NEW__bbb"
    assert item["status"] == "done"


def test_api_tree_shows_queued_placeholder_for_genuinely_new_file(tmp_path, monkeypatch):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "DJI_brand_new.MP4").write_bytes(b"fake")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)

    client = _client(sar_server.app, db_path, watch_dir, monkeypatch)
    resp = client.get("/api/tree")
    item = resp.get_json()["items"][0]

    assert item["status"] == "queued"
    assert item["progress_pct"] == 0
