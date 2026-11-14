"""Люди и доступ из командной строки -- без Telegram.

Зачем. Роль на платформе даёт только вход по персональной ссылке; вход по
общему паролю анонимен. Раньше ссылки выдавал только бот, и установка без
него оставалась без администратора: создать операцию, подключить облако,
выдать права было некому. Здесь -- то же, что умеет бот, той же логикой
(sar_common.person_*), но с консоли владельца машины.

Первого администратора заводят именно так: права выдаёт тот, у кого есть
доступ к серверу, -- явным действием, а не по имени при входе.

    python sar_people.py add "Дмитрий" --role admin   # завести и выдать ссылку
    python sar_people.py list                          # кто есть, роли, заявки
    python sar_people.py role "Дмитрий" moderator      # сменить роль
    python sar_people.py link "Дмитрий"                # новая ссылка взамен старой
    python sar_people.py approve @nick                 # одобрить заявку из бота
    python sar_people.py revoke "Дмитрий"              # отозвать доступ

В контейнере cd: sudo docker exec cd-<проект> python sar_people.py ...

Человек указывается номером (из list), @ником или именем. Ссылка -- это
пароль человека: печатается только в момент выдачи и больше нигде не
хранится в открытом виде, кроме базы.
"""
import argparse
import json
import os
import sys

import sar_common

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

ROLE_HELP = ", ".join(f"{r} — {sar_common.ROLE_LABELS[r]}" for r in
                      (sar_common.ROLE_VIEWER, sar_common.ROLE_MODERATOR,
                       sar_common.ROLE_ADMIN, sar_common.ROLE_MUTED))


def _open(root):
    cfg, config_path = sar_common.load_server_config(root)
    watch = cfg["watch_dir"]
    if not os.path.isabs(watch):
        watch = os.path.abspath(os.path.join(root, watch))
    _, _, db_path, _ = sar_common.resolve_paths(watch, cfg.get("data_dir"))
    sar_common.init_db(db_path)
    base = ""
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            base = (json.load(f).get("telegram_bot") or {}).get("service_url") or ""
    return sar_common.get_db_connection(db_path), base


def _who(conn, ident):
    row = sar_common.person_find(conn, ident)
    if row is None:
        raise sar_common.PersonError(f"«{ident}» не найден — см. list")
    return row


def _print_link(row, base, token):
    url = sar_common.personal_link_url(base, token)
    print(f"Ссылка для {sar_common.display_name_for(row)} (никому не пересылайте, кроме него):")
    print(f"  {url}")
    if not base:
        print("  (адрес платформы не задан -- допишите его спереди; задаётся "
              "telegram_bot.service_url или SAR_SERVICE_URL)")


def cmd_list(conn, base, args):
    rows = sar_common.person_list(conn)
    if not rows:
        print("Пока никого нет. Первый администратор: add \"Имя\" --role admin")
        return 0
    order = {"pending": 0, "approved": 1, "denied": 2}
    for r in sorted(rows, key=lambda r: (order.get(r["status"], 9), r["requested_at"] or "")):
        p = sar_common.person_public(r)
        src = "telegram" if p["telegram"] else "вручную"
        link = "ссылка есть" if p["has_link"] else "ссылки нет"
        print(f"{p['id']:>14}  {p['name']:<24} {p['status']:<9} {p['role']:<10} {src:<9} {link}")
    return 0


def cmd_add(conn, base, args):
    row = sar_common.person_create_local(conn, args.name, args.role)
    print(f"Заведён: {sar_common.display_name_for(row)} (номер {row['chat_id']}), "
          f"роль: {sar_common.ROLE_LABELS[row['role']]}")
    _print_link(row, base, sar_common.person_ensure_token(conn, row["chat_id"]))
    return 0


def cmd_role(conn, base, args):
    row = _who(conn, args.who)
    sar_common.person_set_role(conn, row["chat_id"], args.role)
    print(f"{sar_common.display_name_for(row)} → {sar_common.ROLE_LABELS[args.role]}")
    print("Роль применится при следующем входе по ссылке.")
    return 0


def cmd_link(conn, base, args):
    row = _who(conn, args.who)
    if row["status"] != "approved":
        raise sar_common.PersonError("доступ не выдан — сначала approve")
    _print_link(row, base, sar_common.person_new_token(conn, row["chat_id"]))
    print("Прежняя ссылка этого человека больше не действует.")
    return 0


def cmd_approve(conn, base, args):
    row = _who(conn, args.who)
    sar_common.person_set_status(conn, row["chat_id"], "approved")
    print(f"Доступ выдан: {sar_common.display_name_for(row)}")
    _print_link(row, base, sar_common.person_ensure_token(conn, row["chat_id"]))
    return 0


def cmd_revoke(conn, base, args):
    row = _who(conn, args.who)
    sar_common.person_revoke(conn, row["chat_id"])
    print(f"Доступ отозван: {sar_common.display_name_for(row)}. Ссылка больше не пускает.")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Люди и доступ SAR Review без Telegram")
    ap.add_argument("--root", default=SCRIPT_DIR, help="папка с sar_config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="кто есть, роли, заявки")
    a = sub.add_parser("add", help="завести человека без Telegram и выдать ссылку")
    a.add_argument("name")
    a.add_argument("--role", default=sar_common.DEFAULT_ROLE,
                   choices=sorted(sar_common.VALID_ROLES), help=ROLE_HELP)
    r = sub.add_parser("role", help="сменить роль")
    r.add_argument("who")
    r.add_argument("role", choices=sorted(sar_common.VALID_ROLES), help=ROLE_HELP)
    for name, text in (("link", "новая ссылка взамен старой"),
                       ("approve", "одобрить заявку"),
                       ("revoke", "отозвать доступ")):
        sub.add_parser(name, help=text).add_argument("who")
    return ap


COMMANDS = {"list": cmd_list, "add": cmd_add, "role": cmd_role, "link": cmd_link,
            "approve": cmd_approve, "revoke": cmd_revoke}


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn, base = _open(os.path.abspath(args.root))
    try:
        return COMMANDS[args.cmd](conn, base, args)
    except sar_common.PersonError as e:
        print(f"Не сделано: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
