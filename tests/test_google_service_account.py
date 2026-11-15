"""Google Диск через сервисный аккаунт: подпись, ключ, подключение папки по ссылке.

Сторожим:
- RS256 на стандартной библиотеке совпадает с openssl байт в байт и
  проверяется его открытым ключом -- иначе Google молча отвергал бы все
  запросы;
- ключ не уходит наружу: API отдаёт только адрес аккаунта;
- папка, не расшаренная на аккаунт, НЕ подключается «пустой»: Google отдаёт
  её список пустым без ошибки, поэтому проверяется сама папка (404), и
  человек получает адрес, на который её расшарить;
- подключение привязано к операции, продлевается само (auth='service'),
  дубль той же папки -- отказ;
- права: только администратор.

Ключи создаются на лету через openssl, в репозитории закрытых ключей нет.
"""
import base64
import json
import shutil
import subprocess
import urllib.error

import pytest

import sar_cloud
import sar_common
import sar_server

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None,
                                 reason="нужен openssl для создания тестового ключа")


@pytest.fixture(scope="module")
def rsa_key(tmp_path_factory):
    d = tmp_path_factory.mktemp("rsa")
    key = d / "k.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", str(key)], check=True, capture_output=True)
    pub = d / "pub.pem"
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    return {"pem": key.read_text(), "key": key, "pub": pub, "dir": d}


def _sa_json(pem, email="sar@proj.iam.gserviceaccount.com"):
    return json.dumps({"type": "service_account", "project_id": "proj",
                       "private_key_id": "kid1", "private_key": pem,
                       "client_email": email,
                       "token_uri": "https://oauth2.googleapis.com/token"})


# --- подпись -------------------------------------------------------------------

def test_signature_matches_openssl_byte_for_byte(rsa_key):
    msg = b"header.claims"
    (rsa_key["dir"] / "m").write_bytes(msg)
    out = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(rsa_key["key"])],
                         input=msg, capture_output=True, check=True).stdout
    assert sar_cloud.rs256_sign(rsa_key["pem"], msg) == out


def test_jwt_verifies_with_public_key(rsa_key):
    sa = sar_cloud.parse_service_account(_sa_json(rsa_key["pem"]))
    jwt = sar_cloud.service_account_assertion(sa, now=1_700_000_000)
    h, c, sig = jwt.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    header = json.loads(base64.urlsafe_b64decode(pad(h)))
    claims = json.loads(base64.urlsafe_b64decode(pad(c)))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid1"}
    assert claims["iss"] == "sar@proj.iam.gserviceaccount.com"
    assert claims["scope"] == sar_cloud.GOOGLE_DRIVE_READONLY, "только чтение"
    assert claims["exp"] - claims["iat"] == 3600
    sigf = rsa_key["dir"] / "s"
    sigf.write_bytes(base64.urlsafe_b64decode(pad(sig)))
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(rsa_key["pub"]),
                        "-signature", str(sigf)], input=f"{h}.{c}".encode(), capture_output=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_key_with_escaped_newlines_is_understood(rsa_key):
    escaped = rsa_key["pem"].replace("\n", "\\n")
    assert sar_cloud.rs256_sign(escaped, b"x") == sar_cloud.rs256_sign(rsa_key["pem"], b"x")


@pytest.mark.parametrize("text,needle", [
    ("не json", "не JSON"),
    (json.dumps({"type": "authorized_user"}), "не ключ сервисного"),
    (json.dumps({"type": "service_account", "private_key": "x"}), "client_email"),
    (json.dumps({"type": "service_account", "client_email": "a@b",
                 "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----"}),
     "private_key"),
])
def test_wrong_files_are_refused_with_reason(text, needle):
    with pytest.raises(sar_cloud.CloudError, match=needle):
        sar_cloud.parse_service_account(text)


def test_token_exchange_uses_jwt_bearer_grant(rsa_key, monkeypatch):
    seen = {}

    def fake(url, headers=None, data=None, method=None):
        seen.update(url=url, data=data.decode())
        return {"access_token": "ya29.tok", "expires_in": 3599}
    monkeypatch.setattr(sar_cloud, "http_json", fake)
    tok, exp = sar_cloud.service_account_token(sar_cloud.parse_service_account(_sa_json(rsa_key["pem"])))
    assert tok == "ya29.tok"
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in seen["data"]
    assert "assertion=" in seen["data"]


