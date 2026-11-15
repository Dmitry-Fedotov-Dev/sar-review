"""Каждый встроенный скрипт каждой страницы разбирается JavaScript'ом.

14.11.2026 страница операции открылась без единой кнопки и без списка: в
строке JS оказался настоящий перенос (`'...\\n'` в шаблоне Python стал
переводом строки), и браузер отбросил ВЕСЬ скрипт страницы. Тест на баланс
скобок такое не ловит. Здесь -- настоящий разбор: node --check.

Шаблоны собираются с «0» вместо всех параметров: этого достаточно для
разбора, а реальные значения подставляет сервер.
"""
import re
import shutil
import string
import subprocess

import pytest

import sar_server

NODE = shutil.which("node")


class _Zero(dict):
    def __missing__(self, key):
        return "0"


def _templates():
    for name in dir(sar_server):
        val = getattr(sar_server, name)
        if name.endswith("_HTML") and isinstance(val, str) and "<script" in val:
            yield name, val


@pytest.mark.skipif(NODE is None, reason="нужен node")
@pytest.mark.parametrize("name,tpl", list(_templates()), ids=[n for n, _ in _templates()])
def test_page_scripts_parse(name, tpl, tmp_path):
    html = string.Formatter().vformat(tpl, (), _Zero())
    scripts = [s for s in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
               if s.strip()]
    assert scripts, f"{name}: скриптов не найдено"
    for i, src in enumerate(scripts):
        f = tmp_path / f"{name}_{i}.js"
        f.write_text(src, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name}, скрипт №{i}:\n{r.stderr[:800]}"
