"""Учёт запросов к API.

До этого нагрузка не считалась вообще: ни счётчиков, ни журнала доступа.
Поэтому вопрос «сколько запросов было 15 августа» остался без ответа --
данных просто нет и восстановить их неоткуда. Теперь считаются, но важно
не наделать при этом двух классических ошибок:

  * НЕ группировать по фактическому адресу. /report/<id>/ породил бы
    отдельный ряд на каждый отчёт, и в Prometheus оказались бы десятки
    тысяч рядов вместо одного -- это его убивает;
  * НЕ ронять ответ из-за учёта. Метрика полезна, но не настолько, чтобы
    человек из-за неё не увидел страницу.
"""
import pytest

import sar_common
import sar_health
import sar_server


@pytest.fixture(autouse=True)
def clean():
    sar_health._HTTP_COUNT.clear()
    sar_health._HTTP_BUCKETS.clear()
    sar_health._HTTP_SUM.clear()
    yield


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
                        lambda w, d=None: (str(watch), str(watch), db, str(watch)))
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    return sar_server.app.test_client()


# --- счёт -----------------------------------------------------------------

def test_requests_are_counted(client):
    for _ in range(3):
        client.get("/healthz")
    counts, _, _, _ = sar_health.http_snapshot()
    total = sum(n for (ep, _, _), n in counts.items() if ep == "healthz")
    assert total == 3


def test_status_code_is_recorded(client):
    """Записанный код обязан совпадать с тем, что реально ушло клиенту: без
    этого не отличить «медленно» от «отвечает ошибкой».

    Конкретный код не зашиваем: несуществующий адрес до 404 не доходит --
    его раньше перехватывает проверка входа и отдаёт переход на страницу
    входа. Это как раз то, на чём тест уже один раз ошибся.
    """
    r = client.get("/nope-such-page")
    counts, _, _, _ = sar_health.http_snapshot()
    assert any(status == r.status_code for (_, _, status) in counts)


def test_errors_are_counted_separately_from_success(client):
    client.get("/operations")     # отбит проверкой входа
    client.get("/healthz")        # открыт для монитора
    counts, _, _, _ = sar_health.http_snapshot()
    assert len({status for (_, _, status) in counts}) >= 2, (
        "разные исходы слились в один ряд -- по метрике не увидеть ошибок")


def test_method_is_recorded(client):
    client.get("/healthz")
    client.post("/login", data={"name": "x", "password": "pw"})
    counts, _, _, _ = sar_health.http_snapshot()
    methods = {m for (_, m, _) in counts}
    assert {"GET", "POST"} <= methods


def test_grouped_by_route_not_by_url(client):
    """Ключевое для Prometheus: разные отчёты -- ОДНА метрика."""
    for rid in ("a", "b", "c", "d"):
        client.get(f"/report/{rid}/player/")
    counts, _, _, _ = sar_health.http_snapshot()
    endpoints = {ep for (ep, _, _) in counts}
    assert "player_page" in endpoints
    assert not any("/report/a" in ep for ep in endpoints), (
        "метрика завязалась на конкретный адрес -- в Prometheus будут "
        "десятки тысяч рядов")


# --- гистограмма ----------------------------------------------------------

def test_histogram_is_filled():
    sar_health.record_request("x", "GET", 200, 0.003)
    sar_health.record_request("x", "GET", 200, 0.4)
    sar_health.record_request("x", "GET", 200, 30.0)
    _, buckets, sums, _ = sar_health.http_snapshot()
    assert sum(buckets["x"]) == 3
    assert buckets["x"][-1] == 1, "запрос дольше всех границ должен попасть в хвост"
    assert abs(sums["x"] - 30.403) < 0.01


