"""Запуск одним контейнером (sar_container.py) -- поставка через cd.

Что здесь сторожится:
- конфиг собирается из шаблона + переменных, пути в контейнере
  принудительные, пароль по умолчанию НЕ пускает платформу наружу;
- уже лежащий в папке данных конфиг (то, что поменял человек или бот)
  при перезапуске НЕ затирается -- урок sar_config.json из архива
  обновлений, который возвращал пароль на change_me;
- супервизор: смерть воркера или веба роняет контейнер (а не оставляет
  страницу без очереди), бот без токена не запускается;
- ссылки в Dockerfile указывают ровно туда, где их ищет код.
"""
import json
import os
import sys
import time

import pytest

import sar_common
import sar_container as sc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _example():
    with open(os.path.join(ROOT, "sar_config.example.json"), encoding="utf-8") as f:
        return json.load(f)


def test_paths_and_port_are_forced(tmp_path):
    cur = _example()
    cur["server"]["watch_dir"] = "D:\\Coding\\material"
    cur["server"]["port"] = 8080
    cfg = sc.build_config(cur, {"SAR_SHARED_PASSWORD": "s3cret"}, str(tmp_path), "3000")
    srv = cfg["server"]
    assert srv["watch_dir"] == os.path.join(str(tmp_path), "material")
    assert srv["data_dir"] == os.path.join(str(tmp_path), "sar_data")
    assert srv["port"] == 3000 and srv["host"] == "0.0.0.0"
    assert srv["shared_password"] == "s3cret"


def test_overrides_cannot_move_paths(tmp_path):
    env = {"SAR_SHARED_PASSWORD": "x1",
           "SAR_CONFIG_OVERRIDES": json.dumps({"server": {"data_dir": "/elsewhere", "port": 1},
                                               "conf": 0.3})}
    cfg = sc.build_config(_example(), env, str(tmp_path), 3000)
    assert cfg["server"]["data_dir"] == os.path.join(str(tmp_path), "sar_data")
    assert cfg["server"]["port"] == 3000
    assert cfg["conf"] == 0.3          # прочее перекрытие применяется
    assert cfg["server"]["poll_interval_sec"] == 15   # и не стирает соседей


@pytest.mark.parametrize("pw", [None, "", "change_me"])
def test_default_password_refuses_to_start(tmp_path, pw):
    env = {} if pw is None else {"SAR_SHARED_PASSWORD": pw}
    with pytest.raises(sc.ConfigError, match="пароль"):
        sc.build_config(_example(), env, str(tmp_path), 3000)


def test_env_parsing(tmp_path):
    env = {"SAR_SHARED_PASSWORD": "x1", "SAR_AUTO_PROCESS": "true",
           "SAR_TELEGRAM_BOT_TOKEN": "123:abc",
           "SAR_TELEGRAM_ADMIN_CHAT_IDS": "111, -222;333"}
    cfg = sc.build_config(_example(), env, str(tmp_path), 3000)
    assert cfg["server"]["auto_process"] is True
    assert cfg["telegram_bot"]["bot_token"] == "123:abc"
    assert cfg["telegram_bot"]["admin_chat_ids"] == [111, -222, 333]
    with pytest.raises(sc.ConfigError):
        sc.build_config(_example(), dict(env, SAR_AUTO_PROCESS="maybe"), str(tmp_path), 3000)
    with pytest.raises(sc.ConfigError):
        sc.build_config(_example(), dict(env, SAR_TELEGRAM_ADMIN_CHAT_IDS="@me"), str(tmp_path), 3000)
    with pytest.raises(sc.ConfigError):
        sc.build_config(_example(), dict(env, SAR_CONFIG_OVERRIDES="[1]"), str(tmp_path), 3000)


