"""Тесты эндпоинтов /healthz и /metrics.

Оба открыты БЕЗ пароля -- иначе их не сможет опрашивать внешний монитор, у
которого пароля от платформы нет и быть не должно. Из этого следует главное
требование, которое и проверяется здесь жёстче всего: они не должны отдавать
ничего чувствительного. Ни имён волонтёров, ни телеграм-ников, ни ключей
доступа, ни координат находок, ни имён файлов операции -- только счётчики.

Второе требование -- к коду ответа. 503 обязан означать "всё встало", а не
"есть на что посмотреть": внешние пингеры будят людей именно по нему, и
если 503 начнёт приходить из-за диска на восьми гигабайтах, на него
перестанут реагировать ровно к тому моменту, когда он станет настоящим.
"""
import json

import pytest

import sar_common
import sar_health
import sar_server


@pytest.fixture
def client(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"}, raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_common, "resolve_paths",
                        lambda w: (str(watch), str(watch), db, str(watch)))
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    return sar_server.app.test_client()


def test_healthz_open_without_login(client):
    r = client.get("/healthz")
    assert r.status_code in (200, 503)
    assert r.get_json()["status"] in ("ok", "warn", "crit")


def test_metrics_open_without_login(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "sar_up 1" in r.get_data(as_text=True)


def test_metrics_content_type_is_prometheus(client):
    r = client.get("/metrics")
    assert r.mimetype == "text/plain"


def test_protected_pages_still_require_login(client):
    """Контроль: открыли точечно, а не сняли защиту со всего сайта."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


# --- утечки ---------------------------------------------------------------

def _seed_sensitive(db):
    c = sar_common.get_db_connection(db)
    c.execute("INSERT INTO telegram_access_requests "
              "(chat_id,username,first_name,status,requested_at,access_token,role) "
              "VALUES (7,'секретный_ник','Айгуль','approved','2026-01-01','ТОКЕН-НЕ-СВЕТИТЬ','admin')")
    c.execute("INSERT INTO reports (report_id,rel_path,abs_path,kind,status,created_at,updated_at) "
              "VALUES ('rid','СЕКРЕТНОЕ_ВИДЕО.MP4','/x/СЕКРЕТНОЕ_ВИДЕО.MP4','video','done','a','a')")
    c.execute("INSERT INTO manual_observations "
              "(report_id,viewer_name,timestamp_sec,bbox,label,created_at,lat,lon) "
              "VALUES ('rid','Айгуль',1.0,'[0,0,1,1]','рюкзак','a',39.4831,73.5854)")
    c.commit()
    c.close()


def test_healthz_leaks_no_personal_or_operational_data(client, tmp_path):
    _seed_sensitive(str(tmp_path / "sar_data.db"))
    body = client.get("/healthz").get_data(as_text=True)
    for secret in ("секретный_ник", "Айгуль", "ТОКЕН-НЕ-СВЕТИТЬ",
                    "СЕКРЕТНОЕ_ВИДЕО", "39.4831", "73.5854", "рюкзак"):
        assert secret not in body, f"утечка в /healthz: {secret}"


def test_metrics_leaks_no_personal_or_operational_data(client, tmp_path):
    _seed_sensitive(str(tmp_path / "sar_data.db"))
    body = client.get("/metrics").get_data(as_text=True)
    for secret in ("секретный_ник", "Айгуль", "ТОКЕН-НЕ-СВЕТИТЬ",
                    "СЕКРЕТНОЕ_ВИДЕО", "39.4831", "73.5854", "рюкзак"):
        assert secret not in body, f"утечка в /metrics: {secret}"


def test_counts_are_still_reported(client, tmp_path):
    """Обратная сторона: спрятав лишнее, не спрятать полезное."""
    _seed_sensitive(str(tmp_path / "sar_data.db"))
    body = client.get("/metrics").get_data(as_text=True)
    assert "sar_manual_observations_total 1" in body
    assert "sar_people_approved 1" in body


# --- коды ответа ----------------------------------------------------------

def test_warning_returns_200_not_503(client, monkeypatch):
    """Предупреждение -- повод заняться, а не будить дежурного."""
    monkeypatch.setattr(sar_health, "evaluate",
                        lambda facts: {"disk": (sar_health.WARN, "мало места")})
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["status"] == "warn"


def test_critical_returns_503(client, monkeypatch):
    monkeypatch.setattr(sar_health, "evaluate",
                        lambda facts: {"worker": (sar_health.CRIT, "воркер молчит")})
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.get_json()["status"] == "crit"


def test_healthz_explains_what_is_wrong(client, monkeypatch):
    """Монитор показывает текст человеку -- он должен быть внятным."""
    monkeypatch.setattr(sar_health, "evaluate",
                        lambda facts: {"worker": (sar_health.CRIT, "воркер молчит 30 мин")})
    checks = client.get("/healthz").get_json()["checks"]
    assert checks["worker"]["text"] == "воркер молчит 30 мин"
