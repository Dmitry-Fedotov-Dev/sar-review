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


REMOTE_THUMB_TIMEOUT_SEC = 120


def _generate_thumbnail_remote(url, headers, thumb_path, max_width=320):
    """Превью прямо из облака, БЕЗ скачивания файла целиком.

    Зачем. Облачная запись без превью -- пустой квадрат в списке, и так
    выглядят все файлы, которые ещё не готовили. На 150 записях это
    страница, по которой нельзя ориентироваться глазами. Качать ради
    одного кадра 600 МБ незачем: ffmpeg умеет читать по HTTP и берёт
    ровно то, что нужно -- индекс (у DJI он в конце файла) и начало
    первого кадра. Это единицы мегабайт вместо сотен.

    Почему ffmpeg, а не cv2: cv2.VideoCapture заголовок Authorization
    передать не умеет, а без него Google отвечает отказом.

    ОГОВОРКА ПРО ТОКЕН. Он уходит в командную строку ffmpeg, а её видно
    в списке процессов. На машине с одним пользователем это приемлемо,
    тем более что токен Google живёт час. У Яндекса вопроса нет вовсе:
    он отдаёт разовую ссылку, в которой авторизация уже внутри, и
    заголовок не нужен.
    """
    if not _ffmpeg_available():
        return False

    cmd = ["ffmpeg", "-y", "-v", "error"]
    if headers:
        # ffmpeg ждёт заголовки одной строкой, разделённые CRLF, и
        # завершающий перевод строки обязателен -- без него последний
        # заголовок молча не доедет.
        blob = "".join("%s: %s\r\n" % (k, v) for k, v in headers.items())
        cmd += ["-headers", blob]
    cmd += [
        "-i", url,
        "-frames:v", "1",
        "-vf", "scale=%d:-2" % int(max_width),
        "-f", "image2",
    ]

    tmp = thumb_path + ".part.jpg"
    os.makedirs(os.path.dirname(thumb_path) or ".", exist_ok=True)
    cmd.append(tmp)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=REMOTE_THUMB_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        # Внешняя команда без предела однажды уже подвесила сторож
        # туннеля на восемь суток. Здесь предел есть, и молчать о нём
        # нельзя: иначе превью просто не появляется без объяснений.
        print("[превью] чтение из облака не уложилось в %d с"
              % REMOTE_THUMB_TIMEOUT_SEC, flush=True)
        _rm_quietly(tmp)
        return False
    except OSError as e:
        print("[превью] ffmpeg не запустился: %s" % e, flush=True)
        return False

    if p.returncode != 0 or not os.path.exists(tmp) or not os.path.getsize(tmp):
        err = (p.stderr or "").strip().splitlines()
        print("[превью] из облака не вышло: %s"
              % (err[-1][:160] if err else "код %d" % p.returncode), flush=True)
        _rm_quietly(tmp)
        return False

    # Атомарная публикация -- тот же приём, что и везде в проекте: иначе
    # оборванная запись оставит файл нормального вида, но битый, и он
    # будет считаться готовым превью.
    os.replace(tmp, thumb_path)
    return True


def _rm_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass        # нечего убирать -- это норма, а не беда


def _write_thumbnail(cv2, frame, thumb_path, max_width, quality=80):
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
        ok, buf = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
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


# --- превью находок --------------------------------------------------------
#
# У ручной пометки нет готовой картинки: человек обвёл область прямо на
# проигрываемом видео, и на диске остались только таймкод и координаты
# рамки. Чтобы находку было видно в списке, кадр надо вырезать из видео и
# нарисовать на нём рамку.
#
# Делает это ВОРКЕР, а не сервер. Открыть видео и перемотать к нужной
# секунде -- обработка, а sar_server.py по устройству проекта только читает
# готовые файлы. Кроме того, мы только что вложились в скорость списка, и
# класть в веб-слой распаковку кадров означало бы всё это обнулить.
#
# За один проход делаем не больше нескольких штук: цикл наблюдения не
# должен вставать из-за того, что кто-то за раз наставил полсотни пометок.
FINDING_PREVIEWS_PER_PASS = 5
FINDING_PREVIEW_WIDTH = 960
# Крупный кадр для окна предпросмотра и страницы кадра. 2560 px выбраны по
# делу: окно предпросмотра увеличивает до 8 крат, при ширине окна около
# 380 px это ~3000 px отрисовки. На 960 px кадр в этот момент растягивался
# втрое и превращался в кашу -- ровно та жалоба, с которой начали.
FINDING_PREVIEW_FULL_WIDTH = 2560
FINDING_PREVIEW_FULL_QUALITY = 88


def _generate_finding_preview(abs_path, seconds, bbox, out_path, out_full_path=None):
    try:
        import cv2
    except ImportError:
        return False
    cap = cv2.VideoCapture(abs_path)
    try:
        # Перемотка по миллисекундам, а не по номеру кадра: частота кадров у
        # разных бортов разная, а таймкод пометки хранится в секундах.
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(seconds)) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            # перемотка не удалась (битый индекс) -- берём хотя бы первый кадр
            cap.set(cv2.CAP_PROP_POS_MSEC, 0)
            ok, frame = cap.read()
            if not ok or frame is None:
                return False
        # ОБА кадра пишутся чистыми, без рамки.
        #
        # Раньше в мелкий кадр рамка впечатывалась. При увеличении она
        # растягивалась вместе с пикселями и рассыпалась, а поверх неё
        # рисовалась ещё и SVG-рамка -- получалась двойная жирная линия,
        # причём одна половина размытая. Рамку теперь рисует только
        # интерфейс: она остаётся чёткой на любом масштабе и её можно
        # выключить, чтобы посмотреть на участок без подсказки.
        if out_full_path:
            _write_thumbnail(cv2, frame, out_full_path,
                             FINDING_PREVIEW_FULL_WIDTH,
                             quality=FINDING_PREVIEW_FULL_QUALITY)
        return _write_thumbnail(cv2, frame, out_path, FINDING_PREVIEW_WIDTH)
    except Exception as e:
        print(f"[находки] не удалось вырезать кадр {abs_path} @{seconds}: {e}")
        return False
    finally:
        cap.release()


