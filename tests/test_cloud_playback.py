"""Просмотр материала, который лежит в облаке.

Смотрят ЛЁГКУЮ КОПИЮ, а не оригинал -- это было верно и раньше, просто с
локальным материалом оригинал всегда лежал рядом и разница не проявлялась.
С облаком оригинала на диске нет вовсе, и проверка «есть ли исходный файл»
начала не пускать в плеер материал, который к просмотру готов.

Второе: копию облачному файлу никто не делал. Обход папки ходит по диску,
облачных записей там нет -- значит материал оставался невоспроизводимым
навсегда, молча.

И третье, про расход: 150 файлов из подключённого хранилища -- это десятки
гигабайт трафика. Начинать такое само, без спроса, нельзя.
"""
import os

import pytest

import sar_common
import sar_server


@pytest.fixture
def env(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    conn = sar_common.get_db_connection(db)
    op = sar_common.create_operation(conn, "Курумды", folder="Курумды")
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, cloud_size, fps, created_at, updated_at) VALUES "
        "('c1','2026 08 14/Helicopter/Saykal/C0004.MP4','','video','idle',"
        "'f1',2684000000,30.0,datetime('now'),datetime('now'))")
    conn.commit()
    sar_common.attach_material(conn, op, "c1")
    conn.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", "/no/such/dir")
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "tester"
    return c, str(data), db


def make_proxy(data, rel="2026 08 14/Helicopter/Saykal/C0004.MP4"):
    p = sar_common.proxy_video_path(data, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"proxy")
    return p


# --- плеер ----------------------------------------------------------------

def test_player_opens_when_only_the_proxy_exists(env):
    """Ровно случай облачного материала: оригинала на диске нет, копия
    есть. Смотрят копию -- значит плеер обязан открыться."""
    client, data, _ = env
    make_proxy(data)
    r = client.get("/report/c1/player/")
    assert r.status_code == 200
    assert "не найден на диске" not in r.get_data(as_text=True)


def test_player_without_proxy_explains_instead_of_404(env):
    """404 на материале, который просто ещё не скачан, читается как
    поломка платформы."""
    client, _, _ = env
    r = client.get("/report/c1/player/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "ещё не готов" in body


def test_not_ready_page_says_how_much_will_be_downloaded(env):
    """Человек решает, тратить ли канал, -- значит должен знать цену."""
    client, _, _ = env
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "2.7 ГБ" in body or "2,7 ГБ" in body


def test_not_ready_page_explains_nothing_downloads_by_itself(env):
    client, _, _ = env
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "ничего не качается" in body.lower()


def test_not_ready_page_leads_back_to_the_operation(env):
    """Тупик без выхода -- отдельная категория жалоб в этом проекте."""
    client, _, _ = env
    assert "/operation/1/" in client.get("/report/c1/player/").get_data(as_text=True)


def test_missing_local_file_without_cloud_is_still_an_honest_404(env):
    """Файл просто пропал с диска, облака за ним нет -- это правда 404."""
    client, _, db = env
    conn = sar_common.get_db_connection(db)
    conn.execute("UPDATE reports SET cloud_file_id=NULL WHERE report_id='c1'")
    conn.commit()
    conn.close()
    assert client.get("/report/c1/player/").status_code == 404


# --- просьба подготовить --------------------------------------------------

def test_prepare_marks_the_material(env):
    client, _, db = env
    r = client.post("/api/report/c1/prepare")
    assert r.status_code == 200 and r.get_json()["ok"]
    conn = sar_common.get_db_connection(db)
    row = conn.execute(
        "SELECT proxy_requested FROM reports WHERE report_id='c1'").fetchone()
    conn.close()
    assert row["proxy_requested"] == 1


def test_prepare_does_not_download_in_the_web_layer(env):
    """Сервер по устройству проекта ничего не обрабатывает -- он только
    читает. Скачает воркер, со всеми ограничителями расхода."""
    import inspect
    src = inspect.getsource(sar_server.api_report_prepare)
    for forbidden in ("ensure_local", "download", "urlopen"):
        assert forbidden not in src, (
            f"веб-слой сам качает файл ({forbidden})")


def test_prepare_refuses_non_cloud_material(env):
    client, _, db = env
    conn = sar_common.get_db_connection(db)
    conn.execute("UPDATE reports SET cloud_file_id=NULL WHERE report_id='c1'")
    conn.commit()
    conn.close()
    assert client.post("/api/report/c1/prepare").status_code == 400


def test_prepare_on_unknown_material_is_404(env):
    client, _, _ = env
    assert client.post("/api/report/нет/prepare").status_code == 404


def test_worker_only_fetches_what_was_requested():
    """Страж. Без этого условия подключение хранилища означало бы скачать
    все 150 файлов -- десятки гигабайт -- без единого запроса от человека."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
           ).read_text(encoding="utf-8")
    i = src.index("def _ensure_proxy") if "def _ensure_proxy" in src \
        else src.index("crf = int(CFG.get")
    chunk = src[i:i + 3000]
    assert "proxy_requested=1" in chunk, (
        "воркер берёт облачные файлы без явной просьбы")


# --- крошки пути ----------------------------------------------------------

def test_breadcrumbs_show_the_folder_path(env):
    """Крошки обрывались на названии операции: человек видел файл, но не
    понимал, из какой он папки и как вернуться именно туда."""
    client, data, _ = env
    make_proxy(data)
    body = client.get("/report/c1/player/").get_data(as_text=True)
    for folder in ("2026 08 14", "Helicopter", "Saykal"):
        assert folder in body, f"в крошках нет папки {folder}"


def test_breadcrumb_folders_are_clickable(env):
    client, data, _ = env
    make_proxy(data)
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "/operation/1/?path=" in body


def test_breadcrumb_path_matches_the_tree(env):
    """Крошки, ведущие в папку, которой в дереве нет, хуже их отсутствия."""
    client, data, db = env
    make_proxy(data)
    conn = sar_common.get_db_connection(db)
    view = sar_common.browse_operation(
        conn, "/nowhere", 1, "Google Диск/2026 08 14/Helicopter/Saykal")
    conn.close()
    # без подключения облака корня нет -- путь строится от самого rel_path
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "Saykal" in body


def test_download_failures_back_off():
    """Протухший токен отказывает одинаково на каждой попытке. Повтор раз
    в 15 секунд жжёт лимиты облака и топит журнал -- ровно это и было
    видно при первой живой проверке."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
           ).read_text(encoding="utf-8")
    for anchor in ("def ensure_requested_photos", "def ensure_video_proxies"):
        i = src.index(anchor)
        chunk = src[i:i + 3200]
        assert '_may_try("fetch:' in chunk or '_may_try(key)' in chunk, (
            f"{anchor}: загрузка повторяется без паузы после отказа")


