"""База не должна быть привязана к машине, диску и операционной системе.

В таблице reports есть столбцы abs_path и out_dir с АБСОЛЮТНЫМИ путями.
Они записаны на той машине, где файл впервые увидели. Пока всё живёт на
D: одного ноутбука, это незаметно. Но платформе предстоят три переезда:

  * материал уезжает в облако (локально его больше не держим),
  * служебные данные отделяются от материала на другой диск,
  * сама платформа переезжает на VPS под Linux.

После любого из них все 58 записей боевой базы превращаются в ссылки в
никуда -- причём МОЛЧА: строка в базе есть, файла по ней нет, страница
отдаёт 404 или пустой список.

Поэтому пути вычисляются от текущих корней (sar_common.material_path и
report_dir), а столбцы остаются в схеме только ради внешних разовых
скриптов. Здесь проверяется, что платформа их действительно НЕ ЧИТАЕТ.

Проверено на боевой базе перед переключением: расчёт совпал с хранимым
значением в 58 записях из 58, а два файла, которых расчёт не нашёл,
отсутствовали и по хранимому пути тоже -- то есть их просто нет на диске.
"""
import json
import os
import pathlib
import sqlite3

import pytest

import sar_common
import sar_server


# Путь с ЧУЖОЙ машины: другая буква диска, виндовые разделители. Ровно то,
# что окажется в базе после переезда на Linux или на другой диск.
ALIEN_ABS = r"Z:\old-laptop\material\video.mp4"
ALIEN_OUT = r"Z:\old-laptop\sar_data\reports\rep1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "video.mp4").write_bytes(b"x")

    _, _, _, reports_dir = sar_common.resolve_paths(str(watch))
    out = pathlib.Path(reports_dir) / "rep1"
    out.mkdir(parents=True, exist_ok=True)

    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "out_dir, fps, file_ctime, created_at, updated_at) "
        "VALUES ('rep1', 'video.mp4', ?, 'video', 'done', ?, 30.0, 0.0, "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')", (ALIEN_ABS, ALIEN_OUT))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db, raising=False)
    monkeypatch.setattr(sar_server, "SERVER_CFG",
                        {"watch_dir": str(watch), "shared_password": "pw"},
                        raising=False)
    monkeypatch.setattr(sar_server, "_PATH_ROOTS_CACHE", {}, raising=False)
    monkeypatch.setattr(sar_server, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(sar_server, "_ai_scenes_cache", {}, raising=False)
    monkeypatch.setattr(sar_server, "SCRIPT_DIR", "/no/such/config/dir")
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    client = sar_server.app.test_client()
    with client.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "tester"
    return client, str(watch), out


# --- сами функции ---------------------------------------------------------

def test_material_path_splits_on_forward_slash():
    """rel_path хранится с прямыми слэшами независимо от системы."""
    p = sar_common.material_path("/srv/watch", "Курумды 2026/DJI_1.MP4")
    assert p == os.path.join("/srv/watch", "Курумды 2026", "DJI_1.MP4")


def test_material_path_survives_backslashes_in_rel_path():
    """Записи, сделанные на Windows, могли сохранить обратные слэши.
    На Linux os.path.join склеил бы такую строку в ОДИН сегмент."""
    p = sar_common.material_path("/srv/watch", "Курумды 2026\\DJI_1.MP4")
    assert p == os.path.join("/srv/watch", "Курумды 2026", "DJI_1.MP4")


def test_material_path_ignores_empty_rel_path():
    assert sar_common.material_path("/srv/watch", None) == "/srv/watch"
    assert sar_common.material_path("/srv/watch", "") == "/srv/watch"


def test_report_dir_is_reports_root_plus_id():
    assert (sar_common.report_dir("/srv/data/reports", "rep1")
            == os.path.join("/srv/data/reports", "rep1"))


# --- платформа не читает хранимые пути ------------------------------------

def test_row_reports_derived_path_not_the_stored_one(env):
    """Главное свойство: строка отдаёт путь от ТЕКУЩИХ корней."""
    client, watch, _ = env
    with sar_server.app.test_request_context():
        row = sar_server.get_report_row("rep1")
    assert row["abs_path"] == os.path.join(watch, "video.mp4")
    assert row["abs_path"] != ALIEN_ABS, "путь взят из базы, а не вычислен"
    assert row["out_dir"] != ALIEN_OUT


def test_video_is_served_although_stored_path_points_at_another_machine(env):
    """Это и есть переезд: в базе путь с чужого диска, файл лежит здесь."""
    client, _, _ = env
    assert client.get("/report/rep1/video").status_code == 200


def test_player_opens_with_alien_stored_path(env):
    client, _, _ = env
    r = client.get("/report/rep1/player/")
    assert r.status_code == 200


def test_scenes_are_found_in_the_derived_report_dir(env):
    """detections.json лежит там, куда указывает РАСЧЁТ, а не столбец
    out_dir. Иначе после переезда список сцен молча опустеет -- страница
    откроется, просто окажется пустой, и никто не поймёт почему."""
    client, _, out = env
    hits = [{"frame_idx": 0, "timestamp": "0", "seconds": 0.0, "confidence": 0.5,
             "object_class": "person", "source": "model",
             "bbox": [10, 10, 20, 20], "lat": None, "lon": None, "alt": None,
             "image_path": "crops/c0.jpg", "full_image_path": None,
             "group_id": ""}]
    (out / "detections.json").write_text(json.dumps(hits), encoding="utf-8")
    scenes = client.get("/api/report/rep1/ai_scenes").get_json()
    assert len(scenes) == 1


def test_report_page_reads_the_derived_directory(env):
    client, _, out = env
    (out / "report.html").write_text("<html>отчёт</html>", encoding="utf-8")
    r = client.get("/report/rep1/")
    assert r.status_code == 200
    assert "отчёт" in r.get_data(as_text=True)


# --- воркер ---------------------------------------------------------------

def test_worker_computes_paths_too():
    """Воркер запускает детектор по вычисленному пути: если бы он брал
    abs_path из базы, после переезда обработка падала бы на каждом файле."""
    src = pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
    body = src.read_text(encoding="utf-8")
    start = body.index("def run_one_report")
    end = body.index(chr(10) + "def ", start + 10)
    chunk = body[start:end]
    assert "material_path" in chunk and "report_dir" in chunk, (
        "воркер снова берёт путь из базы")
    assert '"--video", src' in chunk and '"--photo", src' in chunk


def test_nobody_reads_the_stored_columns_anymore():
    """Страж. Столбцы остаются в схеме ради внешних скриптов, но платформа
    читать их не должна -- иначе привязка к машине вернётся незаметно."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("sar_worker.py",):
        body = (root / name).read_text(encoding="utf-8")
        for bad in ('["abs_path"]', '["out_dir"]'):
            assert bad not in body, f"{name} снова читает {bad} из базы"


def test_tree_counts_scenes_from_the_derived_dir_too(env):
    """/api/tree передаёт СЫРУЮ строку из базы, мимо get_report_row.

    Этот тест добавлен после проверки мутацией: возврат к чтению столбца
    out_dir внутри подсчёта сцен НЕ ловился ни одним тестом, потому что
    все они шли через get_report_row, где путь уже перекрыт. А список
    файлов ходит другим путём -- и молча показал бы "0 находок" по всем
    материалам после переезда.
    """
    client, _, out = env
    hits = [{"frame_idx": 0, "timestamp": "0", "seconds": 0.0, "confidence": 0.5,
             "object_class": "person", "source": "model",
             "bbox": [10, 10, 20, 20], "lat": None, "lon": None, "alt": None,
             "image_path": "crops/c0.jpg", "full_image_path": None,
             "group_id": ""}]
    (out / "detections.json").write_text(json.dumps(hits), encoding="utf-8")

    items = client.get("/api/tree").get_json()["items"]
    item = next(i for i in items if i["report_id"] == "rep1")
    assert item["ai_count"] == 1, (
        "список файлов считает сцены по хранимому пути -- после переезда "
        "он молча показал бы ноль находок у всего материала")
