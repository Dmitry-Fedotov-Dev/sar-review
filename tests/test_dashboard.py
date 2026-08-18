"""Тесты дашборда Grafana.

Дашборд -- такой же код, как остальное: он разложен файлом и ломается
молча. Проверять его глазами не годится, потому что сломанная панель
выглядит как панель с нулём, а не как ошибка. Ровно так и случилось:
"Онлайн сейчас" показывал 0 при одном живом зрителе, и это заметил
пользователь, а не тест.

Здесь закрыты три причины той поломки:

  * ссылка на датасорс через переменную ${DS_PROMETHEUS}, которая не
    разрешалась в реальный uid -- панели оставались без данных;
  * noValue: "0" на панели, превращавший ОТСУТСТВИЕ данных в правдоподобный
    ноль. Это та же ошибка, из-за которой возраст бэкапа нельзя отдавать
    нулём: time() - 0 рисует полвека, а online: 0 рисует пустую платформу;
  * неполные reduceOptions у stat-панелей.

Плюс сверка запросов дашборда с тем, что платформа действительно отдаёт:
опечатка в имени метрики иначе обнаружится только на живом экране.
"""
import json
import os
import re

import pytest

import sar_common
import sar_health

DASH = os.path.join("monitoring", "grafana", "provisioning", "dashboards",
                     "sar-health.json")
DS_YML = os.path.join("monitoring", "grafana", "provisioning", "datasources",
                       "datasources.yml")


@pytest.fixture(scope="module")
def dash():
    with open(DASH, encoding="utf-8") as f:
        return json.load(f)


def test_dashboard_is_valid_json(dash):
    assert dash["uid"] and dash["title"]
    assert dash["panels"], "дашборд без панелей"


def test_every_panel_points_at_a_concrete_datasource(dash):
    """Регрессия: панели ссылались на переменную ${DS_PROMETHEUS}, которая
    не разрешалась в uid, и молча оставались без данных."""
    for p in dash["panels"]:
        ds = p.get("datasource")
        assert ds, f"панель «{p['title']}» без датасорса"
        uid = ds.get("uid", "")
        assert not uid.startswith("$"), (
            f"панель «{p['title']}» ссылается на переменную {uid} -- "
            "она не разрешается в реальный uid")


def test_datasource_uid_matches_provisioning(dash):
    """uid в дашборде и в настройках датасорса обязаны совпадать, иначе
    после переустановки панели окажутся пустыми."""
    with open(DS_YML, encoding="utf-8") as f:
        m = re.search(r"^\s*uid:\s*(\S+)", f.read(), re.M)
    assert m, "в datasources.yml не задан явный uid"
    provisioned = m.group(1)
    for p in dash["panels"]:
        assert p["datasource"]["uid"] == provisioned, (
            f"панель «{p['title']}» смотрит на {p['datasource']['uid']}, "
            f"а датасорс называется {provisioned}")


def test_no_panel_disguises_missing_data_as_zero(dash):
    """Главный урок этой поломки.

    Пустота обязана выглядеть пустотой. Ноль на панели «онлайн» означает
    «платформа никому не нужна», а не «данные не пришли» -- и на такой
    ноль смотрят и делают выводы."""
    for p in dash["panels"]:
        nv = p.get("fieldConfig", {}).get("defaults", {}).get("noValue")
        assert nv not in ("0", 0), (
            f"панель «{p['title']}» показывает отсутствие данных как ноль")


def test_stat_panels_have_complete_reduce_options(dash):
    for p in dash["panels"]:
        if p.get("type") != "stat":
            continue
        ro = p.get("options", {}).get("reduceOptions", {})
        assert ro.get("calcs"), f"«{p['title']}»: не задано, что показывать"
        assert "fields" in ro and "values" in ro, (
            f"«{p['title']}»: reduceOptions неполны, поведение непредсказуемо")


def test_dashboard_queries_only_metrics_the_platform_exports(dash, tmp_path):
    """Опечатка в имени метрики иначе обнаружится только на живом экране."""
    db = str(tmp_path / "t.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    facts = sar_health.collect(conn, ".")
    text = sar_health.render_prometheus(facts, sar_health.evaluate(facts))
    conn.close()

    exported = {ln.split("{")[0].split(" ")[0]
                for ln in text.split("\n") if ln and not ln.startswith("#")}
    # На ПУСТОЙ базе часть метрик законно отсутствует -- их нечему породить:
    # нет отчётов, воркер не отмечался, фоновая проба железа ещё не сделана,
    # резервных копий нет, ветка стримов не влита. Отсутствие здесь -- это
    # правильное поведение (см. правило "пустое не подменяем нулём"), поэтому
    # перечисляем их явно, а не ослабляем проверку.
    exported |= {"sar_cpu_percent", "sar_cpu_cores",
                 "sar_memory_total_bytes", "sar_memory_used_bytes",
                 "sar_memory_percent", "sar_reports_bytes",
                 "sar_backup_age_seconds", "sar_streams_active",
                 "sar_reports", "sar_worker_heartbeat_age_seconds"}

    used = set()
    for p in dash["panels"]:
        for t in p.get("targets", []):
            used |= set(re.findall(r"\bsar_[a-z_]+\b", t.get("expr", "")))

    missing = used - exported
    assert not missing, f"дашборд просит метрики, которых нет: {sorted(missing)}"


def test_online_panel_shows_the_right_metric(dash):
    p = next(x for x in dash["panels"] if "нлайн" in x["title"])
    exprs = [t["expr"] for t in p["targets"]]
    assert exprs == ["sar_viewers_online"], exprs
    assert p["options"]["graphMode"] == "none", "просили просто цифру"


def test_hardware_panels_show_the_ceiling(dash):
    """Без потолка проценты не читаются: 80% на двух ядрах и на двадцати --
    разные новости."""
    cpu = next(x for x in dash["panels"] if x["title"] == "Процессор")
    assert any("sar_cpu_cores" in t["expr"] for t in cpu["targets"])
    ram = next(x for x in dash["panels"] if "амять" in x["title"])
    assert any("sar_memory_total_bytes" in t["expr"] for t in ram["targets"])


def test_panels_do_not_overlap(dash):
    """Наложение панелей в Grafana не ошибка, а молча кривая вёрстка."""
    cells = {}
    for p in dash["panels"]:
        g = p["gridPos"]
        for y in range(g["y"], g["y"] + g["h"]):
            for x in range(g["x"], g["x"] + g["w"]):
                prev = cells.get((x, y))
                assert prev is None, (
                    f"«{p['title']}» накладывается на «{prev}» в ({x},{y})")
                cells[(x, y)] = p["title"]


def test_panels_fit_the_grid(dash):
    """Ширина сетки Grafana -- 24 колонки."""
    for p in dash["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24, f"«{p['title']}» выходит за сетку"
