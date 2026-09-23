#!/usr/bin/env python3
"""
sar_telegram_bot.py -- отдельный, независимый процесс: Telegram-бот для
выдачи доступа волонтёрам/тестерам к SAR Review. Не трогает sar_worker.py/
sar_server.py вообще -- общается с ними только через ту же sar_data.db,
как и остальные два процесса (см. CLAUDE.md, "два независимых процесса").
Если бот упадёт или не запустится -- на обработку видео и веб-интерфейс
это никак не влияет.

Поток: человек пишет /start -> заявка появляется у координатора(ов) с
кнопками "одобрить"/"отклонить" -> ссылку, пароль и гайд бот присылает
САМ, только после одобрения. Пока не одобрено -- никакой конкретики
человек не получает вообще, только "заявка отправлена".

Запуск: python sar_telegram_bot.py
Настройки -- sar_config.json -> "telegram_bot": bot_token (см. @BotFather),
admin_chat_ids (кому приходят заявки на одобрение), service_url, guide_url,
presentation_url. Пароль отдельно не хранится -- берётся из
server.shared_password (см. sar_common.load_telegram_bot_config).
"""
import asyncio
import io
import json
import logging
import os
import sqlite3
import urllib.parse
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import sar_alerts
import sar_common

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ТОКЕН БОТА НЕ ДОЛЖЕН ПОПАДАТЬ В ЖУРНАЛ.
#
# httpx на уровне INFO пишет полный URL каждого запроса, а у Telegram токен
# лежит прямо в пути: api.telegram.org/bot<ТОКЕН>/getUpdates. То есть журнал
# набирал по строке с секретом на каждое обращение -- а журнал это ровно то,
# что человек пересылает, когда что-то сломалось.
#
# Токен по устройству проекта живёт только в sar_config.json и не попадает ни
# в репозиторий, ни в архив обновлений. Утечка через собственный журнал
# обходила всю эту осторожность.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("sar_telegram_bot")

