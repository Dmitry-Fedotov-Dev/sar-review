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
import logging
import os
import sqlite3
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import sar_common

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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


def personal_link(chat_id):
    base = (CFG.get("service_url") or "").rstrip("/")
    return f"{base}/login?key={ensure_token(chat_id)}"


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


def access_message(chat_id=None):
    # Персональная ссылка -- основной способ входа: человек попадает внутрь
    # уже опознанным, имя вводить не нужно, и он может участвовать в
    # обсуждениях. Общий пароль оставлен запасным вариантом для работы в поле
    # с чужого устройства, но по нему вход анонимный (без обсуждений).
    personal = f"\U0001F517 Ваша личная ссылка (никому не передавайте):\n{personal_link(chat_id)}\n\n" \
        if chat_id is not None else ""
    return (
        "✅ Доступ одобрен!\n\n"
        + personal +
        f"\U0001F4D6 Как пользоваться: {CFG['guide_url']}\n\n"
        "Коротко:\n"
        "— \U0001F4BB смотрите с ноутбука или компьютера, не с телефона: нужно "
        "разглядеть человека размером в несколько пикселей на фоне снега и скал, "
        "на маленьком экране находку легко пропустить. Плюс удобнее зум, "
        "перемотка и разметка находок мышью\n"
        "— по личной ссылке вы входите под своим именем — так видно, кто что "
        "посмотрел, и работают обсуждения находок\n"
        "— лучше заходить на Wi-Fi, видео тяжёлые для мобильного интернета\n"
        "— ссылка тестовая и может временно смениться — если не открывается, напишите сюда же\n\n"
        f"Запасной вход (если личная ссылка не работает): {CFG['service_url']}\n"
        f"\U0001F511 общий пароль: {CFG['service_password']} — по нему вход "
        f"анонимный, обсуждения будут недоступны"
    )


DECLINE_MESSAGE = "Доступ пока закрыт. Если это ошибка — напишите координатору напрямую."


# Что изменилось, глазами волонтёра, а не разработчика: человеку важно "что
# я теперь могу", а не "какой модуль переписан". Держим здесь, а не тянем из
# git-истории -- туда попадают и внутренние правки, которые пользователю
# ничего не говорят.
CHANGELOG = """📋 Что нового в SAR Review

━━ 17 августа ━━
🔑 Вход по личной ссылке
   Теперь вы входите под своим именем, ничего не вводя. Видно, кто что
   посмотрел, и работают обсуждения. Ссылка личная — не передавайте её.

💬 Обсуждения находок
   Под каждой находкой — своя ветка. Можно написать, что вы думаете, и
   увидеть мнение других. Свои сообщения можно удалить.

❓ Статус «аномалия»
   Для случаев «непонятно что, но на фон не похоже». Раньше такое
   приходилось либо отклонять и терять, либо записывать в «предмет».

🔍 Поиск по имени файла
   Слова в любом порядке: «0003 mp4» найдёт нужное видео.

👁 Подсказки модели выключены по умолчанию
   Открываете плеер — видите чистое видео. Так сделано намеренно: когда
   рамки видны сразу, внимание идёт туда, куда показала модель, и
   остальные участки просматриваются хуже. Сначала своими глазами,
   потом сверьтесь галкой «показывать находки модели».

━━ 16 августа ━━
⚡ Платформа стала заметно быстрее
   Страницы отчётов открывались минутами — исправлено. Причина была в
   цветовом детекторе: на снегу он ловил небо и тени как находки, по
   полтысячи тысяч штук на видео.

🖼 Фото заработали полноценно
   Превью, просмотр с зумом, клик по превью открывает материал.
   Снимки обрабатываются раньше видео — они считаются секунды.

🎯 Ручной режим обработки
   Модель больше не запускается на всё подряд. Файл появляется со
   статусом «без анализа», а прогнать его через модель — кнопкой 🤖.
   Смотреть вручную можно сразу, не дожидаясь ничего.

🎬 Переход из отчёта в плеер
   Из любой сцены и любого кадра — сразу на нужный таймкод.

📊 Полоса просмотра работает до обработки
   Видно, какие куски видео уже отсмотрены, даже пока модель до файла
   не дошла.

━━ Раньше ━━
📍 Координаты находок с поправкой на зум камеры
   Найденная ошибка сдвигала оценку в среднем на 700 метров.

🏷 Разметка находок
   Отметки «точно человек / предмет / отклонено» на каждой находке —
   чтобы команда видела, что уже проверено, а что ждёт внимания.

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

    if row["status"] == "approved":
        await update.message.reply_text(access_message(chat_id))
        return
    if row["status"] == "denied":
        await update.message.reply_text(DECLINE_MESSAGE)
        return

    # окно автовыдачи (например, на ночь, когда координатор спит):
    # доступ выдаётся сразу, но координатор всё равно получает уведомление,
    # чтобы утром видеть, кто зашёл
    if auto_approve_active():
        set_status(chat_id, "approved", 0)
        await update.message.reply_text(access_message(chat_id))
        text = (f"Доступ выдан АВТОМАТИЧЕСКИ (включено окно автовыдачи):\n"
                f"{requester_label(row)}")
        for admin_id in CFG["admin_chat_ids"]:
            try:
                await context.bot.send_message(admin_id, text)
            except Exception:
                log.exception("не удалось уведомить админа %s", admin_id)
        return

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
        await update.message.reply_text(access_message(update.effective_chat.id))
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
        await context.bot.send_message(chat_id, access_message(chat_id) if status == "approved" else DECLINE_MESSAGE)
    except Exception:
        log.exception("не удалось отправить решение пользователю %s", chat_id)


async def _post_init(app):
    # регистрирует команды в меню бота (значок "/" рядом с полем ввода в
    # Telegram) -- без этого /start и /help работают, но их не видно как
    # подсказку, человеку пришлось бы напечатать команду вручную. /pending
    # сюда намеренно не включаем -- это админская команда, незачем
    # подсказывать её всем подряд.
    await app.bot.set_my_commands([
        ("start", "Запросить доступ к SAR Review"),
        ("help", "Прислать ссылку и инструкцию ещё раз"),
        ("changelog", "Что нового в платформе"),
    ])


def build_application():
    app = Application.builder().token(CFG["bot_token"]).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("changelog", changelog_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("role", role_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CommandHandler("people", people_cmd))
    app.add_handler(CallbackQueryHandler(on_decision))
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

    _, _, db_path, _ = sar_common.resolve_paths(server_cfg["watch_dir"])
    DB_PATH = db_path
    sar_common.init_db(DB_PATH)

    log.info("SAR Telegram bot запущен, координаторы: %s", CFG["admin_chat_ids"])
    build_application().run_polling()


if __name__ == "__main__":
    main()
