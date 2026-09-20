"""Материал из облака входит в платформу тем же путём, что локальный.

САМОЕ ВАЖНОЕ ЗДЕСЬ -- rel_path. По нему ищется существующая запись в
reports (см. watcher_loop). Если облачный файл получит имя, отличное от
того, под которым он был локально, тот же материал заведётся ВТОРОЙ
записью -- и вся проделанная по нему работа (пометки, обсуждения, отметки
просмотра) останется на первой, невидимой.

На дублях в этом проекте уже обжигались: было 38 видео и 39 фото вместо
34 и 24, покрытие занижалось до 73% вместо 82%. Поэтому перенос материала
в облако С СОХРАНЕНИЕМ СТРУКТУРЫ ПАПОК обязан подхватывать записи, а не
плодить новые.
"""
import pytest

import sar_cloud
import sar_common


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    sar_common.add_cloud_account(c, provider="google", token="t",
                                  root_id="root", label="Диск")
    yield c
    c.close()


def fake_tree(tree):
    """tree: {folder_id: [FileInfo, ...]}"""
    def lister(folder_id):
        return tree.get(folder_id, [])
    return lister


def F(id, name, size=0, folder=False):
    return sar_cloud.FileInfo(id, name, size, is_folder=folder)


# --- обход ----------------------------------------------------------------

def test_flat_folder_is_listed(conn):
    tree = {"root": [F("1", "DJI_0001.MP4", 1000), F("2", "note.txt")]}
    out = sar_common.scan_cloud_materials(conn, list_folder=fake_tree(tree))
    assert [(r[0], r[1]) for r in out] == [("DJI_0001.MP4", "video")]


def test_non_media_is_skipped(conn):
    tree = {"root": [F("1", "readme.md"), F("2", "трек.SRT"), F("3", "a.JPG", 5)]}
    out = sar_common.scan_cloud_materials(conn, list_folder=fake_tree(tree))
    assert [r[0] for r in out] == ["a.JPG"]


def test_extension_case_does_not_matter(conn):
    """Дрон пишет .MP4, а после выгрузки в облако имя может измениться."""
    tree = {"root": [F("1", "a.mp4", 1), F("2", "b.MP4", 1)]}
    out = sar_common.scan_cloud_materials(conn, list_folder=fake_tree(tree))
    assert {r[1] for r in out} == {"video"}


def test_nested_folders_become_the_same_rel_path_as_locally(conn):
    """Ключевое свойство. Локально файл известен как
    'Курумды август 2026/DJI_1.MP4' -- из облака должен прийти он же."""
    tree = {
        "root": [F("f1", "Курумды август 2026", folder=True)],
        "f1": [F("1", "DJI_1.MP4", 100)],
    }
    out = sar_common.scan_cloud_materials(conn, list_folder=fake_tree(tree))
    assert out[0][0] == "Курумды август 2026/DJI_1.MP4"


def test_recursion_is_depth_limited(conn):
    """Человек может указать корень диска со всем накопленным за годы."""
    tree = {"root": [F("d0", "d0", folder=True)]}
    for i in range(12):
        tree["d%d" % i] = [F("d%d" % (i + 1), "d%d" % (i + 1), folder=True),
                           F("v%d" % i, "v%d.mp4" % i, 1)]
    out = sar_common.scan_cloud_materials(conn, list_folder=fake_tree(tree))
    depths = [r[0].count("/") for r in out]
    assert max(depths) <= sar_common.CLOUD_MAX_DEPTH + 1, depths


def test_size_and_ids_are_carried(conn):
    """Без размера нельзя заранее сказать, хватит ли места; без
    идентификатора нечего качать."""
    tree = {"root": [F("файл-1", "a.mp4", 12345)]}
    rel, kind, acc_id, file_id, size, mtime = sar_common.scan_cloud_materials(
        conn, list_folder=fake_tree(tree))[0]
    assert file_id == "файл-1" and size == 12345 and acc_id


# --- отказы ---------------------------------------------------------------

