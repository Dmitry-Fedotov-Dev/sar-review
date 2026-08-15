"""Регрессионный тест: /guide должен открываться БЕЗ входа (без пароля) --
это гид волонтёра, к нему нужно попадать ДО того, как у человека вообще
появится пароль (переход из Telegram-бота или из поста). Раньше жил как
приватная claude.ai artifact-страница -- пришлось перенести на сам сервер,
см. sar_server.py GUIDE_PAGE_HTML и require_login().

/about (презентация для МЧС) временно снята с публикации по решению
пользователя ("не пойдёт для показа, потом поправим") -- контент оставлен
в ABOUT_PAGE_HTML для будущей доработки, но маршрут отдаёт 404."""
import sar_server


def _anon_client(app, monkeypatch):
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": "."}, raising=False)
    app.secret_key = "test-secret"
    app.testing = True
    return app.test_client()  # НЕ логинимся -- проверяем именно анонимный доступ


def test_guide_page_accessible_without_login(monkeypatch):
    client = _anon_client(sar_server.app, monkeypatch)
    resp = client.get("/guide")
    assert resp.status_code == 200
    assert "Полевой гид SAR Review" in resp.get_data(as_text=True)


def test_about_page_currently_disabled(monkeypatch):
    # /about больше не в open_paths -- анонимный запрос ведёт себя как для
    # любой другой защищённой страницы (редирект на /login), а не как
    # раньше (200 без входа вообще)
    client = _anon_client(sar_server.app, monkeypatch)
    resp = client.get("/about", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_about_page_404_even_when_logged_in(monkeypatch):
    client = _anon_client(sar_server.app, monkeypatch)
    with client.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "tester"
    resp = client.get("/about")
    assert resp.status_code == 404


def test_protected_page_still_requires_login(monkeypatch):
    # контроль: убеждаемся, что open_paths расширили точечно, а не сломали
    # защиту остального сайта заодно
    client = _anon_client(sar_server.app, monkeypatch)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_guide_page_has_critical_coordinates_warning():
    html = sar_server.GUIDE_PAGE_HTML
    assert "«Вероятные координаты» — это расчёт, а не измерение" in html


def test_about_page_is_self_contained_no_external_requests():
    # CSP/офлайн-устойчивость: страница не должна тянуть шрифты/скрипты
    # с внешних хостов -- иначе не откроется без интернета у клиента
    html = sar_server.ABOUT_PAGE_HTML
    assert "https://fonts" not in html
    assert "<script src=" not in html
