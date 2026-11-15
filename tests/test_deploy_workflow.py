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


def _action():
    with open(os.path.join(ROOT, ".github", "actions", "checks", "action.yml"), encoding="utf-8") as f:
        return f.read()


def test_tests_run_before_deploy():
    s = _wf()
    # проверки -- общим действием, на отведённой шаблоном строке, ДО деплоя
    assert s.index("# tests, lint, build checks go here") < s.index("uses: ./.github/actions/checks")
    assert s.index("uses: ./.github/actions/checks") < s.index("uses: Dmitry-Fedotov-Dev/cd@v1")
    a = _action()
    assert "python -m pytest" in a and "--junitxml" in a


def test_checks_keep_exit_code_after_summary():
    """Сводка пишется и при падении, но красные тесты обязаны оставаться
    красными -- иначе деплой ушёл бы с упавшими тестами."""
    a = _action()
    assert "|| rc=$?" in a and "exit $rc" in a


def test_ci_project_action_is_pinned():
    """Шаг из проекта ci -- с точной версией: неудачный релиз ci не должен
    останавливать выкладку. (@v1 к тому же в ci нет как тега.)"""
    import re
    refs = re.findall(r"uses: Dmitry-Fedotov-Dev/ci/[^@\s]+@(\S+)", _action())
    assert refs and all(re.fullmatch(r"v\d+\.\d+\.\d+", r) for r in refs), refs


def test_ci_workflow_does_not_duplicate_master():
    with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
        ci = f.read()
    assert "pull_request" in ci and "branches-ignore: [master]" in ci
    assert "uses: ./.github/actions/checks" in ci
    assert "Dmitry-Fedotov-Dev/cd" not in ci, "ci.yml не деплоит"