def test_broken_account_does_not_break_the_scan(conn):
    """Ошибка одного хранилища не должна отменять остальные и уж точно не
    должна ронять обход: список материалов -- главный экран платформы."""
    sar_common.add_cloud_account(conn, provider="yandex", token="t2",
                                  root_id="disk:/", label="Второй")

    calls = {"n": 0}

    def flaky(folder_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sar_cloud.CloudError("папка удалена")
        return [F("1", "ok.mp4", 5)]

    out = sar_common.scan_cloud_materials(conn, list_folder=flaky)
    assert [r[0] for r in out] == ["ok.mp4"]


def test_failure_reason_is_recorded_on_the_account(conn):
    """«Почему не видно файлов» должно быть видно в админке, а не только
    в журнале воркера."""
    def boom(folder_id):
        raise sar_cloud.CloudError("нет доступа к папке")

    sar_common.scan_cloud_materials(conn, list_folder=boom)
    acc = sar_common.cloud_accounts_public(conn)[0]
    assert "нет доступа" in (acc["last_error"] or "")


def test_success_clears_the_previous_error(conn):
    """Иначе в админке навсегда останется сообщение о давно починенной
    беде, и на него перестанут обращать внимание."""
    sar_common.update_cloud_account(conn, 1, last_error="старая беда")
    sar_common.scan_cloud_materials(
        conn, list_folder=fake_tree({"root": [F("1", "a.mp4", 1)]}))
    assert not sar_common.cloud_accounts_public(conn)[0]["last_error"]


def test_disabled_account_is_not_scanned(conn):
    sar_common.update_cloud_account(conn, 1, enabled=0)
    out = sar_common.scan_cloud_materials(
        conn, list_folder=fake_tree({"root": [F("1", "a.mp4", 1)]}))
    assert out == []


def test_no_accounts_means_no_work(tmp_path):
    db = str(tmp_path / "x.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    assert sar_common.scan_cloud_materials(c) == []
    c.close()


# --- секреты --------------------------------------------------------------

def test_public_view_never_carries_tokens(conn):
    for acc in sar_common.cloud_accounts_public(conn):
        assert "token" not in acc and "refresh_token" not in acc


def test_worker_view_does_carry_tokens(conn):
    """Воркеру токен нужен -- он качает. Разделение именно поэтому сделано
    двумя функциями, а не фильтром на месте использования: фильтр, который
    надо не забыть применить, рано или поздно забудут."""
    assert sar_common.cloud_accounts(conn)[0]["token"] == "t"


# --- перенос локального материала в облако --------------------------------
#
# Ради этого свойства всё и затевалось. Человек заливает ту же папку в
# Google Диск и удаляет её с ноутбука. Платформа обязана УЗНАТЬ свои файлы,
# а не завести их заново.

def test_moving_material_to_cloud_keeps_the_same_identity(conn, tmp_path):
    """Сценарий целиком: файл был локальным, стал облачным.

    Совпадение ищется по rel_path -- значит запись, а с ней пометки,
    обсуждения и отметки просмотра, остаётся той же самой.
    """
    rel = "Курумды август 2026/DJI_1.MP4"
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES ('было', ?, 'D:/old/DJI_1.MP4', "
        "'video', 'done', datetime('now'), datetime('now'))", (rel,))
    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, "
        "timestamp_sec, bbox, label, created_at) VALUES "
        "('было', 'волонтёр', 10.0, '[0,0,1,1]', 'находка', datetime('now'))")
    conn.commit()

    tree = {
        "root": [F("f1", "Курумды август 2026", folder=True)],
        "f1": [F("cloud-1", "DJI_1.MP4", 999)],
    }
    found = sar_common.scan_cloud_materials(conn, list_folder=fake_tree(tree))
    assert found[0][0] == rel, (
        "облачный файл пришёл под другим именем -- запись не найдётся "
        "по rel_path и материал заведётся дублем")

    row = conn.execute("SELECT report_id FROM reports WHERE rel_path=?",
                       (rel,)).fetchone()
    assert row["report_id"] == "было"

    n = conn.execute(
        "SELECT COUNT(*) c FROM manual_observations WHERE report_id='было'"
    ).fetchone()["c"]
    assert n == 1, "пометка потерялась вместе со старой записью"


def test_cloud_file_id_can_change_without_creating_a_duplicate(conn):
    """Файл перезалили -- идентификатор в облаке сменился, материал тот же.

    Привязка к file_id вместо rel_path дала бы вторую запись при каждой
    перезаливке.
    """
    rel = "a.mp4"
    first = sar_common.scan_cloud_materials(
        conn, list_folder=fake_tree({"root": [F("id-1", rel, 5)]}))
    second = sar_common.scan_cloud_materials(
        conn, list_folder=fake_tree({"root": [F("id-2", rel, 5)]}))
    assert first[0][0] == second[0][0] == rel
    assert first[0][3] != second[0][3], "идентификаторы должны различаться"


# --- файл без локального пути ---------------------------------------------
#
# Найдено на боевом подключении. У материала из облака локального пути нет
# вовсе, и make_report_id падал на попытке узнать дату создания: его
# собственный запасной путь бросал ровно то же исключение, что и основной.
#
# Дальше это роняло ВЕСЬ проход наблюдения -- не регистрировался ни один
# материал, ни облачный, ни локальный, а в журнале была одна строка без
# видимых последствий. Платформа выглядела работающей и просто не замечала
# новые файлы.

def test_report_id_for_a_file_that_is_not_on_disk():
    """Ровно случай облачного материала."""
    rid = sar_common.make_report_id("Оп/DJI_1.MP4", "Оп/DJI_1.MP4")
    assert rid.startswith("DJI_1__")


def test_report_id_is_stable_for_the_same_missing_file():
    """Нестабильный идентификатор означал бы новую запись на каждом
    проходе -- то есть бесконечное размножение материала."""
    a = sar_common.make_report_id("Оп/DJI_1.MP4", "Оп/DJI_1.MP4")
    b = sar_common.make_report_id("Оп/DJI_1.MP4", "Оп/DJI_1.MP4")
    assert a == b


def test_date_helper_is_the_only_one():
    """Страж. Вторая копия расчёта даты уже существовала и разошлась с
    первой: get_file_ctime починили, а make_report_id -- нет."""
    import inspect
    src = inspect.getsource(sar_common.make_report_id)
    assert "get_file_ctime" in src
    # Ищем ВЫЗОВ, а не слово: в объяснении рядом оно упоминается законно.
    assert "os.path.getmtime(" not in src, (
        "в make_report_id снова заведён собственный расчёт даты")
    assert "os.path.getctime(" not in src


def test_cloud_scan_failure_does_not_abort_the_whole_pass():
    """Страж на изоляцию. Облачный обход обязан жить в своём try: деля его
    с локальным, он отменял регистрацию вообще всего материала."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
           ).read_text(encoding="utf-8")
    start = src.index("def watcher_loop")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert "[облако] обход не удался" in body, (
        "у облачного обхода нет своего обработчика ошибок")
    # локальный обход отчитывается своим сообщением -- значит их два
    assert "[watcher] ошибка сканирования" in body


# --- дата файла из облака -------------------------------------------------

def test_cloud_date_is_carried(conn):
    """Без даты 150 облачных файлов сваливаются в конец списка одной
    кучей, за всем локальным материалом."""
    tree = {"root": [sar_cloud.FileInfo("1", "a.mp4", 5,
                                         modified="2026-08-15T10:00:00.000Z")]}
    rec = sar_common.scan_cloud_materials(conn, list_folder=fake_tree(tree))[0]
    assert rec[5] > 0


def test_google_and_yandex_date_formats_both_parse():
    """Google отдаёт ...Z, Яндекс -- ...+00:00."""
    g = sar_common.cloud_timestamp("2026-08-15T10:00:00.000Z")
    y = sar_common.cloud_timestamp("2026-08-15T10:00:00+00:00")
    assert g > 0 and y > 0 and abs(g - y) < 1


def test_unparsable_date_is_zero_not_invented():
    """Ноль честнее выдуманного значения: файл просто встанет в конец."""
    assert sar_common.cloud_timestamp("не дата") == 0.0
    assert sar_common.cloud_timestamp(None) == 0.0
