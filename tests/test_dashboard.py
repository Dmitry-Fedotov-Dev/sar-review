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


def test_error_rate_shows_zero_when_there_are_no_errors(dash):
    """Обратная сторона правила «пустое не подменяем нулём».

    Панель доли ошибок молчала на живом проде. Причина: пятисоток не было
    вовсе, sum() по пустому набору даёт пустоту, а деление пустоты на что
    угодно -- снова пустота. Получалось, что «ошибок нет» и «данных нет»
    выглядят одинаково, хотя это противоположные новости: первое -- всё
    хорошо, второе -- мониторинг сломан.

    Здесь ноль честный: запросы шли, ошибок среди них не было. Поэтому у
    числителя обязана быть привязка к реальному трафику -- она даёт 0 при
    живом трафике без ошибок и оставляет панель пустой, когда трафика нет
    совсем.
    """
    p = next(x for x in dash["panels"] if x["title"] == "Доля ошибок")
    expr = p["targets"][0]["expr"]
    assert " or " in expr, (
        "у числителя нет запасного нуля: при отсутствии ошибок панель "
        "замолчит вместо того, чтобы показать 0%")
    assert "0 *" in expr, (
        "запасной ноль должен быть привязан к трафику (0 * <трафик>), "
        "иначе панель покажет 0% даже когда метрик нет вовсе")


def test_health_check_is_excluded_from_the_error_rate(dash):
    """Проверка здоровья отвечает 503, когда что-то не так -- это её штатный
    способ сказать «плохо», а не сбой сервера. Считать её в ошибки значит
    зажигать график каждый раз, когда воркер молчит пару минут."""
    p = next(x for x in dash["panels"] if x["title"] == "Доля ошибок")
    assert 'endpoint!="healthz"' in p["targets"][0]["expr"]


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
    # Один запрос учитываем намеренно: метрики нагрузки появляются только
    # после первого обслуженного запроса. Без этого проверка имён метрик
    # запросов зависела бы от того, ходил ли кто-то в тестовый клиент в
    # предыдущих файлах тестов -- при запуске этого файла в одиночку она
    # падала, а в полном прогоне проходила случайно.
    sar_health.record_request("healthz", "GET", 200, 0.01)
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
    p = next(x for x in dash["panels"]
             if x.get("type") == "stat" and "нлайн" in x["title"])
    exprs = [t["expr"] for t in p["targets"]]
    assert exprs == ["sar_viewers_online"], exprs


def test_stat_tiles_show_a_number_and_not_a_sparkline(dash):
    """Плашка отвечает на вопрос «сколько сейчас», тренд -- дело графика.

    Фон-спарклайн на этих плашках либо дублировал полноценный график,
    который стоит ниже на этом же дашборде (место на диске, запросы в
    секунду, время ответа, ошибки), либо рисовал бессмыслицу: возраст
    heartbeat -- это пила, сбрасывающаяся каждые несколько секунд, а время
    без перезапуска -- прямая, которая всегда растёт. Из обоих нельзя
    прочитать ничего, но они занимают всю плашку и мешают увидеть цифру.
    """
    for p in dash["panels"]:
        if p.get("type") != "stat":
            continue
        assert p["options"].get("graphMode") == "none", (
            f"на плашке «{p['title']}» фоновый график: он либо повторяет "
            "полный график ниже, либо не читается вовсе")


def test_online_has_a_history_chart_and_not_only_a_number(dash):
    """Пробел, который вылез на разборе нагрузки.

    Индикатор «N онлайн» показывал только текущее значение и нигде его не
    хранил. Из-за этого на вопрос «сколько людей было одновременно в ночь
    с 15 на 16» пришлось отвечать по памяти людей: в данных ответа не
    было. Цифры «сейчас» недостаточно -- нужен ряд во времени.
    """
    hist = [p for p in dash["panels"]
            if p.get("type") == "timeseries"
            and any("sar_viewers_online" in t.get("expr", "")
                    for t in p.get("targets", []))]
    assert hist, "нет графика истории онлайна -- только текущее значение"

    exprs = " ".join(t["expr"] for t in hist[0]["targets"])
    assert "max_over_time" in exprs, (
        "нет пика за окно: на длинном интервале Grafana прореживает точки, "
        "и короткий всплеск между ними просто исчезнет с графика")


def test_online_panels_warn_that_old_data_is_understated(dash):
    """Данные тоже бывают сломаны, и об этом надо предупреждать.

    До 21 августа 2026 heartbeat слали только три страницы -- список
    файлов, страница обработки и плеер. Страницы операций его не слали, а
    после входа человек попадает именно туда. Значит вся история онлайна
    левее этой даты занижена: видны только те, кто открыл плеер.

    Само по себе это уже не чинится -- прошлое не перепишешь. Но график
    без оговорки читается как «на платформе никого не было», а это
    неправда, и именно на такие провалы смотрят и делают выводы.
    """
    for p in dash["panels"]:
        if "нлайн" not in p["title"]:
            continue
        desc = p.get("description", "")
        assert "занижен" in desc.lower() or "заниж" in desc.lower(), (
            f"панель «{p['title']}»: нет предупреждения о том, что старые "
            "данные занижены")
        assert "21 август" in desc, (
            f"панель «{p['title']}»: не указано, с какого момента данные "
            "стали верными -- без даты предупреждение бесполезно")


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


def test_dashboard_opens_on_the_current_state(dash):
    """Дашборд открывается на коротком окне: его смотрят, чтобы понять,
    что происходит СЕЙЧАС -- жив ли воркер, идёт ли нагрузка, кто
    онлайн."""
    assert dash["time"]["from"] == "now-5m"
    assert dash["time"]["to"] == "now"


def test_refresh_is_not_slower_than_the_window(dash):
    """На пятиминутном окне обновление раз в минуту означало бы, что треть
    экрана всегда устарела."""
    assert dash["refresh"] in ("5s", "10s", "30s")


def test_longer_ranges_are_one_click_away(dash):
    """Половина панелей -- про сутки: сколько отсмотрено, как менялись
    статусы файлов. Без быстрого возврата короткое окно из удобства
    превращается в ловушку."""
    options = dash.get("timepicker", {}).get("time_options", [])
    assert "24h" in options, "нет быстрого возврата к суткам"
    assert "1h" in options