CFG = {}
DB_PATH = None


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_request(chat_id, username, first_name):
    """Возвращает (row, is_new). Повторный /start от УЖЕ существующей заявки
    не сбрасывает её статус обратно в pending -- иначе approved/denied
    стирался бы каждым повторным /start."""
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM telegram_access_requests WHERE chat_id=?", (chat_id,)).fetchone()
        if row is not None:
            return row, False
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO telegram_access_requests (chat_id, username, first_name, status, requested_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (chat_id, username, first_name, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM telegram_access_requests WHERE chat_id=?", (chat_id,)).fetchone()
        return row, True
    finally:
        conn.close()


def get_request(chat_id):
    conn = _db()
    try:
        return conn.execute("SELECT * FROM telegram_access_requests WHERE chat_id=?", (chat_id,)).fetchone()
    finally:
        conn.close()


def list_pending():
    conn = _db()
    try:
        return conn.execute(
            "SELECT * FROM telegram_access_requests WHERE status='pending' ORDER BY requested_at").fetchall()
    finally:
        conn.close()


def set_status(chat_id, status, decided_by):
    conn = _db()
    try:
        conn.execute(
            "UPDATE telegram_access_requests SET status=?, decided_at=?, decided_by=? WHERE chat_id=?",
            (status, datetime.now(timezone.utc).isoformat(), decided_by, chat_id),
        )
        conn.commit()
    finally:
        conn.close()


def auto_approve_active(now=None):
    """Действует ли сейчас окно автовыдачи доступа (см. auto_approve_until).

    Любая проблема со значением -- считаем, что окно ВЫКЛЮЧЕНО: пустая
    строка, мусор вместо даты, прошедшее время. Ошибаться безопаснее в
    сторону закрытого доступа, а не открытого."""
    raw = (CFG.get("auto_approve_until") or "").strip()
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("auto_approve_until = %r -- не разобрал дату, автовыдача выключена", raw)
        return False
    now = now or datetime.now(until.tzinfo) if until.tzinfo else (now or datetime.now())
    return now < until


def ensure_token(chat_id):
    """Персональный ключ входа. Создаётся один раз и переиспользуется, чтобы
    старая ссылка у человека не переставала работать при каждом /help.

    Координаторам из admin_chat_ids роль администратора проставляется здесь
    же: они и так управляют доступом через бота, странно было бы заставлять
    их выдавать права самим себе отдельной командой."""
    conn = _db()
    try:
        if chat_id in CFG.get("admin_chat_ids", []):
            conn.execute("UPDATE telegram_access_requests SET role=? WHERE chat_id=? "
                         "AND (role IS NULL OR role != ?)",
                         (sar_common.ROLE_ADMIN, chat_id, sar_common.ROLE_ADMIN))
            conn.commit()
        row = conn.execute("SELECT access_token FROM telegram_access_requests WHERE chat_id=?",
                           (chat_id,)).fetchone()
        if row is not None and row["access_token"]:
            return row["access_token"]
        token = sar_common.generate_access_token()
        conn.execute("UPDATE telegram_access_requests SET access_token=? WHERE chat_id=?",
                     (token, chat_id))
        conn.commit()
        return token
    finally:
        conn.close()


def personal_link(chat_id, next_path=None):
    """Личная ссылка входа. next_path -- куда попасть сразу после входа.

    Нужен именно параметр, а не отдельная ссылка: человек, пришедший по
    вечной ссылке на находку, должен оказаться на этой находке, а не на
    общем экране, откуда её ещё надо искать.
    """
    base = (CFG.get("service_url") or "").rstrip("/")
    url = f"{base}/login?key={ensure_token(chat_id)}"
    if next_path:
        url += "&next=" + urllib.parse.quote(next_path, safe="")
    return url


def deep_link(payload):
    """Вечная ссылка на бота. t.me не меняется, в отличие от туннеля."""
    name = (CFG.get("bot_username") or "").lstrip("@")
    if not name:
        return ""
    return f"https://t.me/{name}?start={payload}"


def remember_target(chat_id, next_path):
    """Запоминает, куда вести человека после одобрения.

    Он мог прийти по вечной ссылке на находку и ждать доступа часами.
    Держать это в памяти процесса нельзя: сторож туннеля перезапускает
    бота при каждом обрыве канала, и цель терялась бы чаще, чем
    срабатывала.
    """
    if not next_path:
        return
    conn = _db()
    try:
        conn.execute(
            "UPDATE telegram_access_requests SET pending_target=? WHERE chat_id=?",
            (next_path, chat_id))
        conn.commit()
    finally:
        conn.close()


def take_target(chat_id):
    """Отдаёт запомненную цель и сразу забывает её: она одноразовая."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT pending_target FROM telegram_access_requests WHERE chat_id=?",
            (chat_id,)).fetchone()
        target = row["pending_target"] if row else None
        if target:
            conn.execute(
                "UPDATE telegram_access_requests SET pending_target=NULL WHERE chat_id=?",
                (chat_id,))
            conn.commit()
        return target
    except sqlite3.OperationalError:
        # база от старой версии, без колонки -- не повод ронять выдачу доступа
        return None
    finally:
        conn.close()


def parse_start_payload(args):
    """Разбирает параметр /start. Пока умеет только finding_<id>.

    Возвращает путь внутри платформы либо None. Мусор в параметре -- не
    ошибка: человек мог поделиться чем угодно, и бот должен просто выдать
    обычный доступ, а не ругаться.
    """
    if not args:
        return None
    raw = str(args[0]).strip()
    if raw.startswith("finding_"):
        tail = raw[len("finding_"):]
        if tail.isdigit():
            return f"/finding/{int(tail)}/"
    return None


def find_person(identifier):
    """Ищет человека по @username ИЛИ по chat_id.

    По chat_id -- не роскошь: username в Telegram НЕОБЯЗАТЕЛЕН, и у части
    волонтёров его просто нет. Без поиска по id таким людям нельзя было бы
    выдать никакую роль вообще. chat_id виден в /people.

    Регистр ника не важен -- в Telegram он не значим."""
    ident = str(identifier).strip()
    conn = _db()
    try:
        if ident.lstrip("-").isdigit():
            row = conn.execute("SELECT * FROM telegram_access_requests WHERE chat_id=?",
                               (int(ident),)).fetchone()
            if row is not None:
                return row
        uname = ident.lstrip("@").lower()
        return conn.execute(
            "SELECT * FROM telegram_access_requests WHERE lower(username)=?", (uname,)).fetchone()
    finally:
        conn.close()


def set_role(chat_id, role):
    conn = _db()
    try:
        conn.execute("UPDATE telegram_access_requests SET role=? WHERE chat_id=?", (role, chat_id))
        conn.commit()
    finally:
        conn.close()


def revoke_token(chat_id):
    """Сбрасывает ключ: старая ссылка сразу перестаёт пускать."""
    conn = _db()
    try:
        conn.execute("UPDATE telegram_access_requests SET access_token=NULL WHERE chat_id=?",
                     (chat_id,))
        conn.commit()
    finally:
        conn.close()


def link_ttl():
    try:
        return int(CFG.get("personal_link_ttl_sec", 3))
    except (TypeError, ValueError):
        return 3


def link_message(chat_id, next_path=None):
    """Отдельное сообщение только со ссылкой -- его бот потом удалит.

    Отдельным оно сделано именно ради удаления: стереть можно только всё
    сообщение целиком, и если бы ссылка лежала внутри общей инструкции,
    вместе с ней исчезло бы и всё остальное."""
    ttl = link_ttl()
    note = (f"\n\n⏳ Это сообщение исчезнет через {ttl} сек. "
            f"Успейте нажать на ссылку или сохранить её.\n"
            f"Пропало — просто напишите /help ещё раз." if ttl > 0 else "")
    head = ("\U0001F517 Ваша личная ссылка на находку (никому не передавайте):"
            if next_path else
            "\U0001F517 Ваша личная ссылка (никому не передавайте):")
    return f"{head}\n{personal_link(chat_id, next_path)}{note}"


async def _delete_after(bot_obj, chat_id, message_id, delay):
    """Удаляет сообщение со ссылкой через заданное время.

    Молча проглатывает ошибки: человек мог сам удалить сообщение, могла
    пропасть связь -- ни то, ни другое не повод роняться. Telegram позволяет
    боту удалять свои сообщения не старше 48 часов, чего здесь заведомо
    достаточно."""
    try:
        await asyncio.sleep(delay)
        await bot_obj.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        log.debug("не удалось удалить сообщение со ссылкой у %s", chat_id, exc_info=True)


async def send_access(bot_obj, chat_id, next_path=None):
    """Инструкция и ссылка -- ДВУМЯ сообщениями: инструкция остаётся в
    переписке, ссылка самоуничтожается."""
    await bot_obj.send_message(chat_id, access_message())
    if not (CFG.get("service_url") or ""):
        return
    msg = await bot_obj.send_message(chat_id, link_message(chat_id, next_path),
                                     disable_web_page_preview=True)
    ttl = link_ttl()
    if ttl > 0:
        # именно create_task: ждать удаления здесь означало бы держать
        # обработчик команды занятым всё это время
        asyncio.create_task(_delete_after(bot_obj, chat_id, msg.message_id, ttl))


def access_message(chat_id=None):
    # Ссылка сюда НЕ входит -- она уходит отдельным сообщением (см.
    # send_access), потому что удаляется по таймеру. Общий пароль оставлен
    # запасным вариантом для работы в поле с чужого устройства, но по нему
    # вход анонимный, без обсуждений.
    return (
        "✅ Доступ одобрен!\n\n"
        f"\U0001F4D6 Как пользоваться: {CFG['guide_url']}\n\n"
        "Коротко:\n"
        "— \U0001F4BB смотрите с ноутбука или компьютера, не с телефона: нужно "
        "разглядеть человека размером в несколько пикселей на фоне снега и скал, "
        "на маленьком экране находку легко пропустить. Плюс удобнее зум, "
        "перемотка и разметка находок мышью\n"
        "— по личной ссылке вы входите под своим именем — так видно, кто что "
        "посмотрел, и работают обсуждения находок\n"
        "— лучше заходить на Wi-Fi, видео тяжёлые для мобильного интернета\n"
        "— ссылка тестовая и может временно смениться — если не открывается, напишите /help\n\n"
        "Общий пароль в переписке больше не присылается: он оставался в истории "
        "чата и в резервных копиях Telegram. Нужен вход с чужого устройства — "
        "спросите координатора напрямую."
    )


DECLINE_MESSAGE = "Доступ пока закрыт. Если это ошибка — напишите координатору напрямую."


# Что изменилось, глазами волонтёра, а не разработчика: человеку важно "что
# я теперь могу", а не "какой модуль переписан". Держим здесь, а не тянем из
# git-истории -- туда попадают и внутренние правки, которые пользователю
# ничего не говорят.
CHANGELOG = """📋 Что нового в SAR Review

━━ 18 сентября ━━
☁️ Материал с Google Диска открывается прямо в платформе,
   скачивать себе не нужно. Для Курумды готовы 81 видео из 82
🗺 Карта находок: расчётная точка объекта и позиция борта —
   разными значками, между ними линия. Треки вылетов,
   свои отметки на местности
📊 Отчёт по операции: сколько разобрано по каждому файлу,
   второй проход, отбор по датам, обезличивание имён
🖼 Превью у материала с диска появляются до скачивания
✅ Исправлено: полоса покрытия показывала больше, чем
   разобрано на самом деле

━━ 22 августа ━━
🔍 Масштаб в плеере — колесо мыши, кадр двигается мышью
⌨️ Клавиши: пробел — пауза, ←/→ — ±5 с, Ctrl+←/→ — по кадру,
   M — разметка, F — во весь экран
✏️ Разметка и форма находки работают в полноэкранном режиме
🏷 Статус находки ставится сразу, в той же форме
⚡ Видео открывается быстрее: облегчённая копия, 1080p, объём
   в 8 раз меньше. Флажок «оригинал» возвращает исходник
⏯ Клик по находке останавливает видео на нужном кадре

━━ 21 августа ━━
🖼 Превью находок с увеличением по наведению
✅ Исправлено: отметки проверки не показывались в находках
🔎 Отбор находок по статусу, отклонённое скрыто по умолчанию
💬 Комментарий не пропадает, если список обновился при наборе
⚡ Список материалов открывается мгновенно

━━ 19 августа ━━
📁 Операции: каждый поиск — отдельная операция со своими файлами,
   находками и покрытием. Внутри папки, как в проводнике
🔎 Путь сверху: операция → папка → файл
📍 Из находки — переход на нужный момент видео
📖 Памятка по отсмотру над видео

━━ 17 августа ━━
🔑 Вход по личной ссылке, под своим именем
💬 Обсуждения под каждой находкой
❓ Статус «аномалия» — непонятно что, но на фон не похоже
🔍 Поиск по имени файла, слова в любом порядке
👁 Подсказки модели выключены по умолчанию: сначала своими
   глазами, потом сверьтесь галкой

━━ 16 августа ━━
⚡ Отчёты открываются секунды вместо минут
🖼 Фото: превью, просмотр с зумом, обработка за секунды
🎯 Ручной режим: модель запускается кнопкой 🤖, смотреть
   можно сразу
🎬 Переход из отчёта в плеер на нужный таймкод
📊 Полоса просмотра работает до обработки

Вопросы и проблемы — пишите сюда же."""


def requester_label(row):
    uname = f"@{row['username']}" if row["username"] else "(без username)"
    return f"{row['first_name'] or 'без имени'} {uname} · chat_id {row['chat_id']}"


def decision_keyboard(chat_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{chat_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{chat_id}"),
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    row, is_new = upsert_request(chat_id, user.username, user.first_name)
    # Вечная ссылка на находку приходит сюда как /start finding_<id>.
    next_path = parse_start_payload(context.args)

    if row["status"] == "approved":
        await send_access(context.bot, chat_id, next_path)
        return
    if row["status"] == "denied":
        await update.message.reply_text(DECLINE_MESSAGE)
        return

    # окно автовыдачи (например, на ночь, когда координатор спит):
    # доступ выдаётся сразу, но координатор всё равно получает уведомление,
    # чтобы утром видеть, кто зашёл
    if auto_approve_active():
        set_status(chat_id, "approved", 0)
        await send_access(context.bot, chat_id, next_path)
        text = (f"Доступ выдан АВТОМАТИЧЕСКИ (включено окно автовыдачи):\n"
                f"{requester_label(row)}")
        for admin_id in CFG["admin_chat_ids"]:
            try:
                await context.bot.send_message(admin_id, text)
            except Exception:
                log.exception("не удалось уведомить админа %s", admin_id)
        return

    remember_target(chat_id, next_path)

    if is_new:
        await update.message.reply_text(
            "Заявка отправлена координатору. Как только одобрят — сразу пришлю ссылку, "
            "пароль и инструкцию.")
        text = f"Новая заявка на доступ к SAR Review:\n{requester_label(row)}"
        for admin_id in CFG["admin_chat_ids"]:
            try:
                await context.bot.send_message(admin_id, text, reply_markup=decision_keyboard(chat_id))
            except Exception:
                log.exception("не удалось уведомить админа %s", admin_id)
    else:
        await update.message.reply_text("Заявка уже отправлена, ждёт одобрения координатором.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = get_request(update.effective_chat.id)
    if row is not None and row["status"] == "approved":
        await send_access(context.bot, update.effective_chat.id)
    else:
        await update.message.reply_text("Сначала нужно одобрение координатора — отправьте /start.")


async def changelog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Доступно всем, включая тех, кому доступ ещё не одобрили: список
    изменений ничего секретного не содержит, а человеку полезно понимать,
    что за инструмент он ждёт."""
    await update.message.reply_text(CHANGELOG, disable_web_page_preview=True)


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    rows = list_pending()
    if not rows:
        await update.message.reply_text("Заявок в ожидании нет.")
        return
    for row in rows:
        await update.message.reply_text(requester_label(row), reply_markup=decision_keyboard(row["chat_id"]))


ROLE_HELP = (
    "Управление правами:\n"
    "  /role КТО admin — полные права (модерация + загрузка файлов)\n"
    "  /role КТО moderator — модератор обсуждений (удаляет любые сообщения)\n"
    "  /role КТО muted — запретить писать в обсуждениях\n"
    "  /role КТО viewer — обычный участник\n"
    "  /revoke КТО — отозвать личную ссылку (доступ придётся выдать заново)\n"
    "  /people — список выданных доступов и ролей\n\n"
    "КТО — это @username или chat_id. По chat_id нужно потому, что username "
    "в Telegram необязателен, и у части волонтёров его просто нет; id виден "
    "в /people.\n"
    "Примеры:\n"
    "  /role @wild_high moderator\n"
    "  /role 361029368 admin"
)


async def role_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(ROLE_HELP)
        return
    target, role = args[0], args[1].strip().lower()
    if role not in sar_common.VALID_ROLES:
        await update.message.reply_text(
            f"Неизвестная роль «{role}».\n\n{ROLE_HELP}")
        return
    row = find_person(target)
    if row is None:
        await update.message.reply_text(
            f"{target} не найден. Человек должен хотя бы раз написать боту /start.\n"
            f"Если у него нет @username — укажите его chat_id, он виден в /people.")
        return

    set_role(row["chat_id"], role)
    await update.message.reply_text(
        f"{requester_label(row)}\n→ роль: {sar_common.ROLE_LABELS[role]}")
    # человека предупреждаем -- иначе он не поймёт, почему перестал писать
    try:
        if role == sar_common.ROLE_MUTED:
            await context.bot.send_message(
                row["chat_id"], "Координатор ограничил вам участие в обсуждениях. "
                                "Просмотр и разметка находок работают как обычно.")
        elif role == sar_common.ROLE_MODERATOR:
            await context.bot.send_message(
                row["chat_id"], "Вам выданы права модератора обсуждений: можете удалять "
                                "любые сообщения в обсуждениях находок.")
        elif role == sar_common.ROLE_ADMIN:
            await context.bot.send_message(
                row["chat_id"], "Вам выданы права администратора: модерация обсуждений "
                                "плюс загрузка видео и телеметрии через браузер.")
        else:
            await context.bot.send_message(
                row["chat_id"], "Ваши права в обсуждениях восстановлены.")
    except Exception:
        log.exception("не удалось уведомить %s о смене роли", row["chat_id"])


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(ROLE_HELP)
        return
    row = find_person(args[0])
    if row is None:
        await update.message.reply_text(f"{args[0]} не найден.")
        return
    set_status(row["chat_id"], "denied", update.effective_chat.id)
    revoke_token(row["chat_id"])
    await update.message.reply_text(f"Доступ отозван: {requester_label(row)}")


async def people_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM telegram_access_requests WHERE status='approved' "
            "ORDER BY requested_at").fetchall()
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text("Доступ пока никому не выдан.")
        return
    lines = []
    for r in rows:
        role = r["role"] or sar_common.DEFAULT_ROLE
        mark = "🔑" if r["access_token"] else "—"
        lines.append(f"{mark} {requester_label(r)} · {sar_common.ROLE_LABELS[role]}")
    await update.message.reply_text(
        "Выданные доступы:\n" + "\n".join(lines) + f"\n\n{ROLE_HELP}")


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in CFG["admin_chat_ids"]:
        await query.answer("Только координатор может это делать.", show_alert=True)
        return

    action, chat_id_str = query.data.split(":", 1)
    chat_id = int(chat_id_str)
    status = "approved" if action == "approve" else "denied"
    set_status(chat_id, status, admin_id)
    await query.answer()

    outcome = "✅ одобрено" if status == "approved" else "❌ отклонено"
    await query.edit_message_text(f"{query.message.text}\n\n— {outcome}")

    try:
        if status == "approved":
            await send_access(context.bot, chat_id, take_target(chat_id))
        else:
            await context.bot.send_message(chat_id, DECLINE_MESSAGE)
    except Exception:
        log.exception("не удалось отправить решение пользователю %s", chat_id)


# ---------------------------------------------------------------------------
# Тревоги о недоступности платформы
#
# Проверку делает бот, а не сама платформа: сервис, который лежит, не может
# сообщить о том, что он лежит. Бот -- отдельный процесс, он переживает
# падение веб-слоя и продолжает следить.
#
# Ограничение, которое надо понимать: если выключится вся машина, бот умрёт
# вместе с платформой и не скажет ничего. Это закрывается только внешним
# пингом /healthz откуда-то ещё, см. monitoring/README.md.
# ---------------------------------------------------------------------------

ALERT_INTERVAL_SEC = 60


def health_url():
    base = (CFG.get("service_url") or "").rstrip("/")
    return f"{base}/healthz" if base else ""


async def alert_tick(context: ContextTypes.DEFAULT_TYPE):
    url = health_url()
    if not url:
        return                       # некуда ходить -- адрес сервиса не задан

    # Соединение закрываем обязательно: задача выполняется раз в минуту, и
    # незакрытые дескрипторы копились бы тысячами за сутки.
    conn = _db()
    try:
        # Замьютено -- пропускаем проверку ЦЕЛИКОМ, не трогая состояние. Так
        # после снятия мьюта человек узнает, что сервис всё ещё лежит: если
        # бы мы обновляли состояние молча, тревога считалась бы отправленной.
        if sar_alerts.mute_until(conn) is not None:
            return
        prev = sar_alerts.load_state(conn, "service")
    finally:
        conn.close()

    level, detail = await asyncio.to_thread(sar_alerts.check_service, url)
    state, notify, held = sar_alerts.decide(prev, level, datetime.now())

    conn = _db()
    try:
        sar_alerts.save_state(conn, "service", state["level"], state["since"],
                               state["notified_at"], state["notified_level"])
    finally:
        conn.close()
    if not notify:
        return

    text = sar_alerts.message(level, held, url, detail if level == sar_alerts.DOWN else None)
    for admin_id in CFG.get("admin_chat_ids", []):
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            log.exception("не удалось отправить тревогу админу %s", admin_id)


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    hours = 4.0
    if context.args:
        try:
            hours = float(context.args[0].replace(",", "."))
        except ValueError:
            await update.message.reply_text("Формат: /mute [часов], например /mute 2")
            return
    conn = _db()
    try:
        until = sar_alerts.set_mute(conn, hours)
    finally:
        conn.close()
    await update.message.reply_text(
        f"\U0001F515 Тревоги отключены до {until:%H:%M %d.%m}.\n\n"
        f"Включатся сами — навсегда замьютить нельзя специально: молчащий "
        f"мониторинг хуже отсутствующего.\nВключить раньше: /unmute")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    conn = _db()
    try:
        sar_alerts.clear_mute(conn)
    finally:
        conn.close()
    await update.message.reply_text("\U0001F514 Тревоги включены.")


def monitoring_block():
    """Строки про дашборд для /status. Пусто, если адрес не задан.

    Адрес у бесплатного туннеля временный и меняется при каждом
    перезапуске, а ссылка нужна именно тогда, когда что-то пошло не так.
    Держать её в /status удобнее, чем искать в переписке: команду видно в
    меню, и она всегда отдаёт текущий адрес из конфига.
    """
    url = (CFG.get("grafana_url") or "").strip()
    if not url:
        return []
    out = ["", "\U0001F4CA Дашборд мониторинга", url]
    login = (CFG.get("grafana_login") or "").strip()
    pw = (CFG.get("grafana_password") or "").strip()
    if login and pw:
        out.append(f"вход: {login} / {pw} (только чтение)")
    return out


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Состояние платформы по запросу -- то же, что отдаёт /healthz."""
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    url = health_url()
    if not url:
        await update.message.reply_text("Адрес сервиса не задан в конфиге.")
        return

    import json
    import urllib.request

    def fetch():
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            try:
                return json.loads(e.read().decode("utf-8"))   # 503 отдаёт тело
            except Exception:
                return {"error": f"{type(e).__name__}: {e}"}

    data = await asyncio.to_thread(fetch)
    if "error" in data:
        # Дашборд нужен ИМЕННО СЕЙЧАС: платформа молчит, и посмотреть, что с
        # ней, можно только там. Обрывать сообщение без ссылки -- оставлять
        # человека без единственного оставшегося инструмента.
        await update.message.reply_text(
            "\n".join([f"\U0001F534 Платформа не отвечает", data["error"]]
                       + monitoring_block()),
            disable_web_page_preview=True)
        return

    icon = {"ok": "\U0001F7E2", "warn": "\U0001F7E1", "crit": "\U0001F534"}
    lines = [f"{icon.get(data.get('status'), '')} Состояние платформы\n"]
    for name, c in (data.get("checks") or {}).items():
        lines.append(f"{icon.get(c['level'], '')} {c['text']}")
    conn = _db()
    try:
        muted = sar_alerts.mute_until(conn)
    finally:
        conn.close()
    if muted:
        lines.append(f"\n\U0001F515 Тревоги отключены до {muted:%H:%M %d.%m}")

    lines.extend(monitoring_block())
    await update.message.reply_text("\n".join(lines),
                                     disable_web_page_preview=True)


# Меню команд бота (значок "/" рядом с полем ввода). Без него команда
# работает, но найти её можно только зная название -- именно так семь
# админских команд и оставались невидимыми: они существовали, а в меню их
# не было.
#
# Списка два. Раньше админские команды просто не показывались никому, чтобы
# не подсказывать их всем подряд. Но Telegram умеет РАЗНЫЕ меню для разных
# чатов, поэтому прятать больше незачем: волонтёр видит три команды, которые
# ему нужны, координатор -- все свои.
# ЧТО ЧЕЛОВЕК ВИДИТ, ВПЕРВЫЕ ОТКРЫВ БОТА.
#
# Кнопку «Начать» Telegram рисует сам, её включать не надо. А вот над ней
# до первого нажатия висит ОПИСАНИЕ -- и оно было пустым. Тот, кому ссылку
# переслали (координатор, сотрудник ведомства), видел пустой экран, слово
# «Searchbot» и кнопку, не понимая, куда попал и чего от него хотят.
#
# Два разных поля, и путать их нельзя:
#   BOT_DESCRIPTION -- на пустом экране чата, до первого сообщения (512);
#   BOT_SHORT_DESCRIPTION -- в профиле бота и в поиске (120).
#
# Ставится при каждом запуске, а не однократно руками: иначе оно живёт
# только в настройках у @BotFather, и после смены токена или бота о нём
# никто не вспомнит.
BOT_DESCRIPTION = (
    "SAR Review — платформа совместного разбора видео и фотографий с дрона "
    "для поисково-спасательных работ.\n\n"
    "Бот выдаёт персональную ссылку для входа и присылает уведомления. "
    "Доступ подтверждает координатор.\n\n"
    "Нажмите «Начать», чтобы запросить доступ."
)
BOT_SHORT_DESCRIPTION = (
    "Доступ к платформе разбора аэрофотосъёмки для поисково-спасательных работ"
)

BASE_COMMANDS = [
    ("start", "Запросить доступ к SAR Review"),
    ("help", "Прислать ссылку и инструкцию ещё раз"),
    ("changelog", "Что нового в платформе"),
]

ADMIN_COMMANDS = BASE_COMMANDS + [
    ("status", "Состояние платформы сейчас"),
    ("people", "Кто имеет доступ и с какой ролью"),
    ("pending", "Заявки, ожидающие решения"),
    ("role", "Выдать роль: /role @ник модератор"),
    ("revoke", "Отозвать доступ: /revoke @ник"),
    ("mute", "Отключить тревоги: /mute 4"),
    ("unmute", "Включить тревоги обратно"),
]


def _save_bot_username(username):
    """Дописывает имя бота в sar_config.json, не трогая остальное.

    Читаем-меняем-пишем целиком: конфиг маленький, а частичная запись
    JSON невозможна. Ошибку глушим намеренно -- без имени бота перестанут
    работать только вечные ссылки, а сам бот обязан подняться в любом
    случае.
    """
    path = os.path.join(SCRIPT_DIR, "sar_config.json")
    try:
        with io.open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("telegram_bot", {})["bot_username"] = username
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception:
        log.warning("не удалось записать имя бота в конфиг", exc_info=True)


async def _post_init(app):
    from telegram import BotCommandScopeChat

    # Своё имя бот узнаёт у Telegram и запоминает в конфиге: из него
    # sar_server.py строит вечные ссылки на находки. Спрашивать имя у
    # сервера негде -- токен бота ему намеренно недоступен.
    try:
        me = await app.bot.get_me()
        if me.username and CFG.get("bot_username") != me.username:
            CFG["bot_username"] = me.username
            _save_bot_username(me.username)
            log.info("имя бота записано в конфиг: @%s", me.username)
    except Exception:
        log.warning("не удалось узнать имя бота -- вечные ссылки не заработают",
                    exc_info=True)

    # Описание на пустом экране. Отдельный try: платформа обязана подняться
    # даже если Telegram отказал именно на этом вызове -- без описания бот
    # работает, просто встречает молча.
    try:
        await app.bot.set_my_description(BOT_DESCRIPTION)
        await app.bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except Exception:
        log.warning("не удалось поставить описание бота", exc_info=True)

    await app.bot.set_my_commands(BASE_COMMANDS)

    # Персональное меню каждому администратору. Ошибка на одном чате не
    # должна мешать остальным: координатор мог не начинать диалог с ботом,
    # и Telegram на такой чат ответит отказом.
    for chat_id in CFG.get("admin_chat_ids", []):
        try:
            await app.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception:
            log.warning("не удалось поставить админское меню для %s", chat_id)


def build_application():
    app = Application.builder().token(CFG["bot_token"]).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("changelog", changelog_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("role", role_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CommandHandler("people", people_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CallbackQueryHandler(on_decision))

    # Периодическая проверка доступности. Первый запуск отложен: сразу
    # после старта бота платформа может ещё подниматься, и тревога об этом
    # была бы ложной.
    if app.job_queue is not None:
        app.job_queue.run_repeating(alert_tick, interval=ALERT_INTERVAL_SEC, first=90)
    else:
        log.warning("job_queue недоступна — тревоги о недоступности выключены "
                     "(нужен пакет python-telegram-bot[job-queue])")
    return app


def main():
    global CFG, DB_PATH
    server_cfg, _ = sar_common.load_server_config(SCRIPT_DIR)
    CFG, _ = sar_common.load_telegram_bot_config(SCRIPT_DIR)

    if not CFG.get("bot_token"):
        raise SystemExit(
            "sar_config.json -> telegram_bot.bot_token не задан. Получите токен у @BotFather "
            "и впишите в конфиг перед запуском.")
    if not CFG.get("admin_chat_ids"):
        raise SystemExit(
            "sar_config.json -> telegram_bot.admin_chat_ids пуст -- одобрять заявки будет некому.")

    _, _, db_path, _ = sar_common.resolve_paths(
        server_cfg["watch_dir"], server_cfg.get("data_dir"))
    DB_PATH = db_path
    sar_common.init_db(DB_PATH)

    log.info("SAR Telegram bot запущен, координаторы: %s", CFG["admin_chat_ids"])
    build_application().run_polling()


if __name__ == "__main__":
    main()