# --- лёгкая копия видео для плеера -----------------------------------------
#
# Съёмка идёт на 30 Мбит/с: чтобы смотреть её в реальном времени, столько же
# нужно КАЖДОМУ зрителю через один канал наружу. Копия при том же разрешении
# весит примерно вчетверо меньше.
#
# Разрешение не понижается намеренно -- см. proxy_video в sar_common.
# Оригинал не трогаем: по нему работает детектор, и он же доступен в плеере,
# когда нужно разглядеть вплотную.
#
# Делает это воркер, а не сервер: перекодирование -- обработка, а
# sar_server.py по устройству проекта только отдаёт готовые файлы.
PROXIES_PER_PASS = 1        # одна копия за проход: каждая занимает минуты


def _ffmpeg_available():
    from shutil import which
    return which("ffmpeg") is not None


def _build_proxy(abs_path, out_path, crf, preset, threads):
    """Собрать лёгкую копию. Возвращает True, если получилось."""
    tmp = out_path + ".tmp.mp4"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", abs_path,
        # ТОЛЬКО основной видеопоток: в файлах с дрона рядом лежат
        # служебный поток и мелкая mjpeg-превьюшка, и без явного выбора
        # ffmpeg тащит их за собой.
        "-map", "0:v:0",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
        # Индекс в НАЧАЛО файла. У исходников с дрона он в конце, и браузер
        # вынужден сначала тянуть хвост, прежде чем сможет начать играть.
        "-movflags", "+faststart",
        # звука в этих файлах нет вовсе -- проверено ffprobe
        "-an",
        tmp,
    ]
    # Ждём НЕ блокирующим subprocess.run, а опросом.
    #
    # Кодирование занимает минуты, а отметка "воркер жив" ставится в конце
    # прохода цикла наблюдения. Простое ожидание означало бы многоминутную
    # паузу в отметках -- и мониторинг честно доложил бы, что воркер умер,
    # хотя он занят делом. Поэтому пока ffmpeg работает, продолжаем
    # отмечаться.
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                **_low_priority_kwargs())
    except Exception as e:                                  # noqa: BLE001
        print(f"[копия] не удалось запустить ffmpeg: {e}")
        _remove_quietly(tmp)
        return False

    deadline = time.time() + 3 * 3600
    while proc.poll() is None:
        if time.time() > deadline:
            proc.kill()
            print("[копия] ffmpeg не уложился в отведённое время, прерываю")
            _remove_quietly(tmp)
            return False
        try:
            sar_common.touch_heartbeat(get_db(), "worker")
        except Exception:                                   # noqa: BLE001
            pass
        time.sleep(5)

    if proc.returncode != 0 or not os.path.exists(tmp):
        err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:300]
        print(f"[копия] ffmpeg вернул {proc.returncode}: {err}")
        _remove_quietly(tmp)
        return False
    # Готовый файл появляется одним движением: пока идёт кодирование, его
    # не должно быть видно ни серверу, ни этой же функции на следующем
    # проходе -- иначе отдадим зрителю обрубок.
    os.replace(tmp, out_path)
    return True


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def ensure_requested_photos():
    """Скачивает снимки, которые попросили подготовить.

    Отдельно от видео: лёгкой копии у снимка нет и не нужно -- он весит
    единицы мегабайт. Достаточно положить его во временную папку, откуда
    его увидят и просмотр, и генерация превью
    (см. sar_common.find_material_file).

    Не закрепляем: снимок читается мгновенно, и держать его от вытеснения
    незачем -- в отличие от видео, которое детектор жуёт часами.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT report_id, rel_path, cloud_file_id, cloud_size FROM reports "
            "WHERE kind='photo' AND cloud_file_id IS NOT NULL "
            "AND proxy_requested=1 LIMIT 5").fetchall()
    finally:
        conn.close()
    if not rows:
        return 0

    fetcher = get_fetcher()
    if fetcher is None:
        return 0
    done = 0
    for r in rows:
        # Тот же backoff, что и у превью: протухший токен или исчерпанная
        # квота отказывают одинаково на каждой попытке, и повтор раз в 15
        # секунд только жжёт лимиты облака и топит журнал.
        key = "fetch:" + r["rel_path"]
        if not _may_try(key):
            continue
        try:
            path = fetcher.ensure_local(r["rel_path"], file_id=r["cloud_file_id"],
                                         expected_size=r["cloud_size"] or 0,
                                         pin=False)
        except Exception as e:
            _note_failure(key, "облако")
            print(f"[облако] снимок {r['rel_path']} отложен: {e}", flush=True)
            continue
        _note_success(key)
        _ensure_thumbnail(r["rel_path"], path, kind="photo")
        c2 = get_db()
        c2.execute("UPDATE reports SET proxy_requested=0 WHERE report_id=?",
                   (r["report_id"],))
        c2.commit()
        c2.close()
        done += 1
        print(f"[облако] снимок готов: {r['rel_path']}", flush=True)
    return done


# --- разбор телеметрии: трек дрона ----------------------------------------
#
# Читает SRT и складывает в базу готовый трек. Раньше это считалось в
# веб-слое по запросу: каждый холодный запрос разбирал 114 файлов (2,9 с), а
# кеш жил в памяти процесса и умирал при перезапуске сервера. Плюс это прямо
# противоречило устройству проекта -- сервер только читает, обрабатывает
# воркер.
#
# Разбор одноразовый: результат лежит в telemetry_tracks, и повторяется
# только если SRT заменили (сверяем mtime).

TRACK_MAX_POINTS = 400          # точек на трек после прореживания
TRACKS_PER_PASS = 8             # сколько видео разбирать за проход

_telemetry_index = None


def _telemetry_index_for_worker():
    """Индекс папки телеметрии. Строится один раз на запуск."""
    global _telemetry_index
    if _telemetry_index is None:
        name = CFG.get("telemetry_dir", sar_common.DEFAULT_TELEMETRY_DIR_NAME)
        _telemetry_index = sar_common.build_telemetry_index(
            sar_common.resolve_telemetry_dir(WATCH_DIR, name))
    return _telemetry_index


def _srt_for_video(abs_path):
    """Где телеметрия этого видео. Сначала рядом, потом в общей папке."""
    if not abs_path:
        return None
    side = os.path.splitext(abs_path)[0] + ".srt"
    if os.path.exists(side):
        return side
    match, _reason = sar_common.find_telemetry_for_video(
        abs_path, _telemetry_index_for_worker())
    return str(match) if match else None


def _thin_track(points, limit=TRACK_MAX_POINTS):
    """Прореживает равномерно, но КОНЕЦ маршрута сохраняет всегда.

    В SRT по точке на кадр -- десятки тысяч на видео. На карте разница
    неразличима, а ответ меньше в сотни раз. Обрубив последнюю точку, мы
    показали бы, что дрон не долетел туда, куда долетел.
    """
    if len(points) <= limit:
        return list(points)
    # РОВНОЕ распределение по индексам, а не "каждая N-я".
    #
    # Деление нацело давало 419 точек при потолке 400 -- потолок протекал.
    # Округление вверх чинило это, но платило половиной точек на треках
    # чуть длиннее потолка: 486 превращались в 243 без всякой нужды.
    # Здесь на выходе РОВНО limit точек, распределённых равномерно, и
    # первая с последней на месте всегда: обрубив конец, мы показали бы,
    # что дрон не долетел туда, куда долетел.
    last = len(points) - 1
    return [points[round(i * last / (limit - 1))] for i in range(limit)]


def _store_track(conn, report_id, points, raw_count, first_sec, last_sec,
                 source, mtime):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    conn.execute(
        "INSERT OR REPLACE INTO telemetry_tracks (report_id, points, "
        "raw_points, first_sec, last_sec, min_lat, max_lat, min_lon, max_lon, "
        "source, source_mtime, parsed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (report_id, json.dumps(points), raw_count, first_sec, last_sec,
         min(lats) if lats else None, max(lats) if lats else None,
         min(lons) if lons else None, max(lons) if lons else None,
         source, mtime))
    conn.commit()


def ensure_telemetry_tracks():
    """Разобрать телеметрию тем видео, у которых трека ещё нет."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT r.report_id, r.rel_path, t.source_mtime "
            "FROM reports r LEFT JOIN telemetry_tracks t "
            "  ON t.report_id = r.report_id "
            "WHERE r.kind='video'").fetchall()
    finally:
        conn.close()

    done = 0
    for row in rows:
        if done >= TRACKS_PER_PASS:
            break
        rel = row["rel_path"]
        abs_path = sar_common.material_path(WATCH_DIR, rel)
        srt = _srt_for_video(abs_path)

        mtime = os.path.getmtime(srt) if srt and os.path.exists(srt) else None
        if row["source_mtime"] is not None and row["source_mtime"] == (mtime or 0):
            continue            # уже разобрано, файл не менялся
        if row["source_mtime"] is not None and srt is None:
            continue            # уже отмечено «телеметрии нет»

        key = "track:" + rel
        if not _may_try(key):
            continue

        c2 = get_db()
        try:
            if srt is None:
                # ОТМЕЧАЕМ, что смотрели и не нашли. Без этой записи 95 видео
                # без телеметрии разбирались бы заново каждый проход, вечно.
                _store_track(c2, row["report_id"], [], 0, None, None, None, 0)
                done += 1
                continue
            gps_order = CFG.get("srt_gps_tuple_order", "lat_lon")
            entries = _parse_srt(srt, gps_order)
            pts = [(e[0], e[2].get("lat"), e[2].get("lon")) for e in entries
                   if e[2].get("lat") is not None and e[2].get("lon") is not None]
            thin = _thin_track([[p[1], p[2]] for p in pts])
            _store_track(c2, row["report_id"], thin, len(pts),
                         pts[0][0] if pts else None,
                         entries[-1][1] if entries else None,
                         os.path.basename(srt), mtime or 0)
            _note_success(key)
            done += 1
            if thin:
                print(f"[трек] {os.path.basename(rel)}: {len(pts)} точек "
                      f"-> {len(thin)}", flush=True)
        except Exception as e:                          # noqa: BLE001
            # Битый SRT не должен лишать трека остальные видео.
            _note_failure(key, "трек")
            print(f"[трек] {os.path.basename(rel)}: не разобрался: {e}",
                  flush=True)
        finally:
            c2.close()
    return done


