"""Тесты устойчивости sar_worker.worker_loop() к исключениям.

До правки: необработанное исключение в теле цикла (например, "database is
locked"/"disk full" при заявке задачи, или первый update_report(status=
"processing") в run_one_report(), который вызывается ДО try/except внутри
самой функции) тихо убивало daemon-поток -- watcher_loop продолжал ставить
файлы в очередь, а обрабатывать их было уже некому, и ни строчки в консоли.

Тестируем без реальных потоков/сна: подменяем time.sleep на функцию, которая
после N вызовов бросает специальное исключение -- это детерминированно
останавливает `while True` после ровно одной итерации, без блокировки теста.
"""
import sqlite3

import pytest

import sar_common
import sar_worker


class _StopLoop(BaseException):
    """Сигнал тесту, что пора остановить while True -- не связано с реальной
    логикой воркера, только с тем, как устроен сам тест.

    ВАЖНО: наследуется от BaseException, а не Exception -- иначе широкий
    `except Exception` внутри самого worker_loop() (то, что мы тут и тестируем)
    тихо ловил бы наш сигнал остановки как обычную ошибку обработки, тест бы
    не останавливался и уходил в реальный бесконечный цикл (поймали именно
    так при первом запуске)."""


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    db_path = tmp_path / "sar_data.db"
    sar_common.init_db(str(db_path))
    monkeypatch.setattr(sar_worker, "DB_PATH", str(db_path))
    monkeypatch.setattr(sar_worker, "CFG", {"port": 8080})
    return str(db_path)


