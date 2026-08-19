#!/usr/bin/env python3
"""
sar_worker.py — фоновая обработка видео/фото, отдельно от веб-интерфейса.

Раньше вся эта логика (слежение за папкой + запуск обработки) жила прямо
внутри sar_server.py как фоновые потоки. Проблема: любое обновление кода
sar_server.py требовало его перезапуска, а вместе с ним — обрыва текущей
обработки видео (процесс детектора убивался вместе с сервером).

Теперь это отдельный процесс. sar_server.py — только веб-морда, читающая
из общей БД; sar_worker.py — только обработка, тоже читающая/пишущая ту же
БД. Они не знают друг о друге напрямую, только через SQLite-файл на диске.
Поэтому:
  - можно перезапустить sar_server.py (обновить код, поправить баг в UI) —
    sar_worker.py и текущая обработка видео это никак не почувствуют;
  - можно перезапустить sar_worker.py отдельно — веб-интерфейс продолжит
    отдавать страницы (правда, видео из очереди временно не будут
    обрабатываться, пока воркер не поднимется обратно).

ВАЖНО, честно: если ОБНОВИТЬ САМ sar_worker.py и его перезапустить —
видео, которое в этот момент обрабатывалось, придётся считать заново
(текущий процесс детектора не поддерживает докачку с середины). Это не
"обновление совсем без потерь", а именно разделение ответственности:
sar_server.py действительно обновляется на лету без каких-либо потерь,
для sar_worker.py "на лету" означает "не теряя уже ГОТОВЫЕ результаты и
не теряя очередь", но не "без пересчёта текущего видео".

Запуск (нужны ОБА процесса одновременно, в двух отдельных окнах/сессиях):
    python sar_worker.py
    python sar_server.py
"""

import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CFG = None
WATCH_DIR = DATA_DIR = DB_PATH = REPORTS_DIR = None


def get_db():
    return sar_common.get_db_connection(DB_PATH)


def recover_stale_processing_reports():
    """При старте воркера возвращает в очередь всё, что осталось в статусе
    'processing' с прошлого запуска (например, воркер был убит принудительно
    и не успел сам это исправить)."""
    conn = get_db()
    stale = conn.execute("SELECT report_id, rel_path FROM reports WHERE status='processing'").fetchall()
    if stale:
        conn.execute(
            "UPDATE reports SET status='queued', progress_pct=0, phase=NULL, error=NULL WHERE status='processing'")
        conn.commit()
        print(f"[startup] найдено {len(stale)} отчёт(ов), зависших в статусе 'processing' "
              f"с прошлого запуска — возвращены в очередь на повторную обработку:")
        for row in stale:
            print(f"    - {row['rel_path']} ({row['report_id']})")
    conn.close()


def _generate_thumbnail(abs_path, thumb_path, max_width=320, kind="video"):
    """JPEG-превью для списка файлов на главной странице: первый кадр для
    видео, уменьшенная копия для фото. cv2 -- ЛЕНИВЫЙ импорт (не на уровне
    модуля): sar_worker.py намеренно не тянет тяжёлые зависимости детектора
    при обычном старте (тот же принцип, что и с sar_video_review.py --
    см. main() ниже), импорт происходит только когда реально нужно
    сгенерировать превью."""
    try:
        import cv2
    except ImportError:
        return False
    if kind == "photo":
        # cv2.imread не понимает не-ASCII пути на Windows, поэтому читаем
        # файл сами и декодируем из байт -- имена снимков с дрона обычно
        # ASCII, но полагаться на это нельзя (папка операции может быть
        # названа по-русски)
        try:
            import numpy as np
            with open(abs_path, "rb") as f:
                data = np.frombuffer(f.read(), dtype=np.uint8)
            frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception:
            return False
        if frame is None:
            return False
        return _write_thumbnail(cv2, frame, thumb_path, max_width)

    cap = cv2.VideoCapture(abs_path)
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            return False
        return _write_thumbnail(cv2, frame, thumb_path, max_width)
    except Exception as e:
        print(f"[thumbnail] не удалось создать превью для {abs_path}: {e}")
        return False
    finally:
        cap.release()


