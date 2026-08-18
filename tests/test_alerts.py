"""Тесты тревог о недоступности платформы.

Тревога ценна ровно до тех пор, пока на неё реагируют. Поэтому тесты бьют
не в "сообщение отправилось", а в три свойства, нарушение любого из которых
превращает алерты в фон:

  * одна неудачная проверка НЕ поднимает тревогу -- сетевой чих, перезапуск
    сервера и моргнувший туннель не должны будить человека;
  * о повторяющемся состоянии сообщается ОДИН раз, а не каждую минуту;
  * восстановление сообщается только тем, кому сообщили о падении, и
    переживает перезапуск бота.

Отдельно проверяется, что 503 не считается падением: /healthz отдаёт его,
когда сервер ЖИВ и честно сообщает о проблеме. Тревога о недоступности там,
где сервис отвечает, обесценивает саму тревогу.
"""
from datetime import datetime, timedelta

import pytest

import sar_alerts as al
import sar_common


T0 = datetime(2026, 8, 18, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "sar_data.db")
    sar_common.init_db(db)
    c = sar_common.get_db_connection(db)
    yield c
    c.close()


# --- подтверждение: один сбой это не авария -------------------------------

def test_first_observation_is_silent(conn):
    state, notify, _ = al.decide(None, al.DOWN, T0)
    assert notify is False, "включение мониторинга не повод для тревоги"
    assert state["notified_level"] == al.DOWN


def test_single_blip_does_not_alert():
    """Сервер перезапускали десять секунд -- это не повод писать людям."""
    prev, _, _ = al.decide(None, al.OK, T0)
    prev, notify, _ = al.decide(prev, al.DOWN, T0 + timedelta(seconds=60))
    assert notify is False
    prev, notify, _ = al.decide(prev, al.OK, T0 + timedelta(seconds=120))
    assert notify is False, "тревоги не было -- и восстановления быть не должно"


def test_alert_fires_after_confirmation():
    prev, _, _ = al.decide(None, al.OK, T0)
    t = T0
    fired = False
    for i in range(1, 6):
        t = T0 + timedelta(seconds=60 * i)
        prev, notify, held = al.decide(prev, al.DOWN, t)
        if notify:
            fired = True
            assert held >= al.CONFIRM_SEC
            break
    assert fired, "устойчивое падение обязано поднять тревогу"


def test_alert_is_sent_once_not_every_check():
    prev, _, _ = al.decide(None, al.OK, T0)
    sent = 0
    for i in range(1, 20):
        prev, notify, _ = al.decide(prev, al.DOWN, T0 + timedelta(seconds=60 * i))
        sent += int(notify)
    assert sent == 1, f"отправлено тревог: {sent}"


# --- восстановление -------------------------------------------------------

def test_recovery_is_reported_after_a_real_outage():
    prev, _, _ = al.decide(None, al.OK, T0)
    for i in range(1, 6):
        prev, _, _ = al.decide(prev, al.DOWN, T0 + timedelta(seconds=60 * i))
    # сервис вернулся и держится
    prev, notify, _ = al.decide(prev, al.OK, T0 + timedelta(seconds=600))
    assert notify is False, "сразу после возврата ещё рано"
    prev, notify, held = al.decide(prev, al.OK, T0 + timedelta(seconds=800))
    assert notify is True
    assert held >= al.CONFIRM_SEC


def test_recovery_is_reported_once():
    prev, _, _ = al.decide(None, al.OK, T0)
    for i in range(1, 6):
        prev, _, _ = al.decide(prev, al.DOWN, T0 + timedelta(seconds=60 * i))
    sent = 0
    for i in range(10, 25):
        prev, notify, _ = al.decide(prev, al.OK, T0 + timedelta(seconds=60 * i))
        sent += int(notify)
    assert sent == 1


def test_flapping_does_not_spam():
    """Сервис прыгает вверх-вниз каждую минуту -- сообщений быть не должно,
    ни одно состояние не держится достаточно долго."""
    prev, _, _ = al.decide(None, al.OK, T0)
    sent = 0
    for i in range(1, 30):
        lvl = al.DOWN if i % 2 else al.OK
        prev, notify, _ = al.decide(prev, lvl, T0 + timedelta(seconds=60 * i))
        sent += int(notify)
    assert sent == 0, f"при дребезге отправлено {sent} сообщений"


# --- состояние переживает перезапуск бота ---------------------------------