def test_empty_env_lines_do_not_wipe_config(tmp_path):
    """Шаблон env-файла содержит пустые строки "КЛЮЧ=". Они не должны
    затирать пароль и токен, уже лежащие в конфиге."""
    cur = _example()
    cur["server"]["shared_password"] = "from-file"
    cur["telegram_bot"]["bot_token"] = "123:file"
    cur["telegram_bot"]["admin_chat_ids"] = [42]
    env = {"SAR_SHARED_PASSWORD": "", "SAR_TELEGRAM_BOT_TOKEN": " ",
           "SAR_TELEGRAM_ADMIN_CHAT_IDS": "", "SAR_CONFIG_OVERRIDES": ""}
    cfg = sc.build_config(cur, env, str(tmp_path), 3000)
    assert cfg["server"]["shared_password"] == "from-file"
    assert cfg["telegram_bot"]["bot_token"] == "123:file"
    assert cfg["telegram_bot"]["admin_chat_ids"] == [42]


def test_env_example_parses_as_docker_env_file():
    """Шаблон в репозитории -- рабочий env-файл: только КЛЮЧ=значение и
    комментарии, и все ключи известны контейнеру (опечатка в имени молча
    ничего бы не задала)."""
    known = set(sc.ENV_MAP) | {"SAR_CONFIG_OVERRIDES", "SAR_BACKUP_INTERVAL_HOURS",
                                "SAR_BACKUP_KEEP"}
    with open(os.path.join(ROOT, "deploy", "cd", "sar.env.example"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, _ = line.partition("=")
            assert sep and key == key.strip() and key in known, line
    with open(os.path.join(ROOT, "deploy", "cd", "sar.env.example"), encoding="utf-8") as f:
        text = f.read()
    for name in known:
        assert name in text, f"{name} не описан в шаблоне env-файла"


def test_fresh_install_does_not_autoprocess_but_keeps_explicit_choice(tmp_path):
    cfg = sc.build_config(_example(), {"SAR_SHARED_PASSWORD": "x1"}, str(tmp_path), 3000)
    assert cfg["server"]["auto_process"] is False
    cur = _example()
    cur["server"]["auto_process"] = True
    cfg = sc.build_config(cur, {"SAR_SHARED_PASSWORD": "x1"}, str(tmp_path), 3000)
    assert cfg["server"]["auto_process"] is True


def test_prepare_creates_from_template_then_keeps_human_edits(tmp_path):
    data = str(tmp_path / "data")
    path, cfg = sc.prepare({"SAR_SHARED_PASSWORD": "x1"}, data, ROOT)
    assert path == os.path.join(data, "sar_config.json")
    for sub in ("material", "sar_data", sar_common.BACKUPS_DIR_NAME, "tmp"):
        assert os.path.isdir(os.path.join(data, sub)), sub
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"   # там пароль и токен

    # Человек поправил конфиг на сервере, бот дописал своё имя.
    with open(path, encoding="utf-8") as f:
        on_disk = json.load(f)
    on_disk["conf"] = 0.42
    on_disk["telegram_bot"]["bot_username"] = "sar_test_bot"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(on_disk, f)

    # Перезапуск без SAR_SHARED_PASSWORD: пароль берётся из файла.
    _, cfg = sc.prepare({}, data, ROOT)
    assert cfg["conf"] == 0.42
    assert cfg["telegram_bot"]["bot_username"] == "sar_test_bot"
    assert cfg["server"]["shared_password"] == "x1"
    assert not os.path.exists(path + ".tmp")


def test_config_written_is_what_platform_reads(tmp_path):
    """Сквозная проверка: собранный файл читает тот же загрузчик, что сервер
    и воркер, и получает пути внутри папки данных."""
    data = str(tmp_path / "data")
    path, _ = sc.prepare({"SAR_SHARED_PASSWORD": "x1"}, data, ROOT)
    cfg, cfg_path = sar_common.load_server_config(os.path.dirname(path))
    watch, data_dir, db_path, _ = sar_common.resolve_paths(cfg["watch_dir"], cfg["data_dir"])
    assert watch == os.path.join(data, "material")
    assert db_path == os.path.join(data, "sar_data", "sar_data.db")
    assert cfg["shared_password"] == "x1"


def test_dockerfile_links_match_where_code_looks():
    """Код ищет sar_config.json рядом с собой и копии в backups_dir().
    Ссылки в образе обязаны вести ровно туда, где их создаёт prepare()."""
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
        df = f.read()
    assert "ln -sfn /app/data/sar_config.json /app/sar_config.json" in df
    name = sar_common.BACKUPS_DIR_NAME
    assert os.path.basename(sar_common.backups_dir()) == name
    assert f"ln -sfn /app/data/{name} /app/{name}" in df
    assert sc.DATA_ROOT_DEFAULT == "/app/data"
    assert 'CMD ["python", "-u", "sar_container.py"]' in df


# --- супервизор на подставных процессах -------------------------------------

def _fake_app(tmp_path, worker_code="import time; time.sleep(60)",
              server_code="import time; time.sleep(60)",
              bot_code="import time; time.sleep(60)"):
    app = tmp_path / "app"
    app.mkdir()
    (app / "sar_worker.py").write_text(worker_code)
    (app / "sar_server.py").write_text(server_code)
    (app / "sar_telegram_bot.py").write_text(bot_code)
    return str(app)


def _run_with_timeout(sup, sec=15):
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("code", sup.run()), daemon=True)
    t.start()
    t.join(sec)
    return out.get("code")


def test_worker_death_stops_container(tmp_path, monkeypatch):
    app = _fake_app(tmp_path, worker_code="import sys; sys.exit(3)")
    monkeypatch.setattr(sc, "APP_DIR", app)
    sup = sc.Supervisor(str(tmp_path), {"telegram_bot": {}},
                        dict(os.environ, SAR_BACKUP_INTERVAL_HOURS="0"))
    sup.start()
    assert "bot" not in sup.procs          # без токена бота нет
    code = _run_with_timeout(sup)
    assert code == 1
    assert sup.procs["server"].poll() is not None, "веб должен быть остановлен вместе с воркером"


def test_server_death_stops_container(tmp_path, monkeypatch):
    app = _fake_app(tmp_path, server_code="import sys; sys.exit(1)")
    monkeypatch.setattr(sc, "APP_DIR", app)
    sup = sc.Supervisor(str(tmp_path), {"telegram_bot": {"bot_token": "t"}},
                        dict(os.environ, SAR_BACKUP_INTERVAL_HOURS="0"))
    sup.start()
    assert "bot" in sup.procs
    assert _run_with_timeout(sup) == 1
    assert all(p.poll() is not None for p in sup.procs.values())


def test_bot_death_does_not_stop_platform(tmp_path, monkeypatch):
    app = _fake_app(tmp_path, bot_code="import sys; sys.exit(1)")
    monkeypatch.setattr(sc, "APP_DIR", app)
    monkeypatch.setattr(sc, "BOT_RESTART_DELAY_SEC", 0.5)
    sup = sc.Supervisor(str(tmp_path), {"telegram_bot": {"bot_token": "t"}},
                        dict(os.environ, SAR_BACKUP_INTERVAL_HOURS="0"))
    sup.start()
    first_bot = sup.procs["bot"]
    import threading
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while sup.procs["bot"] is first_bot and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        assert sup.procs["bot"] is not first_bot, "бот не перезапущен"
        assert sup.procs["worker"].poll() is None and sup.procs["server"].poll() is None
    finally:
        sup.stop()
        t.join(5)


def test_sigterm_path_stops_children(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)
    monkeypatch.setattr(sc, "APP_DIR", app)
    sup = sc.Supervisor(str(tmp_path), {"telegram_bot": {}},
                        dict(os.environ, SAR_BACKUP_INTERVAL_HOURS="0"))
    sup.start()
    sup.stopping.set()                       # то, что делает обработчик SIGTERM
    assert _run_with_timeout(sup) == 0
    sup.stop()
    assert all(p.poll() is not None for p in sup.procs.values())


def test_child_env_has_windows_lessons_and_tmp(tmp_path):
    env = sc._child_env(str(tmp_path))
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["TMPDIR"] == os.path.join(str(tmp_path), "tmp")
