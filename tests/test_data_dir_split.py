"""Служебные данные отдельно от материала.

Раньше служебная папка жёстко лежала внутри наблюдаемой:
data_dir = watch_dir/sar_data. Пока и то и другое на одном диске, разницы
нет. Но это две РАЗНЫЕ по характеру нагрузки:

  * материал -- 75 крупных файлов на 21,3 ГБ, читаются подряд и по одному
    разу. Их место в облаке или на большом медленном диске.
  * служебное -- 573 805 мелких файлов отчётов на 7,4 ГБ плюс база SQLite.
    Нужны мгновенно и постоянно. Их место на локальном SSD.

Пока они в одной папке, выбора нет: либо тащить весь материал на
системный диск, либо отправить полмиллиона мелких файлов в облако, где
они гарантированно встанут колом (на exFAT с кластером 128 КБ такой же
набор дал перерасход почти в десять раз).

Здесь проверяется, что разделение действительно разделяет -- и, главное,
что отчёты НЕ уезжают вслед за материалом. Ошибка была бы молчаливой:
всё бы работало, просто отчёты оказались бы не там, где задумано, и
обнаружилось бы это переполнением диска или тормозами облака.
"""
import os

import pytest

import sar_common


def test_default_keeps_the_old_layout(tmp_path):
    """Умолчание не меняется: ни одна существующая установка не переедет
    сама от простого обновления кода."""
    watch = tmp_path / "watch"
    watch.mkdir()
    w, data, db, reports = sar_common.resolve_paths(str(watch))
    assert data == os.path.join(str(watch), "sar_data")
    assert db == os.path.join(data, "sar_data.db")
    assert reports == os.path.join(data, "reports")


def test_explicit_data_dir_moves_everything_service_related(tmp_path):
    watch = tmp_path / "watch"
    watch.mkdir()
    elsewhere = tmp_path / "ssd" / "sar"
    w, data, db, reports = sar_common.resolve_paths(str(watch), str(elsewhere))

    assert w == str(watch), "наблюдаемая папка не должна меняться"
    assert data == str(elsewhere)
    assert db.startswith(str(elsewhere))
    assert reports.startswith(str(elsewhere))


def test_reports_do_not_follow_the_material(tmp_path):
    """Главное свойство. Если отчёты останутся внутри watch_dir, весь
    смысл теряется: полмиллиона мелких файлов уедут в облако вместе с
    материалом."""
    watch = tmp_path / "cloud"
    watch.mkdir()
    local = tmp_path / "ssd"
    _, _, db, reports = sar_common.resolve_paths(str(watch), str(local))

    assert not os.path.abspath(reports).startswith(os.path.abspath(str(watch)))
    assert not os.path.abspath(db).startswith(os.path.abspath(str(watch)))


def test_service_dirs_are_created(tmp_path):
    """Папку на новом диске никто не создаёт руками."""
    watch = tmp_path / "watch"
    watch.mkdir()
    elsewhere = tmp_path / "brand" / "new" / "place"
    _, data, _, reports = sar_common.resolve_paths(str(watch), str(elsewhere))
    assert os.path.isdir(data)
    assert os.path.isdir(reports)


def test_relative_data_dir_is_made_absolute(tmp_path, monkeypatch):
    """В конфиге человек может написать относительный путь. Оставить его
    относительным нельзя: воркер и сервер запускаются из разных мест, и
    один и тот же конфиг привёл бы их в РАЗНЫЕ папки -- то есть в разные
    базы, каждый со своей половиной правды."""
    monkeypatch.chdir(tmp_path)
    _, data, _, _ = sar_common.resolve_paths(str(tmp_path / "watch"), "service")
    assert os.path.isabs(data)


def test_both_processes_agree_on_the_same_paths(tmp_path):
    """Воркер и сервер обязаны видеть один набор путей независимо от того,
    кто стартовал раньше: общаются они ТОЛЬКО через базу и файлы."""
    watch = str(tmp_path / "watch")
    data = str(tmp_path / "ssd")
    assert sar_common.resolve_paths(watch, data) == sar_common.resolve_paths(watch, data)


def test_config_has_the_key_with_a_safe_default():
    assert "data_dir" in sar_common.DEFAULT_SERVER_CONFIG
    assert sar_common.DEFAULT_SERVER_CONFIG["data_dir"] is None, (
        "умолчание обязано сохранять прежнее размещение")


def test_every_entrypoint_passes_the_setting(tmp_path):
    """Страж. Разделение работает только если ВСЕ процессы читают одну
    настройку. Забыть её в одном месте -- и этот процесс уйдёт работать с
    другой базой, молча: он её просто создаст заново и не найдёт ни одного
    материала.

    Ровно так уже было с папкой резервных копий: путь считался в трёх
    местах, два были неверны, и мониторинг годами смотрел не туда.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("sar_server.py", "sar_worker.py", "sar_telegram_bot.py",
                 "sar_dataset_export.py", "sar_backup.py"):
        body = (root / name).read_text(encoding="utf-8")
        assert "resolve_paths(" in body
        idx = body.index("resolve_paths(")
        # настройка должна упоминаться рядом с вызовом, а не где-то в файле
        assert 'data_dir' in body[idx:idx + 260], (
            f"{name} зовёт resolve_paths, не передавая data_dir -- "
            f"этот процесс уйдёт в другую папку")
