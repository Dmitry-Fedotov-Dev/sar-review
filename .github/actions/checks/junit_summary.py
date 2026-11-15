"""Сводка отчёта JUnit (pytest --junitxml) для страницы запуска GitHub.

    python junit_summary.py junit.xml >> "$GITHUB_STEP_SUMMARY"

Что показывает: итог одной строкой, упавшие тесты с началом сообщения
(раскрываются по клику), десять самых медленных. Только стандартная
библиотека: шаг не должен зависеть от того, что ставится для тестов.
"""
import sys
import xml.etree.ElementTree as ET

MAX_FAILED = 30
SLOWEST = 10


def _cases(root):
    for case in root.iter("testcase"):
        name = case.get("name", "?")
        cls = case.get("classname", "")
        full = f"{cls}::{name}" if cls else name
        try:
            t = float(case.get("time") or 0)
        except ValueError:
            t = 0.0
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        skipped = case.find("skipped") is not None
        yield full, t, bad, skipped


def render(path):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        # Нет отчёта -- значит pytest не дошёл до конца (упал на сборке
        # тестов). Говорим это явно: пустая сводка читалась бы как «всё хорошо».
        return f"### ❌ Тесты: отчёта нет ({e}) — pytest не дошёл до конца, смотрите лог шага\n"
    cases = list(_cases(root))
    failed = [c for c in cases if c[2] is not None]
    skipped = sum(1 for c in cases if c[3])
    total_time = sum(c[1] for c in cases)
    passed = len(cases) - len(failed) - skipped
    out = []
    if failed:
        out.append(f"### ❌ Тесты: упало {len(failed)} из {len(cases)}")
    else:
        out.append(f"### ✅ Тесты: {passed} прошли")
    out.append(f"\nпрошло {passed} · упало {len(failed)} · пропущено {skipped} · "
               f"время тестов {total_time:.1f} с\n")
    if failed:
        out.append("| Тест | Причина |\n|---|---|")
        for full, _, bad, _ in failed[:MAX_FAILED]:
            msg = (bad.get("message") or (bad.text or "")).strip().splitlines()
            first = (msg[0] if msg else "").replace("|", "\\|")[:200]
            out.append(f"| `{full}` | {first} |")
        if len(failed) > MAX_FAILED:
            out.append(f"\n…и ещё {len(failed) - MAX_FAILED}, полный список — в логе шага tests.")
        out.append("")
    slow = sorted(cases, key=lambda c: -c[1])[:SLOWEST]
    if slow:
        out.append("<details><summary>Самые медленные</summary>\n")
        out.append("| Тест | с |\n|---|---|")
        out.extend(f"| `{full}` | {t:.2f} |" for full, t, _, _ in slow)
        out.append("\n</details>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render(sys.argv[1] if len(sys.argv) > 1 else "test-results/junit.xml"))
