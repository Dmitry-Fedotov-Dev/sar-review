# -*- coding: utf-8 -*-
"""Одноразовый помощник: получить у Google ключи, которые не протухают за час.

ЗАЧЕМ. Access token у Google живёт РОВНО ЧАС. Подготовка 82 видео идёт
около четырёх часов, то есть без продления она не может закончиться в
принципе -- упрётся в отказ примерно на десятом файле. Продление в
платформе реализовано, но ему нужны три вещи, которые выдаёт только сам
Google: client_id, client_secret и refresh_token. Этот скрипт получает
последний и кладёт всё в базу.

ПОЧЕМУ ЗДЕСЬ МОЖНО ТО, ЧЕГО НЕЛЬЗЯ В ПЛАТФОРМЕ. В проекте записано, что
OAuth с перенаправлением не используется: ему нужен постоянный адрес
возврата, а у платформы его нет -- туннель меняет имя при каждом
перезапуске. Здесь же возврат идёт на 127.0.0.1, и этот адрес не меняется
никогда. Ограничение касалось платформы как сетевой службы, а не разовой
местной программы.

ЧТО НУЖНО ЗАРАНЕЕ (делается руками в консоли Google, платформа этого не
может -- ключи приложения выдаёт только владелец проекта):

  1. console.cloud.google.com -> создать проект
  2. APIs & Services -> Library -> включить "Google Drive API"
  3. APIs & Services -> OAuth consent screen -> External,
     добавить себя в Test users
  4. Credentials -> Create credentials -> OAuth client ID
     -> тип "Desktop app"
  5. Скопировать Client ID и Client secret

Тип "Desktop app" важен: для него Google разрешает возврат на
http://127.0.0.1 с ЛЮБЫМ портом, и заранее регистрировать порт не нужно.
У типа "Web application" пришлось бы вписывать точный адрес.

ЗАПУСК:
    python get_google_token.py

Скрипт спросит ключи, откроет браузер, поймает ответ и запишет всё в базу.
Сами значения он не печатает.
"""
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta

import sar_common

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Только чтение: платформа ничего в облаке не меняет. Просить больше прав,
# чем нужно, -- значит однажды случайно ими воспользоваться.
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

WAIT_TIMEOUT_SEC = 300


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def auth_url(client_id, redirect_uri, state):
    """Адрес согласия.

    access_type=offline и prompt=consent -- обязательны ОБА. Без первого
    refresh_token не выдаётся вовсе; без второго Google не выдаёт его
    повторно, если человек уже давал согласие этому приложению раньше --
    и тогда скрипт «отработает успешно», а главного не принесёт.
    """
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


PAGE_OK = """<!doctype html><meta charset="utf-8">
<title>Готово</title>
<body style="font:16px system-ui;padding:3rem;max-width:32rem">
<h2>Доступ выдан</h2>
<p>Вкладку можно закрыть и вернуться в терминал.</p>
</body>"""

PAGE_FAIL = """<!doctype html><meta charset="utf-8">
<title>Не получилось</title>
<body style="font:16px system-ui;padding:3rem;max-width:32rem">
<h2>Доступ не выдан</h2>
<p>%s</p>
<p>Вернитесь в терминал и запустите скрипт заново.</p>
</body>"""


class Catcher(http.server.BaseHTTPRequestHandler):
    """Ловит единственный ответ Google и кладёт его в result."""

    result = {}
    expected_state = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        state = (q.get("state") or [""])[0]
        code = (q.get("code") or [""])[0]
        err = (q.get("error") or [""])[0]

        if state != self.expected_state:
            # Чужой запрос на наш порт. Не принимаем: иначе кто угодно на
            # этой машине мог бы подсунуть свой код.
            self._send(400, PAGE_FAIL % "Запрос не от этой попытки входа.")
            return
        if err:
            Catcher.result = {"error": err}
            self._send(400, PAGE_FAIL % ("Google ответил: %s" % err))
            return
        if not code:
            Catcher.result = {"error": "ответ без кода"}
            self._send(400, PAGE_FAIL % "Google не прислал код.")
            return
        Catcher.result = {"code": code}
        self._send(200, PAGE_OK)

    def _send(self, status, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass        # свой вывод, а не строки http.server


def wait_for_code(port, state, timeout=WAIT_TIMEOUT_SEC):
    """Поднимает возврат и ждёт ответа. Возвращает код или бросает."""
    Catcher.result = {}
    Catcher.expected_state = state
    srv = http.server.HTTPServer(("127.0.0.1", port), Catcher)
    srv.timeout = 1
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.3},
                         daemon=True)
    t.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if Catcher.result:
                break
            time.sleep(0.3)
    finally:
        srv.shutdown()
        srv.server_close()

    if not Catcher.result:
        raise TimeoutError(
            "ответа от Google не дождались за %d с. Возможно, вкладка не "
            "открылась -- адрес был напечатан выше, откройте его вручную."
            % timeout)
    if "error" in Catcher.result:
        raise RuntimeError("Google отказал: %s" % Catcher.result["error"])
    return Catcher.result["code"]


