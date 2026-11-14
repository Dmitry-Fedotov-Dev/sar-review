"""Деплой -- строго по инструкции cd (github.com/Dmitry-Fedotov-Dev/cd).

deploy.yml -- стартовый шаблон cd в том виде, в каком его пишет
`init.sh sar <сервер>`, плюс тесты на отведённой шаблоном строке. Страж
держит три вещи, которые легко сломать незаметно:
- ветка master: шаблон cd по умолчанию слушает main, и с ним workflow в
  этом репозитории не запустился бы ни разу -- без единой ошибки;
- никаких своих переменных и матриц поверх шаблона (решение пользователя);
- тесты стоят ДО шага деплоя: красный набор не уходит на сервер.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _wf():
    with open(os.path.join(ROOT, ".github", "workflows", "deploy.yml"), encoding="utf-8") as f:
        return f.read()


def test_follows_default_branch_master():
    s = _wf()
    assert "branches: [master]" in s
    assert "[main]" not in s


def test_is_the_cd_starter():
    s = _wf()
    assert "uses: Dmitry-Fedotov-Dev/cd@v1" in s
    assert "server: ${{ vars.CD_SERVER }}" in s
    assert "project: sar\n" in s
    assert "id-token: write" in s
    for own in ("CD_TARGETS", "CD_PROJECT", "matrix"):
        assert own not in s, f"{own}: своё поверх шаблона cd"


def test_tests_run_before_deploy():
    s = _wf()
    assert s.index("python -m pytest") < s.index("uses: Dmitry-Fedotov-Dev/cd@v1")
    # на отведённой шаблоном строке, а не где придётся
    assert s.index("# tests, lint, build checks go here") < s.index("python -m pytest")
