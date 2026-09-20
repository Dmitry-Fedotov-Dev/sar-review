"""Продление доступа к Google.

У Google access token живёт РОВНО ЧАС. Это устройство протокола, а не сбой:
предполагается, что приложение получило вместе с ним refresh token и меняет
протухший на свежий само.

Без продления диск приходится подключать заново каждый час. На боевых
данных подготовка 150 файлов занимает около трёх часов -- то есть без
продления она не может завершиться в принципе, сколько ни начинай. Это и
случилось при первом живом включении: токен выписан в 18:12, последний
успешный запрос в 19:05, дальше 401 на всё подряд.
"""
import io
import json
import time
import urllib.error

import pytest

import sar_cloud
import sar_common


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def auth_error(body=b'{"error":"invalid_grant"}'):
    return urllib.error.HTTPError("http://x", 401, "err", {}, io.BytesIO(body))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(sar_cloud, "_sleep", lambda s: None)


# --- сам обмен ------------------------------------------------------------

def test_refresh_returns_token_and_expiry(monkeypatch):
    payload = {"access_token": "ya29.новый", "expires_in": 3599}
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(json.dumps(payload).encode()))
    token, exp = sar_cloud.refresh_google_token("cid", "secret", "rt")
    assert token == "ya29.новый"
    assert 3500 < exp - time.time() < 3700


def test_refresh_sends_the_right_grant(monkeypatch):
    seen = {}

    def capture(req, **k):
        seen["body"] = req.data.decode()
        return FakeResponse(json.dumps({"access_token": "t"}).encode())

    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", capture)
    sar_cloud.refresh_google_token("cid", "secret", "rt")
    assert "grant_type=refresh_token" in seen["body"]
    assert "refresh_token=rt" in seen["body"]


def test_revoked_access_is_explained_not_just_refused(monkeypatch):
    """Отозванный доступ выглядит как истёкший, но чинится иначе:
    продлевать нечего, человеку надо переподключить диск."""
    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(auth_error()))
    with pytest.raises(sar_cloud.AuthExpired) as e:
        sar_cloud.refresh_google_token("cid", "secret", "rt")
    assert "Подключите диск заново" in str(e.value)


def test_missing_token_in_answer_is_an_error(monkeypatch):
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"{}"))
    with pytest.raises(sar_cloud.CloudError):
        sar_cloud.refresh_google_token("cid", "secret", "rt")


# --- провайдер продлевает сам ---------------------------------------------

def test_expired_listing_is_retried_after_renewal(monkeypatch):
    """Главное свойство: отказ по истёкшему токену не доходит до человека,
    если продлить доступ возможно."""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise auth_error()
        return FakeResponse(json.dumps({"files": []}).encode())

    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", flaky)
    renewed = []
    prov = sar_cloud.GoogleDrive(
        "ya29.old",
        renew=lambda: ("ya29.new", time.time() + 3600),
        on_renew=lambda t, e: renewed.append(t))
    prov.list_folder("root")
    assert renewed == ["ya29.new"]
    assert prov.token == "ya29.new"


def test_renewal_is_attempted_only_once(monkeypatch):
    """Если продление не помогает (доступ отозван), повторять бесконечно
    нельзя -- получится цикл обращений к Google вместо честной ошибки."""
    calls = {"n": 0}

    def always_401(*a, **k):
        calls["n"] += 1
        raise auth_error()

    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", always_401)
    prov = sar_cloud.GoogleDrive("ya29.old",
                                  renew=lambda: ("ya29.new", time.time() + 3600))
    with pytest.raises(sar_cloud.AuthExpired):
        prov.list_folder("root")
    assert calls["n"] <= sar_cloud.MAX_RETRIES * 2 + 1


def test_without_renewal_the_error_reaches_the_caller(monkeypatch):
    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(auth_error()))
    with pytest.raises(sar_cloud.AuthExpired):
        sar_cloud.GoogleDrive("ya29.old").list_folder("root")


def test_download_also_renews(monkeypatch, tmp_path):
    """Трёхгигабайтное видео качается дольше часа -- токен может умереть
    ПРЯМО В ПРОЦЕССЕ. Без продления такой файл не скачается никогда."""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise auth_error()
        return FakeResponse(b"x" * 100)

    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", flaky)
    prov = sar_cloud.GoogleDrive(
        "ya29.old", renew=lambda: ("ya29.new", time.time() + 3600))
    dst = str(tmp_path / "v.mp4")
    assert prov.download("id", dst, expected_size=100) == 100


# --- сохранение и единая точка сборки -------------------------------------

@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


