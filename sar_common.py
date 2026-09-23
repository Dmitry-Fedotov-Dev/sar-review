#!/usr/bin/env python3
"""
sar_common.py — общий код между sar_server.py (веб-интерфейс) и
sar_worker.py (фоновая обработка видео/фото). Оба процесса общаются друг
с другом ТОЛЬКО через одну и ту же SQLite БД и файловую систему — никакого
прямого взаимодействия (сокетов, разделяемой памяти) между ними нет.

Именно поэтому обновление/перезапуск sar_server.py не трогает и не прерывает
работу sar_worker.py (и наоборот) — они не держат друг на друга ссылок в
памяти, только на файл БД на диске.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".ts", ".m4v", ".wmv"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"}
MEDIA_EXTS = VIDEO_EXTS | PHOTO_EXTS

# Телеметрия -- НЕ материал: заводить на SRT запись в reports нельзя, иначе
# он попадёт в список файлов, в очередь обработки и в счёт покрытия. Но и
# отбрасывать его при облачном обходе (как было) неверно: рядом с видео в
# Google Диске лежат SRT, и без них 14 облачных видео навсегда числились
# «телеметрии нет», а пометки на них оставались без координат.
TELEMETRY_EXTS = {".srt"}

# Ранжирование детекций (ручных и модели) на странице плеера -- см.
# detection_priorities в init_db. Только человек может проставить любое из
# этих значений (включая CONFIRMED_PERSON) -- сервер никогда не пишет сюда
# автоматически, ни при обработке, ни при бэкфилле.
PRIORITY_LABELS = {
    "confirmed_person": "✅ точно человек",
    "likely_person": "👤 предположительно человек",
    "confirmed_object": "🎒 предмет",
    "likely_object": "🎒 предположительно предмет",
    # "непонятно что, но на фон не похоже" -- отдельный статус, потому что
    # раньше такое приходилось либо отклонять (и терять), либо записывать в
    # "предположительно предмет" (и врать). В датасет НЕ экспортируется:
    # обучать модель на том, что человек сам не смог опознать, нельзя
    # (см. PRIORITY_TO_CLASS в sar_dataset_export.py)
    "anomaly": "❓ аномалия (непонятно, но подозрительно)",
    "rejected": "❌ отклонено",
}
VALID_PRIORITIES = set(PRIORITY_LABELS)

# ---------------------------------------------------------------------------
# РОЛИ И ОПОЗНАНИЕ ЧЕЛОВЕКА
#
# В системе нет отдельных "учёток": личность даёт Telegram-бот. Он и так знает
# про каждого chat_id и @username и сам решает, кто получает доступ -- раньше
# эта личность просто терялась, потому что бот выдавал ОБЩИЙ пароль.
#
# Теперь одобренному человеку бот выдаёт ПЕРСОНАЛЬНУЮ ссылку с ключом:
#     https://.../login?key=<access_token>
# Сервер по ключу находит запись в telegram_access_requests и сажает человека
# в сессию уже опознанным, со своей ролью. Никакой синхронизации двух списков
# пользователей не нужно -- связь возникает в момент выдачи ключа.
#
# Вход по общему паролю остаётся (это важно для работы в поле с чужого
# ноутбука), но такой человек АНОНИМЕН: смотреть и размечать находки может,
# участвовать в обсуждении -- нет. Иначе модерация не имела бы смысла:
# заблокированный просто зашёл бы по общему паролю и продолжил.
#
# Ключ -- предъявительский: переслал ссылку -- отдал доступ. Это всё равно
# строго лучше общего пароля (у каждого свой, отзывается одной командой,
# видно кто что сделал). Полностью от пересылки защищает только вход через
# сам Telegram (Login Widget), но он требует настоящего домена с HTTPS.
ROLE_VIEWER = "viewer"        # смотреть, размечать находки, комментировать
ROLE_MODERATOR = "moderator"  # + удалять ЛЮБЫЕ комментарии
ROLE_ADMIN = "admin"          # + загрузка файлов и запуск обработки
ROLE_MUTED = "muted"          # смотреть и размечать, но не комментировать
VALID_ROLES = {ROLE_VIEWER, ROLE_MODERATOR, ROLE_ADMIN, ROLE_MUTED}
DEFAULT_ROLE = ROLE_VIEWER

ROLE_LABELS = {
    ROLE_VIEWER: "участник",
    ROLE_MODERATOR: "модератор обсуждений",
    ROLE_ADMIN: "администратор (полные права)",
    ROLE_MUTED: "без права комментировать",
}

# Роли, которым можно то же, что и модератору. Админ -- надмножество: держим
# это одним списком, чтобы при добавлении новой возможности не забыть его
# в одной из проверок.
ROLES_CAN_MODERATE = {ROLE_MODERATOR, ROLE_ADMIN}
ROLES_CAN_COMMENT = {ROLE_VIEWER, ROLE_MODERATOR, ROLE_ADMIN}
ROLES_CAN_UPLOAD = {ROLE_ADMIN}


# ---------------------------------------------------------------------------
# Операции
#
# Папка в watch_dir равна операции -- так сохраняется главный жест системы:
# положил файл в папку, он сам обработался. Обязательный выбор операции при
# загрузке сделал бы платформу хуже, а не лучше.
#
# Опознаётся операция НЕ по имени папки, а по метке внутри неё. Иначе
# переименование "Курумды_август" в "Курумды_2026" завело бы новую операцию,
# а прежняя осталась бы с материалами, которых на месте больше нет.
# ---------------------------------------------------------------------------

OPERATION_MARKER = ".sar_operation"


def read_operation_marker(folder_path):
    """id операции из метки в папке, либо None."""
    path = os.path.join(folder_path, OPERATION_MARKER)
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_operation_marker(folder_path, operation_id):
    path = os.path.join(folder_path, OPERATION_MARKER)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(operation_id))
    return path


# имя папки делаем из названия операции: человек ищет её в проводнике
# по тому же слову, что видит в интерфейсе. Символы, запрещённые в именах
# файлов Windows, заменяем -- иначе операция «Поиск 12/08» создала бы
# вложенную папку вместо одной.
_BAD_IN_NAME = r'<>:"/\\|?*'


def folder_name_for(title):
    name = "".join("_" if ch in _BAD_IN_NAME else ch for ch in str(title))
    name = " ".join(name.split()).strip(" .")     # без хвостовых точек и пробелов
    return name[:80] or "операция"


def create_operation(conn, title, area=None, client=None, coordinator=None,
                      folder=None):
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO operations (title, area, client, coordinator, folder, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (title, area, client, coordinator, folder, now, now))
    conn.commit()
    return cur.lastrowid


def get_operation(conn, operation_id):
    return conn.execute("SELECT * FROM operations WHERE id=?",
                         (operation_id,)).fetchone()


def list_operations(conn):
    return conn.execute("SELECT * FROM operations ORDER BY created_at DESC").fetchall()


def attach_material(conn, operation_id, report_id):
    """Привязывает материал к операции. Повторный вызов безвреден."""
    conn.execute(
        "INSERT OR IGNORE INTO operation_materials (operation_id, report_id, added_at) "
        "VALUES (?,?,?)", (operation_id, report_id, datetime.now().isoformat()))
    conn.commit()


def detach_material(conn, operation_id, report_id):
    conn.execute("DELETE FROM operation_materials WHERE operation_id=? AND report_id=?",
                 (operation_id, report_id))
    conn.commit()


def operations_of_material(conn, report_id):
    return conn.execute(
        "SELECT o.* FROM operations o JOIN operation_materials m ON m.operation_id=o.id "
        "WHERE m.report_id=? ORDER BY o.created_at DESC", (report_id,)).fetchall()


def materials_of_operation(conn, operation_id):
    return conn.execute(
        "SELECT r.* FROM reports r JOIN operation_materials m ON m.report_id=r.report_id "
        "WHERE m.operation_id=? ORDER BY r.file_ctime DESC, r.rel_path",
        (operation_id,)).fetchall()


def create_operation_with_folder(conn, watch_dir, title, area=None, client=None,
                                  coordinator=None):
    """Заводит операцию и её папку разом.

    Порядок важен: сначала запись в базе (нужен id для метки), потом папка,
    потом метка. Если папку создать не удалось -- запись остаётся, операция
    просто без папки, и материалы в неё можно добавлять через браузер. Это
    лучше, чем откатывать: пустая операция чинится одним движением, а
    потерянная -- нет.
    """
    op_id = create_operation(conn, title, area=area, client=client,
                             coordinator=coordinator)
    folder = folder_name_for(title)
    path = os.path.join(watch_dir, folder)
    try:
        os.makedirs(path, exist_ok=True)
        write_operation_marker(path, op_id)
        conn.execute("UPDATE operations SET folder=? WHERE id=?", (folder, op_id))
        conn.commit()
    except OSError:
        folder = None
    return op_id, folder


def merged_length(segments):
    """Суммарная длина отрезков БЕЗ учёта пересечений.

    Нужна, чтобы отличать «просмотрено» от «человеко-часов». Три человека,
    посмотревшие одно и то же видео, дают втрое больше человеко-часов, но
    материал при этом просмотрен ровно один раз.
    """
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(segments):
        if s is None or e is None or e <= s:
            continue
        if cur_e is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def operation_summary(conn, operation_id):
    """Сводка по операции для карточки в списке.

    Различаются две принципиально разные величины:

      watched_sec  -- сколько материала ПРОСМОТРЕНО, с объединением
                      пересечений. Это ответ заказчику: «мы прошли столько-то
                      процентов отснятого».
      viewer_sec   -- человеко-часы команды. Может многократно превышать
                      длительность съёмки, и это нормально.

    Раньше на карточке стояла вторая величина под именем первой, и операция
    показывала «просмотрено 193%» -- цифра, которую нельзя показать клиенту.
    """
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(r.duration_sec),0) secs "
        "FROM reports r JOIN operation_materials m ON m.report_id=r.report_id "
        "WHERE m.operation_id=?", (operation_id,)).fetchone()

    by_material = {}
    viewer_sec = 0.0
    for w in conn.execute(
            "SELECT w.report_id, w.start_sec, w.end_sec FROM watch_segments w "
            "JOIN operation_materials m ON m.report_id=w.report_id "
            "WHERE m.operation_id=?", (operation_id,)):
        s, e = w["start_sec"], w["end_sec"]
        if s is None or e is None or e <= s:
            continue
        viewer_sec += e - s
        by_material.setdefault(w["report_id"], []).append((s, e))
    watched = sum(merged_length(v) for v in by_material.values())

    marks = conn.execute(
        "SELECT COUNT(*) n FROM manual_observations o "
        "JOIN operation_materials m ON m.report_id=o.report_id "
        "WHERE m.operation_id=?", (operation_id,)).fetchone()

    footage = float(row["secs"] or 0)
    # покрытие не может превышать 100%: если сегменты выходят за длительность
    # (перемотка в конец, битая длительность), зажимаем -- лучше честные 100,
    # чем невозможные 193
    pct = min(100, round(watched / footage * 100)) if footage > 0 else 0
    return {"materials": row["n"], "footage_sec": footage,
            "watched_sec": watched, "viewer_sec": viewer_sec,
            "coverage_pct": pct, "marks": marks["n"]}


# Человеческие названия хранилищ. Дублируются из sar_cloud намеренно:
# sar_common не должен тянуть за собой сетевой модуль ради двух строк --
# его импортируют и воркер, и бот, и разовые скрипты.
CLOUD_PROVIDER_NAMES = {"google": "Google Диск", "yandex": "Яндекс.Диск"}


def provider_for_account(conn, acc):
    """Собирает провайдера с продлением доступа -- ОДНА точка.

    Продлённый токен надо сохранить, иначе через час всё повторится, и так
    до бесконечности. Сохранение -- дело базы, а sar_cloud про базу не
    знает и знать не должен: его импортируют и разовые скрипты. Поэтому
    провайдеру передаются две функции, а связывает их это место.

    Собирать провайдера где-то ещё нельзя: тот экземпляр окажется без
    продления и будет отказывать через час, причём молча -- «доступ
    отклонён» выглядит одинаково и когда продлить нечем, и когда просто
    забыли подключить.
    """
    import sar_cloud

    account_id = acc["id"]

    def renew():
        if not (acc.get("client_id") and acc.get("client_secret")
                and acc.get("refresh_token")):
            raise sar_cloud.AuthExpired(
                "доступ истёк, а продлить нечем: у подключения нет ключей "
                "приложения. Подключите диск заново или укажите их в "
                "настройках -- у Google токен живёт один час.")
        return sar_cloud.refresh_google_token(
            acc["client_id"], acc["client_secret"], acc["refresh_token"])

    def save(token, expires_at):
        update_cloud_account(
            conn, account_id, token=token,
            expires_at=datetime.fromtimestamp(expires_at).isoformat(),
            last_error=None)
        acc["token"] = token

    prov = sar_cloud.make_provider(acc["provider"], acc["token"],
                                    renew=renew, on_renew=save)

    # ПРОДЛЕВАЕМ ЗАРАНЕЕ, не дожидаясь отказа: обращение с заведомо мёртвым
    # токеном -- это лишний запрос к Google и лишняя запись об ошибке,
    # которую потом видит человек и пугается.
    exp = acc.get("expires_at")
    if exp and acc.get("client_id"):
        try:
            left = (datetime.fromisoformat(exp) - datetime.now()).total_seconds()
        except (TypeError, ValueError):
            left = None
        if left is not None and left < sar_cloud.REFRESH_MARGIN_SEC:
            try:
                prov.renew_access()
            except Exception:
                # Не вышло -- пусть отказ случится на настоящем запросе, с
                # понятным сообщением. Молча глотать нельзя, но и падать
                # здесь незачем: вызывающий сам обработает.
                pass
    return prov


def cloud_display_roots(conn):
    """id подключения -> имя его корня в дереве."""
    out = {}
    for a in cloud_accounts(conn, enabled_only=False):
        out[a["id"]] = (a.get("label") or "").strip()             or CLOUD_PROVIDER_NAMES.get(a["provider"], a["provider"])
    return out


def material_display_path(rec, op_root, cloud_roots):
    """Путь, по которому материал ПОКАЗЫВАЕТСЯ -- не тот, что хранится.

    Корень подключения кладётся ВНУТРЬ папки операции: дерево начинается от
    неё, и всё, что снаружи, считается «вне операции» и показывается плоским
    списком. А материал из подключённой папки операции ей как раз
    принадлежит -- значит и в дереве должен быть внутри.

    Хранимый rel_path при этом не меняется: по нему ищется совпадение
    материала, и переписать его значит завести дубли.

    Вынесено отдельной функцией, потому что нужно в двух местах -- обходу
    папок и поиску по операции. Посчитать его там и там по-своему значит
    получить дерево и поиск, которые расходятся в том, где лежит файл.
    """
    rel = (rec["rel_path"] or "").replace("\\", "/")
    acc = rec.get("cloud_account_id")
    if acc and acc in cloud_roots:
        return "/".join(p for p in (op_root, cloud_roots[acc], rel) if p)
    return rel


def operation_materials_flat(conn, operation_id):
    """Все материалы операции одним плоским списком -- для поиска.

    Поиск по ТЕКУЩЕЙ папке бесполезен: человек ищет файл как раз тогда,
    когда не помнит, в какой он папке. На 206 материалах список отдаётся
    целиком и фильтруется в браузере -- это мгновенно и без похода на
    сервер на каждую букву.
    """
    op = get_operation(conn, operation_id)
    if op is None:
        return None
    root = (op["folder"] or "").replace("\\", "/").strip("/")
    cloud_roots = cloud_display_roots(conn)

    sources = get_settings(conn).get("material_sources", "all")
    out = []
    for r in materials_of_operation(conn, operation_id):
        r = dict(r)
        in_cloud = bool(r.get("cloud_file_id"))
        if sources == "local" and in_cloud:
            continue
        if sources == "cloud" and not in_cloud:
            continue
        full = material_display_path(r, root, cloud_roots)
        folder, _, name = full.rpartition("/")
        out.append({
            "report_id": r["report_id"],
            "name": name or full,
            "folder": folder,
            "rel_path": r["rel_path"],
            "kind": r["kind"],
            "status": r["status"],
            "in_cloud": in_cloud,
        })
    out.sort(key=lambda x: (x["folder"].lower(), x["name"].lower()))
    return out


def browse_operation(conn, watch_dir, operation_id, subpath=""):
    """Содержимое одной папки операции: подпапки и материалы в ней.

    Ходим по папкам, как в проводнике: показываем ровно один уровень, а не
    всё дерево. Так одинаково работает и на телефоне, и при сотнях файлов --
    список не превращается в кашу.

    Материалы берутся из СВЯЗЕЙ операции, а файлы с диска -- только чтобы
    узнать, в какой папке они лежат. Материал, привязанный к операции, но
    лежащий вне её папки, не теряется: он показывается в корне помеченным.
    """
    op = get_operation(conn, operation_id)
    if op is None:
        return None
    linked = {r["report_id"]: dict(r)
              for r in materials_of_operation(conn, operation_id)}

    # ОТБОР ПО ИСТОЧНИКУ. Настройка из админки: подключив большое
    # хранилище, полезно на время оставить в списке только его -- или
    # наоборот, скрыть облачное, пока разбирают то, что уже на машине.
    # Материал при этом никуда не девается: он остаётся привязанным к
    # операции, просто не показывается.
    sources = get_settings(conn).get("material_sources", "all")
    if sources == "local":
        linked = {k: v for k, v in linked.items() if not v.get("cloud_file_id")}
    elif sources == "cloud":
        linked = {k: v for k, v in linked.items() if v.get("cloud_file_id")}
    # СТРУКТУРА ОБЛАЧНОЙ ПАПКИ СОХРАНЯЕТСЯ.
    #
    # В облаке материал разложен по-своему: на боевом подключении это папки
    # по датам съёмки, а папка операции на диске называется иначе. Такой
    # путь не начинается с имени папки операции, и без отдельной обработки
    # все 150 файлов сваливались бы в «вне папки операции» одной плоской
    # кучей -- то есть та раскладка, которую человек сделал в облаке,
    # пропадала бы ровно там, где он её ищет.
    #
    # Поэтому у каждого подключения свой корень в дереве: под ним лежит его
    # собственная структура, как есть. Хранимый rel_path при этом НЕ
    # меняется -- по нему ищется совпадение материала, и трогать его
    # значит заводить дубли.
    root = (op["folder"] or "").replace("\\", "/").strip("/")

    cloud_roots = cloud_display_roots(conn)

    def display_rel(rec):
        return material_display_path(rec, root, cloud_roots)

    here = "/".join(p for p in (root, subpath.strip("/")) if p)

    by_rel = {}
    for r in linked.values():
        by_rel.setdefault(display_rel(r), []).append(r)

    folders, files, outside = {}, [], []
    for rel, recs in by_rel.items():
        if here and not rel.startswith(here + "/"):
            if not subpath:                       # вне папки операции
                outside.extend(recs)
            continue
        if not here:
            tail = rel
        else:
            tail = rel[len(here) + 1:]
        if "/" in tail:                           # лежит глубже -- это папка
            name = tail.split("/", 1)[0]
            f = folders.setdefault(name, {"name": name, "files": 0})
            f["files"] += len(recs)
        else:
            files.extend(recs)

    # пустые папки берём с диска: в связях их нет по определению, а человек
    # ожидает увидеть свою структуру целиком
    disk = os.path.join(watch_dir, here.replace("/", os.sep)) if here else watch_dir
    if os.path.isdir(disk):
        try:
            for entry in os.scandir(disk):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name in SERVICE_DIRS or entry.name.startswith("."):
                    continue
                folders.setdefault(entry.name, {"name": entry.name, "files": 0})
        except OSError:
            pass

    return {"operation": dict(op),
            "path": subpath.strip("/"),
            "folders": sorted(folders.values(), key=lambda f: f["name"].lower()),
            "files": sorted(files, key=lambda r: (r["rel_path"] or "").lower()),
            "outside": outside if not subpath else []}


# Статус, означающий "это не находка". Такие отметки в список находок не
# попадают: человек их как раз ОТКЛОНИЛ.
PRIORITY_REJECTED = "rejected"


def operation_findings(conn, operation_id):
    """Находки операции -- только размеченные ЧЕЛОВЕКОМ.

    Сцены модели сами по себе сюда не попадают: их тысячи, и почти всё --
    камни. Утопить в них десяток настоящих находок значит сделать вкладку
    бесполезной.

    Находка -- это одно из двух:
      * ручная пометка человека (всегда, со своим статусом триажа, если он
        поставлен);
      * сцена модели, которую человек посмотрел и НЕ отклонил.

    Отклонённое отсюда НЕ вырезается: отбор по статусу -- дело интерфейса,
    там он переключается мгновенно и без новых запросов (находок десятки, а
    не тысячи). Прятать данные на этом уровне значило бы лишить человека
    возможности пересмотреть отбракованное, а в поиске к отвергнутому
    возвращаются.

    А вот одну вещь здесь сделать неправильно легко, и она делала список
    вдвое длиннее правды: триаж, поставленный НА РУЧНУЮ ПОМЕТКУ, -- это не
    отдельная находка, а статус той же самой. Отдельной строкой он
    дублировал пометку: одна находка выглядела как две.
    """
    out = []
    triage = _triage_by_target(conn, operation_id)

    for r in conn.execute(
            "SELECT o.*, rp.rel_path FROM manual_observations o "
            "JOIN operation_materials m ON m.report_id=o.report_id "
            "JOIN reports rp ON rp.report_id=o.report_id "
            "WHERE m.operation_id=? ORDER BY o.created_at DESC",
            (operation_id,)):
        d = dict(r)
        d["kind"] = "manual"
        # статус подмешивается в саму пометку, а не идёт отдельной строкой
        status = triage.get(("manual", str(d["id"])))
        if status is not None:
            d["priority"] = status["priority"]
            d["priority_by"] = status["set_by"]
        out.append(d)
    # Столбец называется set_at, а не updated_at.
    #
    # Здесь стояло "ORDER BY p.updated_at" -- такого столбца в
    # detection_priorities нет и никогда не было. Запрос падал ВСЕГДА, а
    # обёртка try/except Exception: pass это молча съедала. В итоге ни одна
    # из 47 отметок триажа никогда не показывалась во вкладке находок: люди
    # ставили "точно человек" и "предположительно человек", а список находок
    # делал вид, что таких отметок нет вовсе.
    #
    # Глухого перехвата тут больше нет. Отсутствие таблицы -- законная
    # ситуация (база от старой версии), и она проверяется явно; а вот ошибка
    # в самом запросе обязана быть видна, а не притворяться пустотой.
    # Сцены модели -- только те, что человек посмотрел и не отклонил.
    for (target, _ref), status in triage.items():
        if target != "ai_scene":
            continue
        d = dict(status)
        d["target_kind"] = "ai_scene"
        d["kind"] = "triage"
        out.append(d)
    return out


def _triage_by_target(conn, operation_id):
    """Статусы триажа операции: (на что, ключ) -> запись."""
    if not _table_exists(conn, "detection_priorities"):
        # база от старой версии -- законная ситуация, а вот ошибку в самом
        # запросе прятать нельзя, поэтому здесь проверка, а не try/except
        return {}

    out = {}
    for r in conn.execute(
            "SELECT p.*, rp.rel_path FROM detection_priorities p "
            "JOIN operation_materials m ON m.report_id=p.report_id "
            "JOIN reports rp ON rp.report_id=p.report_id "
            "WHERE m.operation_id=? ORDER BY p.set_at DESC",
            (operation_id,)):
        d = dict(r)
        out[(d["kind"], str(d["ref_key"]))] = d
    return out


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def unsorted_materials(conn):
    """Материалы без единой операции -- раздел «Не разобрано».

    Это не отдельная сущность, а зона ожидания: файл кинули в корень
    watch_dir, он обработался как обычно, но к операции не привязан.
    Нужна, чтобы человек не был обязан заранее думать про структуру.
    """
    return conn.execute(
        "SELECT r.* FROM reports r LEFT JOIN operation_materials m "
        "ON m.report_id=r.report_id WHERE m.report_id IS NULL "
        "ORDER BY r.file_ctime DESC, r.rel_path").fetchall()


def touch_heartbeat(conn, service, note=None):
    """Отметка "процесс жив". Вызывается из цикла воркера и бота.

    Пишем в ту же базу, через которую процессы и так общаются -- никакого
    отдельного канала, никакой зависимости одного процесса от другого.
    """
    import os as _os
    conn.execute(
        "INSERT INTO service_heartbeat (service, last_seen, pid, note) VALUES (?,?,?,?) "
        "ON CONFLICT(service) DO UPDATE SET last_seen=excluded.last_seen, "
        "pid=excluded.pid, note=excluded.note",
        (service, datetime.now().isoformat(), _os.getpid(), note))
    conn.commit()


def heartbeat_age_sec(conn, service):
    """Секунд с последней отметки, либо None если процесс не отмечался ни разу."""
    row = conn.execute(
        "SELECT last_seen FROM service_heartbeat WHERE service=?", (service,)).fetchone()
    if row is None or not row["last_seen"]:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(row["last_seen"])).total_seconds()
    except ValueError:
        return None


def generate_access_token():
    """Персональный ключ входа. secrets -- криптостойкий источник, длины
    32 символов достаточно, чтобы ключ нельзя было подобрать перебором."""
    import secrets
    return secrets.token_urlsafe(24)


def find_person_by_token(conn, token):
    """Запись человека по персональному ключу, либо None.

    Отклонённые (status='denied') не опознаются намеренно: отзыв доступа
    должен работать сразу, даже если ссылка у человека осталась."""
    if not token:
        return None
    row = conn.execute(
        "SELECT chat_id, username, first_name, status, role FROM telegram_access_requests "
        "WHERE access_token=?", (token,)).fetchone()
    if row is None or row["status"] != "approved":
        return None
    return row


def display_name_for(row):
    """Как показывать человека в интерфейсе: @username, иначе имя из Telegram."""
    if row["username"]:
        return f"@{row['username']}"
    return row["first_name"] or f"id{row['chat_id']}"


def ai_scene_ref_key(object_class, source, first_frame_idx, first_bbox):
    """Стабильный отпечаток AI-сцены для detection_priorities -- НЕ
    позиционный group_id (см. group_hits_into_scenes), тот меняется при
    переобработке видео. ОБЯЗАТЕЛЬНО единая функция для sar_server.py
    (что видит и размечает человек в плеере) и sar_dataset_export.py (что
    потом читает при экспорте) -- иначе они с гарантией разъедутся.

    class:source:first_frame_idx одного НЕДОСТАТОЧНО -- реальный баг,
    найденный на живых данных: на одном видео сразу несколько разных
    ложных "person"-детекций стартовали на кадре 0 (в разных углах кадра),
    их ref_key совпадал, и триаж одной сцены (проставленный человеком)
    тихо задевал/затирал приоритет двух других. Добавляем координаты
    центра первого бокса, огрублённые до сетки 20px -- разводит одновременно
    стартующие, но пространственно разные треки, и в то же время не
    рассыпается от мелкого дрожания координат при повторном инференсе
    (не бит-в-бит детерминированном на CPU)."""
    cx = (first_bbox[0] + first_bbox[2]) / 2.0
    cy = (first_bbox[1] + first_bbox[3]) / 2.0
    return f"{object_class}:{source}:{first_frame_idx}:{round(cx / 20) * 20}:{round(cy / 20) * 20}"

DEFAULT_SERVER_CONFIG = {
    "watch_dir": ".",
    # Где держать служебные данные: базу, отчёты, прокси-копии, превью.
    # None -- внутри watch_dir, как было всегда.
    #
    # Разделять стоит, когда материал уезжает на другой носитель: в облако
    # или на большой внешний диск. Отчёты туда отправлять нельзя -- их
    # сотни тысяч штук и они мелкие (на боевых данных 573 805 файлов при
    # 7,4 ГБ), любая сетевая или exFAT-папка на таком встаёт колом.
    "data_dir": None,
    "host": "0.0.0.0",
    "port": 8080,
    "shared_password": "change_me",
    "poll_interval_sec": 15,
    # Автоматически ставить КАЖДЫЙ новый файл в очередь на анализ моделью.
    #
    # false -- файлы всё равно появляются в списке, доступны для ручного
    # просмотра сразу (плеер/просмотр снимка, полоса покрытия, разметка
    # находок), но модель по ним не запускается, пока человек сам не нажмёт
    # "обработать" на нужном файле.
    #
    # Зачем выключать: на CPU одна минута видео считается ~30 минут, и
    # автоматическая обработка всего подряд забивает машину на сутки вперёд,
    # мешая тем, кто в это же время смотрит видео руками. Когда материала
    # много, а процессор один, разумнее выбирать, что действительно нужно
    # прогнать через модель.
    "auto_process": True,
    "workers": 1,

    # --- лёгкая копия видео для плеера ---
    #
    # Съёмка с дрона идёт на 30 Мбит/с. Чтобы смотреть её в реальном
    # времени, столько же нужно КАЖДОМУ зрителю, а канал наружу один.
    # Копия при том же разрешении весит примерно вчетверо меньше.
    #
    # Разрешение НЕ понижается намеренно: человек ищет объекты размером в
    # десяток пикселей, и 720p съел бы половину линейного размера -- это
    # прямая потеря того, ради чего всё делается.
    #
    # crf 26 выбран по замеру на реальной находке: сине-жёлтый предмет
    # 41x32 px на осыпи остаётся отчётливо виден. Сжатие съедает мелкую
    # фактуру камней, а находки различаются цветом и формой, и это кодек
    # сохраняет. Меньше число -- лучше качество и больше файл.
    "proxy_video": True,
    "proxy_crf": 26,
    "proxy_preset": "veryfast",
    # Сколько ядер отдать кодированию. Остальные остаются детектору: он
    # важнее, копия -- удобство.
    "proxy_threads": 4,
    # как часто плеер опрашивает сервер за новыми рамками модели, пока
    # видео ещё обрабатывается. Жёстко зажимается снизу до 5 секунд на
    # сервере (см. player_page() в sar_server.py) — конфиг не может
    # заставить одного клиента долбить сервер чаще этого, даже по ошибке.
    "player_ai_poll_interval_sec": 5,
}


# ---------------------------------------------------------------------------
# КОНФИГ
# ---------------------------------------------------------------------------

def load_server_config(script_dir):
    cfg = dict(DEFAULT_SERVER_CONFIG)
    config_path = os.path.join(script_dir, "sar_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            full_cfg = json.load(f)
        cfg.update(full_cfg.get("server", {}))
    return cfg, config_path


DEFAULT_TELEGRAM_BOT_CONFIG = {
    "bot_token": "",
    "admin_chat_ids": [],
    "service_url": "",
    "guide_url": "",
    "presentation_url": "",
    # Дашборд мониторинга. Адрес у бесплатного туннеля временный и меняется
    # при каждом перезапуске, поэтому /status в боте -- единственное место,
    # где его удобно посмотреть, не заходя на машину.
    #
    # Учётка тут ТОЛЬКО ДЛЯ ЧТЕНИЯ и намеренно не админская: Grafana это
    # админ-панель, выставлять её наружу с полными правами значит открыть
    # интернету и брутфорс, и её собственные уязвимости.
    "grafana_url": "",
    "grafana_login": "",
    "grafana_password": "",
    # Временная автовыдача доступа: ISO-время, ДО которого новые заявки
    # одобряются сами, без участия координатора. Пусто = выключено (обычный
    # режим с ручным одобрением).
    #
    # Сделано временем, а НЕ простым флагом, намеренно: включают такое на
    # ночь или на время операции, а выключить потом забывают -- и гейт,
    # который и есть единственная защита доступа к боевому инструменту,
    # тихо остаётся открытым навсегда. По истечении срока бот сам
    # возвращается к ручному одобрению, ничего не нужно помнить.
    #
    # Координатору уведомление приходит в любом случае -- он утром видит,
    # кто получил доступ ночью.
    "auto_approve_until": "",
    # Через сколько секунд бот удаляет сообщение с личной ссылкой.
    #
    # Смысл: ссылка -- это ключ на предъявителя. Оставаясь в переписке, она
    # живёт в истории Telegram, попадает в резервные копии и видна любому,
    # кто заглянет в чужой телефон через плечо.
    #
    # ВНИМАНИЕ на короткие значения: человек получает уведомление, открывает
    # Telegram -- и если срок уже истёк, ссылки нет. Придётся снова писать
    # /help. Чем меньше число, тем чаще это будет происходить.
    # 0 -- не удалять вовсе.
    "personal_link_ttl_sec": 3,
}


def load_telegram_bot_config(script_dir):
    """Настройки sar_telegram_bot.py -- отдельный ключ "telegram_bot" в том же
    sar_config.json. Пароль НЕ дублируется отдельным полем -- берётся из
    server.shared_password, чтобы не было двух источников правды и бот не
    начал выдавать устаревший пароль после его смены в server-секции."""
    cfg = dict(DEFAULT_TELEGRAM_BOT_CONFIG)
    config_path = os.path.join(script_dir, "sar_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            full_cfg = json.load(f)
        cfg.update(full_cfg.get("telegram_bot", {}))
        cfg["service_password"] = full_cfg.get("server", {}).get(
            "shared_password", DEFAULT_SERVER_CONFIG["shared_password"])
    else:
        cfg["service_password"] = DEFAULT_SERVER_CONFIG["shared_password"]
    return cfg, config_path


# ---------------------------------------------------------------------------
# НАСТРОЙКИ, МЕНЯЕМЫЕ НА ХОДУ
#
# ОДНО место, где описано, что вообще можно крутить: имя, тип, умолчание,
# границы и человеческое название. Всё остальное -- страница админки,
# проверка ввода, применение в воркере -- строится отсюда.
#
# Почему реестр, а не просто чтение ключей из базы. Настройка без границ --
# это способ выстрелить себе в ногу через веб-форму: "загрузок 50"
# положит и канал, и квоту облака, и саму машину. Границы живут рядом с
# определением, и сервер зажимает значение по ним ВСЕГДА -- ввести
# опасное значение нельзя даже намеренно.
#
# Тот же принцип уже применён к player_ai_poll_interval_sec: сервер
# зажимает минимум 5 секунд, и конфиг не может заставить клиента
# опрашивать чаще.
# ---------------------------------------------------------------------------

SETTINGS_SCHEMA = {
    "downloads_in_flight": {
        "type": "int", "default": 1, "min": 1, "max": 4,
        "label": "Одновременных загрузок из облака",
        "help": "Главный рычаг расхода канала. На бытовом подключении "
                "больше двух обычно не ускоряет, а мешает: файлы качаются "
                "параллельно и ни один не доходит до конца.",
    },
    "staging_cap_gb": {
        "type": "float", "default": 4.0, "min": 1.0, "max": 100.0,
        "label": "Потолок временной папки, ГБ",
        "help": "Сколько места отдано скачанным оригиналам. Самый большой "
                "файл операции -- 1,92 ГБ, обрабатывается один за раз, так "
                "что 4 ГБ хватает с запасом. Когда место кончается, самые "
                "давно не нужные оригиналы удаляются: отчёт и лёгкая копия "
                "уже сделаны, а сам файл при надобности качается заново.",
    },
    "material_touches_per_pass": {
        "type": "int", "default": 3, "min": 1, "max": 50,
        "label": "Файлов за один обход папки",
        "help": "Сколько файлов разрешено ПРОЧИТАТЬ за проход ради превью "
                "и длительности. Без ограничения подключение папки с "
                "полусотней видео означает попытку скачать всё разом.",
    },
    "daily_traffic_gb": {
        "type": "float", "default": 0.0, "min": 0.0, "max": 10000.0,
        "label": "Суточный лимит трафика, ГБ (0 -- без лимита)",
        "help": "Страховка от исчерпания квоты облака. По достижении "
                "лимита загрузки останавливаются до следующих суток, а в "
                "журнал пишется явная запись -- молча платформа не тормозит.",
    },
    "material_sources": {
        "type": "choice", "default": "all",
        "options": [
            {"value": "all", "label": "локальные и облачные"},
            {"value": "local", "label": "только на этой машине"},
            {"value": "cloud", "label": "только облачные"},
        ],
        "label": "Какой материал показывать",
        "help": "Подключив большое хранилище, полезно на время оставить в "
                "списке только его -- или наоборот, скрыть облачное, пока "
                "разбирают то, что уже на машине. На сам материал это не "
                "влияет: ничего не удаляется и не отвязывается от операции.",
    },
    "auto_process": {
        "type": "bool", "default": True,
        "label": "Ставить новые файлы в очередь автоматически",
        "help": "Выключенным удобно подключать большое хранилище: файлы "
                "появляются в списке и доступны для ручного просмотра, но "
                "модель по ним не запускается, пока человек не попросит.",
    },
}


def _coerce_setting(key, raw):
    """Приводит значение к типу из реестра и зажимает по границам.

    Зажимает, а не отвергает: администратор ввёл 50 загрузок -- получит 4
    и увидит это в форме. Отказ с ошибкой заставил бы гадать, что
    допустимо, а тихое принятие 50 положило бы канал.
    """
    spec = SETTINGS_SCHEMA[key]
    t = spec["type"]
    if t == "choice":
        # Неизвестное значение -- к умолчанию. Отвергать нечего: выбор
        # приходит из списка, который сама же платформа и отдала, а
        # несовпадение означает устаревшую вкладку.
        allowed = {o["value"] for o in spec["options"]}
        v = str(raw or "").strip()
        return v if v in allowed else spec["default"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "да")
    try:
        v = int(raw) if t == "int" else float(raw)
    except (TypeError, ValueError):
        return spec["default"]
    return max(spec["min"], min(spec["max"], v))


def get_settings(conn):
    """Все настройки: умолчания из реестра, поверх -- то, что в базе.

    Неизвестный ключ в базе игнорируется, а не роняет выдачу: настройку
    могли убрать из кода, а строка осталась.
    """
    out = {k: v["default"] for k, v in SETTINGS_SCHEMA.items()}
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    except Exception:
        return out
    for r in rows:
        key = r["key"] if hasattr(r, "keys") else r[0]
        val = r["value"] if hasattr(r, "keys") else r[1]
        if key in SETTINGS_SCHEMA:
            out[key] = _coerce_setting(key, val)
    return out


def set_setting(conn, key, value, who=None):
    """Записывает одну настройку. Возвращает то, что реально сохранено."""
    if key not in SETTINGS_SCHEMA:
        raise KeyError(key)
    v = _coerce_setting(key, value)
    conn.execute(
        "INSERT INTO settings (key, value, set_by, set_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "set_by=excluded.set_by, set_at=excluded.set_at",
        (key, "1" if v is True else "0" if v is False else str(v),
         who, datetime.now().isoformat()))
    conn.commit()
    return v


# ---------------------------------------------------------------------------
# ПОДКЛЮЧЁННЫЕ ОБЛАЧНЫЕ ХРАНИЛИЩА
#
# ТОКЕНЫ -- СЕКРЕТЫ. Наружу (в интерфейс, в журнал, в метрики) они не
# выходят никогда: cloud_accounts_public() отдаёт всё, кроме них. Показать
# токен в админке «для удобства» значит положить его в историю браузера, в
# скриншот и в пересланное сообщение.
# ---------------------------------------------------------------------------

def add_cloud_account(conn, provider, token, label=None, refresh_token=None,
                       expires_at=None, root_id=None, root_name=None, who=None,
                       client_id=None, client_secret=None):
    cur = conn.execute(
        "INSERT INTO cloud_accounts (provider, label, token, refresh_token, "
        "expires_at, root_id, root_name, enabled, added_by, added_at, "
        "client_id, client_secret) "
        "VALUES (?,?,?,?,?,?,?,1,?,?,?,?)",
        (provider, label, token, refresh_token, expires_at, root_id,
         root_name, who, datetime.now().isoformat(),
         client_id, client_secret))
    conn.commit()
    return cur.lastrowid


def cloud_accounts(conn, enabled_only=True):
    """Полные записи, С ТОКЕНАМИ -- только для воркера."""
    q = "SELECT * FROM cloud_accounts"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY id"
    try:
        return [dict(r) for r in conn.execute(q).fetchall()]
    except Exception:
        return []


def cloud_accounts_public(conn):
    """То же самое БЕЗ токенов -- для интерфейса и API.

    Отдельная функция, а не фильтр на месте использования: фильтр, который
    надо не забыть применить, рано или поздно забудут. Здесь забыть нечего.
    """
    out = []
    for a in cloud_accounts(conn, enabled_only=False):
        a.pop("token", None)
        a.pop("refresh_token", None)
        a.pop("client_secret", None)
        # client_id секретом не считается (он виден в адресной строке при
        # авторизации), но и показывать его незачем -- в интерфейсе важно
        # другое: ЕСТЬ ли чем продлевать доступ.
        a["can_refresh"] = bool(a.pop("client_id", None))
        out.append(a)
    return out


def update_cloud_account(conn, account_id, **fields):
    allowed = {"label", "token", "refresh_token", "expires_at", "root_id",
               "root_name", "enabled", "last_error", "last_ok_at",
               "operation_id", "client_id", "client_secret"}
    bad = set(fields) - allowed
    if bad:
        raise KeyError(", ".join(sorted(bad)))
    if not fields:
        return
    sets = ", ".join("%s=?" % k for k in fields)
    conn.execute("UPDATE cloud_accounts SET %s WHERE id=?" % sets,
                 tuple(fields.values()) + (account_id,))
    conn.commit()


def delete_cloud_account(conn, account_id):
    """Отключает хранилище и прибирает за ним материал.

    БЕЗ ЭТОГО записи материала остаются ссылаться на исчезнувшее
    подключение. Корня в дереве у них больше нет, и все файлы проваливаются
    в «вне папки операции» плоской кучей -- ровно так диск и «пропадал»
    после переподключения.

    Что делаем с записями:
      * чисто облачные, по которым никто не работал, -- удаляем: без
        хранилища они пустые ссылки;
      * те, где есть работа человека (пометки, обсуждения, просмотр), --
        ОСТАВЛЯЕМ, сняв привязку к облаку. Терять сделанное людьми нельзя
        ни при каких обстоятельствах, даже если сам файл стал недоступен.
    """
    rows = conn.execute(
        "SELECT report_id, rel_path FROM reports WHERE cloud_account_id=?",
        (account_id,)).fetchall()
    removed = kept = 0
    for r in rows:
        rid = r["report_id"]
        work = conn.execute(
            "SELECT (SELECT COUNT(*) FROM manual_observations WHERE report_id=?) "
            "     + (SELECT COUNT(*) FROM detection_comments WHERE report_id=?) "
            "     + (SELECT COUNT(*) FROM watch_segments WHERE report_id=?) "
            "     + (SELECT COUNT(*) FROM detection_priorities WHERE report_id=?) AS n",
            (rid, rid, rid, rid)).fetchone()["n"]
        if work:
            conn.execute(
                "UPDATE reports SET cloud_account_id=NULL, cloud_file_id=NULL, "
                "cloud_size=NULL, proxy_requested=0 WHERE report_id=?", (rid,))
            kept += 1
        else:
            conn.execute("DELETE FROM operation_materials WHERE report_id=?", (rid,))
            conn.execute("DELETE FROM reports WHERE report_id=?", (rid,))
            removed += 1
    conn.execute("DELETE FROM cloud_accounts WHERE id=?", (account_id,))
    conn.commit()
    return {"removed": removed, "kept": kept}





def resolve_paths(watch_dir, data_dir=None):
    """watch_dir -> (watch_dir_abs, data_dir, db_path, reports_dir), создавая
    служебные папки при необходимости. И sar_server.py, и sar_worker.py
    вызывают это одинаково, чтобы гарантированно смотреть на один и тот же
    набор путей независимо от того, какой из двух процессов стартовал раньше.

    ЗАЧЕМ data_dir ОТДЕЛЬНО ОТ watch_dir. Раньше служебная папка жёстко
    лежала внутри наблюдаемой: data_dir = watch_dir/sar_data. Пока и то и
    другое на одном диске, разницы нет. Но материал и служебные данные --
    это две РАЗНЫЕ по характеру нагрузки:

      * материал -- десятки крупных файлов, читаются подряд и по одному
        разу; их место -- в облаке или на большом медленном диске;
      * служебное -- сотни тысяч мелких файлов отчётов плюс база SQLite,
        которые нужны мгновенно и постоянно; их место -- на локальном SSD.

    Смешивать их в одной папке значит либо тащить 21 ГБ материала на
    системный диск, либо класть полмиллиона мелких файлов в облако, где
    они гарантированно встанут колом.

    Поэтому data_dir настраивается (server.data_dir в конфиге). Умолчание
    сохранено прежнее -- ни одна существующая установка не переедет сама.
    """
    watch_dir = os.path.abspath(watch_dir)
    data_dir = (os.path.abspath(data_dir) if data_dir
                else os.path.join(watch_dir, "sar_data"))
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "sar_data.db")
    reports_dir = os.path.join(data_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    return watch_dir, data_dir, db_path, reports_dir


BACKUPS_DIR_NAME = "sar_backups"


def backups_dir():
    """Куда кладутся резервные копии базы -- ОДНА точка правды.

    Была рассинхронизация, и она сделала мониторинг бесполезным именно в
    том месте, где он нужен: sar_backup.py писал копии в папку ВНУТРИ
    проекта, а проверка здоровья искала их РЯДОМ с проектом (на уровень
    выше). Проверка честно рапортовала "последняя копия 56 ч назад" сразу
    после свежей копии, и тревога о протухших бэкапах не погасла бы
    никогда, сколько их ни делай.

    Считается от расположения самого sar_common.py, а не от watch_dir:
    watch_dir настраивается и может указывать куда угодно, а копии должны
    лежать там же, куда их кладёт скрипт, при любой настройке.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        BACKUPS_DIR_NAME)


