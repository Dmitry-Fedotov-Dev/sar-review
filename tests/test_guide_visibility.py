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


def test_reminder_is_not_inside_the_header_row():
    """Регрессия по вёрстке.

    Памятку сначала вложили в header-row -- флекс-строку с крошками и
    счётчиком онлайн. Она встала третьей колонкой между ними, и шапка
    разъехалась: крошки сжались в две строки, счётчик уехал к краю."""
    html = sar_server.PLAYER_PAGE_HTML
    head = html[html.index('<div class="header-row">'):html.index("<h1>")]
    assert 'class="howto"' not in head


def test_title_does_not_repeat_the_path():
    """Полный путь уже стоит в крошках строкой выше -- повторять его в
    заголовке значит занимать место дважды."""
    html = sar_server.PLAYER_PAGE_HTML
    assert "<h1>{short_name}</h1>" in html
    assert "<h1>Ручной просмотр — {name}</h1>" not in html


def test_crumbs_do_not_wrap():
    """Длинный путь ломал шапку на две строки."""
    assert "white-space:nowrap" in sar_server.PLAYER_PAGE_HTML


@pytest.mark.parametrize("point", [
    "0.5",              # темп
    "9 сектор",         # сетка
    "20–30 мин",        # смены
    "отмечать всегда",  # сомнительное фиксируем
])
def test_reminder_carries_the_key_rules(point):
    """Памятка бесполезна, если в ней общие слова. Четыре правила, которые
    меняют результат просмотра, названы прямо."""
    assert point in sar_server.PLAYER_PAGE_HTML


def test_reminder_avoids_rules_that_need_context():
    """Установка «ищем не снег и не камень» верна, но одной строкой без
    объяснения читается как бессмыслица -- в памятке ей не место, а в
    гиде она разобрана целым разделом."""
    assert "не снег и не камень" not in sar_server.PLAYER_PAGE_HTML
    assert "не снег и не камень" in sar_server.GUIDE_PAGE_HTML


def test_reminder_matches_the_guide():
    """Памятка не должна разойтись с самим гидом -- иначе человек получит
    два разных указания."""
    guide = sar_server.GUIDE_PAGE_HTML
    for point in ("0.5", "Сомнительное фиксируем всегда"):
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