def exchange(client_id, client_secret, code, redirect_uri):
    """Меняет одноразовый код на пару токенов."""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:                               # noqa: BLE001
            pass
        raise RuntimeError("обмен кода не удался (код %d): %s" % (e.code, detail))

    if not data.get("refresh_token"):
        # Молчаливый провал: токен есть, работать будет час, а потом всё
        # встанет ровно так же, как раньше. Лучше сказать сразу.
        raise RuntimeError(
            "Google не выдал refresh_token. Обычно это значит, что согласие "
            "этому приложению уже давалось раньше. Отзовите доступ на "
            "myaccount.google.com/permissions и запустите скрипт заново.")
    expires_at = (datetime.now()
                  + timedelta(seconds=int(data.get("expires_in") or 3600))
                  ).isoformat()
    return data["access_token"], data["refresh_token"], expires_at


def save(conn, client_id, client_secret, token, refresh_token, expires_at):
    """Кладёт всё в существующее подключение Google. Возвращает его id."""
    rows = [a for a in sar_common.cloud_accounts(conn)
            if a["provider"] == "google"]
    if not rows:
        raise RuntimeError(
            "в базе нет подключения Google. Сначала подключите диск в /admin, "
            "потом запустите этот скрипт -- он добавит недостающие ключи.")
    if len(rows) > 1:
        raise RuntimeError(
            "подключений Google несколько (%d). Оставьте одно."
            % len(rows))
    acc_id = rows[0]["id"]
    sar_common.update_cloud_account(
        conn, acc_id, client_id=client_id, client_secret=client_secret,
        token=token, refresh_token=refresh_token, expires_at=expires_at,
        last_error=None)
    return acc_id


def main():
    print("ПОЛУЧЕНИЕ КЛЮЧЕЙ GOOGLE")
    print()
    client_id = (sys.argv[1] if len(sys.argv) > 1
                 else input("Client ID:     ")).strip()
    client_secret = (sys.argv[2] if len(sys.argv) > 2
                     else input("Client secret: ")).strip()
    if not client_id or not client_secret:
        print("нужны оба значения")
        return 2
    if not client_id.endswith(".apps.googleusercontent.com"):
        print("ВНИМАНИЕ: Client ID обычно оканчивается на "
              ".apps.googleusercontent.com -- проверьте, что скопировали его, "
              "а не что-то соседнее.")

    port = free_port()
    redirect_uri = "http://127.0.0.1:%d" % port
    state = secrets.token_urlsafe(16)
    url = auth_url(client_id, redirect_uri, state)

    print()
    print("Открываю браузер. Если не открылся -- скопируйте адрес:")
    print(url)
    print()
    try:
        webbrowser.open(url)
    except Exception:                                   # noqa: BLE001
        pass                # не беда: адрес напечатан выше

    print("Жду ответа (до %d с)..." % WAIT_TIMEOUT_SEC)
    try:
        code = wait_for_code(port, state)
    except Exception as e:                              # noqa: BLE001
        print("\n%s" % e)
        return 1

    print("Код получен, меняю на токены...")
    try:
        token, refresh, expires_at = exchange(client_id, client_secret, code,
                                              redirect_uri)
    except Exception as e:                              # noqa: BLE001
        print("\n%s" % e)
        return 1

    cfg, _ = sar_common.load_server_config(os.path.dirname(os.path.abspath(__file__)))
    _, _, db, _ = sar_common.resolve_paths(cfg["watch_dir"], cfg.get("data_dir"))
    conn = sar_common.get_db_connection(db)
    try:
        acc_id = save(conn, client_id, client_secret, token, refresh, expires_at)
    except Exception as e:                              # noqa: BLE001
        print("\n%s" % e)
        return 1
    finally:
        conn.close()

    print()
    print("ГОТОВО. Ключи записаны в подключение #%d." % acc_id)
    print("Токен теперь продлевается сам -- часовой лимит снят.")
    print("Значения не печатаю: они в базе.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
