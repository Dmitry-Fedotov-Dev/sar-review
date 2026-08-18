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


def resolve_paths(watch_dir):
    """watch_dir -> (watch_dir_abs, data_dir, db_path, reports_dir), создавая
    служебные папки при необходимости. И sar_server.py, и sar_worker.py
    вызывают это одинаково, чтобы гарантированно смотреть на один и тот же
    набор путей независимо от того, какой из двух процессов стартовал раньше."""
    watch_dir = os.path.abspath(watch_dir)
    data_dir = os.path.join(watch_dir, "sar_data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "sar_data.db")
    reports_dir = os.path.join(data_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    return watch_dir, data_dir, db_path, reports_dir


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
                       "ALTER TABLE manual_observations ADD COLUMN est_lat REAL",
                       "ALTER TABLE manual_observations ADD COLUMN est_lon REAL",
                       "ALTER TABLE manual_observations ADD COLUMN est_distance_m REAL",
                       "ALTER TABLE manual_observations ADD COLUMN raw_telemetry TEXT",
                       # персональный ключ входа и роль -- см. ROLE_* ниже
                       "ALTER TABLE telegram_access_requests ADD COLUMN access_token TEXT",
                       "ALTER TABLE telegram_access_requests ADD COLUMN role TEXT"):
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
    try:
        # На разных ОС/ФС "дата создания" трактуется по-разному: ctime на
        # Linux — это время изменения МЕТАДАННЫХ, не создания файла (в
        # POSIX честного "birth time" в общем случае нет). Это лучшее
        # доступное приближение без сторонних зависимостей.
        ctime = os.path.getctime(abs_path)
    except OSError:
        ctime = os.path.getmtime(abs_path)
    date_str = datetime.fromtimestamp(ctime).strftime("%Y%m%d_%H%M%S")
    path_hash = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{safe_stem}__{date_str}__{path_hash}"


def get_file_ctime(abs_path):
    try:
        return os.path.getctime(abs_path)
    except OSError:
        return os.path.getmtime(abs_path)


# ---------------------------------------------------------------------------
# Сканирование watch_dir — ТОЛЬКО корень папки запуска, без рекурсии.
# Осознанно: любая рекурсия рано или поздно натыкается на собственные же
# выходные папки (crops/frames с уже готовыми детекциями) под каким угодно
# именем и начинает гонять детектор по своим же результатам. Не заходить
# в подпапки вообще — самый надёжный способ исключить это в принципе.
# Если нужны видео из подпапок — кладите их (или симлинк на них) в корень.
# ---------------------------------------------------------------------------

def scan_watch_dir(watch_dir):
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


# ---------------------------------------------------------------------------
# ПРЕВЬЮ (первый кадр видео) для списка файлов -- генерирует sar_worker.py
# (см. watcher_loop), sar_server.py только ОТДАЁТ уже готовый файл (та же
# граница ответственности, что и для report.html/detections.json/crops --
# сервер ничего не обрабатывает сам). Путь строится ОДНОЙ функцией, общей
# для обоих процессов, чтобы имя файла превью не могло разойтись между
# тем, кто его пишет, и тем, кто его отдаёт.
# ---------------------------------------------------------------------------

def get_thumbnail_path(data_dir, filename):
    thumbnails_dir = os.path.join(data_dir, "thumbnails")
    os.makedirs(thumbnails_dir, exist_ok=True)
    stem = os.path.splitext(filename)[0]
    safe_name = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", stem)[:100] or "video"
    return os.path.join(thumbnails_dir, f"{safe_name}.jpg")


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
