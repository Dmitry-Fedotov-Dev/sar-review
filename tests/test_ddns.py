"""Сторож имени: запись deSEC следует за меняющимся адресом.

Адрес у домашнего провайдера динамический. Пока платформа жила за
туннелем, это не имело значения -- адрес туннеля и так менялся при каждом
перезапуске, из-за чего постоянные ссылки на находки пришлось пустить
через бота. Прямое подключение эту подпорку убирает, но ровно настолько,
насколько имя действительно следует за адресом.

Главное, что здесь проверяется, -- не «умеет ли обновлять», а КОГДА ОН
ОБЯЗАН НИЧЕГО НЕ ДЕЛАТЬ. Имя платформы, уехавшее на чужой адрес, хуже
имени, отставшего на один проход: во втором случае человек видит, что не
открывается, в первом -- открывается что-то другое.
"""
import io
import json
import urllib.error

import pytest

import sar_ddns


class Resp(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b""):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """Журнал в тестах не нужен, но перехватываем -- часть тестов его читает."""
    lines = []
    monkeypatch.setattr(sar_ddns, "log", lambda m: lines.append(str(m)))
    return lines


# --- разбор адреса --------------------------------------------------------

def test_plain_address_is_accepted():
    assert sar_ddns._valid_ipv4("176.123.225.136\n") == "176.123.225.136"


@pytest.mark.parametrize("junk", [
    "", "не адрес", "1.2.3", "1.2.3.4.5", "999.1.1.1", "-1.2.3.4",
    "<html>error</html>", "1.2.3.x", "0x7f.0.0.1",
])
def test_junk_is_rejected(junk):
    """Сервис определения адреса может ответить страницей ошибки с кодом
    200. Приняв её за адрес, мы наведём имя платформы в никуда."""
    assert sar_ddns._valid_ipv4(junk) is None


# --- подтверждение двумя источниками --------------------------------------

def fake_sources(monkeypatch, answers):
    """answers: список того, что вернёт каждый источник по порядку."""
    seq = list(answers)

    def ask(url):
        return seq.pop(0) if seq else None

    monkeypatch.setattr(sar_ddns, "_ask", ask)


def test_two_agreeing_sources_confirm_the_address(monkeypatch):
    fake_sources(monkeypatch, ["1.2.3.4", "1.2.3.4"])
    assert sar_ddns.public_ip(["a", "b", "c"]) == "1.2.3.4"


def test_one_source_is_not_enough(monkeypatch):
    """ГЛАВНОЕ. Один источник может соврать или быть подменён, и заметить
    это будет некому: имя уедет на чужую машину молча."""
    fake_sources(monkeypatch, ["1.2.3.4", None, None])
    assert sar_ddns.public_ip(["a", "b", "c"]) is None


def test_disagreeing_sources_confirm_nothing(monkeypatch):
    fake_sources(monkeypatch, ["1.2.3.4", "5.6.7.8", None])
    assert sar_ddns.public_ip(["a", "b", "c"]) is None


def test_disagreement_is_visible_in_the_log(monkeypatch, quiet):
    """Тихое расхождение источников -- это неработающий сторож, который
    выглядит работающим."""
    fake_sources(monkeypatch, ["1.2.3.4", "5.6.7.8", None])
    sar_ddns.public_ip(["a", "b", "c"])
    assert any("не согласны" in l for l in quiet)


def test_third_source_can_break_the_tie(monkeypatch):
    fake_sources(monkeypatch, ["1.2.3.4", "5.6.7.8", "5.6.7.8"])
    assert sar_ddns.public_ip(["a", "b", "c"]) == "5.6.7.8"


def test_all_sources_down_means_unknown(monkeypatch):
    fake_sources(monkeypatch, [None, None, None])
    assert sar_ddns.public_ip(["a", "b", "c"]) is None


# --- чтение DNS -----------------------------------------------------------

def test_dns_answer_is_read(monkeypatch):
    body = json.dumps({"Answer": [{"type": 1, "data": "1.2.3.4"}]}).encode()
    monkeypatch.setattr(sar_ddns.urllib.request, "urlopen",
                        lambda *a, **k: Resp(body))
    assert sar_ddns.dns_ip("x.dedyn.io") == "1.2.3.4"


def test_missing_record_is_empty_not_unknown(monkeypatch):
    """«Записи нет» -- осмысленный ответ: её надо создать. Это НЕ то же
    самое, что «не смог спросить»."""
    monkeypatch.setattr(sar_ddns.urllib.request, "urlopen",
                        lambda *a, **k: Resp(b'{"Answer":[]}'))
    assert sar_ddns.dns_ip("x.dedyn.io") == ""


