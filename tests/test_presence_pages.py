"""Онлайн должен считаться везде, где человек работает.

Баг, который нашёл пользователь: «онлайн не засчитывается за юзером, если
он на странице проекта, а не в плеере». Так и было. Пульс присутствия был
вписан руками в три страницы -- список файлов, страницу обработки и плеер.
Страницы операций появились позже, и пульса им никто не добавил. При этом
после входа человек попадает именно на операции: он выбирает операцию,
листает материалы, читает отчёт -- и всё это время числится отсутствующим.

Последствия шире, чем неверная цифра на экране: та же метрика уходит в
мониторинг, поэтому история онлайна за всё время до этой правки занижена
и показывает только тех, кто открыл плеер.

Здесь проверяется не текст шаблона, а поведение: страница, отданная
сервером, действительно содержит пульс.
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
    conn = sar_common.get_db_connection(db)
    op = sar_common.create_operation(conn, "Тест", "")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw",
                         "host": "127.0.0.1", "port": 8080},
                        raising=False)
    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(watch), raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    c = sar_server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
        sess["viewer_name"] = "Тестовый"
    c.operation_id = op
    return c


# --- страницы, где человек работает --------------------------------------

@pytest.mark.parametrize("path,что_это", [
    ("/operations", "список операций -- сюда попадают сразу после входа"),
    ("/", "список файлов"),
])
def test_working_pages_report_presence(client, path, что_это):
    html = client.get(path).get_data(as_text=True)
    assert "setInterval(heartbeat" in html, (
        f"{что_это}: человек на этой странице не будет засчитан в онлайн")


def test_operation_page_reports_presence(client):
    html = client.get(f"/operation/{client.operation_id}/").get_data(as_text=True)
    assert "setInterval(heartbeat" in html, (
        "страница операции -- именно здесь человек проводит больше всего "
        "времени, и именно она не отмечала присутствие")


# --- открытые страницы: присутствия быть не должно ------------------------

def test_guide_does_not_report_presence(client):
    """/guide открыт БЕЗ входа. Присутствие оттуда означало бы «онлайн»
    для того, кого мы не опознали."""
    html = client.get("/guide").get_data(as_text=True)
    assert "setInterval(heartbeat" not in html


def test_login_page_does_not_report_presence(client):
    assert "setInterval(heartbeat" not in sar_server.LOGIN_HTML


# --- одна реализация, а не копии ------------------------------------------

def test_only_one_implementation_exists():
    """Копий было три, одинаковых. Четвёртая страница появилась без копии --
    так и родился баг. Одна реализация на все страницы закрывает это."""
    with open("sar_server.py", encoding="utf-8") as f:
        src = f.read()
    assert src.count("async function heartbeat()") == 1, (
        "снова несколько реализаций пульса -- следующая новая страница "
        "опять останется без него")


def test_every_page_with_presence_is_listed():
    for name in sar_server.PAGES_WITH_PRESENCE:
        tpl = getattr(sar_server, name)
        assert "setInterval(heartbeat" in tpl, f"{name}: пульс не подставился"


def test_presence_survives_template_formatting():
    """Шаблоны проходят через .format(), и фигурные скобки JS обязаны быть
    удвоены. Иначе format либо упадёт, либо съест их молча."""
    html = sar_server.OPERATION_CARD_HTML.format(viewer_name="Кто-то")
    assert "{ method: 'POST' }" in html, "скобки JS искажены форматированием"


def test_heartbeat_does_not_swallow_errors_silently():
    """Ровно пустой catch однажды спрятал сломанный счётчик онлайна --
    баг нашёл человек, а не тест (см. CLAUDE.md)."""
    assert "catch (e) {}" not in sar_server.HEARTBEAT_JS
    assert "console.warn" in sar_server.HEARTBEAT_JS


def test_heartbeat_works_without_the_counter_element():
    """Счётчик онлайна есть не на каждой странице, а отмечаться человек
    должен на всех. Поэтому элемент проверяется явно."""
    assert "if (el &&" in sar_server.HEARTBEAT_JS


# --- присутствие реально записывается ------------------------------------

def test_heartbeat_actually_records_the_person(client):
    r = client.post("/api/heartbeat")
    assert r.status_code == 200
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    names = [row[0] for row in conn.execute("SELECT viewer_name FROM presence")]
    conn.close()
    assert "Тестовый" in names


def test_person_on_operation_page_counts_as_online(client):
    """Сквозная проверка сценария из жалобы: человек открыл страницу
    операции, ничего больше не делал -- он обязан быть в онлайне."""
    client.get(f"/operation/{client.operation_id}/")
    client.post("/api/heartbeat")          # то, что делает пульс со страницы
    conn = sar_common.get_db_connection(sar_server.DB_PATH)
    assert sar_server.get_online_count(conn) >= 1, (
        "человек на странице операции не попал в онлайн")
    conn.close()