def test_new_token_is_saved(conn, monkeypatch):
    """Не сохранив, мы продлевали бы доступ на каждом проходе заново --
    то есть жгли бы лимиты Google на ровном месте."""
    acc_id = sar_common.add_cloud_account(
        conn, provider="google", token="ya29.old", client_id="cid",
        client_secret="sec", refresh_token="rt")
    acc = sar_common.cloud_accounts(conn)[0]

    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse(
            json.dumps({"access_token": "ya29.new", "expires_in": 3600}).encode()))
    prov = sar_common.provider_for_account(conn, dict(acc))
    prov.renew_access()

    saved = sar_common.cloud_accounts(conn)[0]
    assert saved["token"] == "ya29.new"
    assert saved["expires_at"]


def test_renewal_clears_the_previous_error(conn, monkeypatch):
    """Иначе в админке навсегда остаётся «доступ отклонён» уже после
    того, как всё починилось."""
    acc_id = sar_common.add_cloud_account(
        conn, provider="google", token="ya29.old", client_id="cid",
        client_secret="sec", refresh_token="rt")
    sar_common.update_cloud_account(conn, acc_id, last_error="код 401")
    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse(
            json.dumps({"access_token": "ya29.new"}).encode()))
    prov = sar_common.provider_for_account(
        conn, dict(sar_common.cloud_accounts(conn)[0]))
    prov.renew_access()
    assert not sar_common.cloud_accounts_public(conn)[0]["last_error"]


def test_account_without_keys_says_what_is_missing(conn):
    """«Доступ отклонён» выглядит одинаково и когда продлить нечем, и
    когда диск просто не подключен. Различать обязан текст ошибки."""
    sar_common.add_cloud_account(conn, provider="google", token="ya29.t")
    prov = sar_common.provider_for_account(
        conn, dict(sar_common.cloud_accounts(conn)[0]))
    with pytest.raises(sar_cloud.AuthExpired) as e:
        prov.renew_access()
    assert "нет ключей приложения" in str(e.value)


def test_expiring_token_is_renewed_before_the_request(conn, monkeypatch):
    """Обращение с заведомо мёртвым токеном -- лишний запрос к Google и
    лишняя запись об ошибке, которую потом видит человек и пугается."""
    from datetime import datetime, timedelta
    acc_id = sar_common.add_cloud_account(
        conn, provider="google", token="ya29.old", client_id="cid",
        client_secret="sec", refresh_token="rt",
        expires_at=(datetime.now() - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse(
            json.dumps({"access_token": "ya29.fresh"}).encode()))
    prov = sar_common.provider_for_account(
        conn, dict(sar_common.cloud_accounts(conn)[0]))
    assert prov.token == "ya29.fresh", "продление не сработало заранее"


def test_fresh_token_is_not_renewed_needlessly(conn, monkeypatch):
    from datetime import datetime, timedelta
    sar_common.add_cloud_account(
        conn, provider="google", token="ya29.ok", client_id="cid",
        client_secret="sec", refresh_token="rt",
        expires_at=(datetime.now() + timedelta(minutes=50)).isoformat())
    calls = []
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or FakeResponse(b"{}"))
    prov = sar_common.provider_for_account(
        conn, dict(sar_common.cloud_accounts(conn)[0]))
    assert prov.token == "ya29.ok"
    assert calls == [], "продлили живой токен -- лишний запрос к Google"


# --- секреты --------------------------------------------------------------

def test_client_secret_never_leaves_the_server(conn):
    sar_common.add_cloud_account(conn, provider="google", token="ya29.t",
                                  client_id="cid", client_secret="ОЧЕНЬ-СЕКРЕТНО",
                                  refresh_token="rt")
    pub = sar_common.cloud_accounts_public(conn)[0]
    assert "client_secret" not in pub
    assert "refresh_token" not in pub
    assert "ОЧЕНЬ-СЕКРЕТНО" not in json_dumps(pub)


def json_dumps(o):
    import json as _j
    return _j.dumps(o, ensure_ascii=False, default=str)


def test_interface_knows_whether_renewal_is_possible(conn):
    """Главное, что надо знать про подключение: переживёт ли оно час."""
    sar_common.add_cloud_account(conn, provider="google", token="ya29.t")
    assert sar_common.cloud_accounts_public(conn)[0]["can_refresh"] is False
    sar_common.add_cloud_account(conn, provider="google", token="ya29.t2",
                                  client_id="cid", client_secret="s",
                                  refresh_token="rt")
    assert sar_common.cloud_accounts_public(conn)[1]["can_refresh"] is True


# --- страж ----------------------------------------------------------------

def test_everyone_builds_the_provider_through_one_place():
    """Собранный напрямую провайдер будет без продления и откажет через
    час -- причём молча, потому что отказ выглядит так же, как отсутствие
    подключения."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("sar_worker.py",):
        body = (root / name).read_text(encoding="utf-8")
        assert "sar_cloud.make_provider" not in body, (
            f"{name} собирает провайдера мимо provider_for_account")
