"""Запуск платформы одним контейнером -- для поставки через cd.

cd (github.com/Dmitry-Fedotov-Dev/cd) собирает образ из Dockerfile в корне
репозитория и запускает ОДИН контейнер на проект: порт 3000, только чтение
везде, кроме /tmp и одной папки данных, без root. Платформа же -- это
несколько независимых процессов (воркер, веб, бот), которые говорят только
через базу. Этот файл -- их общий родитель внутри контейнера:

1. Собирает sar_config.json в папке данных из шаблона и переменных
   окружения (файл env проекта на сервере, `cdctl project add --env-file`).
   Секреты живут на сервере, в репозиторий и в образ не попадают.
2. Запускает sar_worker.py, sar_server.py и, если задан токен,
   sar_telegram_bot.py. Падение воркера или веба -- выход с ошибкой, docker
   перезапустит контейнер целиком (`--restart unless-stopped`). Тихо жить
   с мёртвым воркером нельзя: страница открывается, а очередь стоит.
   Бот перезапускается сам по себе с паузой -- его падение не повод
   ронять платформу.
3. Делает резервные копии базы по расписанию (sar_backup.py). На Windows
   это делал планировщик, в контейнере его нет.

ПОЧЕМУ НЕ ПРАВКА ЗАГРУЗКИ КОНФИГА. Все процессы ищут sar_config.json и
sar_backups/ рядом со своим кодом (SCRIPT_DIR, sar_common.backups_dir()).
В образе код лежит в /app только для чтения, поэтому Dockerfile кладёт туда
символьные ссылки на папку данных. Ни одна строка платформы не меняется:
что на Windows-машине, то и в контейнере.

ПУТИ В КОНТЕЙНЕРЕ ПРИНУДИТЕЛЬНЫЕ. watch_dir, data_dir, host, port
перезаписываются при каждом старте: другого места для данных у контейнера
нет, а порт задаёт cd. Остальной конфиг -- как у обычной установки.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import sar_common

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Папка данных проекта cd (`--data /app/data`). Единственное место, куда
# контейнер может писать постоянно.
DATA_ROOT_DEFAULT = "/app/data"

PLACEHOLDER_PASSWORDS = {"", "change_me"}

# Через сколько после старта первая копия и как часто следующие. Первая --
# не сразу: базу создаёт воркер (init_db), ей нужно появиться.
BACKUP_FIRST_DELAY_SEC = 60
BACKUP_INTERVAL_HOURS_DEFAULT = 6.0
BACKUP_KEEP_DEFAULT = 10

BOT_RESTART_DELAY_SEC = 30

# Сколько ждём детей после SIGTERM. docker stop по умолчанию даёт 10 с,
# потом SIGKILL; оставляем запас на собственный выход.
STOP_GRACE_SEC = 8


class ConfigError(Exception):
    """Конфиг собрать нельзя -- платформа не должна стартовать."""


def _parse_bool(name, raw):
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on", "да"):
        return True
    if v in ("0", "false", "no", "off", "нет"):
        return False
    raise ConfigError(f"{name}={raw!r}: ожидается true/false")


def _parse_chat_ids(name, raw):
    ids = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise ConfigError(f"{name}: {part!r} не число (chat_id)") from None
    return ids


def _deep_merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# Переменная окружения -> (секция, ключ, разбор). Только то, что меняется
# от установки к установке; остальное -- через SAR_CONFIG_OVERRIDES.
ENV_MAP = {
    "SAR_SHARED_PASSWORD": ("server", "shared_password", None),
    "SAR_AUTO_PROCESS": ("server", "auto_process", _parse_bool),
    "SAR_TELEGRAM_BOT_TOKEN": ("telegram_bot", "bot_token", None),
    "SAR_TELEGRAM_ADMIN_CHAT_IDS": ("telegram_bot", "admin_chat_ids", _parse_chat_ids),
    "SAR_SERVICE_URL": ("telegram_bot", "service_url", None),
    "SAR_GUIDE_URL": ("telegram_bot", "guide_url", None),
}


def build_config(current, env, data_root, port):
    """Конфиг, с которым стартует контейнер. Чистая функция -- для тестов.

    current -- то, что уже лежит в папке данных (или шаблон при первом
    старте). Порядок: current -> SAR_CONFIG_OVERRIDES -> отдельные
    переменные -> принудительные пути. Позднее побеждает: переменная
    точнее общего JSON, а пути не может переопределить никто.
    """
    cfg = json.loads(json.dumps(current))  # глубокая копия
    cfg.setdefault("server", {})
    cfg.setdefault("telegram_bot", {})

    raw = env.get("SAR_CONFIG_OVERRIDES", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
        except ValueError as e:
            raise ConfigError(f"SAR_CONFIG_OVERRIDES: не JSON ({e})") from None
        if not isinstance(extra, dict):
            raise ConfigError("SAR_CONFIG_OVERRIDES: нужен JSON-объект")
        _deep_merge(cfg, extra)

    for name, (section, key, parse) in ENV_MAP.items():
        value = env.get(name, "").strip()
        # Пустое = не задано. В шаблоне env-файла строки вида
        # "SAR_TELEGRAM_BOT_TOKEN=" стоят пустыми, и без этой проверки они
        # затирали бы токен и пароль, уже лежащие в конфиге.
        if not value:
            continue
        cfg.setdefault(section, {})[key] = parse(name, value) if parse else value

    srv = cfg["server"]
    srv["watch_dir"] = os.path.join(data_root, "material")
    srv["data_dir"] = os.path.join(data_root, "sar_data")
    srv["host"] = "0.0.0.0"
    srv["port"] = int(port)
    # Свежая установка НЕ запускает детектор на всём подряд. Модели в образе
    # нет (веса не в репозитории), и без этой строки каждый файл уходил бы в
    # error. Значение переносится в базу один раз (seed_settings_from_config)
    # и дальше меняется в админке; здесь только умолчание для нового конфига.
    srv.setdefault("auto_process", False)

    if srv.get("shared_password", "") in PLACEHOLDER_PASSWORDS:
        raise ConfigError(
            "не задан общий пароль: SAR_SHARED_PASSWORD в env-файле проекта "
            "(или server.shared_password в sar_config.json в папке данных). "
            "Платформа с паролем по умолчанию в интернет не выходит.")
    return cfg


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def prepare(env, data_root, app_dir=APP_DIR):
    """Папки и конфиг в папке данных. Возвращает путь к конфигу."""
    # Имя папки копий -- из sar_common, а не своё: страж test_backup_dir.
    for sub in ("material", "sar_data", sar_common.BACKUPS_DIR_NAME, "tmp"):
        os.makedirs(os.path.join(data_root, sub), exist_ok=True)

    config_path = os.path.join(data_root, "sar_config.json")
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            current = json.load(f)
    else:
        with open(os.path.join(app_dir, "sar_config.example.json"), encoding="utf-8") as f:
            current = json.load(f)
        print(f"[container] первый старт: конфиг создан из шаблона -> {config_path}",
              flush=True)

    cfg = build_config(current, env, data_root, env.get("PORT", "3000"))
    write_json_atomic(config_path, cfg)
    return config_path, cfg


def _child_env(data_root):
    env = dict(os.environ)
    # См. грабли в CLAUDE.md: без этого вывод детей буферизуется и путается
    # кодировка русских строк.
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # /tmp в контейнере cd -- tmpfs на 64 МБ. Временные файлы кодирования и
    # скачивания туда не влезут.
    env["TMPDIR"] = os.path.join(data_root, "tmp")
    return env


def _spawn(script, env):
    return subprocess.Popen([sys.executable, "-u", os.path.join(APP_DIR, script)],
                            cwd=APP_DIR, env=env)


class Supervisor:
    def __init__(self, data_root, cfg, env):
        self.data_root = data_root
        self.env = env
        self.stopping = threading.Event()
        self.procs = {}
        self.with_bot = bool(cfg.get("telegram_bot", {}).get("bot_token"))
        self.backup_interval = float(env.get("SAR_BACKUP_INTERVAL_HOURS",
                                             BACKUP_INTERVAL_HOURS_DEFAULT))
        self.backup_keep = int(env.get("SAR_BACKUP_KEEP", BACKUP_KEEP_DEFAULT))

    def start(self):
        # Воркер первым: он создаёт базу. init_db идемпотентна, порядок
        # не обязателен, но так меньше шума в журнале первого старта.
        self.procs["worker"] = _spawn("sar_worker.py", self.env)
        self.procs["server"] = _spawn("sar_server.py", self.env)
        if self.with_bot:
            self.procs["bot"] = _spawn("sar_telegram_bot.py", self.env)
        else:
            print("[container] SAR_TELEGRAM_BOT_TOKEN не задан -- бот не запускается",
                  flush=True)
        if self.backup_interval > 0:
            threading.Thread(target=self._backup_loop, daemon=True).start()
        else:
            print("[container] резервные копии выключены (SAR_BACKUP_INTERVAL_HOURS=0)",
                  flush=True)

    def _backup_loop(self):
        delay = BACKUP_FIRST_DELAY_SEC
        while not self.stopping.wait(delay):
            r = subprocess.run(
                [sys.executable, "-u", os.path.join(APP_DIR, "sar_backup.py"),
                 "--root", APP_DIR, "--keep", str(self.backup_keep)],
                cwd=APP_DIR, env=self.env)
            if r.returncode != 0:
                # Не молчим: мониторинг увидит протухшую копию, а журнал --
                # причину. Повтор через час, а не через интервал.
                print(f"[container] резервная копия не удалась (код {r.returncode}), "
                      f"повтор через час", flush=True)
                delay = 3600
            else:
                delay = self.backup_interval * 3600

    def run(self):
        """Ждёт, пока не упадёт обязательный процесс или не придёт сигнал."""
        bot_died_at = None
        while not self.stopping.is_set():
            for name in ("worker", "server"):
                code = self.procs[name].poll()
                if code is not None:
                    print(f"[container] {name} завершился с кодом {code} -- "
                          f"останавливаю контейнер, docker перезапустит его",
                          flush=True)
                    self.stop()
                    return 1
            bot = self.procs.get("bot")
            if bot is not None and bot.poll() is not None:
                if bot_died_at is None:
                    bot_died_at = time.monotonic()
                    print(f"[container] бот завершился с кодом {bot.returncode}, "
                          f"перезапуск через {BOT_RESTART_DELAY_SEC} с", flush=True)
                elif time.monotonic() - bot_died_at >= BOT_RESTART_DELAY_SEC:
                    self.procs["bot"] = _spawn("sar_telegram_bot.py", self.env)
                    bot_died_at = None
            self.stopping.wait(1)
        return 0

    def stop(self):
        self.stopping.set()
        for p in self.procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SEC
        for p in self.procs.values():
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()


def main():
    data_root = os.environ.get("SAR_DATA_ROOT", DATA_ROOT_DEFAULT)
    try:
        config_path, cfg = prepare(os.environ, data_root)
    except ConfigError as e:
        print(f"[container] ОШИБКА КОНФИГА: {e}", flush=True)
        return 2
    # Временная папка детей -- в папке данных; остатки прошлого запуска
    # никому не нужны.
    tmp = os.path.join(data_root, "tmp")
    shutil.rmtree(tmp, ignore_errors=True)  # не страшно: пересоздаётся строкой ниже
    os.makedirs(tmp, exist_ok=True)
    print(f"[container] конфиг: {config_path}", flush=True)
    print(f"[container] материал: {cfg['server']['watch_dir']}", flush=True)

    sup = Supervisor(data_root, cfg, _child_env(data_root))
    signal.signal(signal.SIGTERM, lambda *_: sup.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: sup.stopping.set())
    sup.start()
    code = sup.run()
    if sup.procs and any(p.poll() is None for p in sup.procs.values()):
        sup.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
