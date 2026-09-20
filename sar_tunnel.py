# -*- coding: utf-8 -*-
"""Подъём и сторож туннелей наружу.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ. Быстрый туннель cloudflared (`tunnel --url`)
получает СЛУЧАЙНОЕ имя вида xxx.trycloudflare.com при каждом запуске.
При обрыве связи процесс уходит в бесконечный "Retrying connection" и
остаётся живым, но прежнее имя уже не возвращается никогда: при
переподключении выдаётся новое. Отсюда главный отказ этой платформы --
процесс жив, мониторинг зелёный, а снаружи домен не резолвится.

Поэтому проверять надо не процесс, а ОТВЕТ ПО ПУБЛИЧНОМУ АДРЕСУ, и
лечить не переподключением, а полным перезапуском с публикацией нового
адреса в конфиг бота.

Использование:
    python sar_tunnel.py up      -- поднять заново (убить старое, поднять,
                                    проверить снаружи, обновить конфиг,
                                    перезапустить бота)
    python sar_tunnel.py check   -- только проверить, код возврата 0/1
    python sar_tunnel.py watch   -- сторож: проверять раз в N секунд и
                                    поднимать заново при падении

Постоянное решение -- именованный туннель Cloudflare на своём домене:
имя не меняется, и этот сторож становится не нужен. Требует аккаунта
Cloudflare и домена.
"""
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "sar_config.json")
CLOUDFLARED = os.environ.get(
    "SAR_CLOUDFLARED", os.path.expanduser(r"~\bin\cloudflared.exe"))
LOG_DIR = os.path.join(ROOT, "sar_data", "tunnel")

# (имя, локальный порт, ключ в telegram_bot для публичного адреса)
TUNNELS = [
    ("app", 8080, "service_url"),
    ("grafana", 3000, "grafana_url"),
]

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CHECK_TIMEOUT = 25
WATCH_INTERVAL = int(os.environ.get("SAR_TUNNEL_INTERVAL", "120"))


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def read_cfg():
    return json.load(io.open(CFG_PATH, encoding="utf-8"))


def resolve_public(host):
    """Адреса хоста по ПУБЛИЧНОМУ резолверу, а не по системному.

    Роутер провайдера может перестать резолвить trycloudflare.com, оставаясь
    исправным для всего остального -- ровно это и случилось 26.08.2026.
    Системный резолвер тогда говорит "домена нет" про живой туннель. Если
    поверить ему, сторож начнёт перезапускать работающий туннель по кругу,
    каждый раз меняя публичный адрес -- то есть сам станет аварией.
    """
    url = ("https://cloudflare-dns.com/dns-query?name=%s&type=A"
           % urllib.parse.quote(host))
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]


def probe(url, path="/login"):
    """Отвечает ли публичный адрес. Именно это -- признак живого туннеля.

    Возвращает True/False/None: None означает "проверить не удалось"
    (нет связи с публичным резолвером). Перезапускать в этом случае нельзя --
    неизвестность не равна поломке.
    """
    if not url:
        return False
    host = urllib.parse.urlsplit(url).hostname
    try:
        ips = resolve_public(host)
    except Exception:
        return None
    if not ips:
        return False

    ctx = ssl.create_default_context()
    for ip in ips:
        sock = None
        try:
            sock = socket.create_connection((ip, 443), timeout=CHECK_TIMEOUT)
            tls = ctx.wrap_socket(sock, server_hostname=host)
            tls.settimeout(CHECK_TIMEOUT)
            tls.sendall(
                ("GET %s HTTP/1.1\r\nHost: %s\r\n"
                 "User-Agent: sar-tunnel-watchdog\r\nConnection: close\r\n\r\n"
                 % (path, host)).encode())
            head = tls.recv(64).decode("latin-1", "replace")
            tls.close()
            m = re.match(r"HTTP/1\.[01] (\d{3})", head)
            if m:
                code = int(m.group(1))
                # 4xx от нашего приложения означает, что туннель ЖИВ;
                # 502/530 -- что cloudflared до приложения не достучался.
                return 200 <= code < 500
        except Exception:
            continue
        finally:
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
    return False


def check():
    cfg = read_cfg()
    tb = cfg.get("telegram_bot", {})
    ok = True
    for name, _port, key in TUNNELS:
        url = tb.get(key) or ""
        alive = probe(url)
        state = {True: "живёт  ", False: "МЁРТВ  ", None: "НЕ ЯСНО"}[alive]
        log("  %-8s %s  %s" % (name, state, url or "(адрес не задан)"))
        if alive is None:
            # Нет связи с публичным резолвером -- вывод сделать нельзя.
            # Считаем живым, чтобы сторож не перезапускал вслепую.
            continue
        ok = ok and alive
    return ok