def _parse_srt(srt_path, gps_order):
    """Ленивый импорт разбора: sar_worker намеренно не тянет зависимости
    детектора при обычном старте (тот же принцип, что и с cv2 в превью)."""
    from sar_video_review import parse_srt_telemetry
    return parse_srt_telemetry(srt_path, gps_tuple_order=gps_order)


# --- превью облачного материала: фоном, не дожидаясь скачивания -----------
#
# Почему отдельным потоком, а не в общем проходе. Сборка лёгкой копии
# занимает минуты, и проход стоит на ней. Превью же -- это несколько
# мегабайт по сети и доля секунды процессора: класть их в ту же очередь
# значит растянуть полторы сотни превью на часы, хотя мешать друг другу им
# нечем. Одно упирается в процессор, другое в сеть.
#
# Работает и для снимков: ffmpeg читает удалённый JPEG ровно так же, как
# видео, -- отдельного пути для фотографий не нужно.
_rthumbs = {"thread": None}

REMOTE_THUMBS_PER_RUN = 12      # сколько делать за один заход потока


def _remote_thumbs_pass(limit=REMOTE_THUMBS_PER_RUN):
    """Делает превью тем облачным записям, у которых его ещё нет."""
    fetcher = get_fetcher()
    if fetcher is None:
        return 0
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT rel_path, kind, cloud_file_id FROM reports "
            "WHERE cloud_file_id IS NOT NULL").fetchall()
        conn.close()
    except Exception as e:                              # noqa: BLE001
        print(f"[превью] список не прочитался: {e}", flush=True)
        return 0

    made = 0
    for r in rows:
        if made >= limit:
            break
        rel = r["rel_path"]
        thumb = sar_common.get_thumbnail_path(DATA_DIR, rel)
        if os.path.exists(thumb):
            continue
        if sar_common.find_material_file(WATCH_DIR, DATA_DIR, rel):
            continue        # файл рядом -- обычный путь дешевле сетевого
        key = "rthumb:" + rel
        if not _may_try(key):
            continue
        try:
            url, headers = fetcher.stream_source(rel, file_id=r["cloud_file_id"])
        except Exception as e:                          # noqa: BLE001
            _note_failure(key, "превью")
            print(f"[превью] {os.path.basename(rel)}: адрес не получен: {e}",
                  flush=True)
            continue
        if not url:
            continue
        if _generate_thumbnail_remote(url, headers, thumb):
            _note_success(key)
            made += 1
        else:
            _note_failure(key, "превью")
    return made


