"""Метрики расхода на облако.

Без них исчерпание квоты выглядит как «платформа странно тормозит» -- и
это ровно тот молчаливый отказ, который в этом проекте повторяется чаще
всего. Панель ошибок однажды молчала не потому, что ошибок не было.

Отдельная тонкость: ПУСТОЙ ВЕКТОР В PROMQL ДАЁТ ПУСТУЮ ПАНЕЛЬ, А НЕ НОЛЬ.
Поэтому метрики расхода отдаются ВСЕГДА, даже когда облако не подключено:
иначе «ноль скачано» и «мы перестали считать» выглядят на дашборде
одинаково.
"""
import os

import pytest

import sar_common
import sar_health
import sar_staging


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    conn = sar_common.get_db_connection(db)
    data = tmp_path / "data"
    reports = data / "reports"
    reports.mkdir(parents=True)
    yield conn, str(tmp_path / "watch"), str(reports), str(data)
    conn.close()


def facts_for(env):
    conn, watch, reports, _ = env
    os.makedirs(watch, exist_ok=True)
    return sar_health.collect(conn, watch, reports_dir=reports)


# --- всегда есть ----------------------------------------------------------

def test_metrics_exist_even_without_any_cloud(env):
    """Пустой вектор в PromQL -- это пустая панель, а не ноль."""
    f = facts_for(env)
    for key in ("staging_bytes", "staging_files", "cloud_accounts",
                "cloud_accounts_failing", "cloud_materials"):
        assert key in f, key
        assert f[key] == 0


def test_metrics_are_rendered_for_prometheus(env):
    f = facts_for(env)
    text = sar_health.render_prometheus(f, sar_health.evaluate(f))
    for name in ("sar_staging_bytes", "sar_staging_files",
                 "sar_cloud_accounts", "sar_cloud_accounts_failing",
                 "sar_cloud_materials"):
        assert "\n%s " % name in text, name


def test_every_metric_has_help_text(env):
    """Метрика без пояснения бесполезна тому, кто не писал этот код."""
    f = facts_for(env)
    text = sar_health.render_prometheus(f, sar_health.evaluate(f))
    for name in ("sar_staging_bytes", "sar_cloud_accounts_failing"):
        assert "# HELP %s " % name in text, name


# --- считают правду -------------------------------------------------------

def test_staging_size_is_measured(env):
    conn, watch, reports, data = env
    st = sar_staging.Staging(sar_staging.staging_dir(data), cap_bytes=10 ** 9)
    p = st.path_for("оп/видео.mp4")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(b"x" * 4096)
    f = facts_for(env)
    assert f["staging_bytes"] == 4096
    assert f["staging_files"] == 1


def test_connected_accounts_are_counted(env):
    conn, _, _, _ = env
    sar_common.add_cloud_account(conn, provider="google", token="t")
    assert facts_for(env)["cloud_accounts"] == 1


def test_disabled_account_is_not_counted_as_working(env):
    conn, _, _, _ = env
    acc = sar_common.add_cloud_account(conn, provider="google", token="t")
    sar_common.update_cloud_account(conn, acc, enabled=0)
    assert facts_for(env)["cloud_accounts"] == 0


def test_failing_accounts_are_counted_separately(env):
    """«Подключено два хранилища» и «одно из них не отвечает» -- разные
    новости, и вторая важнее."""
    conn, _, _, _ = env
    a1 = sar_common.add_cloud_account(conn, provider="google", token="t")
    sar_common.add_cloud_account(conn, provider="yandex", token="t2")
    sar_common.update_cloud_account(conn, a1, last_error="нет доступа")
    f = facts_for(env)
    assert f["cloud_accounts"] == 2
    assert f["cloud_accounts_failing"] == 1


def test_cloud_materials_are_counted(env):
    conn, _, _, _ = env
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "cloud_file_id, created_at, updated_at) VALUES "
        "('r1','a.mp4','','video','idle','cloud-1',datetime('now'),datetime('now'))")
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, "
        "created_at, updated_at) VALUES "
        "('r2','b.mp4','D:/b.mp4','video','done',datetime('now'),datetime('now'))")
    conn.commit()
    assert facts_for(env)["cloud_materials"] == 1


# --- устойчивость ---------------------------------------------------------

def test_collection_survives_a_missing_staging_folder(env):
    """Облако не подключали -- папки нет. Это норма, а не ошибка."""
    assert facts_for(env)["staging_bytes"] == 0


def test_metrics_do_not_leak_tokens(env):
    """Токен в метриках означает токен в Grafana, в скриншотах панели и в
    любом, кто может читать /metrics."""
    conn, _, _, _ = env
    sar_common.add_cloud_account(conn, provider="google",
                                 token="секретный-токен-abc")
    f = facts_for(env)
    text = sar_health.render_prometheus(f, sar_health.evaluate(f))
    assert "секретный-токен-abc" not in text
