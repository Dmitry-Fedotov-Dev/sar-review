# -*- coding: utf-8 -*-
"""Разовая рассылка объявления всем, кто есть в боте.

ЭТО НЕ КОМАНДА БОТА, А ОТДЕЛЬНЫЙ СКРИПТ, и намеренно.

В проекте есть правило: платформа ничего не рассылает по своей
инициативе -- новое показывается в ответ на /changelog, а не приходит
само. Постоянная команда рассылки в боте это правило размывает: она
появляется в списке, её однажды нажимают не подумав, и отменить
отправленное нельзя. Разовый скрипт требует осознанного запуска с
машины владельца -- ровно та цена, которой такое действие и стоит.

ПО УМОЛЧАНИЮ НИЧЕГО НЕ ОТПРАВЛЯЕТ. Без --send печатает, кому и что
уйдёт, и завершается.

    python send_announcement.py текст.txt            # показать, не слать
    python send_announcement.py текст.txt --send     # отправить

Текст берётся из файла, а не из аргумента: объявление длинное, с
переводами строк и эмодзи, и командная строка их коверкает.
"""
import argparse
import asyncio
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import sar_common

# Консоль Windows по умолчанию не в UTF-8, и объявление с эмодзи роняет
# печать до того, как что-либо отправится. Та же грабля, что и с выводом
# дочерних процессов в воркере.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# Telegram разрешает около 30 сообщений в секунду, но для рассылок
# рекомендует не больше одного в секунду на разных получателей. Спешить
# здесь некуда: 69 человек -- это минута с небольшим.
DELAY_SEC = 1.0


def recipients(conn):
    """Кому слать: одобренные, с chat_id.

    Заявки, которые ещё не одобрили или отклонили, пропускаем -- человек
    не получил доступа, и объявление про новые вкладки ему ни к чему.
    """
    rows = conn.execute(
        "SELECT chat_id, username, first_name FROM telegram_access_requests "
        "WHERE status='approved' AND chat_id IS NOT NULL "
        "ORDER BY chat_id").fetchall()
    return [dict(r) for r in rows]


def label(row):
    return (row.get("username") and "@" + row["username"]) \
        or row.get("first_name") or str(row["chat_id"])


async def send_all(token, people, text, delay=DELAY_SEC):
    """Шлёт по одному, считая доставленное и упавшее.

    Отказ по одному человеку НЕ должен останавливать рассылку: люди
    блокируют ботов и удаляют аккаунты, и это нормально. Но и глотать
    молча нельзя -- в конце печатается, кому не дошло.
    """
    from telegram import Bot
    from telegram.error import TelegramError

    bot = Bot(token)
    ok, failed = 0, []
    for i, row in enumerate(people, 1):
        try:
            await bot.send_message(chat_id=row["chat_id"], text=text,
                                    disable_web_page_preview=False)
            ok += 1
            print("  [%d/%d] %s — доставлено" % (i, len(people), label(row)),
                  flush=True)
        except TelegramError as e:
            failed.append((label(row), str(e)[:60]))
            print("  [%d/%d] %s — НЕ доставлено: %s"
                  % (i, len(people), label(row), str(e)[:60]), flush=True)
        if i < len(people):
            await asyncio.sleep(delay)
    return ok, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("textfile", help="файл с текстом объявления")
    ap.add_argument("--send", action="store_true",
                    help="действительно отправить (без этого только показ)")
    args = ap.parse_args()

    text = io.open(args.textfile, encoding="utf-8").read().strip()
    if not text:
        print("файл пуст")
        return 2

    cfg = json.load(io.open(os.path.join(ROOT, "sar_config.json"),
                            encoding="utf-8"))
    token = (cfg.get("telegram_bot") or {}).get("bot_token") or ""
    if not token:
        print("в sar_config.json нет токена бота")
        return 2

    w, d, db, _ = sar_common.resolve_paths(
        os.path.abspath(ROOT), (cfg.get("server") or {}).get("data_dir"))
    conn = sar_common.get_db_connection(db)
    try:
        people = recipients(conn)
    finally:
        conn.close()

    print("ТЕКСТ ОБЪЯВЛЕНИЯ (%d символов)" % len(text))
    print("-" * 62)
    print(text)
    print("-" * 62)
    print()
    print("ПОЛУЧАТЕЛЕЙ: %d" % len(people))
    for row in people[:5]:
        print("   %s" % label(row))
    if len(people) > 5:
        print("   ... и ещё %d" % (len(people) - 5))
    print()

    if len(text) > 4096:
        print("СЛИШКОМ ДЛИННО: Telegram режет на 4096 символов. Сократите.")
        return 1

    if not args.send:
        print("Это ПРОСМОТР. Ничего не отправлено.")
        print("Чтобы отправить: python %s %s --send"
              % (os.path.basename(__file__), args.textfile))
        return 0

    print("ОТПРАВЛЯЮ. Отменить после начала нельзя.")
    print()
    ok, failed = asyncio.run(send_all(token, people, text))
    print()
    print("ДОСТАВЛЕНО: %d из %d" % (ok, len(people)))
    if failed:
        print("НЕ ДОШЛО (%d):" % len(failed))
        for who, why in failed:
            print("   %-24s %s" % (who, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
