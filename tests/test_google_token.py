"""Получение ключей Google, которые не протухают за час.

Access token у Google живёт РОВНО ЧАС, а подготовка 82 видео идёт около
четырёх. То есть без продления задача не может закончиться в принципе.
Продление в платформе есть, но ему нужны client_id, client_secret и
refresh_token -- их выдаёт только сам Google, и этот скрипт их получает.

Главное, что здесь проверяется, -- НЕМОЛЧАЛИВОСТЬ. Google охотно выдаёт
access token без refresh token, и такой обмен выглядит успешным: ключи
записались, диск заработал. А через час всё встаёт ровно так же, как
раньше, и человек второй раз проходит тот же путь, не понимая почему.
"""
import json
import io
import urllib.error
import urllib.parse

import pytest

import get_google_token as g
import sar_common


class Resp(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def token_answer(**over):
    d = {"access_token": "ya29.new", "refresh_token": "1//refresh",
         "expires_in": 3599}
    d.update(over)
    return json.dumps(d).encode()


# --- адрес согласия -------------------------------------------------------

def parsed(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def test_offline_access_is_requested():
    """Без access_type=offline refresh_token не выдаётся ВОВСЕ."""
    q = parsed(g.auth_url("cid", "http://127.0.0.1:1", "st"))
    assert q["access_type"] == ["offline"]


def test_consent_is_forced():
    """Без prompt=consent Google не выдаёт refresh_token повторно, если
    согласие уже давалось. Скрипт отработает «успешно» и не принесёт
    главного -- худший вид отказа."""
    q = parsed(g.auth_url("cid", "http://127.0.0.1:1", "st"))
    assert q["prompt"] == ["consent"]


def test_only_read_access_is_asked():
    """Платформа в облаке ничего не меняет. Просить больше прав, чем
    нужно, -- однажды ими случайно воспользоваться."""
    q = parsed(g.auth_url("cid", "http://127.0.0.1:1", "st"))
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
    assert "drive.file" not in q["scope"][0]
    assert q["scope"][0].endswith("readonly")


def test_redirect_and_state_are_passed():
    q = parsed(g.auth_url("cid", "http://127.0.0.1:5555", "st-123"))
    assert q["redirect_uri"] == ["http://127.0.0.1:5555"]
    assert q["state"] == ["st-123"]
    assert q["client_id"] == ["cid"]


def test_loopback_port_is_free():
    p = g.free_port()
    assert 1024 < p < 65536


# --- обмен кода на токены -------------------------------------------------

def test_exchange_returns_both_tokens(monkeypatch):
    monkeypatch.setattr(g.urllib.request, "urlopen",
                        lambda *a, **k: Resp(token_answer()))
    tok, refresh, exp = g.exchange("cid", "sec", "code", "http://127.0.0.1:1")
    assert tok == "ya29.new"
    assert refresh == "1//refresh"
    assert exp


def test_missing_refresh_token_is_loud(monkeypatch):
    """САМОЕ ВАЖНОЕ. Ответ без refresh_token выглядит успешным: токен
    есть, диск заработал. А через час всё встаёт, и причина не видна."""
    body = json.dumps({"access_token": "ya29.new", "expires_in": 3599}).encode()
    monkeypatch.setattr(g.urllib.request, "urlopen",
                        lambda *a, **k: Resp(body))
    with pytest.raises(RuntimeError) as e:
        g.exchange("cid", "sec", "code", "http://127.0.0.1:1")
    assert "refresh_token" in str(e.value)


def test_missing_refresh_token_says_how_to_fix(monkeypatch):
    """Сказать «не вышло» и не сказать что делать -- половина дела."""
    body = json.dumps({"access_token": "t"}).encode()
    monkeypatch.setattr(g.urllib.request, "urlopen",
                        lambda *a, **k: Resp(body))
    with pytest.raises(RuntimeError) as e:
        g.exchange("cid", "sec", "code", "http://127.0.0.1:1")
    assert "permissions" in str(e.value)


def test_rejected_exchange_is_explained(monkeypatch):
    err = urllib.error.HTTPError("u", 400, "bad", {},
                                 io.BytesIO(b'{"error":"invalid_grant"}'))
    monkeypatch.setattr(
        g.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(err))
    with pytest.raises(RuntimeError) as e:
        g.exchange("cid", "sec", "code", "http://127.0.0.1:1")
    assert "400" in str(e.value)


def test_exchange_sends_the_authorization_code_grant(monkeypatch):
    seen = {}

    def cap(req, *a, **k):
        seen["body"] = req.data.decode()
        return Resp(token_answer())

    monkeypatch.setattr(g.urllib.request, "urlopen", cap)
    g.exchange("cid", "sec", "the-code", "http://127.0.0.1:7")
    assert "grant_type=authorization_code" in seen["body"]
    assert "code=the-code" in seen["body"]
    assert "client_secret=sec" in seen["body"]


# --- запись в базу --------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


def test_keys_land_on_the_existing_account(conn):
    acc = sar_common.add_cloud_account(conn, provider="google", token="old")
    g.save(conn, "cid", "sec", "ya29.new", "1//r", "2026-01-01T00:00:00")
    row = sar_common.cloud_accounts(conn)[0]
    assert row["id"] == acc
    assert row["client_id"] == "cid"
    assert row["client_secret"] == "sec"
    assert row["refresh_token"] == "1//r"
    assert row["token"] == "ya29.new"


def test_saving_makes_renewal_possible(conn):
    """Смысл всей операции ровно в этом признаке."""
    sar_common.add_cloud_account(conn, provider="google", token="old")
    assert sar_common.cloud_accounts_public(conn)[0]["can_refresh"] is False
    g.save(conn, "cid", "sec", "t", "r", "2026-01-01T00:00:00")
    assert sar_common.cloud_accounts_public(conn)[0]["can_refresh"] is True


def test_previous_error_is_cleared(conn):
    """Иначе в админке навсегда остаётся «доступ истёк» уже после того,
    как всё починилось."""
    acc = sar_common.add_cloud_account(conn, provider="google", token="old")
    sar_common.update_cloud_account(conn, acc, last_error="доступ истёк")
    g.save(conn, "cid", "sec", "t", "r", "2026-01-01T00:00:00")
    assert not sar_common.cloud_accounts_public(conn)[0]["last_error"]


def test_no_account_says_to_connect_first(conn):
    with pytest.raises(RuntimeError) as e:
        g.save(conn, "cid", "sec", "t", "r", "x")
    assert "/admin" in str(e.value)


def test_several_accounts_refuse_to_guess(conn):
    """Записать ключи не в то подключение -- значит чинить одно, а
    ломаться будет другое."""
    sar_common.add_cloud_account(conn, provider="google", token="a")
    sar_common.add_cloud_account(conn, provider="google", token="b")
    with pytest.raises(RuntimeError) as e:
        g.save(conn, "cid", "sec", "t", "r", "x")
    assert "несколько" in str(e.value)


def test_yandex_account_is_not_touched(conn):
    """У Яндекса токен живёт год, ключи приложения ему не нужны."""
    sar_common.add_cloud_account(conn, provider="yandex", token="y")
    with pytest.raises(RuntimeError):
        g.save(conn, "cid", "sec", "t", "r", "x")


# --- возврат на 127.0.0.1 -------------------------------------------------

class FakeRequest:
    """Подделка запроса для Catcher: нас интересует разбор, а не сокеты."""

    def __init__(self, path):
        self.path = path
        self.sent = []
        self.wfile = io.BytesIO()

    def makefile(self, *a, **k):
        return io.BytesIO(b"")


def run_catcher(path, state):
    h = g.Catcher.__new__(g.Catcher)
    h.path = path
    h.wfile = io.BytesIO()
    h.sent = []
    g.Catcher.result = {}
    g.Catcher.expected_state = state
    h.send_response = lambda c: h.sent.append(c)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.do_GET()
    return h.sent[0], dict(g.Catcher.result)


def test_code_is_caught():
    status, res = run_catcher("/?code=abc&state=st", "st")
    assert status == 200
    assert res["code"] == "abc"


def test_foreign_state_is_refused():
    """На этот порт может постучаться кто угодно на машине. Без сверки
    state чужой код был бы принят как свой."""
    status, res = run_catcher("/?code=evil&state=other", "st")
    assert status == 400
    assert res == {}


def test_denied_consent_is_recorded():
    status, res = run_catcher("/?error=access_denied&state=st", "st")
    assert status == 400
    assert res["error"] == "access_denied"


def test_answer_without_code_is_not_success():
    status, res = run_catcher("/?state=st", "st")
    assert status == 400
    assert "error" in res


def test_waiting_has_a_deadline(monkeypatch):
    """Внешнее ожидание без предела -- это уже случалось в проекте: сторож
    туннеля повис на восемь суток на команде без таймаута."""
    import inspect
    src = inspect.getsource(g.wait_for_code)
    assert "timeout" in src and "deadline" in src