def test_state_survives_restart(conn):
    al.save_state(conn, "service", al.DOWN, T0.isoformat(),
                  T0.isoformat(), al.DOWN)
    loaded = al.load_state(conn, "service")
    assert loaded["level"] == al.DOWN
    assert loaded["notified_level"] == al.DOWN

    # бот перезапустился, сервис всё ещё лежит -- второй тревоги быть не должно
    _, notify, _ = al.decide(loaded, al.DOWN, T0 + timedelta(hours=2))
    assert notify is False


def test_unknown_check_loads_as_none(conn):
    assert al.load_state(conn, "нет-такой") is None


def test_save_updates_not_duplicates(conn):
    al.save_state(conn, "service", al.OK, T0.isoformat(), None, al.OK)
    al.save_state(conn, "service", al.DOWN, T0.isoformat(), None, al.OK)
    n = conn.execute("SELECT COUNT(*) c FROM alert_state").fetchone()["c"]
    assert n == 1
    assert al.load_state(conn, "service")["level"] == al.DOWN


def test_corrupt_timestamp_does_not_crash():
    prev = {"level": al.DOWN, "since": "не-дата", "notified_at": None,
            "notified_level": al.OK}
    state, notify, held = al.decide(prev, al.DOWN, T0)
    assert isinstance(held, float)


# --- что считать падением -------------------------------------------------

def test_503_is_not_an_outage(monkeypatch):
    """/healthz отдаёт 503, когда сервер ЖИВ и сообщает о своей проблеме.
    Считать это недоступностью -- обесценить тревогу о настоящем падении."""
    import urllib.error
    import urllib.request

    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    level, detail = al.check_service("http://x/healthz")
    assert level == al.OK
    assert "503" in detail


def test_500_is_an_outage(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert al.check_service("http://x/healthz")[0] == al.DOWN


def test_connection_refused_is_an_outage(monkeypatch):
    import urllib.request

    def boom(url, timeout=None):
        raise ConnectionRefusedError("отказано в подключении")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    level, detail = al.check_service("http://x/healthz")
    assert level == al.DOWN
    assert "ConnectionRefusedError" in detail


# --- мьют -----------------------------------------------------------------

def test_alerts_are_on_by_default(conn):
    assert al.mute_until(conn) is None


def test_mute_silences_for_the_requested_time(conn):
    until = al.set_mute(conn, hours=2, now=T0)
    assert until == T0 + timedelta(hours=2)
    assert al.mute_until(conn, now=T0 + timedelta(hours=1)) is not None


def test_mute_expires_by_itself(conn):
    """Молчащий навсегда мониторинг хуже отсутствующего: он создаёт ложное
    ощущение, что за системой следят."""
    al.set_mute(conn, hours=1, now=T0)
    assert al.mute_until(conn, now=T0 + timedelta(hours=1, minutes=1)) is None


def test_mute_duration_is_capped(conn):
    until = al.set_mute(conn, hours=1000, now=T0)
    assert until <= T0 + timedelta(hours=al.MAX_MUTE_HOURS)


def test_unmute_works(conn):
    al.set_mute(conn, hours=5, now=T0)
    al.clear_mute(conn)
    assert al.mute_until(conn, now=T0) is None


def test_mute_does_not_touch_service_state(conn):
    """Мьют не должен «съедать» тревогу: после снятия человек обязан
    узнать, что сервис всё ещё лежит."""
    al.save_state(conn, "service", al.DOWN, T0.isoformat(), None, al.OK)
    al.set_mute(conn, hours=1, now=T0)
    st = al.load_state(conn, "service")
    assert st["level"] == al.DOWN and st["notified_level"] == al.OK
    _, notify, _ = al.decide(st, al.DOWN, T0 + timedelta(hours=2))
    assert notify is True, "после снятия мьюта тревога обязана сработать"


# --- текст сообщения ------------------------------------------------------

def test_outage_message_tells_what_to_do():
    txt = al.message(al.DOWN, 900, "https://example.com/healthz")
    assert "не отвечает" in txt.lower()
    assert "15 мин" in txt
    assert "sar_server.py" in txt, "нужно сказать, что проверять"
    assert "воркер" in txt.lower(), "важно: обработка при этом продолжается"


def test_recovery_message_states_the_downtime():
    txt = al.message(al.OK, 3900, "https://example.com/healthz")
    assert "снова" in txt.lower()
    assert "1 ч 05 мин" in txt


@pytest.mark.parametrize("sec,expect", [
    (45, "45 с"), (300, "5 мин"), (3600, "1 ч"), (5400, "1 ч 30 мин"),
])
def test_humanize(sec, expect):
    assert al.humanize(sec) == expect
