"""Единственная дверь к байтам материала.

Байты нужны трём местам -- детектору, сборке лёгкой копии, генерации
превью. Пока материал лежал локально, каждое просто открывало путь. С
облаком открытие пути становится скачиванием, и три двери означали бы три
набора ограничителей, которые разойдутся. В этом проекте так уже было с
путём к папке резервных копий: считался в трёх местах, в двух неверно, и
мониторинг годами смотрел не туда.

Здесь проверяется, что дверь одна и что она действительно держит:
одновременность, место, суточный лимит, и -- отдельно -- что отказ всегда
громкий и объясняет причину.
"""
import os
import threading
import time

import pytest

import sar_cloud
import sar_fetch
import sar_staging


MB = 1024 * 1024


class FakeProvider:
    """Провайдер, который «качает» из памяти."""

    def __init__(self, blobs=None, delay=0.0, fail=None):
        self.blobs = blobs or {}
        self.delay = delay
        self.fail = fail
        self.calls = []

    def download(self, file_id, dst, expected_size=0, progress=None, resume=True):
        self.calls.append(file_id)
        if self.fail:
            # ВАЖНО: сначала пишем кусок, потом падаем. Обрыв связи именно
            # так и выглядит -- часть файла уже на диске. Провайдер,
            # падающий ДО записи, не воспроизводит ту ситуацию, ради
            # которой существует discard(), и тест проходил бы впустую.
            with open(dst, "wb") as f:
                f.write(b"partial")
            raise self.fail
        if self.delay:
            time.sleep(self.delay)
        data = self.blobs.get(file_id, b"x" * (expected_size or 0))
        with open(dst, "wb") as f:
            f.write(data)
        return len(data)

    def stream_source(self, file_id):
        return "https://cloud/%s" % file_id, {"Authorization": "Bearer t"}


@pytest.fixture
def st(tmp_path):
    return sar_staging.Staging(str(tmp_path / "staging"), cap_bytes=100 * MB)


def fetcher(st, provider=None, **settings):
    return sar_fetch.Fetcher(st, provider=provider, settings=settings,
                             log=lambda m: None)


# --- уже локально ---------------------------------------------------------

def test_local_file_is_returned_without_touching_the_cloud(st):
    p = st.path_for("v.mp4")
    os.makedirs(os.path.dirname(p) or st.root, exist_ok=True)
    open(p, "wb").write(b"x")
    prov = FakeProvider()
    f = fetcher(st, prov)
    assert f.ensure_local("v.mp4", file_id="id") == p
    assert prov.calls == [], "скачали то, что уже лежало на диске"


def test_using_a_local_file_marks_it_as_recently_used(st):
    """Иначе вытеснение выбросит файл, который читают прямо сейчас."""
    p = st.path_for("v.mp4")
    os.makedirs(os.path.dirname(p) or st.root, exist_ok=True)
    open(p, "wb").write(b"x")
    os.utime(p, (1, 1))
    fetcher(st, FakeProvider()).ensure_local("v.mp4", file_id="id")
    assert time.time() - os.path.getmtime(p) < 60


# --- скачивание -----------------------------------------------------------

def test_download_publishes_the_file(st):
    f = fetcher(st, FakeProvider({"id": b"y" * (5 * MB)}))
    path = f.ensure_local("оп/v.mp4", file_id="id", expected_size=5 * MB)
    assert os.path.getsize(path) == 5 * MB


def test_partial_download_leaves_nothing_behind(st):
    """Остаток .part займёт место, а при следующей попытке будет докачан
    как продолжение -- если файл в облаке подменили, склеятся два разных
    видео, и такой файл откроется."""
    f = fetcher(st, FakeProvider(fail=sar_cloud.CloudError("обрыв")))
    with pytest.raises(sar_fetch.FetchRefused):
        f.ensure_local("v.mp4", file_id="id", expected_size=MB)
    assert not os.path.exists(st.path_for("v.mp4") + ".part")
    assert not st.has("v.mp4")


def test_failure_names_the_file_and_the_reason(st):
    f = fetcher(st, FakeProvider(fail=sar_cloud.CloudError("обрыв связи")))
    with pytest.raises(sar_fetch.FetchRefused) as e:
        f.ensure_local("Курумды/DJI_1.MP4", file_id="id", expected_size=MB)
    msg = str(e.value)
    assert "DJI_1.MP4" in msg and "обрыв связи" in msg


# --- ограничители ---------------------------------------------------------

def test_no_provider_is_an_honest_refusal(st):
    f = fetcher(st, None)
    with pytest.raises(sar_fetch.FetchRefused) as e:
        f.ensure_local("v.mp4", file_id="id")
    assert "не подключено" in str(e.value)


def test_missing_file_id_is_refused(st):
    f = fetcher(st, FakeProvider())
    with pytest.raises(sar_fetch.FetchRefused):
        f.ensure_local("v.mp4")


