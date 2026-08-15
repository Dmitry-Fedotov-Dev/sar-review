"""Детектор должен запускаться с пониженным приоритетом.

Причина из эксплуатации: инференс и веб-сервис живут на одной машине, и при
равном приоритете планировщик ОС делит процессор поровну -- интерфейс
подвисает у всех ровно во время обработки. Инференс задержку терпит, человек
за просмотром -- нет.

Это дополняет резерв ядер (_limit_cpu_threads в sar_video_review.py): там
ограничивается, СКОЛЬКО ядер займёт инференс, здесь -- насколько охотно он их
уступает под нагрузкой."""
import os
import subprocess

import sar_worker


def test_low_priority_kwargs_on_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    kwargs = sar_worker._low_priority_kwargs()
    assert "creationflags" in kwargs
    assert kwargs["creationflags"] == subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert kwargs["creationflags"] != 0, "флаг понижения приоритета не найден"


def test_low_priority_uses_nice_on_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    kwargs = sar_worker._low_priority_kwargs()
    assert "preexec_fn" in kwargs
    assert callable(kwargs["preexec_fn"])


def test_posix_hook_actually_lowers_niceness(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    called = {}
    # os.nice есть только на POSIX -- на Windows атрибута нет вовсе,
    # поэтому raising=False (тест проверяет логику, а не саму ОС)
    monkeypatch.setattr(os, "nice", lambda inc: called.setdefault("inc", inc), raising=False)
    sar_worker._low_priority_kwargs()["preexec_fn"]()
    assert called["inc"] > 0, "приоритет должен ПОНИЖАТЬСЯ (nice > 0)"


def test_detector_is_spawned_with_low_priority():
    """Страховка от расхождения: сам вызов Popen должен применять эти kwargs,
    иначе функция есть, а эффекта нет."""
    import inspect
    src = inspect.getsource(sar_worker.run_one_report)
    assert "_low_priority_kwargs()" in src