def _write_thumbnail(cv2, frame, thumb_path, max_width):
    """Уменьшает кадр и атомарно пишет JPEG. Общий хвост для видео и фото."""
    try:
        h, w = frame.shape[:2]
        if w > max_width:
            scale = max_width / w
            frame = cv2.resize(frame, (max_width, max(1, int(h * scale))))
        # cv2.imwrite() определяет формат ПО РАСШИРЕНИЮ файла -- запись во
        # временный путь вида "*.jpg.tmp" ломается ("could not find a writer
        # for the specified extension"), поймано именно так на реальном видео.
        # cv2.imencode() кодирует в JPEG независимо от имени файла, дальше
        # обычная запись байт + os.replace -- тот же атомарный паттерн, что и
        # везде в проекте (см. flush_partial_detections в sar_video_review.py).
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return False
        tmp_path = thumb_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(buf.tobytes())
        os.replace(tmp_path, thumb_path)
        return True
    except Exception as e:
        print(f"[thumbnail] не удалось записать превью {thumb_path}: {e}")
        return False


def _ensure_thumbnail(name, abs_path, kind="video"):
    """Проверка дешёвая (пара os.path вызовов) -- вызывается на каждом
    скане для каждого файла, реальная генерация (cv2) происходит только
    один раз на файл, и заново -- если исходник на диске новее уже
    закэшированного превью (файл был перезаписан/переснят под тем же
    именем -- тот же класс ситуации, что и с "уехавшим" ctime выше)."""
    thumb_path = sar_common.get_thumbnail_path(DATA_DIR, name)
    try:
        src_mtime = os.path.getmtime(abs_path)
        needs_thumb = not os.path.exists(thumb_path) or os.path.getmtime(thumb_path) < src_mtime
    except OSError:
        needs_thumb = False
    if needs_thumb:
        _generate_thumbnail(abs_path, thumb_path, kind=kind)