def test_404_becomes_not_found(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", boom)
    with pytest.raises(sar_cloud.NotFound):
        sar_cloud.http_json("https://www.googleapis.com/drive/v3/files/x")


def test_listing_asks_for_shared_drives(monkeypatch):
    urls = []
    monkeypatch.setattr(sar_cloud, "http_json", lambda url, headers=None: urls.append(url) or {"files": []})
    sar_cloud.make_provider("google", "tok").list_folder("F")
    assert "supportsAllDrives=true" in urls[0] and "includeItemsFromAllDrives=true" in urls[0]


# --- хранение и продление -----------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "d.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


def test_key_is_stored_but_never_public(rsa_key, conn):
    pub = sar_common.set_service_account(conn, _sa_json(rsa_key["pem"]), who="админ")
    assert pub["email"] == "sar@proj.iam.gserviceaccount.com"
    assert "BEGIN" not in json.dumps(pub)
    assert sar_common.service_account(conn)["private_key"].startswith("-----BEGIN")
    sar_common.drop_service_account(conn)
    assert sar_common.service_account(conn) is None and sar_common.service_account_public(conn) is None


def test_service_connection_renews_itself(rsa_key, conn, monkeypatch):
    sar_common.set_service_account(conn, _sa_json(rsa_key["pem"]))
    acc_id = sar_common.add_cloud_account(conn, provider="google", token="old", root_id="F",
                                          auth="service", expires_at="2000-01-01T00:00:00")
    monkeypatch.setattr(sar_cloud, "service_account_token", lambda sa: ("fresh", 9_999_999_999))
    acc = [a for a in sar_common.cloud_accounts(conn) if a["id"] == acc_id][0]
    prov = sar_common.provider_for_account(conn, acc)
    assert prov.token == "fresh", "просроченный токен продлевается заранее"
    assert [a for a in sar_common.cloud_accounts(conn) if a["id"] == acc_id][0]["token"] == "fresh"
    pub = [a for a in sar_common.cloud_accounts_public(conn) if a["id"] == acc_id][0]
    assert pub["can_refresh"] is True and "token" not in pub


def test_renew_without_key_explains(conn, monkeypatch):
    acc_id = sar_common.add_cloud_account(conn, provider="google", token="old", root_id="F",
                                          auth="service")
    acc = [a for a in sar_common.cloud_accounts(conn) if a["id"] == acc_id][0]
    prov = sar_common.provider_for_account(conn, acc)
    with pytest.raises(sar_cloud.AuthExpired, match="ключ сервисного аккаунта"):
        prov.renew_access()


# --- API ----------------------------------------------------------------------

@pytest.fixture
def web(tmp_path, monkeypatch, rsa_key):
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "w.db")
    sar_common.init_db(db)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG", {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    monkeypatch.setattr(sar_cloud, "service_account_token", lambda sa: ("ya29.tok", 9_999_999_999))
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_common.get_db_connection(db)
    op, _ = sar_common.create_operation_with_folder(c, str(watch), "Курумды")
    c.close()
    return {"client": sar_server.app.test_client(), "db": db, "op": op, "pem": rsa_key["pem"]}


def _as(cl, role):
    with cl.session_transaction() as s:
        s.update(authed=True, verified=True, viewer_name="x", role=role)


class FakeDrive:
    """Google Диск в памяти: какие папки расшарены и что в них лежит."""
    def __init__(self, shared):
        self.shared = shared

    def __call__(self, name, token, renew=None, on_renew=None):
        drive = self

        class P:
            def folder_info(self, fid):
                if fid not in drive.shared:
                    raise sar_cloud.NotFound("404")
                return drive.shared[fid]["name"], drive.shared[fid].get("folder", True)

            def list_folder(self, fid):
                return drive.shared[fid]["items"]
        return P()


def test_upload_key_admin_only_and_public_answer(web):
    cl = web["client"]
    _as(cl, sar_common.ROLE_MODERATOR)
    assert cl.get("/api/admin/google-service-account").status_code == 403
    _as(cl, sar_common.ROLE_ADMIN)
    import io
    r = cl.post("/api/admin/google-service-account", content_type="multipart/form-data",
                data={"key": (io.BytesIO(_sa_json(web["pem"]).encode()), "key.json")})
    body = r.get_json()
    assert body["ok"] and body["account"]["email"].endswith("gserviceaccount.com")
    assert "BEGIN" not in r.get_data(as_text=True)
    assert "BEGIN" not in cl.get("/api/admin/google-service-account").get_data(as_text=True)


