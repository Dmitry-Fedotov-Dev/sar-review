"""Папка резервных копий: одна для скрипта и для мониторинга.

Найдено при подъёме платформы. Проверка здоровья рапортовала "последняя
копия 56 ч назад" сразу ПОСЛЕ свежей копии. Причина: sar_backup.py кладёт
копии в папку ВНУТРИ проекта, а sar_health.py искал их РЯДОМ с проектом --
на уровень выше watch_dir.

Это худший вид поломки мониторинга: он не молчит, а уверенно докладывает
неправду. Тревога о протухших копиях не погасла бы никогда, сколько копий
ни делай, а настоящая пропажа копий на её фоне осталась бы незамеченной.
"""
import io
import os

import sar_backup
import sar_common
import sar_health


def test_script_and_monitoring_look_at_the_same_folder():
    """Собственно регрессия."""
    with open("sar_backup.py", encoding="utf-8") as f:
        assert "sar_common.backups_dir()" in f.read(), (
            "скрипт снова считает папку копий сам")
    with open("sar_health.py", encoding="utf-8") as f:
        assert "sar_common.backups_dir()" in f.read(), (
            "проверка снова считает папку копий сама")


def test_folder_does_not_depend_on_watch_dir():
    """watch_dir настраивается и может указывать куда угодно, а копии
    обязаны лежать там же, куда их кладёт скрипт, при любой настройке."""
    here = os.path.dirname(os.path.abspath(sar_common.__file__))
    assert sar_common.backups_dir() == os.path.join(here, "sar_backups")


def test_fresh_backup_is_seen_as_fresh(tmp_path):
    """Прямая проверка того, что докладывала проверка здоровья.

    Имя файла берём от ТЕКУЩЕГО времени: возраст копии считается по её
    имени, и вписанная жёстко дата делает тест протухающим -- он проходил
    в момент написания и падал через полтора часа.
    """
    from datetime import datetime
    d = tmp_path / "sar_backups"
    d.mkdir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (d / f"sar_backup_{stamp}.zip").write_bytes(b"x")
    age = sar_health.backup_age_hours(str(d))
    assert age is not None and age < 1, f"свежая копия выглядит старой: {age}"


def test_no_backups_is_not_confused_with_zero_age(tmp_path):
    """Пустое не подменяем нулём: возраст 0 означал бы «только что»,
    то есть ровно противоположное тому, что есть на самом деле."""
    d = tmp_path / "пусто"
    d.mkdir()
    assert sar_health.backup_age_hours(str(d)) is None


def test_snapshot_leftovers_are_removed(tmp_path):
    """SQLite кладёт рядом с базой -wal и -shm; удаление одного .db
    оставляло их в папке копий навсегда. Папка с резервными копиями --
    последнее место, где стоит гадать, что за файлы рядом лежат."""
    base = tmp_path / "_snapshot_x.db"
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / ("_snapshot_x.db" + suffix)).write_bytes(b"x")
    sar_backup._remove_snapshot(str(base))
    assert list(tmp_path.iterdir()) == [], (
        f"остались хвосты: {[p.name for p in tmp_path.iterdir()]}")


def test_removing_a_missing_snapshot_is_not_an_error(tmp_path):
    sar_backup._remove_snapshot(str(tmp_path / "нет-такого.db"))


def test_no_module_computes_the_path_on_its_own():
    """Путь считался ТРИ раза в трёх файлах, и два из трёх были неверны.

    Хуже всего был третий: sar_server.py передавал свой вариант явным
    аргументом и перебивал общее значение, поэтому исправление в двух
    других местах ничего не меняло. Пока путь можно собрать "где-то ещё",
    он снова разъедется -- поэтому проверяем, что никто, кроме
    sar_common, его не составляет.
    """
    import glob
    culprits = []
    for path in sorted(glob.glob("sar_*.py")):
        if path == "sar_common.py":
            continue
        with io.open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if line.lstrip().startswith("#"):
                    continue
                if '"sar_backups"' in line or "'sar_backups'" in line:
                    culprits.append("%s:%d" % (path, i))
    assert not culprits, (
        "папка копий составляется в обход sar_common.backups_dir(): %s"
        % culprits)