def test_no_room_refuses_before_downloading(st):
    """Место кончилось и всё закреплено. Скачивание не должно начаться --
    переполнить диск, на котором лежит база операции, нельзя."""
    for name in ("a.mp4", "b.mp4"):
        p = st.path_for(name)
        open(p, "wb").write(b"x" * (50 * MB))
        st.pin(name)
    prov = FakeProvider()
    f = fetcher(st, prov)
    with pytest.raises(sar_fetch.FetchRefused) as e:
        f.ensure_local("новый.mp4", file_id="id", expected_size=40 * MB)
    assert prov.calls == [], "скачивание началось, хотя места нет"
    assert "не начато" in str(e.value)


def test_daily_traffic_limit_stops_downloads(st):
    f = fetcher(st, FakeProvider(), daily_traffic_gb=0.001)   # 1 МБ
    f.traffic.add(2 * MB)
    with pytest.raises(sar_fetch.FetchRefused) as e:
        f.ensure_local("v.mp4", file_id="id", expected_size=MB)
    assert "лимит трафика" in str(e.value)
    assert "настройк" in str(e.value).lower(), "не сказано, что делать"


def test_zero_limit_means_unlimited(st):
    """Ноль в настройках -- это «без лимита», а не «ничего нельзя»."""
    f = fetcher(st, FakeProvider(), daily_traffic_gb=0)
    f.traffic.add(999 * MB)
    f.ensure_local("v.mp4", file_id="id", expected_size=MB)
    assert st.has("v.mp4")


def test_traffic_is_counted(st):
    f = fetcher(st, FakeProvider())
    f.ensure_local("v.mp4", file_id="id", expected_size=3 * MB)
    assert f.traffic.used_bytes() == 3 * MB


def test_concurrent_downloads_respect_the_limit(st):
    """Главный рычаг расхода канала. Без него подключение папки означает
    попытку качать всё сразу."""
    prov = FakeProvider(delay=0.15)
    f = fetcher(st, prov, downloads_in_flight=2)
    peak = {"n": 0, "max": 0}
    lock = threading.Lock()
    orig = prov.download

    def counting(*a, **k):
        with lock:
            peak["n"] += 1
            peak["max"] = max(peak["max"], peak["n"])
        try:
            return orig(*a, **k)
        finally:
            with lock:
                peak["n"] -= 1

    prov.download = counting
    threads = [threading.Thread(target=f.ensure_local,
                                args=("f%d.mp4" % i,),
                                kwargs={"file_id": "id%d" % i,
                                        "expected_size": MB})
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak["max"] <= 2, "одновременных загрузок больше разрешённого: %d" % peak["max"]


# --- закрепление ----------------------------------------------------------

def test_fetched_file_is_pinned_by_default(st):
    """Файл получают перед обработкой. Вытеснить его в этот момент значит
    получить отчёт по половине видео -- и молча."""
    f = fetcher(st, FakeProvider())
    f.ensure_local("v.mp4", file_id="id", expected_size=MB)
    assert st.is_pinned("v.mp4")


def test_release_unpins(st):
    f = fetcher(st, FakeProvider())
    f.ensure_local("v.mp4", file_id="id", expected_size=MB)
    f.release("v.mp4")
    assert not st.is_pinned("v.mp4")


# --- чтение без скачивания ------------------------------------------------

def test_stream_source_prefers_the_local_copy(st):
    p = st.path_for("v.mp4")
    open(p, "wb").write(b"x")
    url, headers = fetcher(st, FakeProvider()).stream_source("v.mp4", "id")
    assert url == p and headers == {}


def test_stream_source_returns_cloud_url_when_not_local(st):
    """Ради превью и длительности качать 600 МБ незачем: ffmpeg сходит по
    этой ссылке и возьмёт только нужные куски."""
    url, headers = fetcher(st, FakeProvider()).stream_source("v.mp4", "id")
    assert url.startswith("https://cloud/")
    assert "Authorization" in headers


def test_stream_source_without_cloud_is_empty_not_an_error(st):
    """Материал просто ещё не подключён -- это не повод ронять обход."""
    url, headers = fetcher(st, None).stream_source("v.mp4")
    assert url is None


# --- наблюдаемость --------------------------------------------------------

def test_stats_expose_what_metrics_need(st):
    """Без этих чисел исчерпание квоты выглядит как «платформа странно
    тормозит» -- ровно тот молчаливый отказ, что повторяется чаще всего."""
    f = fetcher(st, FakeProvider(), downloads_in_flight=2)
    s = f.stats()
    for key in ("downloads_in_flight", "downloads_limit", "staging_bytes",
                "staging_cap_bytes", "traffic_today_bytes"):
        assert key in s, key
    assert s["downloads_limit"] == 2