def _insert_queued_report(db_path, report_id="rep1"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (report_id, "video.mp4", "/tmp/video.mp4", "video", "queued", "/tmp/out", 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()


def _report_status(db_path, report_id="rep1"):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status, error FROM reports WHERE report_id=?", (report_id,)).fetchone()
    conn.close()
    return row


def test_worker_loop_survives_run_one_report_crash_and_marks_error(worker_db, monkeypatch):
    _insert_queued_report(worker_db)

    def boom(report):
        raise RuntimeError("симулированный сбой обработки")

    monkeypatch.setattr(sar_worker, "run_one_report", boom)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise _StopLoop  # останавливаем while True после ровно одной итерации

    monkeypatch.setattr(sar_worker.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        sar_worker.worker_loop(0)

    # ГЛАВНОЕ: поток не "умер молча" -- отчёт помечен error с текстом причины,
    # а не остался вечно висеть в processing
    status, error = _report_status(worker_db)
    assert status == "error"
    assert "симулированный сбой" in error
    assert sleep_calls == [2]  # цикл дошёл до паузы после ошибки, как и задумано


def test_worker_loop_survives_db_error_before_claiming_report(worker_db, monkeypatch):
    # исключение ДО того, как отчёт вообще был выбран (report остаётся None) --
    # раньше такое тоже тихо убивало поток, теперь должно просто залогироваться
    # и продолжить (здесь -- остановиться через фейковый sleep, как в тесте выше)
    def broken_get_db():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sar_worker, "get_db", broken_get_db)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise _StopLoop

    monkeypatch.setattr(sar_worker.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        sar_worker.worker_loop(0)

    assert sleep_calls == [2]  # не упало наружу из функции -- поймано и обработано


def test_worker_loop_normal_dispatch_still_works(worker_db, monkeypatch):
    """Убеждаемся, что защита try/except не сломала счастливый путь --
    отчёт по-прежнему нормально забирается и уходит в run_one_report()."""
    _insert_queued_report(worker_db)
    called_with = []

    def fake_run_one_report(report):
        called_with.append(report["report_id"])
        raise _StopLoop  # останавливаем тест сразу после успешной диспетчеризации

    monkeypatch.setattr(sar_worker, "run_one_report", fake_run_one_report)

    with pytest.raises(_StopLoop):
        sar_worker.worker_loop(0)

    assert called_with == ["rep1"]
    # отчёт должен быть атомарно переведён в processing ПЕРЕД вызовом run_one_report
    status, _ = _report_status(worker_db)
    assert status == "processing"


def test_recover_stale_processing_reports_requeues_only_stale(worker_db, monkeypatch):
    monkeypatch.setattr(sar_worker, "WATCH_DIR", "/tmp")
    _insert_queued_report(worker_db, "rep_processing")
    conn = sqlite3.connect(worker_db)
    conn.execute("UPDATE reports SET status='processing' WHERE report_id='rep_processing'")
    conn.commit()
    conn.close()

    sar_worker.recover_stale_processing_reports()

    status, _ = _report_status(worker_db, "rep_processing")
    assert status == "queued"


def test_watcher_loop_does_not_requeue_already_known_file(tmp_path, worker_db, monkeypatch):
    """Регрессия, поймана на реальных файлах в проде (видео 948 МБ и видео,
    изначально скопированное как 0 байт): раньше watcher_loop() искал
    существующую запись ПО report_id, пересчитанному из ctime файла. Если
    ctime у большого/ещё дописывающегося файла отличается между двумя
    сканами (типичная ситуация для видео, которое ещё копируется в папку),
    watcher решал, что видит "новый" файл, и ставил его в очередь ПОВТОРНО:
    видео начинало обрабатываться с нуля под другим report_id, а исходная
    запись (и весь прогресс по ней) осиротевала и переставала быть видна
    в /api/tree. Теперь ищем по имени файла (rel_path), которое не зависит
    от ctime, поэтому report_id вообще не пересчитывается для уже известных
    файлов."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    video_path = watch_dir / "DJI_test.MP4"
    video_path.write_bytes(b"fake video content")

    monkeypatch.setattr(sar_worker, "WATCH_DIR", str(watch_dir))
    monkeypatch.setattr(sar_worker, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(sar_worker, "CFG", {"port": 8080, "poll_interval_sec": 15})

    # запись для этого файла УЖЕ есть в БД (как будто поставлена в очередь на
    # предыдущем скане) -- report_id намеренно НЕ совпадает с тем, что
    # make_report_id() посчитал бы сейчас по текущему ctime файла
    conn = sqlite3.connect(worker_db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("DJI_test__ALREADY_QUEUED__abc123", "DJI_test.MP4", str(video_path), "video",
         "processing", str(tmp_path / "reports" / "whatever"), 123.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    make_report_id_calls = []
    original_make_report_id = sar_common.make_report_id

    def spy_make_report_id(name, abs_path):
        make_report_id_calls.append(name)
        return original_make_report_id(name, abs_path)

    monkeypatch.setattr(sar_common, "make_report_id", spy_make_report_id)

    def fake_sleep(seconds):
        raise _StopLoop

    monkeypatch.setattr(sar_worker.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        sar_worker.watcher_loop()

    # уже известный файл -- report_id для него вообще не должен пересчитываться
    assert make_report_id_calls == []

    conn = sqlite3.connect(worker_db)
    rows = conn.execute("SELECT report_id FROM reports WHERE rel_path=?", ("DJI_test.MP4",)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "DJI_test__ALREADY_QUEUED__abc123"  # старая запись не тронута, дубль не создан


def test_watcher_loop_still_queues_genuinely_new_file(tmp_path, worker_db, monkeypatch):
    """Убеждаемся, что фикс не сломал обычное обнаружение новых файлов --
    для файла, которого раньше в БД не было, запись всё ещё создаётся."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    video_path = watch_dir / "DJI_new.MP4"
    video_path.write_bytes(b"fake video content")

    monkeypatch.setattr(sar_worker, "WATCH_DIR", str(watch_dir))
    monkeypatch.setattr(sar_worker, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(sar_worker, "CFG", {"port": 8080, "poll_interval_sec": 15})

    def fake_sleep(seconds):
        raise _StopLoop

    monkeypatch.setattr(sar_worker.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        sar_worker.watcher_loop()

    conn = sqlite3.connect(worker_db)
    rows = conn.execute("SELECT report_id, status FROM reports WHERE rel_path=?", ("DJI_new.MP4",)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "queued"
