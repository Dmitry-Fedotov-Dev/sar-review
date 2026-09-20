"""Облачные хранилища: листинг, скачивание, отказы.

Сеть в тестах не используется -- подменяется urlopen. Проверяется то, что
ломается на практике и ломается МОЛЧА:

  * недокачанный файл выглядит как целый и даёт отчёт по половине видео;
  * докачка дописывает в конец, когда сервер проигнорировал Range, --
    получается каша из двух копий, и она тоже «открывается»;
  * 429 от облака, принятый за обычную ошибку, превращается в бан вместо
    замедления;
  * исчерпанная квота Google (403) неотличима от отозванного доступа,
    если не смотреть в тело ответа.
"""
import io
import json
import os
import urllib.error

import pytest

import sar_cloud


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self._headers = headers or {}

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b"", retry_after=None):
    hdrs = {}
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://x", code, "err",
                                   _Headers(hdrs), io.BytesIO(body))


class _Headers(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Повторы не должны растягивать прогон тестов на минуты."""
    monkeypatch.setattr(sar_cloud, "_sleep", lambda s: None)


# --- листинг --------------------------------------------------------------

def test_google_listing_is_normalised(monkeypatch):
    payload = {"files": [
        {"id": "1", "name": "DJI_0001.MP4", "size": "1234",
         "mimeType": "video/mp4", "modifiedTime": "2026-08-15T10:00:00Z"},
        {"id": "2", "name": "Курумды", "mimeType":
         "application/vnd.google-apps.folder"},
    ]}
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(json.dumps(payload).encode()))
    files = sar_cloud.GoogleDrive("t").list_folder("root")
    assert [f.name for f in files] == ["DJI_0001.MP4", "Курумды"]
    assert files[0].size == 1234 and not files[0].is_folder
    assert files[1].is_folder and files[1].size == 0


def test_yandex_listing_is_normalised(monkeypatch):
    payload = {"_embedded": {"total": 2, "items": [
        {"name": "DJI_0002.MP4", "path": "disk:/Оп/DJI_0002.MP4",
         "type": "file", "size": 99, "modified": "2026-08-15T10:00:00+00:00"},
        {"name": "Вложенная", "path": "disk:/Оп/Вложенная", "type": "dir"},
    ]}}
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(json.dumps(payload).encode()))
    files = sar_cloud.YandexDisk("t").list_folder("disk:/Оп")
    assert files[0].size == 99 and not files[0].is_folder
    assert files[1].is_folder
    assert files[0].id == "disk:/Оп/DJI_0002.MP4", (
        "у Яндекса идентификатор -- это путь, и он нужен для скачивания")


def test_both_providers_return_the_same_shape():
    """Весь остальной код не должен знать, с каким облаком работает."""
    a = sar_cloud.FileInfo("1", "a", 10)
    assert {"id", "name", "size", "modified", "is_folder", "mime"} <= set(
        sar_cloud.FileInfo.__slots__)
    assert a.size == 10 and a.is_folder is False


# --- отказы ---------------------------------------------------------------

def test_rate_limit_is_retried_then_reported(monkeypatch):
    """429 нельзя принимать за обычную ошибку: повторы без паузы
    превращают замедление в бан."""
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise http_error(429, retry_after=1)

    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", boom)
    with pytest.raises(sar_cloud.RateLimited):
        sar_cloud.GoogleDrive("t").list_folder("root")
    assert len(calls) == sar_cloud.MAX_RETRIES, "повторов не было"


def test_retry_after_is_respected(monkeypatch):
    waited = []
    monkeypatch.setattr(sar_cloud, "_sleep", lambda s: waited.append(s))
    state = {"n": 0}
    payload = json.dumps({"files": []}).encode()

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(429, retry_after=7)
        return FakeResponse(payload)

    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", flaky)
    sar_cloud.GoogleDrive("t").list_folder("root")
    assert waited == [7], "сервер сказал, сколько ждать, -- его проигнорировали"


def test_expired_token_is_distinguished_from_quota(monkeypatch):
    """Google отвечает 403 и на исчерпанную квоту, и на отозванный доступ.
    В первом случае надо подождать, во втором -- попросить человека
    переподключить диск. Перепутать значит либо ждать вечно, либо
    дёргать человека на пустом месте."""
    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            http_error(403, b'{"error":{"message":"Invalid Credentials"}}')))
    with pytest.raises(sar_cloud.AuthExpired):
        sar_cloud.GoogleDrive("t").list_folder("root")


def test_quota_403_is_not_treated_as_expired_token(monkeypatch):
    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            http_error(403, b'{"error":{"message":"User Rate Limit Exceeded"}}')))
    with pytest.raises(sar_cloud.CloudError) as e:
        sar_cloud.GoogleDrive("t").list_folder("root")
    assert not isinstance(e.value, sar_cloud.AuthExpired)


# --- скачивание -----------------------------------------------------------

def test_short_download_is_an_error(monkeypatch, tmp_path):
    """САМОЕ ВАЖНОЕ. Недокачанное видео открывается и читается, просто
    кончается раньше времени -- получится отчёт по половине материала, и
    никто этого не заметит."""
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"x" * 10))
    dst = str(tmp_path / "v.mp4")
    with pytest.raises(sar_cloud.CloudError) as e:
        sar_cloud.GoogleDrive("t").download("id", dst, expected_size=100)
    assert "неполный" in str(e.value)


def test_full_download_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"x" * 100))
    dst = str(tmp_path / "v.mp4")
    assert sar_cloud.GoogleDrive("t").download("id", dst, expected_size=100) == 100
    assert os.path.getsize(dst) == 100


def test_resume_sends_a_range_header(monkeypatch, tmp_path):
    """Обрыв на 600-мегабайтном видео не должен означать «начать сначала»:
    на плохом канале такая закачка не завершится никогда."""
    seen = {}

    def capture(req, **k):
        seen.update(req.headers)
        return FakeResponse(b"y" * 40, status=206)

    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen", capture)
    dst = str(tmp_path / "v.mp4")
    with open(dst, "wb") as f:
        f.write(b"x" * 60)
    sar_cloud.GoogleDrive("t").download("id", dst, expected_size=100)
    assert any("bytes=60-" in str(v) for v in seen.values()), seen
    assert os.path.getsize(dst) == 100


def test_server_ignoring_range_restarts_instead_of_appending(monkeypatch, tmp_path):
    """Сервер ответил 200 вместо 206 -- значит прислал файл ЦЕЛИКОМ.
    Дописав его в конец, получим склейку двух копий: такой файл тоже
    «открывается», просто содержит мусор после середины."""
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"z" * 100, status=200))
    dst = str(tmp_path / "v.mp4")
    with open(dst, "wb") as f:
        f.write(b"x" * 60)
    sar_cloud.GoogleDrive("t").download("id", dst, expected_size=100)
    assert os.path.getsize(dst) == 100, "файл склеен из двух копий"
    assert open(dst, "rb").read() == b"z" * 100


def test_already_complete_file_is_not_downloaded_again(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or FakeResponse(b""))
    dst = str(tmp_path / "v.mp4")
    with open(dst, "wb") as f:
        f.write(b"x" * 100)
    sar_cloud.GoogleDrive("t").download("id", dst, expected_size=100)
    assert calls == [], "целый файл скачан заново -- лишний трафик на пустом месте"


# --- чтение по частям -----------------------------------------------------

def test_google_stream_source_carries_authorisation():
    # Токен намеренно латиницей: заголовки HTTP кодируются в latin-1, и
    # кириллица в токене теперь отвергается на входе -- см. _check_token.
    url, headers = sar_cloud.GoogleDrive("ya29.token").stream_source("f1")
    assert "alt=media" in url
    assert headers["Authorization"] == "Bearer ya29.token"


def test_yandex_stream_source_asks_for_a_one_time_link(monkeypatch):
    """У Яндекса ссылка одноразовая и уже содержит авторизацию -- слать
    туда заголовок не следует."""
    monkeypatch.setattr(
        sar_cloud.urllib.request, "urlopen",
        lambda *a, **k: FakeResponse(json.dumps({"href": "https://dl/x"}).encode()))
    url, headers = sar_cloud.YandexDisk("t").stream_source("disk:/a.mp4")
    assert url == "https://dl/x"
    assert headers == {}


def test_yandex_without_link_fails_loudly(monkeypatch):
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"{}"))
    with pytest.raises(sar_cloud.CloudError):
        sar_cloud.YandexDisk("t").stream_source("disk:/a.mp4")


# --- общее ----------------------------------------------------------------

def test_unknown_provider_is_refused():
    with pytest.raises(sar_cloud.CloudError):
        sar_cloud.make_provider("dropbox", "t")


def test_known_providers_are_registered():
    assert set(sar_cloud.PROVIDERS) == {"google", "yandex"}
    for name in sar_cloud.PROVIDERS:
        p = sar_cloud.make_provider(name, "t")
        assert p.label, "у хранилища нет человеческого названия для интерфейса"


def test_every_network_call_has_a_timeout():
    """Сторож туннеля однажды повис на восемь суток ровно потому, что ждал
    без таймаута."""
    assert sar_cloud.HTTP_TIMEOUT_SEC > 0
    src = open(sar_cloud.__file__, encoding="utf-8").read()
    assert src.count("urlopen(") == src.count("timeout=HTTP_TIMEOUT_SEC"), (
        "где-то вызов urlopen без таймаута")


# --- что вообще не является токеном ---------------------------------------
#
# Найдено на живой проверке: заголовки HTTP кодируются в latin-1, и токен с
# кириллицей роняет запрос UnicodeEncodeError из недр http.client. Человек в
# момент настройки получает трассировку вместо объяснения -- и это худший
# момент для невнятной ошибки, потому что он и так не уверен, что делает
# правильно. Через форму админки этот путь тоже достижим.

def test_cyrillic_in_token_is_refused_clearly():
    with pytest.raises(sar_cloud.CloudError) as e:
        sar_cloud.GoogleDrive("токен-по-русски")
    msg = str(e.value)
    assert "не тот текст" in msg or "быть не может" in msg


def test_token_with_spaces_is_refused(): 
    """Копируют обычно с лишним текстом вокруг."""
    with pytest.raises(sar_cloud.CloudError) as e:
        sar_cloud.YandexDisk("ya29.a0 AfH6")
    assert "пробел" in str(e.value)


def test_empty_token_is_refused():
    with pytest.raises(sar_cloud.CloudError):
        sar_cloud.GoogleDrive("")
    with pytest.raises(sar_cloud.CloudError):
        sar_cloud.GoogleDrive(None)


def test_normal_token_passes_and_is_trimmed():
    """Лишние пробелы по краям -- обычное следствие копирования, и это не
    повод отказывать: их достаточно убрать."""
    p = sar_cloud.GoogleDrive("  ya29.a0AfH6SMB_real-looking_token  ")
    assert p.token == "ya29.a0AfH6SMB_real-looking_token"


def test_refusal_happens_before_any_network_call(monkeypatch):
    """Проверка должна отсекать мусор ДО обращения в сеть: иначе человек
    ждёт таймаут, чтобы узнать, что вставил не то."""
    calls = []
    monkeypatch.setattr(sar_cloud.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1))
    with pytest.raises(sar_cloud.CloudError):
        sar_cloud.make_provider("google", "кириллица")
    assert calls == []
