# -*- coding: utf-8 -*-
"""Телеметрия облачного материала.

ПЯТЫЙ случай одной и той же ошибки: «где файл» спрашивают у диска, а
облачного материала на диске нет по определению.

Здесь она была разложена на два независимых отказа, и каждый по
отдельности молчал:

1. Облачный обход (`_walk_cloud`) отбрасывал всё, кроме видео и фото.
   В Google Диске операции рядом с видео лежат 36 SRT -- платформа не
   видела их в принципе, и 81 из 82 облачных видео навсегда числились
   «телеметрии нет».

2. `get_telemetry_for_report` в веб-слое читала `report["abs_path"]`.
   У облачной записи воркер пишет туда пустую строку, поэтому
   `os.path.splitext("")[0] + ".srt"` давало ".srt" (не существует), а
   `find_telemetry_for_video("")` сопоставляет по `Path(...).stem` --
   у пустой строки он пустой и не совпадает ни с чем. SRT не находился
   даже тогда, когда человек клал его в telemetry/ руками.

Следствие в бою: пометка на облачном видео сохранялась без координат, на
карту не попадала, в выгрузку KML/GPX не попадала -- и ни одной ошибки
нигде.
"""
import os

import pytest

import sar_common
import sar_server


# --- 1. обход собирает SRT, но НЕ заводит их как материал ------------------

class _Item:
    def __init__(self, name, ident, folder=False, size=10, modified=None):
        self.name, self.id, self.is_folder = name, ident, folder
        self.size, self.modified = size, modified


def _tree(mapping):
    def lister(folder_id):
        return mapping.get(folder_id, [])
    return lister


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "t.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    c.execute(
        "INSERT INTO cloud_accounts (id, provider, label, token, root_id, "
        "enabled, added_at) VALUES (1,'google','Диск','tok','root',1,"
        "datetime('now'))")
    c.commit()
    return c


def test_srt_is_collected_separately(conn):
    """SRT попадает в telemetry_out и НЕ попадает в материал."""
    tree = _tree({
        "root": [_Item("08 12", "d1", folder=True)],
        "d1": [_Item("DJI_0001.MP4", "v1", size=100),
               _Item("DJI_0001.SRT", "s1", size=7)],
    })
    srt = []
    out = sar_common.scan_cloud_materials(conn, list_folder=tree,
                                          telemetry_out=srt)
    assert [r[0] for r in out] == ["08 12/DJI_0001.MP4"], \
        "SRT не должен попасть в материал: иначе он станет файлом в списке"
    assert [r[0] for r in srt] == ["08 12/DJI_0001.SRT"]
    assert srt[0][2] == "s1", "нужен file_id, иначе файл не скачать"


def test_srt_collection_is_optional(conn):
    """Без telemetry_out обход работает как раньше -- SRT просто пропускается.

    Страж обратной совместимости: scan_cloud_materials зовут и из мест,
    которым телеметрия не нужна.
    """
    tree = _tree({
        "root": [_Item("DJI_0002.MP4", "v2", size=100),
                 _Item("DJI_0002.SRT", "s2", size=7)],
    })
    out = sar_common.scan_cloud_materials(conn, list_folder=tree)
    assert [r[0] for r in out] == ["DJI_0002.MP4"]


def test_srt_found_deep_in_folders(conn):
    """Телеметрия лежит внутри вложенных папок, как в боевом хранилище."""
    tree = _tree({
        "root": [_Item("2026 08 15", "d1", folder=True)],
        "d1": [_Item("dron part 2", "d2", folder=True)],
        "d2": [_Item("DJI_20260815162956_0001_Z.MP4", "v", size=9),
               _Item("DJI_20260815162956_0001_Z.SRT", "s", size=9)],
    })
    srt = []
    sar_common.scan_cloud_materials(conn, list_folder=tree, telemetry_out=srt)
    assert srt and srt[0][0].endswith("DJI_20260815162956_0001_Z.SRT")