def _remote_thumbs_worker():
    """Работает, ПОКА ЕСТЬ ЧТО ДЕЛАТЬ, а не одну пачку.

    Первая версия делала REMOTE_THUMBS_PER_RUN штук и выходила, полагаясь
    на то, что следующий проход запустит её снова. Но проход стоит на
    сборке лёгкой копии -- это минуты, -- и выходило 0,5 превью в минуту:
    полтораста записей растянулись бы на четыре часа. Пачка нужна не для
    того, чтобы уступать место (уступать некому: сеть и процессор заняты
    разными работами), а чтобы не держать соединение с базой открытым
    всё время.
    """
    total = 0
    try:
        while True:
            n = _remote_thumbs_pass()
            total += n
            if n == 0:
                break
    except Exception as e:                              # noqa: BLE001
        # Тихо упавший поток выглядит как «превью просто не делаются», и
        # искать причину пришлось бы по косвенным признакам.
        print(f"[превью] фоновый проход сорвался: {e}", flush=True)
    if total:
        print(f"[превью] из облака сделано {total}", flush=True)


def _start_remote_thumbs():
    th = _rthumbs["thread"]
    if th is not None and th.is_alive():
        return
    th = threading.Thread(target=_remote_thumbs_worker, name="rthumbs",
                          daemon=True)
    _rthumbs["thread"] = th
    th.start()


# --- конвейер: качаем следующий, пока кодируется текущий ------------------
#
# Скачивание и кодирование делят между собой только диск, а не процессор и
# не канал -- то есть ждать друг друга им незачем. Пока ждали, подготовка
# всего облачного материала стоила сумму: 3,7 ч закачки ПЛЮС 3,1 ч
# кодирования. Перекрыв их, платим только за большее из двух.
#
# Больше одного файла вперёд не качаем: выигрыш даёт уже первый (кодирование
# и закачка сопоставимы по длительности), а каждый следующий -- это ещё
# несколько гигабайт во временной папке под тем же потолком.
_prefetch = {"thread": None, "name": None}


def _prefetch_worker(name, file_id, size):
    fetcher = get_fetcher()
    if fetcher is None:
        return
    key = "fetch:" + name
    try:
        fetcher.ensure_local(name, file_id=file_id, expected_size=size)
        _note_success(key)
    except Exception as e:
        # Заранее -- значит необязательно: место кончилось или облако
        # отказало. Основной путь попробует сам и объяснит человеку.
        # Молчать нельзя: иначе непонятно, почему конвейер не ускоряет.
        _note_failure(key, "облако")
        print(f"[конвейер] {os.path.basename(name)} заранее не скачался: {e}",
              flush=True)


def _start_prefetch(cloud_jobs, busy_name):
    """Ставит в фон закачку следующего попрошенного файла."""
    th = _prefetch["thread"]
    if th is not None and th.is_alive():
        return
    fetcher = get_fetcher()
    if fetcher is None:
        return
    for name, cloud in cloud_jobs:
        if name == busy_name:
            continue
        if os.path.exists(sar_common.proxy_video_path(DATA_DIR, name)):
            continue
        if not _may_try("fetch:" + name):
            continue
        th = threading.Thread(target=_prefetch_worker,
                              args=(name, cloud[0], cloud[1]),
                              name="prefetch", daemon=True)
        _prefetch["thread"] = th
        _prefetch["name"] = name
        th.start()
        return


def ensure_video_proxies(conn):
    """Догенерировать недостающие лёгкие копии."""
    if not CFG.get("proxy_video", True):
        return 0
    if not _ffmpeg_available():
        return 0

    crf = int(CFG.get("proxy_crf", 26))
    preset = str(CFG.get("proxy_preset", "veryfast"))
    threads = int(CFG.get("proxy_threads", 4))

    # ЧТО БЕРЁМ В РАБОТУ.
    #
    # Локальный материал -- как раньше: всё, у чего копии ещё нет.
    #
    # Облачный -- ТОЛЬКО по явной просьбе человека (proxy_requested). Само
    # ничего не качается: на боевом подключении это 150 файлов и около
    # 88 ГБ, и начинать такое молча нельзя. Человек открывает материал,
    # видит «в облаке, не готов» и нажимает подготовить -- тогда файл
    # скачивается, из него собирается лёгкая копия, а сам оригинал дальше
    # не нужен и может быть вытеснен.
    jobs = [(name, abs_path, None)
            for name, abs_path, kind in sar_common.scan_all_materials(WATCH_DIR)
            if kind == "video" and os.path.exists(abs_path)]

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT rel_path, cloud_file_id, cloud_size FROM reports "
            "WHERE kind='video' AND cloud_file_id IS NOT NULL "
            "AND proxy_requested=1").fetchall()
    finally:
        conn.close()
    cloud_jobs = [(r["rel_path"], (r["cloud_file_id"], r["cloud_size"] or 0))
                  for r in rows]
    jobs += [(name, None, cloud) for name, cloud in cloud_jobs]

    made = 0
    for name, abs_path, cloud in jobs:
        if made >= PROXIES_PER_PASS:
            break
        out_path = sar_common.proxy_video_path(DATA_DIR, name)
        if os.path.exists(out_path):
            continue
        short = os.path.basename(name)

        # НЕУДАЧНАЯ СБОРКА НЕ ДОЛЖНА ЗАНИМАТЬ СЛОТ ВЕЧНО.
        #
        # Копий делается одна за проход (PROXIES_PER_PASS). Файл, который
        # не собирается, копии не получает -- значит следующий проход
        # выберет его снова, и снова, и очередь из восьмидесяти файлов
        # встанет намертво за одним битым. Ровно это и случилось
        # 17.09.2026: в облаке нашёлся МР4, залитый не до конца (Google
        # отдаёт 593 МБ, а внутри заявлено 3,44 ГБ), ffmpeg на нём падает
        # с «moov atom not found», и дальше него дело не шло.
        build_key = "proxy:" + name
        if not _may_try(build_key):
            continue

        fetched = None
        if cloud:
            fetcher = get_fetcher()
            if fetcher is None:
                continue
            key = "fetch:" + name
            if not _may_try(key):
                continue
            try:
                abs_path = fetcher.ensure_local(name, file_id=cloud[0],
                                                 expected_size=cloud[1])
                fetched = name
                _note_success(key)
            except Exception as e:
                # Отказ ограничителя -- не ошибка материала. Просьба
                # остаётся в силе, попробуем позже и с нарастающей паузой:
                # протухший токен отказывает одинаково каждый раз.
                _note_failure(key, "облако")
                print(f"[копия] {short} отложен: {e}", flush=True)
                continue

        # Файл на диске, впереди минуты кодирования -- самое время занять
        # простаивающий канал следующим.
        if cloud_jobs:
            _start_prefetch(cloud_jobs, name)

        print(f"[копия] делаю лёгкую копию {short} (crf {crf}, {preset})")
        built = _build_proxy(abs_path, out_path, crf, preset, threads)
        if fetched:
            f = get_fetcher()
            if f is not None:
                f.release(fetched)
        if not built:
            # Причину пишем в саму запись материала, а не только в журнал:
            # иначе страница файла говорит «в очереди» и молчит, а человек
            # ждёт копию, которой не будет никогда.
            n = _note_failure(build_key, "копия")
            if n >= FAILURE_GIVE_UP_AFTER:
                c3 = get_db()
                c3.execute(
                    "UPDATE reports SET error=? WHERE rel_path=?",
                    ("лёгкая копия не собирается: ffmpeg не может прочитать "
                     "исходник. Скорее всего, файл в хранилище залит не "
                     "полностью.", name))
                c3.commit()
                c3.close()
            continue

        _note_success(build_key)
        if built:
            was = os.path.getsize(abs_path) / 1e6
            now = os.path.getsize(out_path) / 1e6
            print(f"[копия] {short}: {was:.0f} МБ -> {now:.0f} МБ "
                  f"(в {was / max(now, 0.1):.1f} раза меньше)")
            made += 1
            if cloud:
                # Просьба выполнена -- снимаем, чтобы не переделывать.
                c2 = get_db()
                c2.execute("UPDATE reports SET proxy_requested=0 "
                           "WHERE rel_path=?", (name,))
                c2.commit()
                c2.close()
    return made