def test_unreachable_dns_is_unknown(monkeypatch):
    monkeypatch.setattr(
        sar_ddns.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("нет связи")))
    assert sar_ddns.dns_ip("x.dedyn.io") is None


def test_dns_is_asked_through_a_public_resolver(monkeypatch):
    """Системный резолвер отвечает из кеша и может рассказывать про запись
    то, чего в DNS уже нет. Ровно этот урок стоил сторожу туннеля цикла
    перезапусков живого туннеля."""
    seen = {}

    def cap(req, *a, **k):
        seen["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return Resp(b'{"Answer":[]}')

    monkeypatch.setattr(sar_ddns.urllib.request, "urlopen", cap)
    sar_ddns.dns_ip("x.dedyn.io")
    assert "cloudflare-dns.com" in seen["url"] or "dns-query" in seen["url"]


# --- само обновление ------------------------------------------------------

def test_update_sends_token_and_host(monkeypatch):
    seen = {}

    def cap(req, *a, **k):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return Resp(b"good")

    monkeypatch.setattr(sar_ddns.urllib.request, "urlopen", cap)
    assert sar_ddns.update("x.dedyn.io", "СЕКРЕТ", "1.2.3.4") is True
    assert "hostname=x.dedyn.io" in seen["url"]
    assert "myipv4=1.2.3.4" in seen["url"]
    assert seen["auth"] == "Token СЕКРЕТ"


def test_update_preserves_the_ipv6_record(monkeypatch):
    """Без preserve обновление по IPv4 СТИРАЕТ запись AAAA -- тихо, и
    видно это станет только когда кто-то с IPv6 не сможет войти."""
    seen = {}
    monkeypatch.setattr(sar_ddns.urllib.request, "urlopen",
                        lambda req, *a, **k: (seen.update(url=req.full_url),
                                              Resp(b"good"))[1])
    sar_ddns.update("x.dedyn.io", "t", "1.2.3.4")
    assert "myipv6=preserve" in seen["url"]


def test_nochg_is_also_success(monkeypatch):
    monkeypatch.setattr(sar_ddns.urllib.request, "urlopen",
                        lambda *a, **k: Resp(b"nochg"))
    assert sar_ddns.update("x.dedyn.io", "t", "1.2.3.4") is True


def test_unexpected_answer_is_a_failure(monkeypatch):
    """Ответ, которого мы не понимаем, нельзя считать успехом: имя может
    остаться на старом адресе, а сторож отрапортует, что всё хорошо."""
    monkeypatch.setattr(sar_ddns.urllib.request, "urlopen",
                        lambda *a, **k: Resp(b"<html>maintenance</html>"))
    assert sar_ddns.update("x.dedyn.io", "t", "1.2.3.4") is False


def test_rejected_token_is_explained(monkeypatch, quiet):
    monkeypatch.setattr(
        sar_ddns.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(401)))
    assert sar_ddns.update("x.dedyn.io", "t", "1.2.3.4") is False
    assert any("токен не принят" in l for l in quiet)


def test_rate_limit_is_named(monkeypatch, quiet):
    monkeypatch.setattr(
        sar_ddns.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(429)))
    assert sar_ddns.update("x.dedyn.io", "t", "1.2.3.4") is False
    assert any("429" in l for l in quiet)


def test_token_never_reaches_the_log(monkeypatch, quiet):
    """Журнал сторожа люди пересылают, когда что-то не работает."""
    monkeypatch.setattr(
        sar_ddns.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(500, b"boom")))
    sar_ddns.update("x.dedyn.io", "ОЧЕНЬ-СЕКРЕТНЫЙ-ТОКЕН", "1.2.3.4")
    assert not any("ОЧЕНЬ-СЕКРЕТНЫЙ-ТОКЕН" in l for l in quiet)


# --- проход целиком -------------------------------------------------------

CFG = {"ddns": {"hostname": "sar.dedyn.io", "token": "t"}}


def arrange(monkeypatch, ip, in_dns, updated=True):
    monkeypatch.setattr(sar_ddns, "public_ip", lambda *a, **k: ip)
    monkeypatch.setattr(sar_ddns, "dns_ip", lambda h: in_dns)
    calls = []
    monkeypatch.setattr(sar_ddns, "update",
                        lambda h, t, i: (calls.append(i), updated)[1])
    return calls