# --- 2. веб-слой находит телеметрию облачного видео ------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)

    c = sar_common.get_db_connection(db)
    # abs_path ПУСТОЙ -- ровно так воркер пишет облачную запись.
    c.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('c1','2026 08 12/DJI_0001.MP4','','video','idle','f1',"
        "datetime('now'), datetime('now'))")
    c.commit()
    c.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_telemetry_cache", {}, raising=False)
    return watch, db


SRT = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-08-12 22:15:21,000,000
[latitude: 39.5] [longitude: 72.9] [rel_alt: 100.0 abs_alt: 4000.0] </font>

"""


def test_cloud_video_finds_srt_in_telemetry_folder(server, monkeypatch):
    """ГЛАВНЫЙ СТРАЖ. Облачное видео + SRT в telemetry/ -> координаты есть.

    До починки здесь был пустой список: стем пустого abs_path не совпадал
    ни с чем, и пометка сохранялась без широты и долготы.
    """
    watch, db = server
    tdir = sar_common.resolve_telemetry_dir(str(watch))
    (tdir / "DJI_0001.SRT").write_text(SRT, encoding="utf-8")
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX",
                        sar_common.build_telemetry_index(tdir), raising=False)

    c = sar_common.get_db_connection(db)
    report = c.execute("SELECT * FROM reports WHERE report_id='c1'").fetchone()
    c.close()

    telemetry = sar_server.get_telemetry_for_report(report)
    assert telemetry, "телеметрия облачного видео не найдена -- баг вернулся"
    found = sar_server.lookup_telemetry(telemetry, 0.0)
    assert found.get("lat") == pytest.approx(39.5)
    assert found.get("lon") == pytest.approx(72.9)


def test_local_video_still_finds_srt_next_to_it(server, monkeypatch):
    """Локальное поведение не сломано: SRT рядом с видео по-прежнему главнее."""
    watch, db = server
    folder = watch / "2026 08 12"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "DJI_0001.MP4").write_bytes(b"x")
    (folder / "DJI_0001.srt").write_text(SRT, encoding="utf-8")
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX",
                        {"by_stem": {}, "by_timestamp": []}, raising=False)

    c = sar_common.get_db_connection(db)
    report = c.execute("SELECT * FROM reports WHERE report_id='c1'").fetchone()
    c.close()
    assert sar_server.get_telemetry_for_report(report), \
        "SRT рядом с видео должен находиться без всякого индекса"


def test_no_telemetry_stays_empty(server, monkeypatch):
    """Нет SRT -- пустой список, а не выдуманные координаты.

    У 68 из 82 облачных видео телеметрии нет и в облаке. Подставить им
    «ближайшую» было бы хуже отсутствия: группу увели бы не туда.
    """
    watch, db = server
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX",
                        {"by_stem": {}, "by_timestamp": []}, raising=False)
    c = sar_common.get_db_connection(db)
    report = c.execute("SELECT * FROM reports WHERE report_id='c1'").fetchone()
    c.close()
    assert sar_server.get_telemetry_for_report(report) == []


def test_server_does_not_read_abs_path(server, monkeypatch):
    """Страж правила проекта: пути ВЫЧИСЛЯЮТСЯ, abs_path не читается.

    Если кто-то вернёт чтение колонки, битое значение снова начнёт рушить
    поиск телеметрии -- молча. Здесь abs_path указывает в несуществующее
    место, а телеметрия обязана найтись всё равно.
    """
    watch, db = server
    c = sar_common.get_db_connection(db)
    c.execute("UPDATE reports SET abs_path='/несуществующий/путь/чужой.MP4' "
              "WHERE report_id='c1'")
    c.commit()
    report = c.execute("SELECT * FROM reports WHERE report_id='c1'").fetchone()
    c.close()

    tdir = sar_common.resolve_telemetry_dir(str(watch))
    (tdir / "DJI_0001.SRT").write_text(SRT, encoding="utf-8")
    monkeypatch.setattr(sar_server, "_TELEMETRY_INDEX",
                        sar_common.build_telemetry_index(tdir), raising=False)

    assert sar_server.get_telemetry_for_report(report), \
        "телеметрию нашли по abs_path вместо вычисленного пути"


# --- 3. скачивание SRT воркером не плодит дубликаты ------------------------

class _FakeFetcher:
    """Заглушка единой двери к байтам: отдаёт заранее готовый файл."""

    def __init__(self, src):
        self.src = src
        self.asked = []

    def ensure_local(self, rel_path, file_id=None, expected_size=0, pin=True):
        self.asked.append(rel_path)
        return self.src

    def release(self, rel_path):
        pass


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    import sar_worker
    watch = tmp_path / "watch"
    watch.mkdir()
    src = tmp_path / "source.SRT"
    src.write_text(SRT, encoding="utf-8")

    monkeypatch.setattr(sar_worker, "WATCH_DIR", str(watch), raising=False)
    monkeypatch.setattr(sar_worker, "CFG", {}, raising=False)
    monkeypatch.setattr(sar_worker, "_telemetry_index", None, raising=False)
    # _forget_missing_telemetry ходит в базу -- здесь она не нужна
    monkeypatch.setattr(sar_worker, "_forget_missing_telemetry",
                        lambda: 0, raising=False)
    fetcher = _FakeFetcher(str(src))
    monkeypatch.setattr(sar_worker, "get_fetcher", lambda: fetcher, raising=False)
    return sar_worker, watch, fetcher


def test_cloud_srt_lands_in_telemetry_folder(worker_env):
    """Скачанный SRT кладётся под ИСХОДНЫМ именем: по нему идёт сопоставление."""
    worker, watch, fetcher = worker_env
    got = worker.fetch_cloud_telemetry(
        [("08 12/DJI_0001.SRT", 1, "fid", 7, None)])
    assert got == 1
    dest = watch / "telemetry" / "DJI_0001.SRT"
    assert dest.exists(), "файл не появился в telemetry/ -- его никто не найдёт"
    assert dest.read_text(encoding="utf-8") == SRT


def test_existing_telemetry_in_subfolder_is_not_downloaded_again(worker_env):
    """СТРАЖ ДУБЛЕЙ. Тот же SRT уже лежит в подпапке -- качать второй незачем.

    telemetry/ сканируется рекурсивно, и на боевой машине телеметрия лежит
    в папке «12.08.2026 Субтитры полетов». Проверка «нет файла ровно по
    конечному пути» скачала бы второй экземпляр, после чего поиск SRT на
    каждом обращении сообщал бы о неоднозначности имени и молча брал один
    из двух -- а на следующем проходе всё повторилось бы снова.
    """
    worker, watch, fetcher = worker_env
    sub = watch / "telemetry" / "Субтитры полетов"
    sub.mkdir(parents=True)
    (sub / "DJI_0001.SRT").write_text(SRT, encoding="utf-8")

    got = worker.fetch_cloud_telemetry(
        [("08 12/DJI_0001.SRT", 1, "fid", 7, None)])
    assert got == 0, "скачали то, что уже лежит в подпапке"
    assert fetcher.asked == [], "к облаку вообще не должны были обращаться"
    assert not (watch / "telemetry" / "DJI_0001.SRT").exists()


def test_fetch_without_cloud_does_nothing(worker_env, monkeypatch):
    """Облако не подключено -- тихо ничего не делаем, а не падаем."""
    worker, watch, _ = worker_env
    monkeypatch.setattr(worker, "get_fetcher", lambda: None, raising=False)
    assert worker.fetch_cloud_telemetry(
        [("08 12/DJI_0001.SRT", 1, "fid", 7, None)]) == 0
