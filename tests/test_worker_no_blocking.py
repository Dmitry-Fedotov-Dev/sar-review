"""Долгая работа воркера не держит остальной проход.

15.11.2026: отмеченная находка показывала «Кадр готовится…» больше десяти
минут. Кадр вырезается за секунду, но в том же цикле собиралась лёгкая
копия видео, и на двух ядрах VPS ролик с телефона в 4K кодировался долго:
до следующего прохода (а с ним и до кадра находки, превью, регистрации
облачного материала) приходилось ждать конца кодирования.
"""
import inspect
import threading
import time

import sar_worker


def _body(fn):
    return inspect.getsource(fn)


def test_proxies_are_not_built_in_the_watcher_pass():
    assert "ensure_video_proxies(" not in _body(sar_worker.watcher_loop)
    assert "ensure_finding_previews(" in _body(sar_worker.watcher_loop)


def test_proxy_thread_is_started():
    assert "target=proxy_loop" in _body(sar_worker.main)


def test_proxy_loop_survives_a_failure_and_keeps_going(monkeypatch, capsys):
    """Сбой одной сборки не должен останавливать поток навсегда."""
    calls = []

    def fake(conn):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("ffmpeg упал")
        if len(calls) >= 3:
            raise SystemExit          # выход из бесконечного цикла для теста
        return 1
    monkeypatch.setattr(sar_worker, "ensure_video_proxies", fake)
    monkeypatch.setattr(sar_worker, "get_db", lambda: None)
    monkeypatch.setitem(sar_worker.CFG, "poll_interval_sec", 0) if isinstance(
        getattr(sar_worker, "CFG", None), dict) else monkeypatch.setattr(
        sar_worker, "CFG", {"poll_interval_sec": 0})
    monkeypatch.setattr(sar_worker.time, "sleep", lambda s: None)
    try:
        sar_worker.proxy_loop()
    except SystemExit:
        pass
    assert len(calls) == 3
    assert "ffmpeg упал" in capsys.readouterr().out


# --- кадр находки: запасной источник и отступ после неудач ------------------

import json
import os
import sqlite3

import sar_common


def _setup(tmp_path, monkeypatch):
    watch = tmp_path / "w"
    watch.mkdir()
    data = tmp_path / "d"
    data.mkdir()
    db = str(data / "sar_data.db")
    sar_common.init_db(db)
    monkeypatch.setattr(sar_worker, "WATCH_DIR", str(watch), raising=False)
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_worker, "_failures", {})
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    (watch / "a.mp4").write_bytes(b"orig")
    c.execute("INSERT INTO reports (report_id, rel_path, abs_path, kind, status, created_at, "
              "updated_at) VALUES ('r', 'a.mp4', '', 'video', 'idle', 'x', 'x')")
    c.execute("INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, "
              "created_at) VALUES ('r', 'x', 7, '[0,0,1,1]', 'x')")
    c.commit()
    proxy = sar_common.proxy_video_path(str(data), "a.mp4")
    os.makedirs(os.path.dirname(proxy), exist_ok=True)
    open(proxy, "wb").write(b"proxy")
    return c, proxy


def test_falls_back_to_proxy_when_original_fails(tmp_path, monkeypatch):
    c, proxy = _setup(tmp_path, monkeypatch)
    tried = []

    def gen(src, seconds, bbox, out, full):
        tried.append(os.path.basename(src))
        return src == proxy
    monkeypatch.setattr(sar_worker, "_generate_finding_preview", gen)
    assert sar_worker.ensure_finding_previews(c) == 1
    assert tried == ["a.mp4", os.path.basename(proxy)]


def test_failure_is_reported_and_backed_off(tmp_path, monkeypatch, capsys):
    c, _ = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(sar_worker, "_generate_finding_preview",
                        lambda *a: calls.append(1) or False)
    sar_worker.ensure_finding_previews(c)
    sar_worker.ensure_finding_previews(c)          # сразу второй проход
    assert len(calls) == 2, "оба источника попробованы один раз, второй проход ждёт паузу"
    assert "кадр не вырезался" in capsys.readouterr().out