def test_histogram_is_cumulative():
    """В формате Prometheus корзины НАКОПИТЕЛЬНЫЕ: le=0.5 включает всё,
    что быстрее. Иначе гистограмма читается неверно."""
    for s in (0.003, 0.02, 0.4):
        sar_health.record_request("x", "GET", 200, s)
    text = sar_health.render_http_metrics()
    vals = {}
    for line in text.split("\n"):
        if "_bucket" in line and 'endpoint="x"' in line:
            le = line.split('le="')[1].split('"')[0]
            vals[le] = int(line.rsplit(" ", 1)[1])
    assert vals["0.005"] == 1
    assert vals["0.025"] == 2
    assert vals["0.5"] == 3
    assert vals["+Inf"] == 3


def test_count_matches_inf_bucket():
    for s in (0.01, 0.02, 99.0):
        sar_health.record_request("y", "GET", 200, s)
    text = sar_health.render_http_metrics()
    inf = next(l for l in text.split("\n")
               if "_bucket" in l and 'endpoint="y"' in l and 'le="+Inf"' in l)
    cnt = next(l for l in text.split("\n")
               if "_count" in l and 'endpoint="y"' in l)
    assert inf.rsplit(" ", 1)[1] == cnt.rsplit(" ", 1)[1]


# --- формат ---------------------------------------------------------------

def test_metrics_endpoint_exposes_requests(client):
    client.get("/healthz")
    text = client.get("/metrics").get_data(as_text=True)
    assert "sar_http_requests_total" in text
    assert "sar_http_request_duration_seconds_bucket" in text


def test_empty_snapshot_renders_nothing():
    """Пока запросов не было, лишних строк в /metrics быть не должно."""
    assert sar_health.render_http_metrics() == ""


def test_prometheus_lines_are_parseable(client):
    client.get("/healthz")
    text = client.get("/metrics").get_data(as_text=True)
    for line in text.strip().split("\n"):
        if line.startswith("#") or not line:
            continue
        name, _, value = line.rpartition(" ")
        assert name
        float(value)


# --- учёт не должен вредить -----------------------------------------------

def test_login_check_is_still_registered():
    """Хук учёта вставлялся рядом с проверкой входа и при первой попытке
    оказался МЕЖДУ декоратором @app.before_request и require_login: декоратор
    достался счётчику, а проверка входа перестала регистрироваться -- сайт
    открывался без пароля. Поймали существующие тесты; этот закрепляет.
    """
    names = [f.__name__ for f in sar_server.app.before_request_funcs[None]]
    assert "require_login" in names, "проверка входа не зарегистрирована"
    assert names.count("_metrics_start") == 1, "хук учёта зарегистрирован дважды"
    assert names.index("_metrics_start") < names.index("require_login"), (
        "учёт должен идти первым, иначе отказы по входу не попадут в метрики")


def test_redirects_to_login_are_counted(client):
    """Запрос, отбитый проверкой входа, тоже нагрузка на сервер."""
    r = client.get("/operations")
    counts, _, _, _ = sar_health.http_snapshot()
    assert counts, "запрос, не дошедший до обработчика, потерялся из учёта"
    assert any(status == r.status_code for (_, _, status) in counts)



def test_broken_recording_does_not_break_the_response(client, monkeypatch):
    """Метрика полезна, но не настолько, чтобы из-за неё человек не увидел
    страницу."""
    def boom(*a, **k):
        raise RuntimeError("счётчик сломался")

    monkeypatch.setattr(sar_health, "record_request", boom)
    r = client.get("/healthz")
    # 503 здесь -- честный ответ проверки здоровья на пустой базе (воркер не
    # слал heartbeat), а не поломка. Важно, что это НЕ 500: значит исключение
    # счётчика проглочено и ответ дошёл целым.
    assert r.status_code != 500
    assert r.get_json() is not None


def test_counters_are_not_a_rate():
    """Отдаём счётчик, а не мгновенный RPS: скорость считает Prometheus
    через rate(), и окно выбирается на графике, а не зашивается здесь."""
    sar_health.record_request("x", "GET", 200, 0.01)
    text = sar_health.render_http_metrics()
    assert "counter" in text
    assert "rps" not in text.lower()
