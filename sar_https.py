# -*- coding: utf-8 -*-
"""Обратный прокси перед платформой: HTTPS и постоянное имя.

Зачем отдельный запускатель, а не просто `caddy run`. Имя платформы уже
лежит в sar_config.json -- там же, откуда его берут бот и сторож имени.
Записав его вторым местом в Caddyfile, мы завели бы два источника правды,
которые обязательно разойдутся: в этом проекте так уже было с путём к
папке резервных копий (считался трижды, дважды неверно -- мониторинг
годами смотрел не туда). Поэтому имя подставляется в Caddy из конфига.

Что делает:
  python sar_https.py          поднять прокси
  python sar_https.py check    проверить конфиг и готовность, ничего не поднимая
  python sar_https.py local    проверить схему БЕЗ домена, на localhost

Режим local существует ради одного: убедиться, что связка
браузер -> Caddy -> платформа работает, ещё ДО того, как настроен роутер.
Сертификат там свой, браузер на него поругается -- это ожидаемо.
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "sar_config.json")
CADDYFILE = os.path.join(ROOT, "Caddyfile")
LOG_DIR = os.path.join(ROOT, "sar_data", "https")
LOCAL_PORT = 8081      # для режима local: проверка цепочки без TLS


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def read_cfg():
    if not os.path.exists(CFG_PATH):
        return {}
    return json.load(io.open(CFG_PATH, encoding="utf-8"))


def caddy_path():
    """Где caddy. Отдельно, потому что winget кладёт его в папку, которой
    может не быть в PATH текущей оболочки -- она читает PATH при запуске,
    а установка была позже."""
    found = shutil.which("caddy")
    if found:
        return found

    local = os.environ.get("LOCALAPPDATA", "")
    guesses = [
        os.path.join(local, "Microsoft", "WinGet", "Links", "caddy.exe"),
        r"C:\Program Files\Caddy\caddy.exe",
        "/usr/bin/caddy",
        "/usr/local/bin/caddy",
    ]
    for g in guesses:
        if g and os.path.exists(g):
            return g

    # winget кладёт пакет в папку, в имени которой есть меняющийся хвост
    # (источник и подпись), поэтому путь не угадать -- ищем.
    pkgs = os.path.join(local, "Microsoft", "WinGet", "Packages")
    if os.path.isdir(pkgs):
        for name in os.listdir(pkgs):
            if not name.lower().startswith("caddyserver"):
                continue
            cand = os.path.join(pkgs, name, "caddy.exe")
            if os.path.exists(cand):
                return cand
    return None


def port_busy(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def settings():
    cfg = read_cfg()
    d = cfg.get("ddns") or {}
    srv = cfg.get("server") or {}
    return {
        "domain": (d.get("hostname") or "").strip(),
        "port": int(srv.get("port") or 8080),
    }


def check(verbose=True):
    """Готово ли всё к запуску. Возвращает список претензий (пустой -- ок).

    Претензии СПИСКОМ, а не первой найденной: человек, у которого не
    настроено три вещи, должен увидеть все три сразу, а не чинить их по
    одной за три запуска. На этом уже обжигались в подключении облака.
    """
    problems = []
    s = settings()

    if not s["domain"]:
        problems.append(
            "не задано имя: впишите ddns.hostname в sar_config.json "
            "(например sar-kurumdy.dedyn.io)")
    elif "." not in s["domain"]:
        problems.append("имя %r не похоже на доменное" % s["domain"])

    if caddy_path() is None:
        problems.append("caddy не найден. Установите: "
                        "winget install CaddyServer.Caddy")

    if not port_busy(s["port"]):
        problems.append(
            "платформа не отвечает на 127.0.0.1:%d -- запустите "
            "sar_server.py, иначе прокси будет отдавать пустоту" % s["port"])

    for p in (80, 443):
        if port_busy(p):
            problems.append("порт %d уже занят -- Caddy его не получит" % p)

    if verbose:
        print("имя:       %s" % (s["domain"] or "НЕ ЗАДАНО"))
        print("caddy:     %s" % (caddy_path() or "НЕ НАЙДЕН"))
        print("платформа: %s" % ("отвечает на :%d" % s["port"]
                                 if port_busy(s["port"]) else "НЕ ОТВЕЧАЕТ"))
        print()
        if problems:
            print("МЕШАЕТ ЗАПУСКУ:")
            for p in problems:
                print("  - %s" % p)
        else:
            print("всё готово, можно запускать: python sar_https.py")
    return problems


def _env(domain):
    env = dict(os.environ)
    env["SAR_DOMAIN"] = domain
    return env


def run(local=False):
    s = settings()
    if local:
        # ПЛАЙН HTTP на высоком порту, а не https://localhost.
        #
        # Для localhost Caddy выписывает свой сертификат и лезет ставить
        # корневой в хранилище Windows -- это запрос прав, который в фоне
        # некому подтвердить, и запуск встаёт намертво. Проверять здесь
        # надо не доверие к сертификату (это забота Caddy и она работает),
        # а свою цепочку: доходят ли запросы до платформы, не ломаются ли
        # куски видео, не сжимается ли лишнее.
        domain = "http://localhost:%d" % LOCAL_PORT
    else:
        problems = check(verbose=False)
        if problems:
            log("запускать рано:")
            for p in problems:
                log("  - %s" % p)
            return 1
        domain = s["domain"]

    caddy = caddy_path()
    if caddy is None:
        log("caddy не найден. winget install CaddyServer.Caddy")
        return 1

    os.makedirs(LOG_DIR, exist_ok=True)
    log("поднимаю прокси на %s -> 127.0.0.1:%d" % (domain, s["port"]))
    if not local:
        log("первый запуск выпускает сертификат: нужен проброшенный порт 80")
    cmd = [caddy, "run", "--config", CADDYFILE, "--adapter", "caddyfile"]
    try:
        return subprocess.call(cmd, cwd=ROOT, env=_env(domain))
    except KeyboardInterrupt:
        return 0


def validate():
    """Разбирает ли Caddy наш Caddyfile. Проверка синтаксиса без запуска."""
    caddy = caddy_path()
    if caddy is None:
        log("caddy не найден")
        return 1
    s = settings()
    env = _env(s["domain"] or "example.org")
    p = subprocess.run([caddy, "validate", "--config", CADDYFILE,
                        "--adapter", "caddyfile"],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=60)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode == 0:
        log("Caddyfile разбирается без ошибок")
    else:
        log("Caddyfile не разбирается:")
        print(out.strip())
    return p.returncode


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "check":
        check()
        return validate()
    if cmd == "local":
        return run(local=True)
    if cmd == "run":
        return run()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