def _read_video_duration(abs_path):
    """Длительность видео из МЕТАДАННЫХ контейнера (кадры не декодируются),
    либо None. Нужна, чтобы полоса покрытия ручного просмотра работала ДО
    обработки детектором: сам детектор пишет duration_sec только когда
    доходит до файла, а это могут быть часы ожидания в очереди -- при этом
    ручной плеер доступен сразу, и человек уже смотрит видео."""
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture(abs_path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if fps > 0 and frames > 0:
            return frames / fps
    except Exception:
        pass
    finally:
        cap.release()
    return None


def _ensure_duration(report_id, abs_path, current_duration):
    """Заполняет duration_sec один раз, если он ещё не известен. Дешёвый
    no-op на последующих сканах -- чтения метаданных не будет вовсе."""
    if current_duration:
        return
    duration = _read_video_duration(abs_path)
    if not duration:
        return
    conn = get_db()
    conn.execute("UPDATE reports SET duration_sec=? WHERE report_id=? AND duration_sec IS NULL",
                 (duration, report_id))
    conn.commit()
    conn.close()


def watcher_loop():
    while True:
        try:
            for name, abs_path, kind in sar_common.scan_all_materials(WATCH_DIR):
                conn = get_db()
                # Ищем существующую запись ПО ИМЕНИ ФАЙЛА (rel_path), а НЕ по
                # report_id, пересчитанному из текущего ctime. ctime у больших
                # файлов может не устояться между двумя сканами (файл ещё
                # копируется/дописывается при первом скане) -- тогда на втором
                # скане report_id получится ДРУГИМ, watcher решит, что это
                # "новый" файл, и поставит его в очередь ПОВТОРНО: то же самое
                # видео начинает обрабатываться (или уже обработалось) под
                # одним report_id, а воркер стартует заново под другим -- первая
                # запись становится осиротевшей и невидимой в /api/tree (см.
                # тот же фикс там). Поймано на реальных файлах в проде: видео
                # на 948 МБ и видео, изначально скопированное как 0 байт.
                row = conn.execute("SELECT report_id, duration_sec FROM reports WHERE rel_path=?",
                                    (name,)).fetchone()
                if row is None:
                    report_id = sar_common.make_report_id(name, abs_path)
                    now = datetime.now().isoformat()
                    out_dir = os.path.join(REPORTS_DIR, report_id)
                    file_ctime = sar_common.get_file_ctime(abs_path)
                    # при выключенной авто-обработке файл всё равно
                    # регистрируется и сразу доступен для РУЧНОГО просмотра,
                    # но модель по нему не запускается, пока человек не
                    # нажмёт "обработать" (см. auto_process в конфиге)
                    initial_status = "queued" if CFG.get("auto_process", True) else "idle"
                    conn.execute(
                        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
                        "out_dir, file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (report_id, name, abs_path, kind, initial_status, out_dir, file_ctime, now, now))
                    conn.commit()
                    where = "в очереди" if initial_status == "queued" else "без авто-обработки"
                    print(f"[watcher] новый файл ({where}): {name} -> {report_id}")
                else:
                    report_id = row["report_id"]

                # Файл, лежащий в папке операции, должен попадать в неё САМ.
                # Без этой привязки материалы находились и обрабатывались, но
                # висели в «Не разобрано»: папка операции не работала как
                # папка операции. Вызов идемпотентен и делается на каждом
                # проходе -- операцию могли завести уже ПОСЛЕ появления файлов.
                op_id = sar_common.attach_by_folder(conn, WATCH_DIR, report_id, name)
                if op_id is not None and row is None:
                    print(f"[watcher] {name} -> операция #{op_id}")
                conn.close()

                # превью нужно и видео, и фото -- список файлов показывает
                # его одинаково для обоих; длительность, разумеется, только
                # у видео
                _ensure_thumbnail(name, abs_path, kind=kind)
                if kind == "video":
                    _ensure_duration(report_id, abs_path,
                                      row["duration_sec"] if row is not None else None)
        except Exception as e:
            print(f"[watcher] ошибка сканирования: {e}")

        # Отметка "жив" -- ставится ПОСЛЕ обработки ошибки, а не вместо неё:
        # воркер, у которого падает сканирование, всё равно живой процесс,
        # и путать это с его смертью не надо. Мониторинг различает две беды
        # отдельно: молчащий воркер и растущая очередь при живом воркере.
        try:
            sar_common.touch_heartbeat(get_db(), "worker")
        except Exception as e:
            print(f"[watcher] не удалось отметиться: {e}")

        time.sleep(CFG["poll_interval_sec"])


# ---------------------------------------------------------------------------
# Запуск обработки — отдельным процессом на файл, стримим stdout в logs
# ---------------------------------------------------------------------------

PROGRESS_RE = re.compile(r"\.\.\.(\d+)%")
TQDM_RE = re.compile(r"(\d+)%\|")  # прогресс сторонних tqdm-баров (например, скачивание весов модели)
TOTALS_RE = re.compile(r"fps=([\d.]+)\s+total_frames=(\d+)")

# Реестр всех сейчас запущенных дочерних процессов обработки. Нужен, чтобы
# при остановке ВОРКЕРА (Ctrl+C, SIGTERM) явно прибить их — не полагаясь на
# то, что ОС сама решит убить детей вместе с родителем (это происходит не
# всегда: например, "Завершить задачу" в диспетчере задач без "завершить
# дерево" оставляет детей осиротевшими).
_active_processes_lock = threading.Lock()
_active_processes = set()


def _low_priority_kwargs():
    """Запускать детектор с ПОНИЖЕННЫМ приоритетом.

    Смысл: инференс -- работа фоновая и терпит задержку, а человек, который в
    это время листает кадры и смотрит видео, задержку замечает сразу. При
    равном приоритете планировщик ОС делит процессор поровну, и веб-сервис
    начинает подвисать ровно тогда, когда идёт обработка (поймано на реальной
    работе команды из 6 человек).

    Понижение приоритета не замедляет обработку, когда машина свободна: ядра
    всё равно достаются детектору. Оно лишь определяет, КТО уступит, когда
    ресурса не хватает на всех -- и уступать должен детектор.

    Резерв ядер (см. _limit_cpu_threads в sar_video_review.py) решает другую
    половину задачи: там ограничивается СКОЛЬКО ядер занимает инференс, здесь --
    насколько охотно он их отдаёт."""
    if os.name == "nt":
        # BELOW_NORMAL_PRIORITY_CLASS -- мягче, чем IDLE: на простое машины
        # детектор всё равно получает всё, но уступает интерактивной работе
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)}
    # POSIX: nice(+10) в дочернем процессе до exec
    return {"preexec_fn": lambda: os.nice(10)}


def _register_process(proc):
    with _active_processes_lock:
        _active_processes.add(proc)


def _unregister_process(proc):
    with _active_processes_lock:
        _active_processes.discard(proc)