def test_gdrive_needs_key_first(web):
    cl = web["client"]
    _as(cl, sar_common.ROLE_ADMIN)
    r = cl.post(f"/api/operations/{web['op']}/gdrive",
                json={"url": "https://drive.google.com/drive/folders/F1"})
    assert r.status_code == 400 and r.get_json()["need_key"] is True


def test_gdrive_unshared_folder_tells_whom_to_share_with(web, monkeypatch):
    cl = web["client"]
    c = sar_common.get_db_connection(web["db"])
    sar_common.set_service_account(c, _sa_json(web["pem"]))
    monkeypatch.setattr(sar_cloud, "make_provider", FakeDrive({}))
    _as(cl, sar_common.ROLE_ADMIN)
    r = cl.post(f"/api/operations/{web['op']}/gdrive",
                json={"url": "https://drive.google.com/drive/folders/F1"}).get_json()
    assert not r["ok"] and "sar@proj.iam.gserviceaccount.com" in r["error"]
    assert c.execute("SELECT COUNT(*) n FROM cloud_accounts").fetchone()["n"] == 0


def test_gdrive_connects_to_operation_and_refuses_duplicate(web, monkeypatch):
    cl = web["client"]
    c = sar_common.get_db_connection(web["db"])
    sar_common.set_service_account(c, _sa_json(web["pem"]))
    items = [sar_cloud.FileInfo("a", "DJI_1.MP4"), sar_cloud.FileInfo("b", "день 2", is_folder=True)]
    monkeypatch.setattr(sar_cloud, "make_provider",
                        FakeDrive({"F1": {"name": "Курумды дрон", "items": items},
                                   "FILE": {"name": "a.mp4", "folder": False, "items": []}}))
    _as(cl, sar_common.ROLE_ADMIN)
    r = cl.post(f"/api/operations/{web['op']}/gdrive",
                json={"url": "https://drive.google.com/drive/folders/F1?usp=sharing"}).get_json()
    assert r["ok"] and r["label"] == "Курумды дрон" and r["found"] == 2 and r["folders"] == 1
    acc = sar_common.cloud_accounts(c)[0]
    assert (acc["auth"], acc["root_id"], acc["operation_id"]) == ("service", "F1", web["op"])
    again = cl.post(f"/api/operations/{web['op']}/gdrive",
                    json={"url": "https://drive.google.com/drive/folders/F1"})
    assert again.status_code == 409
    notdir = cl.post(f"/api/operations/{web['op']}/gdrive",
                     json={"url": "https://drive.google.com/drive/folders/FILE"}).get_json()
    assert not notdir["ok"] and "папк" in notdir["error"]
    # корень облачной папки помечен в дереве операции -- для значка Google Диска
    cl2 = sar_server.app.test_client()
    _as(cl2, sar_common.ROLE_VIEWER)
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, created_at, "
              "updated_at, cloud_account_id, cloud_file_id) VALUES "
              "('r1', 'DJI_1.MP4', '', 'video', 'idle', 'x', 'x', ?, 'a')", (acc["id"],))
    sar_common.attach_material(c, web["op"], "r1")
    folders = cl2.get(f"/api/operations/{web['op']}/browse").get_json()["folders"]
    assert {"name": "Курумды дрон", "files": 1, "provider": "google"} in folders


def test_gdrive_admin_only(web):
    cl = web["client"]
    for role in (sar_common.ROLE_VIEWER, sar_common.ROLE_MODERATOR):
        _as(cl, role)
        assert cl.post(f"/api/operations/{web['op']}/gdrive", json={"url": "x"}).status_code == 403


@pytest.mark.parametrize("url", ["root", "   "])
def test_gdrive_refuses_whole_drive(web, monkeypatch, url):
    """Отказ ДО обращения к Google. Первая версия теста проходила по неверной
    причине: «root» уходил в сеть, а в песочнице сети нет -- на сервере с
    сетью подключился бы весь диск."""
    cl = web["client"]
    c = sar_common.get_db_connection(web["db"])
    sar_common.set_service_account(c, _sa_json(web["pem"]))

    def no_network(*a, **k):
        raise AssertionError("к Google обращаться не должны")
    monkeypatch.setattr(sar_cloud, "make_provider", no_network)
    _as(cl, sar_common.ROLE_ADMIN)
    r = cl.post(f"/api/operations/{web['op']}/gdrive", json={"url": url}).get_json()
    assert not r["ok"] and ("конкретную папку" in r["error"] or "ссылку" in r["error"])
