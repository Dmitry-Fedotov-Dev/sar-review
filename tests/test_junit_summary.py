"""Сводка отчёта JUnit для страницы запуска (.github/actions/checks/junit_summary.py).

Строится по НАСТОЯЩЕМУ отчёту pytest, а не по выдуманному XML: формат
должен совпадать с тем, что pytest пишет на самом деле."""
import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "junit_summary", os.path.join(ROOT, ".github", "actions", "checks", "junit_summary.py"))
js = importlib.util.module_from_spec(spec)
spec.loader.exec_module(js)


def _run(tmp_path, body):
    (tmp_path / "test_x.py").write_text(body, encoding="utf-8")
    xml = tmp_path / "junit.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    f"--junitxml={xml}", str(tmp_path / "test_x.py")],
                   cwd=tmp_path, capture_output=True)
    return js.render(str(xml))


def test_green_run(tmp_path):
    md = _run(tmp_path, "def test_a():\n    assert 1\n\ndef test_b():\n    pass\n")
    assert md.startswith("### ✅ Тесты: 2 прошли")
    assert "Самые медленные" in md


def test_failures_are_listed_with_reason(tmp_path):
    md = _run(tmp_path, "import pytest\n"
                        "def test_ok():\n    pass\n"
                        "def test_bad():\n    assert 2 + 2 == 5, 'арифметика | сломалась'\n"
                        "@pytest.mark.skip\ndef test_skip():\n    pass\n")
    assert md.startswith("### ❌ Тесты: упало 1 из 3")
    assert "test_bad" in md and "арифметика \\| сломалась" in md, "| экранирован для таблицы"
    assert "прошло 1 · упало 1 · пропущено 1" in md


def test_missing_report_is_not_silent(tmp_path):
    md = js.render(str(tmp_path / "нет.xml"))
    assert md.startswith("### ❌") and "отчёта нет" in md
