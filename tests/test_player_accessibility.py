"""Тесты доступности ручного плеера независимо от статуса обработки.

Раньше кнопка "▶ плеер" в списке файлов и сама страница плеера были
недоступны для видео в статусе 'queued' (только что добавлено, воркер ещё
не взял в работу) -- хотя исходный файл физически уже на диске и ручная
разметка от результата детектора не зависит вообще. Пользователь явно
попросил: ручной анализ должен быть доступен сразу, рамки модели
подгружаются по мере обработки отдельно."""
import sqlite3

import sar_common
import sar_server


def _client(app, db_path, script_dir, monkeypatch):
    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", script_dir)
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": script_dir, "player_ai_poll_interval_sec": 5},
                         raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    return client


def _make_report(tmp_path, status, progress_pct=0.0):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake video content")

    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, progress_pct, "
        "out_dir, file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("rep1", "video.mp4", str(video_path), "video", status, progress_pct,
         str(tmp_path / "out"), 0.0, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    return db_path


def test_player_accessible_for_queued_video(tmp_path, monkeypatch):
    db_path = _make_report(tmp_path, "queued")
    client = _client(sar_server.app, db_path, str(tmp_path), monkeypatch)

    resp = client.get("/report/rep1/player/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "очереди" in html  # баннер про очередь, а не про 0% "обработки"
    assert "isProcessing=true" in html.replace(" ", "")


def test_player_accessible_for_processing_video_with_progress_banner(tmp_path, monkeypatch):
    db_path = _make_report(tmp_path, "processing", progress_pct=42.0)
    client = _client(sar_server.app, db_path, str(tmp_path), monkeypatch)

    resp = client.get("/report/rep1/player/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "42%" in html
    assert "isProcessing=true" in html.replace(" ", "")


def test_player_accessible_for_error_video_with_error_banner(tmp_path, monkeypatch):
    db_path = _make_report(tmp_path, "error")
    client = _client(sar_server.app, db_path, str(tmp_path), monkeypatch)

    resp = client.get("/report/rep1/player/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "ошибк" in html.lower()
    # для error НЕ должны бесконечно опрашивать статус в ожидании 'done'
    assert "isProcessing=false" in html.replace(" ", "")


def test_player_accessible_for_done_video_without_banner(tmp_path, monkeypatch):
    db_path = _make_report(tmp_path, "done", progress_pct=100.0)
    client = _client(sar_server.app, db_path, str(tmp_path), monkeypatch)

    resp = client.get("/report/rep1/player/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'id="processing-banner"' not in html  # сам div баннера, не CSS-класс в <style>
    assert "isProcessing=false" in html.replace(" ", "")


def test_tree_page_player_link_not_restricted_to_done_or_processing():
    """Регрессионная защита: ссылка на плеер в списке файлов раньше
    показывалась только для it.status === 'done' || 'processing' -- проверяем,
    что эта строка больше не ограничивает видимость плеера только этими
    статусами (простая проверка исходника сгенерированной страницы, т.к.
    JS-движка для полноценного рендера в тестах нет)."""
    html = sar_server.TREE_PAGE_HTML.format(viewer_name="tester", upload_section="")
    assert "it.status === 'done' || it.status === 'processing'))" not in html.replace(" ", "").replace("\n", "")
    assert "const playerLink = (it.kind === 'video')" in html.replace("\n", " ")
