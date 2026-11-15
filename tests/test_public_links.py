"""Постоянные ссылки: свой домен (контейнер cd) против туннеля (Windows).

Ссылки на находки шли через бота (t.me/<bot>?start=finding_<id>), потому что
адрес туннеля менялся при каждом перезапуске и прямая ссылка протухала
молча. С постоянным адресом (server.public_url, его задаёт SAR_SERVICE_URL
в контейнере) обход не нужен: ссылка прямая, открывается без Telegram.

Сторожим, что:
- при public_url ссылка на находку прямая, даже если бот настроен;
- адрес ТУННЕЛЯ (telegram_bot.service_url, пишет sar_tunnel.py) постоянным
  не считается -- там по-прежнему бот;
- контейнер ставит public_url из SAR_SERVICE_URL;
- браузер узнаёт о постоянном адресе (stable) и в JS не уходит в бота.
"""
import json

import pytest

import sar_common
import sar_container
import sar_server


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setitem(sar_server._EXTERNAL_BASE_CACHE, "url", None)

    def write(cfg):
        (tmp_path / "sar_config.json").write_text(json.dumps(cfg), encoding="utf-8")
        sar_server._EXTERNAL_BASE_CACHE["url"] = None    # сброс кэша
    return write


def test_stable_address_gives_direct_link_even_with_bot(cfgdir):
    cfgdir({"server": {"public_url": "https://sar.example/"},
            "telegram_bot": {"service_url": "https://sar.example", "bot_username": "sar_bot"}})
    assert sar_server.finding_share_link(7) == "https://sar.example/finding/7/"
    assert sar_server.public_url() == "https://sar.example"


def test_tunnel_address_is_not_trusted_bot_link_kept(cfgdir):
    cfgdir({"telegram_bot": {"service_url": "https://random-words.trycloudflare.com",
                             "bot_username": "sar_bot"}})
    assert sar_server.public_url() == ""
    assert sar_server.finding_share_link(7) == "https://t.me/sar_bot?start=finding_7"


def test_tunnel_without_bot_still_gives_direct_link(cfgdir):
    cfgdir({"telegram_bot": {"service_url": "https://t.example"}})
    assert sar_server.finding_share_link(3) == "https://t.example/finding/3/"


def test_api_tells_browser_the_address_is_stable(cfgdir):
    cfgdir({"server": {"public_url": "https://sar.example"},
            "telegram_bot": {"bot_username": "sar_bot"}})
    sar_server.app.testing = True
    sar_server.app.secret_key = "t"
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    d = c.get("/api/external_base").get_json()
    assert d == {"base": "https://sar.example", "bot": "sar_bot", "stable": True}


def test_js_link_builders_respect_stable_address():
    """Обе копии findingLink (страница операции и плеер) не уходят в бота
    при постоянном адресе -- иначе сервер и кнопка давали бы разные ссылки."""
    import inspect
    src = inspect.getsource(sar_server)
    assert src.count("if (obsId && BOT_NAME && !STABLE_BASE)") == 2
    assert "if (obsId && BOT_NAME) return" not in src


def test_container_sets_public_url_from_service_url(tmp_path):
    cfg = sar_container.build_config(
        {"server": {}, "telegram_bot": {}},
        {"SAR_SHARED_PASSWORD": "x1", "SAR_SERVICE_URL": "https://sar.example/"},
        str(tmp_path), 3000)
    assert cfg["server"]["public_url"] == "https://sar.example"
    assert cfg["telegram_bot"]["service_url"] == "https://sar.example/"
    cfg = sar_container.build_config({"server": {}, "telegram_bot": {}},
                                     {"SAR_SHARED_PASSWORD": "x1"}, str(tmp_path), 3000)
    assert "public_url" not in cfg["server"]


def test_people_links_are_absolute_on_stable_address(cfgdir, tmp_path, monkeypatch):
    cfgdir({"server": {"public_url": "https://sar.example"}})
    db = str(tmp_path / "d.db")
    sar_common.init_db(db)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    sar_server.app.secret_key = "t"
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s.update(authed=True, verified=True, role=sar_common.ROLE_ADMIN, viewer_name="a")
    r = c.post("/api/admin/people", json={"name": "Аня"}).get_json()
    assert r["link"].startswith("https://sar.example/login?key=")
