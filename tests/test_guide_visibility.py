"""Методика отсмотра должна быть видна оттуда, где смотрят.

Обнаружено на живой платформе: ссылок на /guide не было НИ ОДНОЙ. Гид с
методикой существовал, но найти его можно было, только зная адрес, --
волонтёры его просто не видели. Методика при этом главное, что отличает
внимательный просмотр от бесполезного: темп, сетка, смены, установка
«ищем не снег и не камень».

Тесты держат две вещи: ссылка есть на страницах, откуда начинают работу,
и памятка стоит в плеере -- там, где человек реально смотрит.
"""
import pytest

import sar_common
import sar_server


@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_common, "resolve_paths",
                        lambda w: (str(watch), str(watch), db, str(watch)))
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["verified"] = True
        s["viewer_name"] = "волонтёр"
        s["role"] = sar_common.ROLE_MODERATOR
    return c


def test_operations_page_links_to_the_guide(client):
    html = client.get("/operations").get_data(as_text=True)
    assert 'href="/guide"' in html
    assert "Как смотреть" in html


def test_operation_card_links_to_the_guide():
    html = sar_server.OPERATION_CARD_HTML.format(viewer_name="волонтёр")
    assert 'href="/guide"' in html


def test_player_shows_the_reminder():
    """Памятка стоит НАД видео: методика имеет значение именно в момент
    просмотра, а не когда-то раньше."""
    html = sar_server.PLAYER_PAGE_HTML
    assert 'class="howto"' in html
    assert 'href="/guide"' in html


@pytest.mark.parametrize("point", [
    "0.5",            # темп
    "9 сектор",       # сетка
    "20–30 мин",      # смены
    "не снег и не камень",   # главная установка про цвет
])
def test_reminder_carries_the_key_rules(point):
    """Памятка бесполезна, если в ней общие слова. Четыре правила, которые
    меняют результат просмотра, названы прямо."""
    assert point in sar_server.PLAYER_PAGE_HTML


def test_reminder_matches_the_guide():
    """Памятка не должна разойтись с самим гидом -- иначе человек получит
    два разных указания."""
    guide = sar_server.GUIDE_PAGE_HTML
    for point in ("0.5", "не снег и не камень"):
        assert point in guide, f"в гиде нет: {point}"


def test_guide_stays_open_without_login(client):
    """На гид ссылаются из бота и из постов -- он должен открываться до
    входа, иначе ссылка приведёт на форму пароля."""
    sar_server.app.testing = True
    anon = sar_server.app.test_client()
    assert anon.get("/guide").status_code == 200


def test_guide_link_opens_in_new_tab():
    """Человек не должен терять место в видео, открывая методику."""
    assert 'href="/guide" target="_blank"' in sar_server.PLAYER_PAGE_HTML