def ensure_finding_previews(conn):
    """Догенерировать недостающие превью ручных пометок."""
    try:
        rows = conn.execute(
            "SELECT o.id, o.timestamp_sec, o.bbox, r.rel_path, r.kind "
            "FROM manual_observations o "
            "JOIN reports r ON r.report_id = o.report_id "
            "ORDER BY o.id DESC").fetchall()
    except Exception as e:
        print(f"[находки] не удалось прочитать пометки: {e}")
        return 0

    made = 0
    for row in rows:
        if made >= FINDING_PREVIEWS_PER_PASS:
            break
        out_path = sar_common.finding_preview_path(DATA_DIR, row["id"])
        out_full = sar_common.finding_preview_path(DATA_DIR, row["id"], full=True)
        # Готово только когда есть ОБА файла: у находок, снятых до появления
        # крупного кадра, мелкий уже лежит, и проверка "существует ли
        # out_path" молча оставила бы их без полноразмерного навсегда.
        if os.path.exists(out_path) and os.path.exists(out_full):
            continue
        # ОТКУДА РЕЗАТЬ КАДР.
        #
        # Раньше здесь брался только оригинал в наблюдаемой папке -- и
        # облачная пометка молча оставалась без превью НАВСЕГДА: у такого
        # материала оригинала на диске нет по определению. В списке находок
        # это пустой квадрат вместо кадра, то есть находку не отличить от
        # соседней, не открыв её.
        #
        # Порядок: оригинал (он же во временной папке, если скачан), потом
        # лёгкая копия. Копия -- тот же материал с теми же таймкодами,
        # перекодированный; для кадра размером с ноготь разницы нет.
        #
        # Это ЧЕТВЁРТЫЙ случай одной и той же ошибки: вопрос «где файл»
        # задаётся диску, а облако на него не отвечает. См. CLAUDE.md.
        abs_path = sar_common.find_material_file(
            WATCH_DIR, DATA_DIR, row["rel_path"])
        if not abs_path:
            proxy = sar_common.proxy_video_path(DATA_DIR, row["rel_path"])
            abs_path = proxy if os.path.exists(proxy) else None
        if not abs_path:
            continue        # резать пока нечего -- это не ошибка, а «рано»
        try:
            bbox = json.loads(row["bbox"]) if row["bbox"] else None
        except (ValueError, TypeError):
            bbox = None
        seconds = row["timestamp_sec"] or 0
        if row["kind"] == "photo":
            # у фото таймкода нет -- кадр это сам снимок
            seconds = 0
        if _generate_finding_preview(abs_path, seconds, bbox, out_path, out_full):
            made += 1
            print(f"[находки] превью пометки #{row['id']} готово")
    return made


# ---------------------------------------------------------------------------
# ПОВТОРЫ ПОСЛЕ НЕУДАЧИ
#
# _ensure_thumbnail и _ensure_duration вызываются для КАЖДОГО файла на
# КАЖДОМ проходе наблюдения, а проход идёт раз в poll_interval_sec (15
# секунд по умолчанию). Признак "надо сделать" -- отсутствие файла превью
# на диске. Значит сорвавшаяся генерация повторяется НА СЛЕДУЮЩЕМ ПРОХОДЕ.
# И на следующем. И так вечно: 240 попыток в час на один битый файл.
#
# На локальном диске это почти безобидно -- лишнее чтение битого файла.
# Но как только материал переедет в облако, каждая попытка станет
# скачиванием, и один проблемный файл будет молча выжигать квоту и канал,
# мешая работать всем остальным.
#
# Состояние держим в памяти процесса, а не в базе. Перезапуск воркера --
# это осмысленный сигнал "попробовать заново" (обычно после того, как
# человек что-то починил), и терять историю неудач при нём правильно.
# ---------------------------------------------------------------------------

# Сколько файлов трогаем за проход -- НАСТРОЙКА, а не константа: см.
# material_touches_per_pass в sar_common.SETTINGS_SCHEMA. Держать здесь
# второе умолчание нельзя -- два значения одного смысла рано или поздно
# разойдутся, и выяснится это на боевой операции.
#
# Сам приём в проекте не нов: PROXIES_PER_PASS = 1 и
# FINDING_PREVIEWS_PER_PASS = 5 существуют давно, здесь он лишь применён к
# двум местам, где про него забыли. Очередь не теряется: непокрытые файлы
# обработаются на следующих проходах.

FAILURE_FIRST_DELAY_SEC = 60
FAILURE_MAX_DELAY_SEC = 3600
FAILURE_GIVE_UP_AFTER = 8

# ключ -> (сколько_неудач, когда_можно_пробовать_снова)
_failures = {}
_failures_lock = threading.Lock()


# Считает РЕАЛЬНЫЕ обращения к файлу материала, а не вызовы функций:
# дешёвая проверка "превью уже есть" бюджет тратить не должна, иначе за
# проход мы будем осматривать три файла вместо всей папки.
_touches_used = 0


def _note_touch():
    global _touches_used
    _touches_used += 1


def _may_try(key):
    """Пора ли пробовать снова. Для нового ключа -- да."""
    with _failures_lock:
        rec = _failures.get(key)
    if rec is None:
        return True
    count, not_before = rec
    if count >= FAILURE_GIVE_UP_AFTER:
        return False
    return time.time() >= not_before


