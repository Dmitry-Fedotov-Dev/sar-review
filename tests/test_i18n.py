"""Перевод интерфейса на английский: словарь на клиенте + внедрение скрипта.

Проверяется три вещи, каждая уже ломалась в этом проекте в том или ином виде:

1. Скрипт подключается ЕДИНОЙ точкой (after_request), а не правкой
   четырнадцати шаблонов. Ровно так однажды разъехался heartbeat присутствия:
   его вставляли копированием, он оказался на 3 страницах из 6, и счётчик
   «онлайн» занижал число работающих людей.
2. Внедрение не трогает не-HTML. JSON, картинки и видео должны уходить
   байт в байт.
3. Словарь не расходится с шаблонами. Ключ, которого больше нет в
   интерфейсе, — мёртвый груз; но, что важнее, тест ловит обратное:
   переименовали строку в шаблоне — перевод молча перестал применяться.
   Это был бы классический для проекта молчаливый отказ.
"""
import json
import os
import re

import sar_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DICT_PATH = os.path.join(ROOT, "static", "i18n.en.json")
JS_PATH = os.path.join(ROOT, "static", "i18n.js")


def _client(monkeypatch):
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": "."}, raising=False)
    sar_server.app.secret_key = "test-secret"
    sar_server.app.testing = True
    return sar_server.app.test_client()


# --- внедрение скрипта -------------------------------------------------

def test_script_injected_into_html_page(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/guide")          # публичная страница, вход не нужен
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '/static/i18n.js' in body, "скрипт перевода не попал в HTML-страницу"


def test_script_injected_exactly_once(monkeypatch):
    client = _client(monkeypatch)
    body = client.get("/guide").get_data(as_text=True)
    assert body.count('/static/i18n.js') == 1


def test_hook_leaves_json_untouched():
    """JSON-ответы обязаны уходить нетронутыми.

    Дописанный в JSON тег сломал бы разбор на клиенте -- и сломал бы его
    молча, потому что JS проглотил бы ошибку разбора в собственном catch.
    Хук проверяется напрямую: так тесту не нужны ни база, ни маршруты.
    """
    with sar_server.app.test_request_context("/"):
        body = '{"ok": true, "body": "</body>"}'
        resp = sar_server.app.response_class(body, mimetype="application/json")
        out = sar_server._inject_i18n(resp)
    assert out.get_data(as_text=True) == body


def test_hook_leaves_plain_text_untouched():
    """Формат Prometheus -- text/plain. Тег в нём сделал бы метрики
    неразбираемыми, а сломанный сбор метрик в этом проекте уже случался."""
    with sar_server.app.test_request_context("/"):
        body = "sar_up 1\n"
        resp = sar_server.app.response_class(body, mimetype="text/plain")
        out = sar_server._inject_i18n(resp)
    assert out.get_data(as_text=True) == body


def test_hook_injects_before_closing_body():
    with sar_server.app.test_request_context("/"):
        resp = sar_server.app.response_class(
            "<html><body><p>x</p></body></html>", mimetype="text/html")
        out = sar_server._inject_i18n(resp).get_data(as_text=True)
    assert out.count("/static/i18n.js") == 1
    assert out.index("/static/i18n.js") < out.index("</body>")


def test_hook_skips_html_without_body_tag():
    """Фрагменты HTML (их отдают некоторые API) закрывающего </body> не
    имеют -- дописывать туда нечего, и молча портить их нельзя."""
    with sar_server.app.test_request_context("/"):
        body = "<div>кусок разметки</div>"
        resp = sar_server.app.response_class(body, mimetype="text/html")
        out = sar_server._inject_i18n(resp)
    assert out.get_data(as_text=True) == body


def test_static_files_served(monkeypatch):
    client = _client(monkeypatch)
    for path in ("/static/i18n.js", "/static/i18n.en.json"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} не отдаётся"


# --- страж порядка декораторов ----------------------------------------

def test_require_login_still_registered():
    """Новый after_request не должен был увести чужой декоратор.

    Однажды вставка нового before_request МЕЖДУ `@app.before_request` и
    `def require_login()` увела декоратор на соседнюю функцию, а
    require_login осталась незарегистрированной -- платформа открылась
    без пароля. Тест держит именно это.
    """
    names = [f.__name__ for f in sar_server.app.before_request_funcs[None]]
    assert "require_login" in names


def test_login_still_required(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# --- словарь -----------------------------------------------------------

def test_dictionary_is_valid_and_not_empty():
    with open(DICT_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, dict)
    assert len(data) > 100
    for ru, en in data.items():
        assert ru.strip() == ru, f"ключ с лишними пробелами: {ru!r}"
        assert en.strip(), f"пустой перевод для {ru!r}"


def test_dictionary_keys_are_russian_and_values_are_not():
    """Ключ -- русский, значение -- нет. Перепутанная пара означала бы, что
    строка «переводится» сама в себя и человек этого не заметит."""
    with open(DICT_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    cyr = re.compile(r"[А-Яа-яЁё]")
    for ru, en in data.items():
        assert cyr.search(ru), f"ключ без кириллицы: {ru!r}"
        assert not cyr.search(en), f"перевод остался русским: {ru!r} -> {en!r}"


def test_dictionary_keys_still_present_in_templates():
    """Страж от расхождения словаря с интерфейсом.

    Если строку в шаблоне переименовали, перевод для неё просто перестанет
    применяться -- без ошибок и без следов. Порог мягкий: часть ключей
    появляется только в собираемых скриптом кусках и здесь не находится.
    """
    with open(os.path.join(ROOT, "sar_server.py"), encoding="utf-8") as fh:
        src = fh.read()
    with open(DICT_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    missing = [k for k in data if k not in src]
    assert len(missing) <= len(data) * 0.15, (
        "слишком много ключей словаря не встречается в sar_server.py -- "
        f"похоже, интерфейс переписали, а словарь нет: {missing[:10]}"
    )


# --- сам скрипт --------------------------------------------------------

def test_js_has_no_silent_catch():
    """Пустой catch в этом проекте запрещён по умолчанию."""
    with open(JS_PATH, encoding="utf-8") as fh:
        js = fh.read()
    assert "catch (e) {}" not in js
    assert "catch(e){}" not in js


def test_js_does_not_touch_value_attribute():
    """`value` переводить нельзя: это отправляемые данные, а не подпись.

    Перевод value у скрытого поля или у кнопки формы означал бы, что на
    сервер уедет другое значение -- и выяснилось бы это не сразу.
    """
    with open(JS_PATH, encoding="utf-8") as fh:
        js = fh.read()
    attrs = re.search(r"var ATTRS = \[(.*?)\]", js, re.S)
    assert attrs, "список переводимых атрибутов не найден"
    assert '"value"' not in attrs.group(1)


def test_js_is_local_only():
    """Платформа обязана работать в поле без интернета -- значит никаких
    внешних сервисов перевода."""
    with open(JS_PATH, encoding="utf-8") as fh:
        js = fh.read()
    for bad in ("translate.google", "googleapis", "http://", "https://"):
        assert bad not in js, f"внешняя ссылка в скрипте перевода: {bad}"
