"""Тесты модуля нагрузочного тестирования.

Инструмент измерения обязан быть точнее того, что измеряет: если врёт
перцентиль, все выводы о производительности -- мусор, а на них принимают
решения о железе. Поэтому перцентили проверяем на числах с известным
ответом, а не "работает же".

Отдельно проверяем два свойства, которые важнее скорости:
  * тест НЕ ПИШЕТ в базу -- его можно гонять по работающей системе;
  * тест ОТКАЗЫВАЕТСЯ бить по публичному адресу без явного согласия --
    иначе он положит туннель тем, кто в этот момент ищет людей.
"""
import json

import pytest

import sar_loadtest as lt


# --- перцентили: основа всех выводов -------------------------------------

def test_percentile_on_known_values():
    v = list(range(1, 101))            # 1..100
    assert lt.percentile(v, 50) == pytest.approx(50.5)
    assert lt.percentile(v, 95) == pytest.approx(95.05, abs=0.1)
    assert lt.percentile(v, 100) == 100
    assert lt.percentile(v, 0) == 1


def test_percentile_single_value():
    assert lt.percentile([42.0], 95) == 42.0


def test_percentile_empty_is_zero_not_crash():
    """Пустая выборка бывает, когда шаг целиком отвалился по ошибке."""
    assert lt.percentile([], 95) == 0.0


def test_percentile_is_not_the_mean():
    """Ключевое: один провал не должен растворяться в среднем.
    Среднее тут ~109, а p95 обязан показать выброс."""
    v = [10.0] * 99 + [10000.0]
    assert lt.percentile(v, 95) < 100      # p95 всё ещё в норме
    assert lt.percentile(v, 100) == 10000  # но максимум виден


# --- защита от удара по продакшену ---------------------------------------

def _run_main(monkeypatch, argv):
    monkeypatch.setattr(lt.sys, "argv", ["sar_loadtest.py"] + argv)
    return lt.main()


def test_refuses_remote_target_without_explicit_flag(monkeypatch, capsys):
    code = _run_main(monkeypatch, ["--url", "https://example.trycloudflare.com",
                                    "--password", "x", "--duration", "1"])
    out = capsys.readouterr().out
    assert code == 2
    assert "ОТКАЗ" in out
    assert "--allow-remote" in out, "надо подсказать, как согласиться осознанно"


def test_local_target_is_allowed_through_the_guard(monkeypatch, capsys):
    """Локальный адрес не должен упираться в защиту -- падать он может
    дальше, на отсутствии сервера, но не на проверке хоста."""
    code = _run_main(monkeypatch, ["--url", "http://127.0.0.1:9", "--password", "x",
                                    "--duration", "1"])
    out = capsys.readouterr().out
    assert "не локальный адрес" not in out
    assert code == 2 and "не удалось получить список файлов" in out


# --- только чтение --------------------------------------------------------

def test_scenario_is_read_only():
    """Ни один шаг сценария не должен вести на пишущий эндпоинт.
    Проверяем по списку известных пишущих маршрутов сервера."""
    writing = ("observations", "comments", "priorities", "enqueue",
               "playback_watched", "heartbeat", "upload")
    for label, key in lt.SCENARIO:
        assert not any(w in key for w in writing), f"шаг «{label}» пишет в базу"


def test_virtual_user_only_issues_get_after_login(monkeypatch):
    """Сам обход тоже не должен слать POST: логин -- единственное исключение."""
    calls = []

    class FakeResp:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(self, req, timeout=None):
        calls.append(req if isinstance(req, str) else "POST:" + req.full_url)
        return FakeResp()

    monkeypatch.setattr(lt.urllib.request.OpenerDirector, "open", fake_open)

    targets = {"reports": [{"report_id": "r1"}], "thumbs": ["a.MP4"], "total": 1}
    out = []
    u = lt.VirtualUser(0, "http://127.0.0.1:8080", "pw", targets,
                       stop_at=0, out=out)   # stop_at=0 -> цикл не крутится
    u.run()
    assert len(calls) == 1 and calls[0].startswith("POST:"), calls
    assert calls[0].endswith("/login")


# --- отчёт ----------------------------------------------------------------

def test_report_breaks_results_down_per_endpoint(capsys):
    """Одно среднее число бесполезно: надо видеть, какой шаг медленный."""
    samples = [lt.Sample("список файлов (данные)", 2000.0, 200),
               lt.Sample("превью", 5.0, 200)]
    lt.report(samples, wall=10.0, users=3)
    out = capsys.readouterr().out
    assert "список файлов (данные)" in out
    assert "превью" in out
    assert "узкое место" in out
    assert "список файлов (данные)" in out.split("узкое место")[1]


def test_report_surfaces_errors_with_codes(capsys):
    samples = [lt.Sample("превью", 1.0, 500, "HTTP 500"),
               lt.Sample("превью", 1.0, 200)]
    lt.report(samples, wall=1.0, users=1)
    out = capsys.readouterr().out
    assert "ОШИБКИ" in out and "HTTP 500" in out


def test_verdict_reflects_measured_latency(capsys):
    lt.report([lt.Sample("превью", 20.0, 200)], wall=1.0, users=6)
    assert "Запас есть" in capsys.readouterr().out

    lt.report([lt.Sample("превью", 9000.0, 200)], wall=1.0, users=6)
    assert "не тянет" in capsys.readouterr().out


def test_raw_samples_are_written_for_audit(tmp_path):
    """Результат должен быть перепроверяемым, а не только напечатанным."""
    p = tmp_path / "raw.json"
    lt.report([lt.Sample("превью", 12.5, 200)], wall=2.0, users=4, out_json=str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["users"] == 4 and data["wall_sec"] == 2.0
    assert data["samples"][0]["ms"] == 12.5
    assert data["samples"][0]["step"] == "превью"