def _note_failure(key, what):
    """Запоминает неудачу и отодвигает следующую попытку."""
    with _failures_lock:
        count = _failures.get(key, (0, 0))[0] + 1
        delay = min(FAILURE_FIRST_DELAY_SEC * (2 ** (count - 1)),
                    FAILURE_MAX_DELAY_SEC)
        _failures[key] = (count, time.time() + delay)
    if count == FAILURE_GIVE_UP_AFTER:
        # Сообщаем РОВНО ОДИН раз -- на попытке, после которой сдаёмся.
        # Молчать совсем нельзя: файл так и останется без превью, и это
        # должно быть видно, а не выясняться через неделю.
        print(f"[{what}] {key}: не вышло {count} раз подряд, больше не пробую "
              f"до перезапуска воркера", flush=True)
    return count


def _note_success(key):
    with _failures_lock:
        _failures.pop(key, None)


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
        key = "thumb:" + name
        if not _may_try(key):
            return
        _note_touch()
        if _generate_thumbnail(abs_path, thumb_path, kind=kind):
            _note_success(key)
        else:
            _note_failure(key, "превью")


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
    key = "duration:" + report_id
    if not _may_try(key):
        return
    _note_touch()
    duration = _read_video_duration(abs_path)
    if not duration:
        # Длительность не прочиталась: битый файл, недокачанный, или
        # контейнер её не сообщает. Повторять каждые 15 секунд бессмысленно
        # в любом из случаев.
        _note_failure(key, "длительность")
        return
    _note_success(key)
    conn = get_db()
    conn.execute("UPDATE reports SET duration_sec=? WHERE report_id=? AND duration_sec IS NULL",
                 (duration, report_id))
    conn.commit()
    conn.close()


DURATIONS_PER_PASS = 12


