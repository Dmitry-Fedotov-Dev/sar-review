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


def access_message():
    return (
        "✅ Доступ одобрен!\n\n"
        f"\U0001F517 Сервис: {CFG['service_url']}\n"
        f"\U0001F511 Пароль: {CFG['service_password']}\n\n"
        f"\U0001F4D6 Как пользоваться: {CFG['guide_url']}\n\n"
        "Коротко:\n"
        "— \U0001F4BB смотрите с ноутбука или компьютера, не с телефона: нужно "
        "разглядеть человека размером в несколько пикселей на фоне снега и скал, "
        "на маленьком экране находку легко пропустить. Плюс удобнее зум, "
        "перемотка и разметка находок мышью\n"
        "— при входе укажите своё настоящее имя или ник (не «тест1») — по имени "
        "система отслеживает, кто что уже посмотрел\n"
        "— лучше заходить на Wi-Fi, видео тяжёлые для мобильного интернета\n"
        "— ссылка тестовая и может временно смениться — если не открывается, напишите сюда же"
    )


DECLINE_MESSAGE = "Доступ пока закрыт. Если это ошибка — напишите координатору напрямую."


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
        await update.message.reply_text(access_message())
        return
    if row["status"] == "denied":
        await update.message.reply_text(DECLINE_MESSAGE)
        return

    # окно автовыдачи (например, на ночь, когда координатор спит):
    # доступ выдаётся сразу, но координатор всё равно получает уведомление,
    # чтобы утром видеть, кто зашёл
    if auto_approve_active():
        set_status(chat_id, "approved", 0)
        await update.message.reply_text(access_message())
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
        await update.message.reply_text(access_message())
    else:
        await update.message.reply_text("Сначала нужно одобрение координатора — отправьте /start.")


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in CFG["admin_chat_ids"]:
        return
    rows = list_pending()
    if not rows:
        await update.message.reply_text("Заявок в ожидании нет.")
        return
    for row in rows:
        await update.message.reply_text(requester_label(row), reply_markup=decision_keyboard(row["chat_id"]))


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
        await context.bot.send_message(chat_id, access_message() if status == "approved" else DECLINE_MESSAGE)
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
    ])


def build_application():
    app = Application.builder().token(CFG["bot_token"]).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
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