# --- состояние подготовки -------------------------------------------------
#
# Человек нажал «подготовить» и вернулся на страницу -- а там снова та же
# кнопка и ни слова о том, что просьба принята. Выглядит так, будто нажатие
# не сработало, и он жмёт снова.

def test_queued_material_says_so(env):
    client, _, db = env
    client.post("/api/report/c1/prepare")
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "В очереди" in body


def test_button_is_disabled_once_queued(env):
    """Иначе человек жмёт её повторно, не понимая, принято ли."""
    client, _, db = env
    client.post("/api/report/c1/prepare")
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "Уже в очереди" in body and "disabled" in body


def test_partial_download_shows_progress(env):
    """Скачивание 929 МБ на плохом канале идёт долго. Без процента человек
    не отличает «идёт» от «застряло»."""
    import sar_staging
    client, data, db = env
    st = sar_staging.Staging(sar_staging.staging_dir(data), cap_bytes=1)
    part = st.path_for("2026 08 14/Helicopter/Saykal/C0004.MP4") + ".part"
    os.makedirs(os.path.dirname(part), exist_ok=True)
    with open(part, "wb") as f:
        f.write(b"x" * 1_000_000)
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "Скачивается" in body


def test_connection_error_is_shown_on_the_page(env):
    """САМОЕ ВАЖНОЕ. Без этого страница говорит «в очереди» и молчит
    месяцами, когда на самом деле протух токен: человек ждёт, а платформа
    каждые 15 секунд получает отказ. Ровно это и вышло при первом живом
    включении."""
    client, _, db = env
    conn = sar_common.get_db_connection(db)
    acc = sar_common.add_cloud_account(conn, provider="google", token="ya29.t")
    sar_common.update_cloud_account(conn, acc,
                                     last_error="доступ к облаку отклонён (код 401)")
    conn.execute("UPDATE reports SET cloud_account_id=? WHERE report_id='c1'",
                 (acc,))
    conn.commit()
    conn.close()
    client.post("/api/report/c1/prepare")
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "401" in body
    assert "подготовка не идёт" in body


def test_blocked_page_points_at_the_settings(env):
    """Сказать «не работает» и не сказать куда идти -- половина дела."""
    client, _, db = env
    conn = sar_common.get_db_connection(db)
    acc = sar_common.add_cloud_account(conn, provider="google", token="ya29.t")
    sar_common.update_cloud_account(conn, acc, last_error="отказ")
    conn.execute("UPDATE reports SET cloud_account_id=? WHERE report_id='c1'",
                 (acc,))
    conn.commit()
    conn.close()
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "/admin" in body


def test_page_refreshes_itself_while_working(env):
    client, _, _ = env
    client.post("/api/report/c1/prepare")
    assert "location.reload" in client.get("/report/c1/player/").get_data(as_text=True)


def test_fresh_material_offers_the_button(env):
    client, _, _ = env
    body = client.get("/report/c1/player/").get_data(as_text=True)
    assert "Подготовить к просмотру" in body
    assert "В очереди" not in body
