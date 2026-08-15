"""Тесты приоритета очереди в sar_worker.py.

Регрессия из реальной эксплуатации: пользователь сообщил "система не умеет
работать с фото". На деле фото обрабатывались корректно (проверено прямым
запуском sar_photo_review.py -- секунды на снимок), но стояли в строгом FIFO
за видео, которых накопилось на ~21 час счёта. Результата по снимкам не было
до следующих суток, что и выглядело как отсутствие поддержки.

Фото теперь берутся из очереди раньше видео. Это безопасно в одну сторону:
пачка снимков задержит видео на минуты, тогда как одно видео задерживало
снимки на часы."""
import sqlite3

import sar_common


QUEUE_SQL = ("SELECT rel_path FROM reports WHERE status='queued' "
             "ORDER BY CASE kind WHEN 'photo' THEN 0 ELSE 1 END, created_at")


def _db(tmp_path):
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    return db_path


def _add(db_path, rel_path, kind, created_at, status="queued"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (rel_path, rel_path, f"/watch/{rel_path}", kind, status, "/out", 0.0,
         created_at, created_at))
    conn.commit()
    conn.close()


def _queue(db_path):
    conn = sqlite3.connect(db_path)
    rows = [r[0] for r in conn.execute(QUEUE_SQL).fetchall()]
    conn.close()
    return rows


def test_photo_is_taken_before_video_added_earlier(tmp_path):
    """Главный сценарий бага: видео добавлено раньше, но фото должно пойти
    первым -- иначе снимок ждёт часами за многочасовой обработкой видео."""
    db = _db(tmp_path)
    _add(db, "video.mp4", "video", "2026-01-01T10:00:00")
    _add(db, "photo.jpg", "photo", "2026-01-01T11:00:00")
    assert _queue(db) == ["photo.jpg", "video.mp4"]


def test_photos_keep_fifo_order_between_themselves(tmp_path):
    db = _db(tmp_path)
    _add(db, "b.jpg", "photo", "2026-01-01T11:00:00")
    _add(db, "a.jpg", "photo", "2026-01-01T10:00:00")
    assert _queue(db) == ["a.jpg", "b.jpg"]


def test_videos_keep_fifo_order_between_themselves(tmp_path):
    db = _db(tmp_path)
    _add(db, "second.mp4", "video", "2026-01-01T11:00:00")
    _add(db, "first.mp4", "video", "2026-01-01T10:00:00")
    assert _queue(db) == ["first.mp4", "second.mp4"]


def test_mixed_queue_all_photos_before_all_videos(tmp_path):
    db = _db(tmp_path)
    _add(db, "v1.mp4", "video", "2026-01-01T09:00:00")
    _add(db, "p1.jpg", "photo", "2026-01-01T10:00:00")
    _add(db, "v2.mp4", "video", "2026-01-01T11:00:00")
    _add(db, "p2.jpg", "photo", "2026-01-01T12:00:00")
    assert _queue(db) == ["p1.jpg", "p2.jpg", "v1.mp4", "v2.mp4"]


def test_already_processing_and_done_are_not_in_queue(tmp_path):
    db = _db(tmp_path)
    _add(db, "done.jpg", "photo", "2026-01-01T09:00:00", status="done")
    _add(db, "busy.mp4", "video", "2026-01-01T09:30:00", status="processing")
    _add(db, "waiting.mp4", "video", "2026-01-01T10:00:00")
    assert _queue(db) == ["waiting.mp4"]


def test_worker_uses_this_exact_priority_query():
    """Страховка от расхождения: если запрос в воркере поменяют, а тест
    оставят со старым SQL -- тест продолжит проходить на своей копии и
    ничего не поймает. Поэтому сверяем с реальным исходником."""
    import inspect
    import sar_worker
    src = inspect.getsource(sar_worker.worker_loop)
    assert "CASE kind WHEN 'photo' THEN 0 ELSE 1 END" in src