def test_unknown_address_changes_nothing(monkeypatch):
    """САМОЕ ВАЖНОЕ. Не сумели узнать адрес -- не трогаем ничего. Иначе
    первый же сбой сети наведёт имя платформы неизвестно куда."""
    calls = arrange(monkeypatch, None, "1.2.3.4")
    out = sar_ddns.once(CFG)
    assert calls == []
    assert "не подтверждён" in out


def test_unreachable_dns_changes_nothing(monkeypatch):
    calls = arrange(monkeypatch, "5.6.7.8", None)
    out = sar_ddns.once(CFG)
    assert calls == []
    assert "DNS не отвечает" in out


def test_matching_record_is_left_alone(monkeypatch):
    """Лишние обновления жгут лимиты deSEC на ровном месте."""
    calls = arrange(monkeypatch, "1.2.3.4", "1.2.3.4")
    out = sar_ddns.once(CFG)
    assert calls == []
    assert "совпадает" in out


def test_changed_address_is_written(monkeypatch):
    calls = arrange(monkeypatch, "5.6.7.8", "1.2.3.4")
    out = sar_ddns.once(CFG)
    assert calls == ["5.6.7.8"]
    assert "1.2.3.4 -> 5.6.7.8" in out


def test_absent_record_is_created(monkeypatch):
    calls = arrange(monkeypatch, "5.6.7.8", "")
    out = sar_ddns.once(CFG)
    assert calls == ["5.6.7.8"]
    assert "записи не было" in out


def test_failed_update_is_reported_loudly(monkeypatch):
    """Тихая неудача здесь означает: адрес сменился, имя показывает на
    старый, и платформа «просто не открывается» без объяснений."""
    calls = arrange(monkeypatch, "5.6.7.8", "1.2.3.4", updated=False)
    out = sar_ddns.once(CFG)
    assert calls == ["5.6.7.8"]
    assert "НЕ УДАЛОСЬ" in out


def test_missing_settings_say_what_to_add(monkeypatch):
    out = sar_ddns.once({})
    assert "ddns.hostname" in out and "ddns.token" in out


def test_settings_without_token_are_not_enough():
    out = sar_ddns.once({"ddns": {"hostname": "sar.dedyn.io"}})
    assert "не настроено" in out


# --- сторож переживает сбои ----------------------------------------------

def test_watch_survives_a_broken_pass(monkeypatch, quiet):
    """Упавший сторож молчит ровно так же, как исправный. В этом проекте
    сторож уже однажды перестал работать незаметно -- на восемь суток."""
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        raise RuntimeError("сеть отвалилась")

    monkeypatch.setattr(sar_ddns, "once", boom)
    monkeypatch.setattr(sar_ddns, "heartbeat", lambda note=None: None)
    monkeypatch.setattr(sar_ddns.time, "sleep", lambda s: None)
    monkeypatch.setattr(sar_ddns, "ddns_cfg", lambda cfg=None: {})
    with pytest.raises(KeyboardInterrupt):
        sar_ddns.watch()
    assert any("сорвался" in l for l in quiet)


def test_watch_marks_the_heartbeat(monkeypatch):
    """Без отметки зависший сторож неотличим от работающего."""
    beats = []

    def stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(sar_ddns, "once", lambda cfg=None: "ок")
    monkeypatch.setattr(sar_ddns, "heartbeat", lambda note=None: beats.append(note))
    monkeypatch.setattr(sar_ddns.time, "sleep", stop)
    monkeypatch.setattr(sar_ddns, "ddns_cfg", lambda cfg=None: {})
    with pytest.raises(KeyboardInterrupt):
        sar_ddns.watch()
    assert beats == ["ок"]


def test_repeated_state_is_not_repeated_in_the_log(monkeypatch, quiet):
    """Журнал, забитый одинаковыми строками, прячет настоящую беду."""
    calls = {"n": 0}

    def tick(s):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(sar_ddns, "once", lambda cfg=None: "адрес 1.2.3.4, запись совпадает")
    monkeypatch.setattr(sar_ddns, "heartbeat", lambda note=None: None)
    monkeypatch.setattr(sar_ddns.time, "sleep", tick)
    monkeypatch.setattr(sar_ddns, "ddns_cfg", lambda cfg=None: {})
    with pytest.raises(KeyboardInterrupt):
        sar_ddns.watch()
    same = [l for l in quiet if "совпадает" in l]
    assert len(same) == 1, "одно и то же состояние напечатано %d раз" % len(same)