def ensure_cloud_durations():
    """Длительность облачного видео -- из ЛЁГКОЙ КОПИИ.

    ЗАЧЕМ. `_ensure_duration` читает оригинал, а у облачного материала его
    на диске нет: обход папки такие записи не видит вовсе. В итоге из 114
    видео операции длительность была известна у 32.

    И это не косметика. «Отснято» и «просмотрено» в шапке операции
    складываются ТОЛЬКО по видео с известной длительностью -- остальные не
    участвуют ни в числителе, ни в знаменателе. Экран показывал
    «просмотрено 1 ч 21 мин из 1 ч 34 мин», то есть 86%, хотя это 86% от
    28% материала: 80 видео из 114 не открывал никто. Покрытие было
    завышено втрое, и по нему можно было решить, что смотреть больше
    нечего.

    Лёгкая копия годится: это перекодированный тот же материал, секунды
    те же, и лежит она локально.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT report_id, rel_path FROM reports "
            "WHERE kind='video' AND cloud_file_id IS NOT NULL "
            "AND (duration_sec IS NULL OR duration_sec=0)").fetchall()
    finally:
        conn.close()

    done = 0
    for row in rows:
        if done >= DURATIONS_PER_PASS:
            break
        proxy = sar_common.proxy_video_path(DATA_DIR, row["rel_path"])
        if not os.path.exists(proxy):
            continue            # копии ещё нет -- не ошибка, просто рано
        key = "cdur:" + row["rel_path"]
        if not _may_try(key):
            continue
        duration = _read_video_duration(proxy)
        if not duration:
            _note_failure(key, "длительность")
            continue
        _note_success(key)
        c2 = get_db()
        c2.execute("UPDATE reports SET duration_sec=? WHERE report_id=?",
                   (duration, row["report_id"]))
        c2.commit()
        c2.close()
        done += 1
    if done:
        print(f"[длительность] из лёгких копий: {done}", flush=True)
    return done


def seed_settings_from_config():
    """Переносит в базу настройки, которые раньше жили только в конфиге.

    Нужно ровно один раз на установку. Без этого установка, где в
    sar_config.json стоит "auto_process": false, после обновления молча
    начала бы ставить всё в очередь: умолчание реестра -- true. Молчаливая
    смена поведения при обновлении -- худший вид сюрприза, особенно когда
    он выражается в запуске обработки на всём хранилище.

    Трогаем ТОЛЬКО ключи, которых в базе ещё нет: иначе каждый перезапуск
    воркера отменял бы то, что администратор поставил через админку.
    """
    if "auto_process" not in CFG:
        return
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM settings WHERE key='auto_process'").fetchone()
        if row is None:
            sar_common.set_setting(conn, "auto_process",
                                    CFG.get("auto_process", True),
                                    who="перенос из sar_config.json")
            print(f"[watcher] настройка auto_process перенесена из конфига: "
                  f"{CFG.get('auto_process', True)}", flush=True)
    finally:
        conn.close()


def watcher_loop():
    seed_settings_from_config()
    while True:
        # НАСТРОЙКИ ПЕРЕЧИТЫВАЮТСЯ КАЖДЫЙ ПРОХОД.
        #
        # Их меняет администратор через веб-страницу, то есть СЕРВЕР -- а
        # применяет их здесь воркер. Это разные процессы, общающиеся только
        # через базу. Прочитай мы настройки один раз при старте -- админка
        # молча перестала бы работать: форма принимает, значение
        # показывается, поведение прежнее до перезапуска воркера.
        #
        # Цена -- один SELECT из пяти строк раз в poll_interval_sec.
        try:
            conn = get_db()
            settings = sar_common.get_settings(conn)
            conn.close()
        except Exception as e:
            # База занята или ещё не создана -- работаем на умолчаниях, но
            # говорим об этом: молча свалиться к умолчаниям значит
            # игнорировать выставленные человеком ограничения.
            print(f"[watcher] не удалось прочитать настройки ({e}), "
                  f"работаю на умолчаниях", flush=True)
            settings = {k: v["default"] for k, v in sar_common.SETTINGS_SCHEMA.items()}

        # Бюджет на проход: сколько файлов разрешено ПРОЧИТАТЬ с носителя
        # ради превью и длительности. Список и очередь при этом полные --
        # ограничивается только чтение самих файлов.
        budget = [settings["material_touches_per_pass"]]
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
                    # Столбец остаётся ради внешних разовых скриптов, но
                    # платформа его не читает -- путь везде вычисляется.
                    out_dir = sar_common.report_dir(REPORTS_DIR, report_id)
                    file_ctime = sar_common.get_file_ctime(abs_path)
                    # при выключенной авто-обработке файл всё равно
                    # регистрируется и сразу доступен для РУЧНОГО просмотра,
                    # но модель по нему не запускается, пока человек не
                    # нажмёт "обработать" (см. auto_process в конфиге)
                    # Настройка из админки главнее конфига: подключая
                    # большое хранилище, автообработку выключают именно
                    # на ходу, а не правкой файла и перезапуском.
                    initial_status = "queued" if settings["auto_process"] else "idle"
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
                if budget[0] > 0:
                    before = _touches_used
                    _ensure_thumbnail(name, abs_path, kind=kind)
                    if kind == "video":
                        _ensure_duration(report_id, abs_path,
                                          row["duration_sec"] if row is not None else None)
                    budget[0] -= (_touches_used - before)
        except Exception as e:
            print(f"[watcher] ошибка сканирования: {e}")

        # ОБЛАЧНЫЙ ОБХОД -- В СВОЁМ try.
        #
        # Раньше он делил try с локальным обходом, и одна ошибка здесь
        # отменяла ВСЁ: ни один материал не регистрировался, включая
        # локальный, а в журнале была одна строка без последствий. Тот же
        # приём, что и с превью находок ниже: сбой одной части прохода не
        # должен отменять остальные.
        try:
            # МАТЕРИАЛ ИЗ ОБЛАКА -- тем же путём и по тем же правилам.
            #
            # Регистрируем, но НЕ КАЧАЕМ: файл появляется в списке по одним
            # метаданным. Скачивание происходит только когда до него дойдёт
            # обработка, и проходит через ограничители (sar_fetch).
            #
            # Поиск существующей записи -- по rel_path, как и для локальных.
            # Поэтому перенос материала в облако С СОХРАНЕНИЕМ СТРУКТУРЫ
            # ПАПОК подхватит записи вместе со всей проделанной работой:
            # пометками, обсуждениями, отметками просмотра.
            cloud_ops = {a["id"]: a.get("operation_id")
                         for a in sar_common.cloud_accounts(get_db())}
            for rel, kind, acc_id, file_id, size, mtime in sar_common.scan_cloud_materials(
                    get_db(), log=lambda m: print(m, flush=True)):
                conn = get_db()
                row, what = sar_common.match_existing_material(conn, rel, WATCH_DIR)
                if what == "skip":
                    # Локальная копия на месте и разобрана -- вторая запись
                    # только растащила бы работу по двум материалам.
                    conn.close()
                    continue
                if row is None:
                    report_id = sar_common.make_report_id(rel, rel)
                    now = datetime.now().isoformat()
                    status = "queued" if settings["auto_process"] else "idle"
                    conn.execute(
                        "INSERT INTO reports (report_id, rel_path, abs_path, kind, "
                        "status, out_dir, file_ctime, cloud_account_id, "
                        "cloud_file_id, cloud_size, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (report_id, rel, "", kind, status,
                         sar_common.report_dir(REPORTS_DIR, report_id), mtime,
                         acc_id, file_id, size, now, now))
                    conn.commit()
                    print(f"[облако] новый файл: {rel} ({size / 1e6:.0f} МБ)",
                          flush=True)
                else:
                    report_id = row["report_id"]
                    # Идентификатор в облаке мог смениться: файл перезалили.
                    # Без обновления обработка пошла бы за несуществующим.
                    conn.execute(
                        "UPDATE reports SET cloud_account_id=?, cloud_file_id=?, "
                        "cloud_size=?, file_ctime=COALESCE(NULLIF(file_ctime,0),?) "
                        "WHERE report_id=?",
                        (acc_id, file_id, size, mtime, report_id))
                    conn.commit()
                    # Сообщаем ОДИН раз -- в момент, когда доступ реально
                    # появился. Иначе строка повторяется на каждом проходе
                    # (раз в 15 секунд) и топит в себе всё остальное: журнал
                    # перестают читать, а вместе со спамом перестают
                    # замечать настоящие сообщения.
                    was_cloudless = not row["cloud_file_id"]
                    if was_cloudless and not os.path.exists(
                            sar_common.material_path(WATCH_DIR, row["rel_path"])):
                        print(f"[облако] вернулся доступ к {row['rel_path']}",
                              flush=True)

                # ПРИВЯЗКА К ОПЕРАЦИИ. По имени папки её не угадать:
                # структура в облаке своя (на боевом подключении это папки
                # по датам съёмки, а операция называется иначе). Поэтому
                # операцию указывают явно при подключении диска; если не
                # указана -- работает прежний разбор по имени папки, и
                # материал попадёт в «Не разобрано».
                op_id = cloud_ops.get(acc_id)
                if op_id:
                    sar_common.attach_material(conn, op_id, report_id)
                else:
                    sar_common.attach_by_folder(conn, WATCH_DIR, report_id, rel)
                conn.close()

        except Exception as e:
            print(f"[облако] обход не удался: {e}", flush=True)

        # Догенерация превью находок -- отдельно от сканирования папки и в
        # своём try: вырезание кадра из видео может упасть на битом файле, и
        # это не повод ронять весь проход наблюдения.
        try:
            ensure_finding_previews(get_db())
        except Exception as e:
            print(f"[находки] проход превью не удался: {e}")

        # Снимки, которые попросили подготовить. Отдельный try: сбой здесь
        # не должен отменять остальной проход.
        try:
            ensure_requested_photos()
        except Exception as e:
            print(f"[облако] подготовка снимков не удалась: {e}", flush=True)

        # ПРЕВЬЮ ДЛЯ СКАЧАННОГО. Файл мог появиться во временной папке
        # после подготовки -- значит превью для него теперь сделать можно,
        # а обход папки его не видит: он не в наблюдаемой папке.
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT rel_path, kind, cloud_file_id FROM reports "
                "WHERE cloud_file_id IS NOT NULL").fetchall()
            conn.close()
            left = settings["material_touches_per_pass"]
            for r in rows:
                if left <= 0:
                    break
                rel = r["rel_path"]
                thumb = sar_common.get_thumbnail_path(DATA_DIR, rel)
                if os.path.exists(thumb):
                    continue

                found = sar_common.find_material_file(WATCH_DIR, DATA_DIR, rel)
                if found:
                    before = _touches_used
                    _ensure_thumbnail(rel, found, kind=r["kind"])
                    left -= max(1, _touches_used - before)
                    continue

                # Файла на диске нет -- превью сделает фоновый поток,
                # читая кадр прямо из облака (см. _start_remote_thumbs).
                # Здесь ждать нельзя: этот проход идёт перед сборкой
                # копий, и сетевое чтение задержало бы её.
                continue
        except Exception as e:
            print(f"[облако] превью не удались: {e}", flush=True)

        # Разбор телеметрии: дешёвый (чтение текстового файла), но делать
        # его в веб-слое по запросу нельзя -- сервер только читает.
        try:
            ensure_telemetry_tracks()
        except Exception as e:                          # noqa: BLE001
            print(f"[трек] проход не удался: {e}", flush=True)

        # Длительность облачного видео -- из лёгкой копии. Без неё материал
        # не попадает ни в «отснято», ни в «просмотрено», и покрытие в
        # шапке операции оказывается завышенным втрое.
        try:
            ensure_cloud_durations()
        except Exception as e:                          # noqa: BLE001
            print(f"[длительность] проход не удался: {e}", flush=True)

        # Превью недокачанного материала -- ФОНОМ, до сборки копий.
        # Запускаем здесь, чтобы поток работал ровно то время, пока
        # основной проход занят ffmpeg: сеть и процессор друг другу не
        # мешают, и полторы сотни превью не ждут окончания кодирования.
        try:
            _start_remote_thumbs()
        except Exception as e:                          # noqa: BLE001
            print(f"[превью] не запустились: {e}", flush=True)

        # Лёгкие копии -- последними в проходе и по одной за раз: это самая
        # долгая из фоновых работ, и она не должна задерживать ни постановку
        # новых файлов в очередь, ни отметку "воркер жив".
        try:
            ensure_video_proxies(get_db())
        except Exception as e:
            print(f"[копия] проход не удался: {e}")

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


# Загрузчик материала -- один на процесс. Создаётся лениво: пока облако не
# подключено, он не нужен вовсе, а создавать его на каждое видео значит
# каждый раз заново считать занятое место и терять счётчик трафика.
_fetcher = None
_fetcher_key = None
_fetcher_lock = threading.Lock()


def get_fetcher():
    """Загрузчик под ТЕКУЩИЕ настройки и текущее подключение.

    Пересоздаётся, когда администратор поменял ограничения или переподключил
    диск -- иначе изменение в админке не доехало бы до того, кто качает, и
    это был бы ровно тот молчаливый отказ, ради которого настройки вообще
    положены в базу.
    """
    global _fetcher, _fetcher_key
    conn = get_db()
    try:
        settings = sar_common.get_settings(conn)
        accounts = sar_common.cloud_accounts(conn, enabled_only=True)
    finally:
        conn.close()
    if not accounts:
        return None
    acc = accounts[0]
    key = (acc["id"], acc.get("token"), settings["downloads_in_flight"],
           settings["staging_cap_gb"], settings["daily_traffic_gb"])
    with _fetcher_lock:
        if _fetcher is None or _fetcher_key != key:
            import sar_cloud
            import sar_fetch
            import sar_staging
            staging = sar_staging.Staging(
                sar_staging.staging_dir(DATA_DIR),
                cap_bytes=int(float(settings["staging_cap_gb"]) * 1e9))
            # ЧЕРЕЗ ОБЩУЮ ТОЧКУ: собранный здесь напрямую провайдер был бы
            # без продления и отказывал через час -- причём молча, потому
            # что «доступ отклонён» выглядит одинаково и когда продлить
            # нечем, и когда диск просто не подключен.
            provider = sar_common.provider_for_account(get_db(), acc)
            _fetcher = sar_fetch.Fetcher(staging, provider=provider,
                                          settings=settings)
            _fetcher_key = key
        return _fetcher


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

    # Пути ВЫЧИСЛЯЮТСЯ от текущих корней, а не берутся из столбцов
    # abs_path/out_dir: хранимый абсолютный путь записан на той машине, где
    # файл впервые увидели, и после любого переезда (материал в облако,
    # платформа на VPS) ведёт в никуда. Причём молча -- строка в базе есть,
    # файла по ней нет. См. sar_common.material_path.
    src = sar_common.material_path(WATCH_DIR, report["rel_path"])
    out = sar_common.report_dir(REPORTS_DIR, report_id)

    # МАТЕРИАЛ ИЗ ОБЛАКА нужно сначала получить на диск: детектор умеет
    # читать только путь (cv2.VideoCapture берёт путь, не поток). Файл
    # ЗАКРЕПЛЯЕТСЯ на время обработки -- вытеснить его в этот момент значит
    # получить отчёт по половине видео, причём молча.
    fetched = None
    if report.get("cloud_file_id") and not os.path.exists(src):
        fetcher = get_fetcher()
        if fetcher is None:
            update_report(report_id, status="error",
                          error="материал в облаке, но получить его нечем")
            return
        try:
            src = fetcher.ensure_local(
                report["rel_path"], file_id=report["cloud_file_id"],
                expected_size=report.get("cloud_size") or 0)
            fetched = report["rel_path"]
        except Exception as e:
            # Отказ ограничителя (нет места, лимит трафика, обрыв) -- это НЕ
            # ошибка материала. Возвращаем в очередь, чтобы попробовать
            # позже, а не помечаем файл битым навсегда.
            update_report(report_id, status="queued", phase=None,
                          error=None)
            append_log(report_id, f"[облако] отложено: {e}")
            print(f"[облако] {report['rel_path']} отложен: {e}", flush=True)
            return

    if report["kind"] == "video":
        script = os.path.join(SCRIPT_DIR, "sar_video_review.py")
        cmd = [sys.executable, "-u", script, "--video", src, "--out", out,
               "--tracking-report-id", report_id, "--tracking-api-base", api_base]
    else:
        script = os.path.join(SCRIPT_DIR, "sar_photo_review.py")
        cmd = [sys.executable, "-u", script, "--photo", src, "--out", out,
               "--tracking-report-id", report_id, "--tracking-api-base", api_base]

    append_log(report_id, f"$ {' '.join(cmd)}")
    # Закрепление снимаем В ЛЮБОМ случае: не снятое не потеряет файл, но
    # займёт место во временной папке навсегда, и через несколько видео
    # вытеснять станет нечего.

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
        # Закрепление снимаем ВСЕГДА, каким бы ни был исход. Не снятое не
        # потеряет файл, но займёт место во временной папке навсегда -- и
        # через несколько видео вытеснять станет нечего, а новые загрузки
        # начнут получать отказ «освободить нечем».
        if fetched:
            f = get_fetcher()
            if f is not None:
                f.release(fetched)


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
    WATCH_DIR, DATA_DIR, DB_PATH, REPORTS_DIR = sar_common.resolve_paths(
        CFG["watch_dir"], CFG.get("data_dir"))

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