# ---------------------------------------------------------------------------
# БД (SQLite) — схема общая для обоих процессов
# ---------------------------------------------------------------------------

def get_db_connection(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    """Идемпотентно — безопасно вызывать из обоих процессов при старте,
    неважно, в каком порядке они запускаются."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS reports (
        report_id TEXT PRIMARY KEY,
        rel_path TEXT NOT NULL,
        abs_path TEXT NOT NULL,
        kind TEXT NOT NULL,               -- 'video' | 'photo'
        status TEXT NOT NULL DEFAULT 'queued',  -- queued|processing|done|error
        progress_pct REAL DEFAULT 0,
        total_frames INTEGER,
        fps REAL,
        duration_sec REAL,
        error TEXT,
        out_dir TEXT,
        file_ctime REAL,
        phase TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        line TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_logs_report ON logs(report_id, id);
    CREATE TABLE IF NOT EXISTS watch_segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id TEXT NOT NULL,
        viewer_name TEXT NOT NULL,
        start_sec REAL NOT NULL,
        end_sec REAL NOT NULL,
        ts TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_watch_report ON watch_segments(report_id);
    CREATE TABLE IF NOT EXISTS presence (
        viewer_name TEXT PRIMARY KEY,
        last_seen REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS manual_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id TEXT NOT NULL,
        viewer_name TEXT NOT NULL,
        timestamp_sec REAL NOT NULL,
        bbox TEXT NOT NULL,   -- JSON [x1,y1,x2,y2], нормализовано 0..1 от размера кадра
        label TEXT,
        note TEXT,
        lat REAL,             -- координаты ДРОНА (из телеметрии по timestamp_sec)
        lon REAL,
        est_lat REAL,          -- "вероятные координаты" объекта -- ОЦЕНКА, см. sar_common.estimate_ground_point
        est_lon REAL,
        est_distance_m REAL,
        raw_telemetry TEXT,    -- весь текст SRT-блока на этом кадре как есть, см. parse_srt_telemetry
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_manual_obs_report ON manual_observations(report_id);
    -- Точки, поставленные человеком ПРЯМО НА КАРТЕ.
    --
    -- Отдельная таблица, а не строка в manual_observations, потому что это
    -- другая сущность. Пометка в плеере -- наблюдение В КАДРЕ: у неё есть
    -- материал, таймкод, рамка, а координаты ВЫЧИСЛЕНЫ из телеметрии.
    -- Точка на карте -- место на земле, и она ни к какому кадру не
    -- привязана: «группа сообщила отсюда», «это ущелье не облетали»,
    -- «свидетель указал сюда».
    --
    -- Втиснув её в manual_observations, пришлось бы разрешить пустой
    -- report_id, и тогда каждая выборка пометок по материалу, каждый
    -- подсчёт покрытия и каждая страница находки обязаны были бы помнить
    -- про строки без материала. Забыли бы -- и появились бы находки,
    -- которые не открываются.
    CREATE TABLE IF NOT EXISTS map_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id INTEGER NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        label TEXT,
        note TEXT,
        viewer_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_map_marks_op ON map_marks(operation_id);
    -- Разобранный трек дрона: результат чтения SRT, а не сам SRT.
    --
    -- Разбор делает ВОРКЕР, а не веб-слой. Сервер по устройству проекта
    -- только читает базу и файлы; когда трек считался по запросу, каждый
    -- холодный запрос разбирал 114 файлов телеметрии (2,9 с), а кеш жил в
    -- памяти процесса и умирал при перезапуске.
    --
    -- Пустой points (строка "[]") -- это «разобрали, телеметрии нет», а НЕ
    -- «ещё не разбирали». Без такой отметки 95 видео без телеметрии
    -- разбирались бы заново каждый проход, вечно.
    --
    -- bbox хранится отдельно, чтобы карта могла выставить границы, не
    -- читая все точки.
    CREATE TABLE IF NOT EXISTS telemetry_tracks (
        report_id TEXT PRIMARY KEY,
        points TEXT NOT NULL,        -- JSON [[lat,lon],...], уже прорежено
        raw_points INTEGER NOT NULL, -- сколько было до прореживания
        first_sec REAL,
        last_sec REAL,
        min_lat REAL, max_lat REAL,
        min_lon REAL, max_lon REAL,
        source TEXT,                 -- какой SRT прочитан
        source_mtime REAL,           -- чтобы перечитать, если файл заменили
        parsed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS detection_priorities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id TEXT NOT NULL,
        kind TEXT NOT NULL,        -- 'ai_scene' | 'manual' -- НЕ путать с полем
                                    -- "source" самой детекции (model/color) --
                                    -- это дискриминатор ДРУГОЙ оси: откуда сама
                                    -- запись триажа (авто-сцена или ручная метка)
        ref_key TEXT NOT NULL,     -- ai_scene: "class:source:first_frame_idx"
                                    -- (стабильный отпечаток содержимого сцены,
                                    -- НЕ позиционный group_id -- тот меняется
                                    -- при переобработке видео, см. обсуждение
                                    -- с пользователем); manual: id из
                                    -- manual_observations (уже стабилен)
        priority TEXT NOT NULL,    -- confirmed_person|likely_person|
                                    -- confirmed_object|likely_object|rejected
        set_by TEXT NOT NULL,      -- viewer_name -- система НИКОГДА не пишет
                                    -- в эту таблицу сама, только по явному
                                    -- действию залогиненного человека
        set_at TEXT NOT NULL,
        UNIQUE(report_id, kind, ref_key)
    );
    CREATE INDEX IF NOT EXISTS idx_priorities_report ON detection_priorities(report_id);
    CREATE TABLE IF NOT EXISTS detection_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id TEXT NOT NULL,
        kind TEXT NOT NULL,        -- 'ai_scene' | 'manual', как в detection_priorities
        ref_key TEXT NOT NULL,     -- тот же ключ, что и у статуса находки:
                                    -- ai_scene -- отпечаток содержимого сцены
                                    -- (см. ai_scene_ref_key), manual -- id
                                    -- наблюдения. Благодаря этому обсуждение
                                    -- не теряется при переобработке видео
        author TEXT NOT NULL,      -- viewer_name из сессии
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_comments_report ON detection_comments(report_id, kind, ref_key);
    CREATE TABLE IF NOT EXISTS telegram_access_requests (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied
        requested_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by INTEGER
    );

    -- Операция -- поисковые работы целиком: "Курумды, август 2026".
    -- Человек мыслит поисками, а не файлами; плоский список материалов
    -- работает, пока поиск один, и перестаёт на третьем.
    CREATE TABLE IF NOT EXISTS operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        area TEXT,                     -- район работ
        client TEXT,                   -- заказчик, если работа платная
        coordinator TEXT,
        folder TEXT,                   -- имя папки в watch_dir, если операция заведена папкой
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Связь материала с операцией вынесена в отдельную таблицу, а не в
    -- колонку у reports. Так одним механизмом закрываются все три случая:
    --   связей нет   -> материал в "Не разобрано" (лежит в корне watch_dir);
    --   одна связь   -> материал в операции;
    --   две и больше -> один вылет пригодился в двух поисках.
    -- Разметка при этом остаётся привязанной к САМОМУ МАТЕРИАЛУ (триаж,
    -- комментарии, покрытие -- по report_id), поэтому работа общая: если
    -- вылет разобрали в одной операции, во второй его не пересматривают.
    -- Пересматривать час уже разобранного видео в поиске непозволительно.
    CREATE TABLE IF NOT EXISTS operation_materials (
        operation_id INTEGER NOT NULL,
        report_id TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (operation_id, report_id)
    );
    CREATE INDEX IF NOT EXISTS idx_opmat_report ON operation_materials(report_id);

    -- Признак жизни фоновых процессов. Без него смерть воркера неотличима
    -- от "сейчас нечего обрабатывать": очередь пуста в обоих случаях, и
    -- узнают об этом, только когда кто-то положит видео и оно не начнёт
    -- считаться. На поисковой операции это часы потерянного времени.
    CREATE TABLE IF NOT EXISTS service_heartbeat (
        service TEXT PRIMARY KEY,     -- worker | bot
        last_seen TEXT NOT NULL,
        pid INTEGER,
        note TEXT
    );

    -- Журнал уже отправленных тревог: нужен, чтобы слать сообщение при
    -- СМЕНЕ состояния, а не каждую проверку. Иначе через сутки на алерты
    -- перестанут смотреть -- и пропустят настоящий.
    -- НАСТРОЙКИ, МЕНЯЕМЫЕ НА ХОДУ
    --
    -- Почему в базе, а не в sar_config.json. Меняет их администратор через
    -- веб-страницу -- то есть СЕРВЕР. А применяет их воркер: это он читает
    -- файлы и качает материал. Сервер и воркер -- разные процессы, которые
    -- по устройству проекта общаются ТОЛЬКО через эту базу. Запиши сервер
    -- новое значение в конфиг-файл -- воркер узнает о нём в лучшем случае
    -- после перезапуска, а качать продолжит по-старому. Через базу он
    -- перечитывает настройку на каждом проходе.
    --
    -- В sar_config.json остаются секреты и то, что нужно ДО старта (порт,
    -- пути, пароль). Здесь -- только то, что осмысленно крутить на ходу.
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,          -- всегда текст, разбор по SETTINGS_SCHEMA
        set_by TEXT,                  -- кто поменял: спросить будет у кого
        set_at TEXT
    );

    -- ПОДКЛЮЧЁННЫЕ ОБЛАЧНЫЕ ХРАНИЛИЩА
    --
    -- Здесь лежат ТОКЕНЫ ДОСТУПА -- это секреты того же уровня, что общий
    -- пароль и токен бота. Вся папка sar_data/ исключена из git (см.
    -- .gitignore), и выносить токены в конфиг или в логи нельзя ни при
    -- каких обстоятельствах.
    --
    -- Почему в базе, а не в sar_config.json: подключает диск администратор
    -- через веб-страницу, а читает воркер. Между ними только эта база --
    -- см. соседнюю таблицу settings.
    CREATE TABLE IF NOT EXISTS cloud_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,       -- google / yandex, см. sar_cloud.PROVIDERS
        label TEXT,                   -- как человек назвал это подключение
        token TEXT NOT NULL,
        refresh_token TEXT,
        expires_at TEXT,              -- когда токен протухнет, ISO
        root_id TEXT,                 -- папка с материалом внутри хранилища
        root_name TEXT,               -- её человеческое имя для интерфейса
        enabled INTEGER NOT NULL DEFAULT 1,
        last_error TEXT,              -- почему последний раз не вышло
        last_ok_at TEXT,              -- когда последний раз всё получилось
        added_by TEXT,
        added_at TEXT
    );

    CREATE TABLE IF NOT EXISTS alert_state (
        check_name TEXT PRIMARY KEY,
        level TEXT NOT NULL,          -- текущее наблюдаемое состояние
        since TEXT NOT NULL,          -- когда оно началось (для подтверждения)
        notified_at TEXT,
        -- о чём человеку уже сказали. Хранится отдельно от level именно
        -- чтобы пережить смену состояния: без этого после восстановления
        -- неизвестно, сообщали ли о падении, и "снова работает" уходит
        -- тем, кто про падение не слышал.
        notified_level TEXT
    );
    """)
    conn.commit()

    # миграции для БД, созданных более ранними версиями схемы
    for alter_sql in ("ALTER TABLE reports ADD COLUMN file_ctime REAL",
                       "ALTER TABLE reports ADD COLUMN phase TEXT",
                       # откуда взялся файл: если из облака, здесь лежит
                       # подключение и идентификатор файла в нём
                       # к какой операции относится подключённая папка:
                       # структура в облаке своя, и по имени папки операцию
                       # не угадать
                       "ALTER TABLE cloud_accounts ADD COLUMN operation_id INTEGER",
                       # Для продления доступа к Google. Токен там живёт ЧАС,
                       # и без обновления диск приходится подключать заново
                       # каждый час -- на подготовку 150 файлов (около трёх
                       # часов) этого не хватает по определению.
                       #
                       # client_secret -- секрет того же уровня, что и токен:
                       # вся папка sar_data/ исключена из репозитория.
                       "ALTER TABLE cloud_accounts ADD COLUMN client_id TEXT",
                       "ALTER TABLE cloud_accounts ADD COLUMN client_secret TEXT",
                       "ALTER TABLE reports ADD COLUMN cloud_account_id INTEGER",
                       "ALTER TABLE reports ADD COLUMN cloud_file_id TEXT",
                       "ALTER TABLE reports ADD COLUMN cloud_size INTEGER",
                       # человек попросил подготовить этот файл к просмотру:
                       # скачать из облака и собрать лёгкую копию. Само
                       # ничего не качается -- 150 файлов это около 88 ГБ.
                       "ALTER TABLE reports ADD COLUMN proxy_requested INTEGER",
                       "ALTER TABLE manual_observations ADD COLUMN est_lat REAL",
                       "ALTER TABLE manual_observations ADD COLUMN est_lon REAL",
                       "ALTER TABLE manual_observations ADD COLUMN est_distance_m REAL",
                       "ALTER TABLE manual_observations ADD COLUMN raw_telemetry TEXT",
                       # персональный ключ входа и роль -- см. ROLE_* ниже
                       "ALTER TABLE telegram_access_requests ADD COLUMN access_token TEXT",
                       "ALTER TABLE telegram_access_requests ADD COLUMN role TEXT",
                       # куда вести человека после одобрения: он мог прийти
                       # по вечной ссылке на конкретную находку, и к моменту
                       # выдачи доступа эта цель должна пережить и ожидание,
                       # и перезапуск бота (сторож туннеля перезапускает его
                       # при каждом обрыве канала)
                       "ALTER TABLE telegram_access_requests ADD COLUMN pending_target TEXT",
                       # notified_level появился ПОСЛЕ того, как alert_state уже
                       # была создана на боевой базе. CREATE TABLE IF NOT EXISTS
                       # существующую таблицу не меняет, поэтому без этой строки
                       # тревоги падали на каждой проверке с "no such column" --
                       # молча, в лог бота, при внешне работающем мониторинге.
                       "ALTER TABLE alert_state ADD COLUMN notified_level TEXT"):
        try:
            conn.execute(alter_sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже есть

    rows = conn.execute("SELECT report_id, abs_path FROM reports WHERE file_ctime IS NULL").fetchall()
    for report_id, abs_path in rows:
        if os.path.exists(abs_path):
            conn.execute("UPDATE reports SET file_ctime=? WHERE report_id=?",
                         (get_file_ctime(abs_path), report_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# report_id: имя файла + дата создания файла + короткий хэш пути (уникальность)
# ---------------------------------------------------------------------------

def make_report_id(name, abs_path):
    stem = os.path.splitext(os.path.basename(name))[0]
    safe_stem = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", stem)[:60] or "file"
    # Дату берём через get_file_ctime -- ОДНУ функцию на весь проект.
    #
    # Здесь была вторая, собственная копия той же логики, и в ней запасной
    # путь падал точно так же, как основной: у несуществующего файла
    # getmtime бросает ровно то же исключение, что и getctime. Для файла из
    # ОБЛАКА локального пути нет вовсе -- и это роняло ВЕСЬ проход
    # наблюдения, из-за чего не регистрировался ни один материал, ни
    # облачный, ни локальный.
    #
    # Ровно та ловушка, про которую отдельный раздел в CLAUDE.md: одно и то
    # же, посчитанное в двух местах, расходится. Починили одно место --
    # второе осталось.
    ctime = get_file_ctime(abs_path)
    date_str = datetime.fromtimestamp(ctime).strftime("%Y%m%d_%H%M%S")
    path_hash = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{safe_stem}__{date_str}__{path_hash}"


# ---------------------------------------------------------------------------
# ПУТИ ВЫЧИСЛЯЮТСЯ, А НЕ ХРАНЯТСЯ
#
# В reports есть столбцы abs_path и out_dir с АБСОЛЮТНЫМИ путями. Они
# избыточны: оба выводятся из того, что уже лежит рядом в той же строке.
# Проверено на боевой базе -- out_dir совпал с расчётом во всех 58 записях,
# abs_path расходился только разделителем (/ против \).
#
# Чем плохо хранить. Абсолютный путь прибивает базу к одной машине, одной
# букве диска и одной операционной системе. Пока всё живёт на D: одного
# ноутбука, это незаметно; при любом переезде -- на другой диск, на сетевую
# папку с материалом, на VPS под Linux -- все 58 записей превращаются в
# ссылки в никуда. Причём молча: строка в базе есть, файла по ней нет.
#
# Это ровно та грабля, что уже описана в CLAUDE.md про папку резервных
# копий, только в другом виде: там путь СЧИТАЛСЯ в трёх местах и разошёлся,
# здесь он ХРАНИТСЯ в двух формах (rel_path и abs_path) и расходится при
# переезде. Лечение одно -- одна точка правды. Ею становится rel_path.
#
# Столбцы остаются в схеме: их пишет воркер при регистрации, ими пользуются
# внешние разовые скрипты, и сносить их ради чистоты значит ломать то, что
# работает. Но ЧИТАТЬ их платформа больше не должна -- только эти две
# функции.
# ---------------------------------------------------------------------------

def material_path(watch_dir, rel_path):
    """Абсолютный путь к файлу материала.

    rel_path хранится с прямыми слэшами независимо от системы (так его
    формирует scan_all_materials), поэтому разбираем именно по "/", а не
    по os.sep: на Linux os.path.join с виндовым разделителем внутри строки
    молча склеил бы один сегмент вместо двух.
    """
    parts = [x for x in str(rel_path or "").replace("\\", "/").split("/") if x]
    return os.path.join(watch_dir, *parts) if parts else watch_dir


def report_dir(reports_dir, report_id):
    """Папка отчёта. report_id уже безопасен как имя файла -- он собран из
    очищенного имени, даты и хэша (см. make_report_id)."""
    return os.path.join(reports_dir, str(report_id))


def find_material_file(watch_dir, data_dir, rel_path):
    """Где на диске лежит файл материала СЕЙЧАС, или None.

    Мест два: наблюдаемая папка и временная папка скачанного из облака.
    Всё, что читает файл -- превью, просмотр снимка, отдача оригинала --
    обязано спрашивать здесь, а не складывать путь самостоятельно.

    Иначе получается то, что уже случилось: материал скачан и готов, а
    страница отвечает «файл не найден на диске», потому что смотрит только
    в одно из двух мест. Причём для каждого потребителя отдельно -- то есть
    чинить пришлось бы в каждом.
    """
    local = material_path(watch_dir, rel_path)
    if os.path.exists(local):
        return local
    try:
        import sar_staging
        staged = sar_staging.Staging(
            sar_staging.staging_dir(data_dir), cap_bytes=1).path_for(rel_path)
    except Exception:
        return None
    return staged if os.path.exists(staged) else None


def get_file_ctime(abs_path):
    """Дата создания файла, 0.0 если узнать нельзя.

    Раньше при недоступном файле второй вызов (getmtime) падал ровно так
    же, как первый, и исключение уходило наружу. Это роняло ВЕСЬ список
    материалов из-за одной строки: файл, удалённый между обходом папки и
    чтением атрибутов, делал страницу недоступной целиком. Та же категория,
    что и пустой out_dir, который когда-то ронял список с TypeError.

    Теперь это нужно и для облака: у файла, который ещё не скачан, даты
    создания на диске нет вовсе.
    """
    for fn in (os.path.getctime, os.path.getmtime):
        try:
            return fn(abs_path)
        except OSError:
            continue
    return 0.0


# ---------------------------------------------------------------------------
# Сканирование watch_dir — ТОЛЬКО корень папки запуска, без рекурсии.
# Осознанно: любая рекурсия рано или поздно натыкается на собственные же
# выходные папки (crops/frames с уже готовыми детекциями) под каким угодно
# именем и начинает гонять детектор по своим же результатам. Не заходить
# в подпапки вообще — самый надёжный способ исключить это в принципе.
# Если нужны видео из подпапок — кладите их (или симлинк на них) в корень.
# ---------------------------------------------------------------------------

# Каталоги, внутрь которых заглядывать нельзя ни при каком обходе: там
# служебные данные, резервные копии, готовые отчёты с десятками тысяч кропов
# и телеметрия. Попади они в дерево -- человек увидит мусор вместо своих
# папок, а обход подорожает на порядки.
SERVICE_DIRS = {"sar_data", "sar_dataset", "sar_backups", "monitoring",
                "proxies",
                "telemetry", "weights", ".git", ".claude", "__pycache__",
                ".pytest_cache", "tests", "static", "node_modules"}


def scan_watch_dir(watch_dir):
    """Медиафайлы прямо в корне рабочего каталога.

    Осталась для совместимости и как страховка: файл, положенный мимо
    операции, не должен исчезать бесследно. Основной обход теперь идёт по
    папкам операций -- см. scan_operation_folder.
    """
    found = []
    with os.scandir(watch_dir) as it:
        for entry in it:
            if not entry.is_file(follow_symlinks=True):
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in MEDIA_EXTS:
                kind = "video" if ext in VIDEO_EXTS else "photo"
                found.append((entry.name, entry.path, kind))
    return found


def scan_operation_folder(watch_dir, folder, max_depth=12):
    """Рекурсивный обход папки операции. Возвращает (файлы, папки).

    Заказчик раскладывает материал так же, как у себя в облаке: по дням,
    бортам, вылетам. Скачанную папку с Google или Яндекс Диска должно быть
    достаточно распаковать внутрь операции, и она отобразится как есть.

    Файлы отдаются с путём ОТНОСИТЕЛЬНО watch_dir, а не одним именем. Это
    принципиально: одноимённые DJI_0001.MP4 из папок «борт 1» и «борт 2» --
    разные материалы, а поиск записи идёт по rel_path. Плоское имя схлопнуло
    бы их в одну запись и потеряло второй файл.

    Пустые папки тоже возвращаются: человек ожидает увидеть свою структуру
    целиком, а не только те ветки, где нашлась съёмка.

    Глубина ограничена: облачные выгрузки бывают вложены как угодно, а
    симлинк на родителя устроил бы бесконечный обход.
    """
    root = os.path.join(watch_dir, folder) if folder else watch_dir
    files, dirs = [], []
    if not os.path.isdir(root):
        return files, dirs

    base_depth = root.rstrip(os.sep).count(os.sep)
    for cur, subdirs, filenames in os.walk(root, followlinks=False):
        if cur.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            subdirs[:] = []
        subdirs[:] = sorted(d for d in subdirs if d not in SERVICE_DIRS
                             and not d.startswith("."))

        for d in subdirs:
            rel = os.path.relpath(os.path.join(cur, d), watch_dir)
            dirs.append(rel.replace(os.sep, "/"))

        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in MEDIA_EXTS:
                continue
            abs_path = os.path.join(cur, name)
            rel = os.path.relpath(abs_path, watch_dir).replace(os.sep, "/")
            kind = "video" if ext in VIDEO_EXTS else "photo"
            files.append((rel, abs_path, kind))
    return files, dirs


def operation_for_path(watch_dir, rel_path):
    """К какой операции относится файл по своему расположению.

    Определяется по метке .sar_operation в папке верхнего уровня. Нужна,
    чтобы файл, положенный в папку операции, попадал в неё САМ. Без этого
    материалы находились и обрабатывались, но висели в «Не разобрано» -- то
    есть папка операции не работала как папка операции.
    """
    rel = (rel_path or "").replace("\\", "/").strip("/")
    if "/" not in rel:
        return None                       # лежит в корне watch_dir
    top = rel.split("/", 1)[0]
    if top in SERVICE_DIRS or top.startswith("."):
        return None
    return read_operation_marker(os.path.join(watch_dir, top))


def attach_by_folder(conn, watch_dir, report_id, rel_path):
    """Привязывает материал к операции, в чьей папке он лежит.

    Вызывается при появлении нового файла. Повторный вызов безвреден, а
    ручную привязку к другой операции не ломает: связи складываются, а не
    заменяют друг друга.
    """
    op_id = operation_for_path(watch_dir, rel_path)
    if op_id is None:
        return None
    if get_operation(conn, op_id) is None:
        return None                       # метка осталась от удалённой операции
    attach_material(conn, op_id, report_id)
    return op_id


def match_existing_material(conn, rel_path, watch_dir):
    """Находит уже известную запись для файла из облака.

    ПОЧЕМУ НЕ ПРОСТО ПО rel_path. В облаке материал может лежать иначе, чем
    локально: на боевом подключении путь в облаке -- "2026 08 11/DJI_1.MP4",
    а в базе -- "Курумды август 2026/DJI_1.MP4". Совпадение по пути не
    срабатывает, и тот же файл заводится ВТОРОЙ записью: вся работа по нему
    (пометки, обсуждения, отметки просмотра) остаётся на первой, невидимой.
    Именно так проект уже терял покрытие -- 73 процента вместо 82.

    Поэтому вторым заходом сверяемся по ИМЕНИ ФАЙЛА. Но только когда оно
    однозначно: если таких имён в базе несколько, угадывать нельзя --
    привязать работу не к тому материалу хуже, чем завести новый.

    Возвращает (запись, что_делать):
      ("skip")   -- файл уже есть локально, облачная копия не нужна;
      ("adopt")  -- запись есть, а файла на диске нет: облако его вернёт;
      ("new")    -- ничего похожего, заводим новую запись.
    """
    row = conn.execute("SELECT * FROM reports WHERE rel_path=?",
                       (rel_path,)).fetchone()
    if row is not None:
        return row, "adopt"

    name = os.path.basename(str(rel_path or "").replace("\\", "/"))
    same = conn.execute(
        "SELECT * FROM reports WHERE rel_path=? OR rel_path LIKE ?",
        (name, "%/" + name)).fetchall()
    if len(same) != 1:
        return None, "new"

    row = same[0]
    local = material_path(watch_dir, row["rel_path"])
    if os.path.exists(local):
        # Локальная копия на месте и уже разобрана. Качать её из облака
        # незачем, а заводить вторую запись -- прямой путь к потере работы.
        return row, "skip"
    # Файла нет, а работа по нему есть. Облако возвращает к нему доступ --
    # это лучшее, что вообще может дать подключение.
    return row, "adopt"


def cloud_timestamp(value):
    """Дата файла из облака в секундах, 0.0 если её нет.

    Нужна, чтобы облачный материал становился в список ПО ДАТЕ СЪЁМКИ, а
    не сваливался в конец общей кучей: на боевом подключении это 150
    файлов, и без даты они оказывались за всем локальным материалом.

    Google отдаёт "2026-08-15T10:00:00.000Z", Яндекс --
    "2026-08-15T10:00:00+00:00". Разбираем оба, а при неудаче честно
    возвращаем ноль вместо выдуманного значения.
    """
    if not value:
        return 0.0
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, OSError):
        return 0.0


def scan_cloud_materials(conn, list_folder=None, log=None, telemetry_out=None):
    """Материал в подключённых облачных хранилищах.

    Возвращает [(rel_path, kind, account_id, file_id, size)].

    telemetry_out -- необязательный список, куда складываются найденные
    рядом SRT в том же виде. Отдельным списком, а не в общей выдаче:
    телеметрия не материал, и попав в reports она стала бы «файлом» в
    списке, в очереди обработки и в знаменателе покрытия.

    rel_path строится КАК У ЛОКАЛЬНОГО ФАЙЛА -- "Папка/Файл.MP4". Это не
    косметика: по rel_path ищется существующая запись в reports (см.
    watcher_loop), и если облачный файл получит другое имя, тот же самый
    материал заведётся второй записью. На дублях в этом проекте уже
    обжигались: было 38 видео и 39 фото вместо 34 и 24.

    Поэтому ПЕРЕНОС МАТЕРИАЛА В ОБЛАКО С СОХРАНЕНИЕМ СТРУКТУРЫ ПАПОК
    подхватывает существующие записи вместе со всей проделанной по ним
    работой -- пометками, обсуждениями, отметками просмотра.

    Обход рекурсивный, но с ограничением глубины: облачную папку человек
    может выбрать любую, включая корень диска со всем накопленным за годы.

    Ошибка одного хранилища не отменяет остальные и не роняет обход: она
    записывается в last_error этого подключения и видна в админке.
    """
    out = []
    for acc in cloud_accounts(conn, enabled_only=True):
        try:
            lister = list_folder or _cloud_lister(acc, conn)
            _walk_cloud(lister, acc.get("root_id") or "", "", out, acc, depth=0,
                        telemetry_out=telemetry_out)
            update_cloud_account(conn, acc["id"], last_error=None,
                                  last_ok_at=datetime.now().isoformat())
        except Exception as e:
            update_cloud_account(conn, acc["id"], last_error=str(e))
            if log:
                log("[облако] %s: %s" % (acc.get("label") or acc["provider"], e))
    return out


CLOUD_MAX_DEPTH = 3


def _cloud_lister(acc, conn=None):
    """Листинг с продлением доступа. conn нужен, чтобы сохранить новый
    токен: без сохранения он продлевался бы на каждом проходе заново."""
    return provider_for_account(conn, acc).list_folder


def _walk_cloud(list_folder, folder_id, prefix, out, acc, depth,
                telemetry_out=None):
    if depth > CLOUD_MAX_DEPTH:
        return
    for item in list_folder(folder_id):
        rel = (prefix + "/" + item.name) if prefix else item.name
        if item.is_folder:
            _walk_cloud(list_folder, item.id, rel, out, acc, depth + 1,
                        telemetry_out)
            continue
        ext = os.path.splitext(item.name)[1].lower()
        if ext in TELEMETRY_EXTS:
            # Собираем ОТДЕЛЬНО от материала: SRT не должен попасть в reports.
            if telemetry_out is not None:
                telemetry_out.append((rel, acc["id"], item.id, item.size,
                                      cloud_timestamp(item.modified)))
            continue
        if ext not in MEDIA_EXTS:
            continue
        kind = "video" if ext in VIDEO_EXTS else "photo"
        out.append((rel, kind, acc["id"], item.id, item.size,
                    cloud_timestamp(item.modified)))
    return out


def scan_all_materials(watch_dir):
    """Все медиафайлы: в папках операций и оставшиеся в корне.

    Единая точка обхода для воркера и сервера -- чтобы список файлов и
    очередь обработки не могли разойтись в том, что считают материалом.

    Корень продолжает просматриваться намеренно: файл, положенный мимо
    операции, не должен исчезать бесследно. Он попадёт в «Не разобрано»,
    откуда его перекладывают в нужную операцию.
    """
    seen, found = set(), []
    for op_id, name, path in operation_folders(watch_dir):
        files, _ = scan_operation_folder(watch_dir, name)
        for rel, abs_path, kind in files:
            key = os.path.normcase(os.path.abspath(abs_path))
            if key in seen:
                continue
            seen.add(key)
            found.append((rel, abs_path, kind))
    for rel, abs_path, kind in scan_watch_dir(watch_dir):
        key = os.path.normcase(os.path.abspath(abs_path))
        if key not in seen:
            seen.add(key)
            found.append((rel, abs_path, kind))
    return found


def operation_folders(watch_dir):
    """Папки верхнего уровня, помеченные как операции.

    Помечена -- значит внутри лежит .sar_operation. Папка без метки не
    операция, а просто папка: пользователь мог создать её для своих нужд,
    и превращать её в операцию самовольно нельзя.
    """
    out = []
    if not os.path.isdir(watch_dir):
        return out
    with os.scandir(watch_dir) as it:
        for entry in it:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in SERVICE_DIRS or entry.name.startswith("."):
                continue
            op_id = read_operation_marker(entry.path)
            if op_id is not None:
                out.append((op_id, entry.name, entry.path))
    return sorted(out)


# ---------------------------------------------------------------------------
# ПРЕВЬЮ (первый кадр видео) для списка файлов -- генерирует sar_worker.py
# (см. watcher_loop), sar_server.py только ОТДАЁТ уже готовый файл (та же
# граница ответственности, что и для report.html/detections.json/crops --
# сервер ничего не обрабатывает сам). Путь строится ОДНОЙ функцией, общей
# для обоих процессов, чтобы имя файла превью не могло разойтись между
# тем, кто его пишет, и тем, кто его отдаёт.
# ---------------------------------------------------------------------------

def get_thumbnail_path(data_dir, filename):
    """Путь превью. filename -- rel_path материала (может содержать папки).

    К очищенному имени добавляется хэш ПОЛНОГО относительного пути. Без
    него материалы «борт 1/DJI_0001.MP4» и «борт 2/DJI_0001.MP4» получили бы
    одно имя превью и перезаписали друг друга -- в списке показывался бы
    чужой кадр, причём молча. С появлением папок внутри операции такие
    совпадения перестали быть редкостью: облачные выгрузки почти всегда
    содержат одинаковые имена в разных папках.
    """
    thumbnails_dir = os.path.join(data_dir, "thumbnails")
    os.makedirs(thumbnails_dir, exist_ok=True)
    norm = str(filename).replace("\\", "/")
    stem = os.path.splitext(os.path.basename(norm))[0]
    safe_name = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", stem)[:80] or "video"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return os.path.join(thumbnails_dir, f"{safe_name}__{digest}.jpg")


def proxy_video_path(data_dir, filename):
    """Лёгкая копия видео для плеера.

    Съёмка с дрона идёт на 30 Мбит/с: чтобы смотреть её в реальном времени,
    столько же нужно каждому зрителю через туннель. Копия весит в несколько
    раз меньше при том же разрешении -- разрешение понижать нельзя, человек
    ищет объекты размером в десяток пикселей.

    Оригинал остаётся нетронутым: по нему работает детектор, и он же
    доступен в плеере кнопкой, когда нужно разглядеть вплотную.

    Ключ -- как у превью: очищенное имя плюс хэш полного пути, иначе файлы
    с одинаковыми именами в разных папках операции перезапишут друг друга.
    """
    proxies_dir = os.path.join(data_dir, "proxies")
    os.makedirs(proxies_dir, exist_ok=True)
    norm = str(filename).replace("\\", "/")
    stem = os.path.splitext(os.path.basename(norm))[0]
    safe_name = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", stem)[:80] or "video"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return os.path.join(proxies_dir, f"{safe_name}__{digest}.mp4")


def finding_preview_path(data_dir, observation_id, full=False):
    """Кадр находки.

    У ручной пометки НЕТ готовой картинки: человек обвёл область прямо на
    проигрываемом видео, и на диске остались только таймкод и координаты
    рамки. Поэтому кадр приходится вырезать отдельно -- этим занимается
    воркер, потому что это обработка видео, а сервер файлы только отдаёт.

    Ключ -- id наблюдения: он уникален сам по себе, и хэш от пути здесь не
    нужен (в отличие от превью материала, где совпадающие имена файлов в
    разных папках -- обычное дело).

    Файла два, и они разные по назначению:

    * обычный -- мелкий (960 px), рамка ВЖЖЕНА в картинку. Его показывает
      сетка находок, где важна скорость: таких превью на экране десятки.
    * full -- крупный и БЕЗ рамки. Его подгружает окно предпросмотра и
      страница кадра, где картинку увеличивают. Рамка там рисуется поверх
      в SVG: она остаётся чёткой на любом масштабе и её можно выключить,
      чтобы посмотреть на находку своими глазами, а не в обводке.
    """
    previews_dir = os.path.join(data_dir, "finding_previews")
    os.makedirs(previews_dir, exist_ok=True)
    suffix = "_full" if full else ""
    return os.path.join(previews_dir, f"obs_{int(observation_id)}{suffix}.jpg")


# ---------------------------------------------------------------------------
# ТЕЛЕМЕТРИЯ (SRT) — единый индекс папки telemetry/, общий для
# sar_video_review.py (CLI/воркер), sar_batch.py и sar_server.py (плеер).
#
# Раньше SRT искался ТОЛЬКО рядом с видео (то же имя + .srt в watch_dir).
# В реальном использовании телеметрия часто лежит отдельно -- целой папкой
# экспорта с полётов (например "12.08.2026 Субтитры полетов/*.SRT"), и
# видео её просто не находило: GPS в отчётах молча отсутствовал, хотя
# файл с координатами лежал прямо в проекте.
#
# telemetry/ сканируется РЕКУРСИВНО (в отличие от watch_dir с видео/фото --
# там рекурсия осознанно исключена, см. scan_watch_dir), потому что это
# папка ТОЛЬКО с телеметрией: рекурсивная обработка "по кругу" ей не грозит
# (в ней не бывает видео/фото, которые сама же система туда положила).
# ---------------------------------------------------------------------------

DEFAULT_TELEMETRY_DIR_NAME = "telemetry"

_DJI_TIMESTAMP_RE = re.compile(r"(\d{14})")


def resolve_telemetry_dir(base_dir, telemetry_dir_name=DEFAULT_TELEMETRY_DIR_NAME):
    """base_dir/telemetry_dir_name -- создаётся автоматически, если ещё нет,
    чтобы папку можно было сразу увидеть и начать класть туда SRT."""
    tdir = Path(base_dir) / telemetry_dir_name
    tdir.mkdir(parents=True, exist_ok=True)
    return tdir


def _extract_dji_timestamp(filename):
    """DJI по умолчанию называет файлы DJI_YYYYMMDDHHMMSS_NNNN_Z.* -- это
    и есть метка времени начала записи, встроенная прямо в имя файла.
    Используется как дешёвый (без чтения содержимого файлов) резервный
    способ сопоставить видео и SRT, если их имена не совпадают дословно."""
    m = _DJI_TIMESTAMP_RE.search(Path(filename).stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def build_telemetry_index(telemetry_dir):
    """Рекурсивно индексирует все .srt-файлы в telemetry_dir ОДИН РАЗ
    (вызывающий код кэширует результат сам -- см. использование в
    sar_video_review.py/sar_batch.py/sar_server.py), а не на каждое видео.

    Возвращает {"by_stem": {имя_без_расширения_lower: Path},
                "by_timestamp": [(datetime, Path), ...]} -- второе только
    для файлов, чьё имя удалось разобрать как метку времени DJI.

    Устойчиво к отсутствующей/недоступной папке и к ошибкам чтения --
    в этих случаях просто возвращает пустой индекс с предупреждением в
    консоль, никогда не бросает исключение наружу (анализ видео должен
    продолжаться и без телеметрии)."""
    by_stem, by_timestamp = {}, []
    telemetry_dir = Path(telemetry_dir)
    if not telemetry_dir.is_dir():
        return {"by_stem": by_stem, "by_timestamp": by_timestamp}

    try:
        candidates = [p for p in telemetry_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() == ".srt"]
    except OSError as e:
        print(f"[телеметрия] не удалось просканировать {telemetry_dir}: {e}")
        return {"by_stem": by_stem, "by_timestamp": by_timestamp}

    for path in candidates:
        key = path.stem.lower()
        if key in by_stem and by_stem[key] != path:
            print(f"[телеметрия] неоднозначность имени '{key}': есть и {by_stem[key]}, "
                  f"и {path} -- использую первый найденный, второй игнорирую.")
        else:
            by_stem[key] = path
        ts = _extract_dji_timestamp(path.name)
        if ts is not None:
            by_timestamp.append((ts, path))

    return {"by_stem": by_stem, "by_timestamp": by_timestamp}


def find_telemetry_for_video(video_path, telemetry_index, max_fallback_gap=timedelta(minutes=5)):
    """Ищет SRT для видео в уже построенном индексе (build_telemetry_index).

    1. Точное совпадение имени файла без расширения (регистронезависимо) --
       однозначно, как и раньше.
    2. Если имена не совпали -- ближайшая по времени метка DJI в имени файла
       (DJI_YYYYMMDDHHMMSS_...), но только если разница не больше
       max_fallback_gap. Специально НЕ угадываем "самый близкий, что нашёлся,
       независимо от разницы" -- слишком большой разрыв означает, что это
       телеметрия скорее всего от другого вылета, и подставить её в отчёт
       было бы хуже, чем не подставить никакой (см. copybtn/GPS-текст --
       "нет GPS" явно лучше неверных координат).

    Возвращает (Path или None, человекочитаемая причина или None).
    """
    stem = Path(video_path).stem.lower()
    by_stem = telemetry_index.get("by_stem", {})
    if stem in by_stem:
        return by_stem[stem], "имя файла совпадает"

    by_timestamp = telemetry_index.get("by_timestamp", [])
    video_ts = _extract_dji_timestamp(os.path.basename(video_path))
    if video_ts is None or not by_timestamp:
        return None, None

    nearest_path, nearest_gap = None, None
    for ts, path in by_timestamp:
        gap = abs(ts - video_ts)
        if nearest_gap is None or gap < nearest_gap:
            nearest_gap, nearest_path = gap, path

    if nearest_path is not None and nearest_gap <= max_fallback_gap:
        return nearest_path, f"по метке времени в имени файла (разница {nearest_gap})"
    return None, None


# ---------------------------------------------------------------------------
# ОЦЕНКА КООРДИНАТ ОБНАРУЖЕННОГО ОБЪЕКТА (не дрона) по геометрии съёмки.
#
# GPS в SRT-телеметрии -- это позиция САМОГО ДРОНА в момент кадра, а не
# место, куда смотрит камера. Реальный объект в кадре может находиться в
# сотнях метров от точки прямо под дроном, если высота большая и/или подвес
# наклонён -- в этом проекте так и есть (реальные данные: высота ~1162 м,
# gb_pitch ~ -56.6°).
#
# estimate_ground_point() даёт ГРУБУЮ ОЦЕНКУ через простую тригонометрию:
# луч из объектива под известным углом (наклон подвеса + смещение пикселя
# от центра кадра в долях FOV) пересекает плоскость на высоте точки взлёта
# дрона. Специально называется "оценка"/"вероятные координаты" везде в UI,
# не "координаты объекта" -- у неё есть реальная, не всегда малая погрешность:
#
#   - FOV камеры -- НЕ приходит в SRT, задаётся конфигом camera_hfov_deg
#     (см. sar_video_review.DEFAULT_CONFIG). Неверное значение -> все оценки
#     систематически смещены.
#   - Зум -- SRT пишет focal_len (см. parse_srt_telemetry), но зная только
#     число без точных характеристик сенсора конкретной камеры, надёжно
#     пересчитать его в реальный FOV нельзя -- поэтому эта функция ВСЕГДА
#     использует один и тот же camera_hfov_deg, зум не учитывается. focal_len
#     всё равно показывается рядом в UI, чтобы человек мог на глаз заметить
#     "этот кадр был снят с зумом -- оценке доверять меньше".
#   - Ровность рельефа -- формула предполагает, что объект на той же высоте,
#     что и точка взлёта дрона. В горной местности с сильным перепадом высот
#     (а это именно такой случай) реальная погрешность может быть намного
#     больше, чем от одних лишь неточностей FOV/телеметрии.
#   - Крен (gb_roll) не учитывается -- предполагается, что подвес
#     стабилизирован по крену (в реальных данных gb_roll стабильно ~0).
# ---------------------------------------------------------------------------

EARTH_RADIUS_M = 6371000.0


DEFAULT_MAX_ESTIMATE_DISTANCE_M = 3000.0

# --- FOV из фактического зума кадра ---------------------------------------
# Раньше FOV был жёстко зафиксирован (camera_hfov_deg=84) на ВСЕ кадры. На
# реальных данных это оказалось грубой ошибкой: 87% детекций сняты с зумом,
# focal_len в SRT гуляет от 48 до 960 (20-кратный диапазон), а FOV при этом
# считался неизменным. Для объекта В ЦЕНТРЕ кадра это не влияло ни на что
# (смещение от оси = 0), но чем дальше объект от центра -- тем сильнее уезжала
# оценка: на медианном зуме до ~1 км у края кадра, на максимальном -- до ~1.9 км.
#
# Плюс отдельная путаница: 84 -- это ДИАГОНАЛЬНЫЙ FOV на самом широком угле
# (расчёт по матрице даёт 79.6), а использовался он как ГОРИЗОНТАЛЬНЫЙ, да ещё
# и вертикальный приравнивался к нему же (vertical_fov_deg=horizontal_fov_deg),
# хотя матрица не квадратная.
#
# Значения по умолчанию -- под DJI M30T (zoom-камера, файлы с суффиксом _Z),
# на которой снимали в этой операции: матрица 1/2" (6.4 x 4.8 мм), а focal_len
# в SRT пишется в ДЕСЯТЫХ долях мм физического фокусного (48.0 -> 4.8 мм,
# 960.0 -> 96.0 мм, что совпадает с паспортным диапазоном ~4.5-90 мм).
# ВСЁ ЭТО НАСТРАИВАЕТСЯ: другая камера/дрон -- поменяйте в sar_config.json,
# не полагайтесь на эти значения вслепую (тот же принцип, что и с порядком
# lat/lon в GPS(a,b,c) -- неверная догадка тут хуже отсутствия оценки).
DEFAULT_SENSOR_WIDTH_MM = 6.4
DEFAULT_SENSOR_HEIGHT_MM = 4.8
DEFAULT_FOCAL_LEN_SCALE = 0.1  # SRT-единицы -> мм


def fov_from_focal_len(focal_len_raw, sensor_width_mm=DEFAULT_SENSOR_WIDTH_MM,
                        sensor_height_mm=DEFAULT_SENSOR_HEIGHT_MM,
                        focal_len_scale=DEFAULT_FOCAL_LEN_SCALE):
    """(hfov_deg, vfov_deg) для КОНКРЕТНОГО кадра по его focal_len из SRT,
    либо None, если focal_len отсутствует/бессмысленный -- тогда вызывающий
    код должен откатиться на фиксированный camera_hfov_deg из конфига."""
    if focal_len_raw is None:
        return None
    try:
        f_mm = float(focal_len_raw) * focal_len_scale
    except (TypeError, ValueError):
        return None
    if f_mm <= 0:
        return None
    hfov = 2.0 * math.degrees(math.atan(sensor_width_mm / (2.0 * f_mm)))
    vfov = 2.0 * math.degrees(math.atan(sensor_height_mm / (2.0 * f_mm)))
    return hfov, vfov


def resolve_frame_fov(focal_len_raw, cfg):
    """(hfov, vfov) для кадра с учётом настроек. Приоритет -- фактический зум
    кадра (focal_len); если его нет -- фиксированный camera_hfov_deg из
    конфига, как раньше (vfov при этом приходится приравнивать к hfov, это
    заведомо грубее -- но лучше, чем ничего)."""
    if cfg.get("use_focal_len_fov", True):
        fov = fov_from_focal_len(
            focal_len_raw,
            sensor_width_mm=cfg.get("camera_sensor_width_mm", DEFAULT_SENSOR_WIDTH_MM),
            sensor_height_mm=cfg.get("camera_sensor_height_mm", DEFAULT_SENSOR_HEIGHT_MM),
            focal_len_scale=cfg.get("focal_len_scale", DEFAULT_FOCAL_LEN_SCALE))
        if fov is not None:
            return fov
    hfov = cfg.get("camera_hfov_deg", 84.0)
    return hfov, hfov


def estimate_ground_point(drone_lat, drone_lon, altitude_m, gimbal_yaw_deg, gimbal_pitch_deg,
                           bbox_center_frac_x, bbox_center_frac_y,
                           horizontal_fov_deg, vertical_fov_deg=None,
                           max_distance_m=DEFAULT_MAX_ESTIMATE_DISTANCE_M):
    """Грубая оценка координат точки на земле, на которую указывает пиксель
    (bbox_center_frac_x, bbox_center_frac_y) кадра -- доли от 0 до 1
    (0.5, 0.5 = центр кадра, куда прицелен сам подвес).

    Конвенция DJI для gimbal_pitch: 0° = горизонт, -90° = точно вниз (надир).
    gimbal_yaw принимается как АБСОЛЮТНЫЙ компасный азимут (0° = север, по
    часовой стрелке) -- как это обычно пишут SRT-логи DJI, но это не
    стопроцентно стандартизовано между прошивками (тот же класс
    неопределённости, что и с порядком lat/lon в GPS(a,b,c), см.
    parse_srt_telemetry) -- если оценки координат систематически "смотрят"
    не в ту сторону, сверьте с картой.

    max_distance_m -- ВАЖНАЯ защита от численного взрыва: при угле от надира,
    близком к 90° (почти горизонт, tan(x) -> бесконечность), даже небольшая
    неточность телеметрии/FOV даёт РЕЗУЛЬТАТ В ДЕСЯТКИ КИЛОМЕТРОВ -- формально
    "меньше 90°", но физически бессмысленно (объект в кадре явно не в 70 км
    от дрона). Без этой защиты такие оценки выглядели бы как настоящие
    координаты, хотя это чистый численный артефакт -- нашли на реальных
    данных (pitch около -9°, почти горизонт, дало 69.8 км). Лучше не отдавать
    оценку вообще, чем отдать координату, которая уведёт поиск на 70 км в
    сторону.

    Возвращает (est_lat, est_lon, horizontal_distance_m) или None, если
    входных данных недостаточно, луч не пересекает землю в разумных пределах
    (камера направлена выше/у горизонта), либо результат превышает
    max_distance_m."""
    if None in (drone_lat, drone_lon, altitude_m, gimbal_yaw_deg, gimbal_pitch_deg):
        return None
    if altitude_m <= 0:
        return None

    if vertical_fov_deg is None:
        vertical_fov_deg = horizontal_fov_deg  # грубое приближение при отсутствии отдельного значения

    # угловое отклонение конкретного пикселя от оптической оси (центра кадра)
    offset_x_deg = (bbox_center_frac_x - 0.5) * horizontal_fov_deg
    offset_y_deg = (bbox_center_frac_y - 0.5) * vertical_fov_deg

    # ниже центра кадра (offset_y > 0) -> луч смотрит ЕЩЁ ниже горизонта,
    # то есть эффективный pitch ещё более отрицательный
    total_pitch_deg = gimbal_pitch_deg - offset_y_deg
    total_yaw_deg = gimbal_yaw_deg + offset_x_deg

    # угол от надира: 0° = точно вниз (валидный случай, distance=0 -- ЭТО и
    # есть самый надёжный, не приближённый результат из всех), 90° = горизонт
    angle_from_nadir_deg = 90.0 + total_pitch_deg
    if angle_from_nadir_deg < 0 or angle_from_nadir_deg >= 90:
        return None  # физически невозможный угол или камера выше/на уровне горизонта

    horizontal_distance_m = altitude_m * math.tan(math.radians(angle_from_nadir_deg))
    if horizontal_distance_m > max_distance_m:
        return None  # угол формально валиден, но результат -- численный артефакт, не оценка
    bearing_rad = math.radians(total_yaw_deg % 360)

    delta_lat_rad = (horizontal_distance_m * math.cos(bearing_rad)) / EARTH_RADIUS_M
    delta_lon_rad = (horizontal_distance_m * math.sin(bearing_rad)) / (
        EARTH_RADIUS_M * math.cos(math.radians(drone_lat)))

    est_lat = drone_lat + math.degrees(delta_lat_rad)
    est_lon = drone_lon + math.degrees(delta_lon_rad)
    return est_lat, est_lon, horizontal_distance_m
