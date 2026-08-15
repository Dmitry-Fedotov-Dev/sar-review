#!/usr/bin/env python3
"""
backfill_telemetry.py — пересчитывает GPS/высоту/углы подвеса и
перезаписывает detections.json/detections.csv/report.html для УЖЕ
обработанных видео-отчётов, БЕЗ повторного запуска детектора (кропы/кадры
на диске не трогаются, каждое видео заново не считается).

Зачем это отдельный разовый скрипт, а не автоматика в sar_worker.py:
чинит два конкретных случая одноразово, по требованию, а не на каждый
чих -- это разовая миграция уже накопленных данных, не постоянный процесс.

  1. Видео обработалось ДО того, как телеметрия была разложена в telemetry/
     -- тогда GPS в отчёте отсутствовал, хотя SRT для этого полёта на самом
     деле есть и теперь находится через sar_common.find_telemetry_for_video.
  2. detections.json/csv были на этой конкретной машине записаны БЕЗ явного
     encoding="utf-8" (баг, исправленный в save_outputs() в
     sar_video_review.py) -- Python на Windows в этом случае берёт кодировку
     консоли (cp1251), и sar_server.py падает с UnicodeDecodeError при
     попытке прочитать файл как UTF-8. Перезапись через save_outputs()
     (уже исправленный) чинит и это заодно.

Группировка детекций в сцены НЕ пересчитывается заново -- используются уже
существующие group_id из старого detections.json, чтобы не перетасовать
карточки сцен, которые люди уже могли отсматривать.

Использование:
    python backfill_telemetry.py                     # все готовые видео-отчёты
    python backfill_telemetry.py --report-id <id>     # только один отчёт
    python backfill_telemetry.py --dry-run            # только показать, что изменилось бы
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common
from sar_video_review import Hit, lookup_telemetry, parse_srt_telemetry, save_outputs, load_config


def _resolve_frame_dimensions(hits, abs_path):
    """Возвращает (width, height) кадра -- нужны для перевода bbox в доли
    кадра при оценке координат объекта (sar_common.estimate_ground_point).
    Если детекции уже несут frame_width/frame_height (обработаны текущей
    версией кода) -- берём оттуда. Для СТАРЫХ отчётов (до этой фичи) один
    раз открываем видео ТОЛЬКО чтобы прочитать разрешение через метаданные
    контейнера -- это не декодирование кадров и не пересчёт детектора."""
    for h in hits:
        if h.frame_width and h.frame_height:
            return h.frame_width, h.frame_height
    if not abs_path or not os.path.exists(abs_path):
        return None, None
    try:
        import cv2
        cap = cv2.VideoCapture(abs_path)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h_ = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return (w, h_) if w and h_ else (None, None)
    except Exception as e:
        print(f"[телеметрия] не удалось прочитать разрешение {abs_path}: {e} "
              f"-- оценка координат объекта для этого отчёта недоступна")
        return None, None


def _read_detections_tolerant(path):
    """Возвращает (данные, кодировка_которой_получилось_прочитать).
    Пробуем UTF-8 (правильная кодировка) первой, cp1251 -- как резерв для
    файлов, записанных до исправления encoding="utf-8" в save_outputs()."""
    for enc in ("utf-8", "cp1251"):
        try:
            with open(path, "r", encoding=enc) as f:
                return json.load(f), enc
        except UnicodeDecodeError:
            continue
    raise ValueError(f"не удалось прочитать {path} ни как utf-8, ни как cp1251")


def backfill_one_report(report, telemetry_dir, cfg, tracking_api_base, dry_run=False, force=False):
    report_id = report["report_id"]
    out_dir = report["out_dir"]
    abs_path = report["abs_path"]
    det_path = os.path.join(out_dir, "detections.json")
    if not os.path.exists(det_path):
        print(f"[{report_id}] нет detections.json, пропускаю")
        return False

    raw_hits, used_encoding = _read_detections_tolerant(det_path)
    needs_reencode = used_encoding != "utf-8"
    if not raw_hits:
        print(f"[{report_id}] нет детекций, пропускаю")
        return False

    # телеметрия: сначала рядом с видео (старое поведение, приоритет),
    # потом -- индекс telemetry/ (см. sar_common.find_telemetry_for_video)
    srt_path = os.path.splitext(abs_path)[0] + ".srt"
    if not os.path.exists(srt_path):
        telemetry_index = sar_common.build_telemetry_index(telemetry_dir)
        match, _reason = sar_common.find_telemetry_for_video(abs_path, telemetry_index)
        srt_path = str(match) if match else None

    telemetry = []
    if srt_path and os.path.exists(srt_path):
        gps_order = cfg.get("srt_gps_tuple_order", "lat_lon")
        telemetry = parse_srt_telemetry(srt_path, gps_tuple_order=gps_order)

    hits = [Hit(**h) for h in raw_hits]
    gps_changed = False
    raw_telemetry_changed = False
    if telemetry:
        for h in hits:
            telem = lookup_telemetry(telemetry, h.seconds)
            if telem["lat"] is not None and h.lat is None:
                gps_changed = True
            h.lat, h.lon, h.alt = telem["lat"], telem["lon"], telem["alt"]
            h.alt_abs = telem.get("alt_abs")
            h.yaw, h.pitch, h.roll = telem.get("yaw"), telem.get("pitch"), telem.get("roll")
            h.focal_len = telem.get("focal_len")
            # "вся телеметрия кадра" -- заполняем только если ещё не было
            # (детерминированно от (видео, таймкод), в отличие от оценки
            # координат тут нет риска "уже посчитано неверно" -- нечего
            # пересчитывать заново без необходимости)
            new_raw = telem.get("raw_telemetry")
            if new_raw and h.raw_telemetry != new_raw:
                h.raw_telemetry = new_raw
                raw_telemetry_changed = True

    # "вероятные координаты" объекта (не дрона!) -- см. предупреждения в
    # sar_common.estimate_ground_point. ВСЕГДА пересчитываем заново (не
    # только там, где оценки ещё нет) -- иначе уже посчитанная оценка,
    # сделанная до исправления защиты от численного взрыва при угле, близком
    # к горизонту (см. max_distance_m), так и осталась бы в отчёте неверной
    # (реальный случай: 69.8 км вместо разумной оценки) навсегда, потому что
    # backfill её больше никогда бы не тронул. Пересчёт идемпотентен и
    # дешёвый (чистая геометрия, без I/O) -- лишний раз пересчитать то, что
    # и так было верно, безопаснее, чем рискнуть оставить опасно неверную
    # координату непересчитанной.
    est_changed = False
    frame_w, frame_h = _resolve_frame_dimensions(hits, abs_path)
    max_estimate_distance_m = cfg.get("max_estimate_distance_m", sar_common.DEFAULT_MAX_ESTIMATE_DISTANCE_M)
    if frame_w and frame_h:
        for h in hits:
            if h.frame_width is None:
                h.frame_width, h.frame_height = frame_w, frame_h
            bbox_cx_frac = ((h.bbox[0] + h.bbox[2]) / 2.0) / frame_w
            bbox_cy_frac = ((h.bbox[1] + h.bbox[3]) / 2.0) / frame_h
            # FOV по фактическому зуму кадра (focal_len уже лежит в Hit)
            frame_hfov, frame_vfov = sar_common.resolve_frame_fov(h.focal_len, cfg)
            estimate = sar_common.estimate_ground_point(
                drone_lat=h.lat, drone_lon=h.lon, altitude_m=h.alt,
                gimbal_yaw_deg=h.yaw, gimbal_pitch_deg=h.pitch,
                bbox_center_frac_x=bbox_cx_frac, bbox_center_frac_y=bbox_cy_frac,
                horizontal_fov_deg=frame_hfov, vertical_fov_deg=frame_vfov,
                max_distance_m=max_estimate_distance_m)
            new_est = estimate if estimate else (None, None, None)
            if new_est != (h.est_lat, h.est_lon, h.est_distance_m):
                h.est_lat, h.est_lon, h.est_distance_m = new_est
                est_changed = True

    if not gps_changed and not est_changed and not raw_telemetry_changed and not needs_reencode and not force:
        print(f"[{report_id}] нечего обновлять (GPS/оценка/телеметрия уже были или недоступны, кодировка в порядке)")
        return False

    reasons = []
    if gps_changed:
        reasons.append("добавлен GPS")
    if est_changed:
        reasons.append("добавлена оценка координат объекта")
    if raw_telemetry_changed:
        reasons.append("добавлена полная телеметрия кадра")
    if needs_reencode:
        reasons.append("исправлена кодировка (была cp1251)")
    if force and not reasons:
        reasons.append("принудительная перегенерация (--force)")

    if dry_run:
        print(f"[{report_id}] (dry-run) обновил(о) бы: {', '.join(reasons)}")
        return True

    # группы -- из уже существующих group_id, не пересчитываем группировку
    # заново, чтобы не перетасовать то, что уже могли отсматривать люди
    by_gid = defaultdict(list)
    for h in hits:
        by_gid[h.group_id].append(h)
    groups = []
    for gid, g_hits in by_gid.items():
        g_hits.sort(key=lambda x: x.frame_idx)
        groups.append({"id": gid, "hits": g_hits})

    # ВАЖНО: tracking_report_id/tracking_api_base нужно передавать -- иначе
    # save_outputs() решает, что отчёт открывают локально без сервера, и
    # молча не пишет в report.html ни ссылку "к списку файлов", ни ссылку на
    # ручной плеер, ни скрипт статистики просмотра сцен. Ровно этот пропуск
    # уже был здесь и убрал ссылку на плеер из первого прогона этого скрипта.
    save_outputs(hits, groups, out_dir, abs_path,
                 tracking_report_id=report_id, tracking_api_base=tracking_api_base)
    print(f"[{report_id}] перезаписаны detections.json/csv/report.html: {', '.join(reasons)}")
    return True


def backfill_manual_observations(db_path, telemetry_dir, cfg, report_id=None):
    """Пересчитывает координаты дрона и 'вероятные координаты' объекта для
    УЖЕ существующих ручных наблюдений (таблица manual_observations).

    Зачем отдельно от backfill_one_report(): у ручных наблюдений нет своего
    detections.json, который переписывается целиком -- это строки в БД,
    которые нужно обновлять напрямую через UPDATE. Без этой функции
    наблюдения, сделанные до появления оценки координат (или ДО исправления
    защиты от численного взрыва в estimate_ground_point, см. max_distance_m)
    так и остались бы без неё/с неверной оценкой навсегда."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT mo.id, mo.report_id, mo.timestamp_sec, mo.bbox, mo.lat, mo.lon,
               mo.est_lat, mo.est_lon, mo.est_distance_m, mo.raw_telemetry, r.abs_path
        FROM manual_observations mo JOIN reports r ON r.report_id = mo.report_id
    """
    if report_id:
        rows = conn.execute(query + " WHERE mo.report_id=?", (report_id,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()

    max_estimate_distance_m = cfg.get("max_estimate_distance_m", sar_common.DEFAULT_MAX_ESTIMATE_DISTANCE_M)
    gps_order = cfg.get("srt_gps_tuple_order", "lat_lon")

    updated = 0
    telemetry_cache = {}  # report_id -> распарсенная телеметрия, не парсим один SRT дважды
    for row in rows:
        report_id, abs_path = row["report_id"], row["abs_path"]
        if report_id not in telemetry_cache:
            srt_path = os.path.splitext(abs_path)[0] + ".srt"
            if not os.path.exists(srt_path):
                telemetry_index = sar_common.build_telemetry_index(telemetry_dir)
                match, _reason = sar_common.find_telemetry_for_video(abs_path, telemetry_index)
                srt_path = str(match) if match else None
            telemetry_cache[report_id] = (
                parse_srt_telemetry(srt_path, gps_tuple_order=gps_order)
                if srt_path and os.path.exists(srt_path) else [])

        telemetry = telemetry_cache[report_id]
        if not telemetry:
            continue

        telem = lookup_telemetry(telemetry, row["timestamp_sec"])
        bbox = json.loads(row["bbox"])
        bbox_cx_frac = (bbox[0] + bbox[2]) / 2.0
        bbox_cy_frac = (bbox[1] + bbox[3]) / 2.0
        frame_hfov, frame_vfov = sar_common.resolve_frame_fov(telem.get("focal_len"), cfg)
        estimate = sar_common.estimate_ground_point(
            drone_lat=telem.get("lat"), drone_lon=telem.get("lon"), altitude_m=telem.get("alt"),
            gimbal_yaw_deg=telem.get("yaw"), gimbal_pitch_deg=telem.get("pitch"),
            bbox_center_frac_x=bbox_cx_frac, bbox_center_frac_y=bbox_cy_frac,
            horizontal_fov_deg=frame_hfov, vertical_fov_deg=frame_vfov,
            max_distance_m=max_estimate_distance_m)
        est_lat, est_lon, est_distance_m = estimate if estimate else (None, None, None)
        # raw_telemetry детерминирован от (видео, таймкод) -- не переписываем,
        # если уже есть, только заполняем отсутствующее
        raw_telemetry = row["raw_telemetry"] or telem.get("raw_telemetry")

        old = (row["lat"], row["lon"], row["est_lat"], row["est_lon"], row["est_distance_m"], row["raw_telemetry"])
        new = (telem.get("lat"), telem.get("lon"), est_lat, est_lon, est_distance_m, raw_telemetry)
        if old != new:
            conn.execute(
                "UPDATE manual_observations SET lat=?, lon=?, est_lat=?, est_lon=?, "
                "est_distance_m=?, raw_telemetry=? WHERE id=?",
                (*new, row["id"]))
            updated += 1

    conn.commit()
    conn.close()
    return updated


def main():
    p = argparse.ArgumentParser(
        description="Пересчитать GPS и/или починить кодировку для уже обработанных видео-отчётов")
    p.add_argument("--report-id", default=None,
                    help="обработать только один отчёт (по умолчанию -- все готовые видео)")
    p.add_argument("--dry-run", action="store_true",
                    help="только показать, что было бы изменено, ничего не перезаписывать")
    p.add_argument("--force", action="store_true",
                    help="перегенерировать report.html/detections.json/csv даже если GPS/кодировка "
                         "не менялись -- нужно после правок HTML-шаблона в save_outputs()")
    args = p.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "sar_config.json")
    cfg = load_config(config_path if os.path.exists(config_path) else None)
    server_cfg, _ = sar_common.load_server_config(script_dir)
    watch_dir, data_dir, db_path, reports_dir = sar_common.resolve_paths(server_cfg["watch_dir"])
    # идемпотентно -- та же миграция схемы, что применяется при старте
    # sar_worker.py/sar_server.py. Без этого вызова здесь backfill,
    # запущенный до первого перезапуска воркера/сервера после добавления
    # новой колонки (например, raw_telemetry), падал бы с "no such column".
    sar_common.init_db(db_path)
    telemetry_dir = sar_common.resolve_telemetry_dir(
        watch_dir, cfg.get("telemetry_dir", sar_common.DEFAULT_TELEMETRY_DIR_NAME))
    # тот же формат, что и в sar_worker.py -- report.html должен знать, что
    # его открывают ЧЕРЕЗ сервер (иначе теряет ссылки "к списку файлов"/
    # "ручной плеер" и скрипт статистики просмотра сцен, см. save_outputs())
    tracking_api_base = f"http://127.0.0.1:{server_cfg['port']}"

    conn = sar_common.get_db_connection(db_path)
    if args.report_id:
        rows = conn.execute(
            "SELECT * FROM reports WHERE report_id=? AND kind='video'", (args.report_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM reports WHERE status='done' AND kind='video'").fetchall()
    conn.close()

    if not rows:
        print("Подходящих отчётов не найдено.")
        return

    print(f"Найдено отчётов для проверки: {len(rows)} (телеметрия: {telemetry_dir})")
    updated = 0
    for row in rows:
        report = dict(row)
        try:
            if backfill_one_report(report, telemetry_dir, cfg, tracking_api_base,
                                    dry_run=args.dry_run, force=args.force):
                updated += 1
        except Exception as e:
            print(f"[{report['report_id']}] ОШИБКА: {e}")

    suffix = " (dry-run, файлы не менялись)" if args.dry_run else ""
    print(f"\nГотово. Обновлено отчётов: {updated}/{len(rows)}{suffix}")

    if not args.dry_run:
        updated_obs = backfill_manual_observations(db_path, telemetry_dir, cfg, report_id=args.report_id)
        if updated_obs:
            print(f"Обновлено ручных наблюдений (координаты дрона/оценка объекта): {updated_obs}")


if __name__ == "__main__":
    main()
