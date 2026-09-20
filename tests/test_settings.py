"""Настройки, меняемые на ходу через админку.

ПОЧЕМУ В БАЗЕ, А НЕ В КОНФИГЕ. Меняет их администратор через веб-страницу,
то есть СЕРВЕР. А применяет воркер: это он читает файлы и качает материал.
Сервер и воркер -- разные процессы, которые по устройству проекта общаются
ТОЛЬКО через базу. Запиши сервер новое значение в sar_config.json -- воркер
узнает о нём после перезапуска, а качать продолжит по-старому. Отказ был бы
молчаливым: форма приняла, значение показывается, поведение прежнее.

ПОЧЕМУ ГРАНИЦЫ ОБЯЗАТЕЛЬНЫ. Настройка без границ -- это способ выстрелить
себе в ногу через веб-форму. «Загрузок 50» положит канал, квоту облака и
саму машину, на которой лежит база операции.
"""
import pytest

import sar_common


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


# --- реестр ---------------------------------------------------------------

def test_every_setting_is_fully_described():
    """Неполное описание means страница админки покажет параметр без
    подписи или без границ -- то есть человек не поймёт, что вводит."""
    for key, spec in sar_common.SETTINGS_SCHEMA.items():
        assert spec.get("label"), f"{key} без названия"
        assert spec.get("help"), f"{key} без пояснения"
        assert "default" in spec, f"{key} без умолчания"
        assert spec["type"] in ("int", "float", "bool", "choice"), key
        if spec["type"] in ("int", "float"):
            assert "min" in spec and "max" in spec, f"{key} без границ"
            assert spec["min"] <= spec["default"] <= spec["max"], key
        if spec["type"] == "choice":
            # Выбор без вариантов -- пустой список в форме: человек видит
            # поле, которое ничего не предлагает.
            assert spec.get("options"), f"{key} без вариантов"
            values = [o["value"] for o in spec["options"]]
            assert spec["default"] in values, f"{key}: умолчания нет в списке"
            for o in spec["options"]:
                assert o.get("label"), f"{key}: вариант без названия"


def test_defaults_are_the_safe_end_of_the_range():
    """Умолчание должно быть осторожным: платформа работает в поле, и
    незнакомый параметр трогать никто не будет."""
    assert sar_common.SETTINGS_SCHEMA["downloads_in_flight"]["default"] == 1


# --- зажим значений -------------------------------------------------------

def test_value_above_maximum_is_clamped_not_rejected():
    """Зажимаем, а не отвергаем: администратор увидит в форме 4 и поймёт,
    что это потолок. Отказ заставил бы гадать, что допустимо."""
    assert sar_common._coerce_setting("downloads_in_flight", 50) == 4


def test_value_below_minimum_is_clamped():
    """Ноль загрузок -- это остановка платформы через форму настроек."""
    assert sar_common._coerce_setting("downloads_in_flight", 0) == 1


def test_garbage_falls_back_to_default():
    assert (sar_common._coerce_setting("staging_cap_gb", "абв")
            == sar_common.SETTINGS_SCHEMA["staging_cap_gb"]["default"])


def test_booleans_understand_human_input():
    for yes in (True, "1", "true", "on", "да", "YES"):
        assert sar_common._coerce_setting("auto_process", yes) is True
    for no in (False, "0", "false", "off", "нет", ""):
        assert sar_common._coerce_setting("auto_process", no) is False


# --- чтение и запись ------------------------------------------------------

def test_defaults_are_returned_when_nothing_is_saved(conn):
    s = sar_common.get_settings(conn)
    assert s["downloads_in_flight"] == 1
    assert s["auto_process"] is True


def test_saved_value_overrides_the_default(conn):
    sar_common.set_setting(conn, "downloads_in_flight", 3, who="админ")
    assert sar_common.get_settings(conn)["downloads_in_flight"] == 3


def test_saving_clamps_before_storing(conn):
    """Опасное значение не должно попасть в базу даже на секунду: его мог
    бы прочитать воркер между записью и исправлением."""
    assert sar_common.set_setting(conn, "downloads_in_flight", 99) == 4
    assert sar_common.get_settings(conn)["downloads_in_flight"] == 4


def test_boolean_survives_the_round_trip(conn):
    """Хранится всё текстом: "False" -- непустая строка, и наивное чтение
    превратило бы выключенное в включённое."""
    sar_common.set_setting(conn, "auto_process", False)
    assert sar_common.get_settings(conn)["auto_process"] is False
    sar_common.set_setting(conn, "auto_process", True)
    assert sar_common.get_settings(conn)["auto_process"] is True


def test_unknown_key_is_refused(conn):
    with pytest.raises(KeyError):
        sar_common.set_setting(conn, "выключить_всё", 1)


def test_stale_key_in_db_does_not_break_reading(conn):
    """Настройку убрали из кода, строка осталась. Ронять на этом выдачу
    нельзя -- иначе обновление кода кладёт платформу."""
    conn.execute("INSERT INTO settings (key, value) VALUES ('древний', 'x')")
    conn.commit()
    s = sar_common.get_settings(conn)
    assert "древний" not in s
    assert s["downloads_in_flight"] == 1


def test_author_and_time_are_recorded(conn):
    """Спросить «кто поставил суточный лимит в 1 ГБ» должно быть у кого."""
    sar_common.set_setting(conn, "daily_traffic_gb", 5, who="Дмитрий")
    row = conn.execute(
        "SELECT set_by, set_at FROM settings WHERE key='daily_traffic_gb'").fetchone()
    assert row["set_by"] == "Дмитрий"
    assert row["set_at"]


def test_table_survives_being_created_twice(tmp_path):
    """init_db зовут оба процесса в любом порядке -- это уже часть
    контракта проекта."""
    db = str(tmp_path / "x.db")
    sar_common.init_db(db)
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    sar_common.set_setting(c, "downloads_in_flight", 2)
    sar_common.init_db(db)
    assert sar_common.get_settings(c)["downloads_in_flight"] == 2, (
        "повторная миграция затёрла настройки"
    )
    c.close()


# --- выбор из вариантов ---------------------------------------------------

def test_choice_values_round_trip(conn):
    sar_common.set_setting(conn, "material_sources", "cloud")
    assert sar_common.get_settings(conn)["material_sources"] == "cloud"


def test_unknown_choice_falls_back_to_default():
    """Значение приходит из списка, который платформа сама и отдала.
    Несовпадение означает устаревшую вкладку -- отвергать нечего."""
    assert sar_common._coerce_setting("material_sources", "выдумка") == "all"
    assert sar_common._coerce_setting("material_sources", None) == "all"


def test_every_source_mode_is_reachable():
    values = {o["value"] for o
              in sar_common.SETTINGS_SCHEMA["material_sources"]["options"]}
    assert values == {"all", "local", "cloud"}