def _run(cmd, timeout=30):
    """Внешняя команда, которая ОБЯЗАНА завершиться.

    Ровно здесь сторож и повесился: subprocess.run без таймаута ждёт
    вечно, и процесс остался жив, но перестал делать что-либо -- восемь
    суток подряд платформа была недоступна снаружи, а снаружи это выглядело
    как работающий сторож. Тот же самый отказ, от которого он защищает.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout or ""
    except subprocess.TimeoutExpired:
        log("  команда не уложилась в %d с: %s" % (timeout, cmd[0]))
        return ""
    except Exception as e:                              # noqa: BLE001
        log("  команда не выполнилась (%s): %s" % (cmd[0], e))
        return ""


def heartbeat(note=None):
    """Отметка «сторож жив» в общую базу.

    Без неё зависший сторож неотличим от работающего: процесс есть,
    мониторинг зелёный, туннеля нет.
    """
    try:
        cfg = read_cfg()
        watch = os.path.abspath(os.path.join(
            ROOT, (cfg.get("server") or {}).get("watch_dir") or "."))
        db = os.path.join(watch, "sar_data", "sar_data.db")
        if not os.path.exists(db):
            return
        conn = sar_common.get_db_connection(db)
        try:
            sar_common.touch_heartbeat(conn, "tunnel", note)
        finally:
            conn.close()
    except Exception:                                   # noqa: BLE001
        # Пульс -- вспомогательная вещь. Уронить из-за него сторож нельзя.
        pass


def kill_cloudflared():
    if os.name == "nt":
        _run(["taskkill", "/F", "/IM", "cloudflared.exe"])
    else:
        _run(["pkill", "-f", "cloudflared"])
    time.sleep(1.5)


def start_tunnel(name, port):
    os.makedirs(LOG_DIR, exist_ok=True)
    logfile = os.path.join(LOG_DIR, "tun_%s.log" % name)
    # Старый лог удаляем: адрес ищется в нём, и хвост прошлого запуска
    # даст устаревшее имя.
    for p in (logfile,):
        try:
            os.remove(p)
        except OSError:
            pass
    create = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", "http://127.0.0.1:%d" % port,
         "--logfile", logfile],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=create)
    return logfile


def wait_url(logfile, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = io.open(logfile, encoding="utf-8", errors="replace").read()
            found = URL_RE.findall(text)
            if found:
                return found[-1]
        except OSError:
            pass
        time.sleep(1)
    return None


def restart_bot():
    """Бот держит адрес в памяти -- без перезапуска он раздаёт мёртвые ссылки."""
    if os.name != "nt":
        _run(["pkill", "-f", "sar_telegram_bot.py"])
    else:
        # wmic из Windows 11 удалён -- спрашиваем через CIM в PowerShell.
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like '*sar_telegram_bot.py*' } | "
              "Select-Object -ExpandProperty ProcessId")
        out = _run(["powershell", "-NoProfile", "-Command", ps])
        for pid in re.findall(r"\d{2,}", out or ""):
            _run(["taskkill", "/F", "/PID", pid])
    time.sleep(2)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    create = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen([sys.executable, "-u", "sar_telegram_bot.py"],
                     cwd=ROOT, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=create)


def up():
    log("перезапуск туннелей")
    kill_cloudflared()
    started = [(name, key, start_tunnel(name, port)) for name, port, key in TUNNELS]

    urls = {}
    for name, key, logfile in started:
        url = wait_url(logfile)
        if not url:
            log("  %s: адрес не появился в логе -- ПРОВАЛ" % name)
            return False
        urls[key] = url
        log("  %s -> %s" % (name, url))

    # Имя появляется в логе раньше, чем маршрут начинает работать:
    # cloudflared печатает адрес сразу после регистрации, а рёбра
    # Cloudflare подхватывают его через несколько секунд. Без этого
    # ожидания проверка стабильно валится на только что поднятом туннеле.
    for key, url in urls.items():
        deadline = time.time() + 90
        while time.time() < deadline:
            if probe(url) is True:
                break
            time.sleep(4)
        else:
            log("  адрес не отвечает снаружи за 90 с: %s -- ПРОВАЛ" % url)
            return False
    log("оба адреса отвечают снаружи")

    cfg = read_cfg()
    # Копия конфига содержит общий пароль и токен бота, поэтому кладём её в
    # sar_data -- эта папка целиком вне git. В корне репозитория такие файлы
    # оказывались видны для коммита: правила .gitignore закрывали
    # sar_config.json и sar_config.json.*, но не sar_config.backup_*.json.
    bak_dir = os.path.join(ROOT, "sar_data", "config_backups")
    os.makedirs(bak_dir, exist_ok=True)
    bak = os.path.join(bak_dir, "sar_config.backup_%s.json"
                       % datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(CFG_PATH, bak)
    tb = cfg.setdefault("telegram_bot", {})
    tb["service_url"] = urls["service_url"]
    tb["guide_url"] = urls["service_url"].rstrip("/") + "/guide"
    tb["grafana_url"] = urls["grafana_url"]
    io.open(CFG_PATH, "w", encoding="utf-8").write(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    log("конфиг обновлён (копия: %s)" % os.path.basename(bak))

    restart_bot()
    log("бот перезапущен -- новые ссылки выдаются по /help")
    log("ВНИМАНИЕ: адрес сменился, ранее выданные ссылки больше не работают")
    return True


def watch():
    log("сторож запущен, проверка раз в %d с" % WATCH_INTERVAL)
    while True:
        try:
            alive = check()
            heartbeat("живы" if alive else "поднимаю заново")
            if not alive:
                log("туннель не отвечает -- поднимаю заново")
                up()
                heartbeat("поднят")
            time.sleep(WATCH_INTERVAL)
        except KeyboardInterrupt:
            log("сторож остановлен")
            return
        except Exception as e:
            # Сторож не имеет права умирать от единичной ошибки: он и есть
            # последняя линия наблюдения за внешней доступностью.
            log("ошибка в цикле сторожа: %r" % e)
            time.sleep(WATCH_INTERVAL)


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if cmd == "check":
        sys.exit(0 if check() else 1)
    elif cmd == "up":
        sys.exit(0 if up() else 1)
    elif cmd == "watch":
        watch()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