def shutdown_all_children():
    with _active_processes_lock:
        procs = list(_active_processes)
    if not procs:
        return
    print(f"[shutdown] останавливаю {len(procs)} процесс(ов) обработки...")
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                print(f"[shutdown] процесс {proc.pid} не ответил на terminate(), убит принудительно")
            except OSError:
                pass
    print("[shutdown] все дочерние процессы остановлены")


def _handle_termination_signal(signum, frame):
    shutdown_all_children()
    sys.exit(0)


def append_log(report_id, line):
    conn = get_db()
    conn.execute("INSERT INTO logs (report_id, ts, line) VALUES (?,?,?)",
                 (report_id, datetime.now().isoformat(), line))
    conn.commit()
    conn.close()


def update_report(report_id, **fields):
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn = get_db()
    conn.execute(f"UPDATE reports SET {set_clause} WHERE report_id=?",
                 (*fields.values(), report_id))
    conn.commit()
    conn.close()


def run_one_report(report):
    report_id = report["report_id"]
    update_report(report_id, status="processing", progress_pct=0, phase="запуск")
    api_base = f"http://127.0.0.1:{CFG['port']}"

    # -u (unbuffered) — ОБЯЗАТЕЛЬНО: без него print() в дочернем процессе
    # буферизуется блоками, когда stdout не терминал (а у нас — пайп), и вывод
    # застревает внутри процесса вместо появления построчно у нас в логе.
    #
    # PYTHONIOENCODING=utf-8 — ОБЯЗАТЕЛЬНО на Windows: без этого дочерний
    # процесс пишет в stdout в кодировке консоли по умолчанию (cp1251/cp866),
    # а мы читаем как UTF-8 — получаются кракозябры на русском тексте.
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")

    if report["kind"] == "video":
        script = os.path.join(SCRIPT_DIR, "sar_video_review.py")
        cmd = [sys.executable, "-u", script, "--video", report["abs_path"], "--out", report["out_dir"],
               "--tracking-report-id", report_id, "--tracking-api-base", api_base]
    else:
        script = os.path.join(SCRIPT_DIR, "sar_photo_review.py")
        cmd = [sys.executable, "-u", script, "--photo", report["abs_path"], "--out", report["out_dir"],
               "--tracking-report-id", report_id, "--tracking-api-base", api_base]

    append_log(report_id, f"$ {' '.join(cmd)}")
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace",
                                 bufsize=1, cwd=SCRIPT_DIR, env=env,
                                 **_low_priority_kwargs())
        _register_process(proc)
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            append_log(report_id, line)

            m = PROGRESS_RE.search(line)
            if m:
                update_report(report_id, progress_pct=float(m.group(1)), phase="анализ кадров")
            else:
                m_tqdm = TQDM_RE.search(line)
                if m_tqdm:
                    update_report(report_id, progress_pct=float(m_tqdm.group(1)), phase="загрузка модели")

            m2 = TOTALS_RE.search(line)
            if m2:
                fps = float(m2.group(1))
                total_frames = int(m2.group(2))
                duration = total_frames / fps if fps > 0 else None
                update_report(report_id, fps=fps, total_frames=total_frames, duration_sec=duration)

        proc.wait()
        if proc.returncode == 0:
            update_report(report_id, status="done", progress_pct=100, phase="готово")
            append_log(report_id, "=== ГОТОВО ===")
        else:
            update_report(report_id, status="error", error=f"процесс завершился с кодом {proc.returncode}")
            append_log(report_id, f"=== ОШИБКА: код завершения {proc.returncode} ===")
    except Exception as e:
        update_report(report_id, status="error", error=str(e))
        append_log(report_id, f"=== ОШИБКА ЗАПУСКА: {e} ===")
    finally:
        if proc is not None:
            _unregister_process(proc)


def worker_loop(worker_idx):
    # ВАЖНО: весь цикл обёрнут в try/except. Раньше исключение здесь (например,
    # "database is locked"/"disk full" при заявке задачи или самом первом
    # update_report(status="processing") до старта try в run_one_report())
    # тихо убивало этот daemon-поток -- процесс продолжал жить, watcher_loop
    # продолжал ставить файлы в очередь, а обрабатывать их было уже некому,
    # и НИКАКОГО сообщения об этом нигде не появлялось. При workers=1 (дефолт)
    # это останавливало вообще всю обработку до ручного перезапуска воркера.
    while True:
        report = None
        try:
            conn = get_db()
            # ФОТО ВПЕРЁД ВИДЕО, дальше -- по времени добавления.
            #
            # Раньше очередь была строгим FIFO, и это выглядело как "система
            # не умеет работать с фото": фото обрабатывается за СЕКУНДЫ, но
            # вставало в хвост за видео, которых могло быть на десятки часов
            # счёта (на CPU одна минута видео -- это ~30 минут обработки).
            # Человек загружал снимки и не видел результата до следующего дня,
            # хотя всё работало.
            #
            # Фото не могут "заморить" видео: их обработка занимает секунды,
            # так что даже большая пачка снимков задержит очередь видео на
            # считанные минуты. Обратное -- неверно, поэтому приоритет
            # односторонний.
            row = conn.execute(
                "SELECT * FROM reports WHERE status='queued' "
                "ORDER BY CASE kind WHEN 'photo' THEN 0 ELSE 1 END, created_at LIMIT 1").fetchone()
            conn.close()
            if row is None:
                time.sleep(2)
                continue
            report = dict(row)
            conn = get_db()
            updated = conn.execute(
                "UPDATE reports SET status='processing' WHERE report_id=? AND status='queued'",
                (report["report_id"],)).rowcount
            conn.commit()
            conn.close()
            if not updated:
                continue  # другой воркер успел раньше
            print(f"[worker-{worker_idx}] обрабатываю: {report['rel_path']}")
            run_one_report(report)
        except Exception as e:
            print(f"[worker-{worker_idx}] НЕОЖИДАННАЯ ОШИБКА в цикле обработки "
                  f"({report['rel_path'] if report else '?'}): {e}")
            if report is not None:
                try:
                    update_report(report["report_id"], status="error", error=f"воркер: {e}")
                except Exception:
                    pass  # уже залогировали в консоль выше -- вторую ошибку БД проглатываем осознанно
            time.sleep(2)


def main():
    global CFG, WATCH_DIR, DATA_DIR, DB_PATH, REPORTS_DIR

    CFG, config_path = sar_common.load_server_config(SCRIPT_DIR)
    WATCH_DIR, DATA_DIR, DB_PATH, REPORTS_DIR = sar_common.resolve_paths(CFG["watch_dir"])

    sar_common.init_db(DB_PATH)
    recover_stale_processing_reports()

    atexit.register(shutdown_all_children)
    signal.signal(signal.SIGTERM, _handle_termination_signal)

    # создаём папку telemetry/ проактивно при старте, чтобы её можно было
    # сразу увидеть и начать класть туда SRT, не дожидаясь первого видео.
    # Читаем telemetry_dir напрямую из sar_config.json (без импорта
    # sar_video_review.py -- он тянет cv2/numpy, а sar_worker.py намеренно
    # не имеет прямой Python-зависимости от детектора, только subprocess).
    telemetry_dir_name = sar_common.DEFAULT_TELEMETRY_DIR_NAME
    config_json_path = os.path.join(SCRIPT_DIR, "sar_config.json")
    if os.path.exists(config_json_path):
        try:
            with open(config_json_path, "r", encoding="utf-8") as f:
                telemetry_dir_name = json.load(f).get("telemetry_dir", telemetry_dir_name)
        except (OSError, ValueError):
            pass  # не критично -- просто используем дефолтное имя папки
    telemetry_dir = sar_common.resolve_telemetry_dir(WATCH_DIR, telemetry_dir_name)

    print(f"[worker] слежу за папкой: {WATCH_DIR}")
    print(f"[worker] данные: {DATA_DIR}")
    print(f"[worker] телеметрия: {telemetry_dir}")
    print(f"[worker] воркеров обработки: {max(1, CFG['workers'])}")

    threading.Thread(target=watcher_loop, daemon=True).start()
    threads = [threading.Thread(target=worker_loop, args=(i,), daemon=True)
               for i in range(max(1, CFG["workers"]))]
    for t in threads:
        t.start()

    try:
        # основной поток просто спит вечно -- вся работа в daemon-потоках
        # выше; нужен, чтобы процесс не завершился сразу и чтобы Ctrl+C
        # (KeyboardInterrupt) можно было поймать в главном потоке
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[shutdown] получен Ctrl+C, останавливаюсь...")
    finally:
        shutdown_all_children()


if __name__ == "__main__":
    main()
