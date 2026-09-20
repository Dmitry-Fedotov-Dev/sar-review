"""Конвейер: качаем следующий файл, пока кодируется текущий.

Замер 14.09.2026 показал, что канал -- потолок, а не одно соединение:
один поток даёт 30,2 Мбит/с, восемь -- 36,1. То есть ускорять САМО
скачивание нечем, оно уже выбирает канал. Ускорить можно только одно:
перестать ждать. Сейчас 58,3 ГБ облачного видео стоят 3,7 ч закачки ПЛЮС
3,1 ч кодирования, потому что одно ждёт другого, хотя делят они только
диск -- не канал и не процессор.

Здесь же два условия, без которых конвейер опасен:

1. Один файл должен качаться ОДИН раз. Временный путь считается из
   rel_path, поэтому два потока писали бы в общий `.part` вперемешку.
   Пока поток был один, это не проявлялось.
2. Потолок временной папки не знал о реально свободном месте на диске.
   Он задаётся человеком (до 100 ГБ) и рядом с базой на диске в 16 ГБ
   означал бы переполнение -- а базе нужно место под WAL.
"""
import os
import threading
import time

import pytest

import sar_fetch
import sar_staging


class SlowProvider:
    """Качает медленно и считает, сколько раз его дёрнули."""

    def __init__(self, delay=0.3, size=1000):
        self.calls = []
        self.delay = delay
        self.size = size
        self.entered = threading.Event()

    def download(self, file_id, dst_path, expected_size=0, **kw):
        self.calls.append(file_id)
        self.entered.set()
        time.sleep(self.delay)
        with open(dst_path, "wb") as f:
            f.write(b"x" * self.size)
        return self.size


def make_fetcher(tmp_path, provider, cap_gb=1.0, **settings):
    st = sar_staging.Staging(str(tmp_path / "staging"),
                             cap_bytes=int(cap_gb * 1e9))
    s = {"downloads_in_flight": 2}
    s.update(settings)
    return sar_fetch.Fetcher(staging=st, provider=provider, settings=s,
                             log=lambda m: None)


# --- один файл качается один раз -----------------------------------------

def test_same_file_asked_twice_is_downloaded_once(tmp_path):
    """ГЛАВНОЕ свойство. Без него фоновая докачка и основной путь пишут в
    один `.part`, и получается склейка двух половин -- файл, который
    открывается и читается, просто он не тот."""
    prov = SlowProvider()
    f = make_fetcher(tmp_path, prov)
    out = {}

    def ask(i):
        out[i] = f.ensure_local("a/v.mp4", file_id="f1", expected_size=1000)

    ths = [threading.Thread(target=ask, args=(i,)) for i in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=30)

    assert prov.calls == ["f1"], (
        "файл скачан %d раза вместо одного" % len(prov.calls))
    assert out[0] == out[1]
    assert os.path.getsize(out[0]) == 1000, "файл побит склейкой"


def test_second_asker_gets_the_file_not_an_error(tmp_path):
    """Второй должен ДОЖДАТЬСЯ и получить файл, а не отказ."""
    prov = SlowProvider()
    f = make_fetcher(tmp_path, prov)
    f.ensure_local("a/v.mp4", file_id="f1", expected_size=1000)
    again = f.ensure_local("a/v.mp4", file_id="f1", expected_size=1000)
    assert os.path.exists(again)
    assert prov.calls == ["f1"]


def test_different_files_are_not_serialised_by_the_same_lock(tmp_path):
    """Очередь должна быть на файл, а не общая: иначе конвейер упрётся
    сам в себя и смысла в нём не будет."""
    prov = SlowProvider(delay=0.4)
    f = make_fetcher(tmp_path, prov)
    t0 = time.time()
    ths = [threading.Thread(target=f.ensure_local, args=("v%d.mp4" % i,),
                            kwargs={"file_id": "f%d" % i, "expected_size": 1000})
           for i in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=30)
    took = time.time() - t0
    assert len(prov.calls) == 2
    assert took < 0.75, "разные файлы качались по очереди (%.2f с)" % took


# --- место на диске -------------------------------------------------------

def test_download_refused_when_the_disk_is_nearly_full(tmp_path, monkeypatch):
    """Потолок папки ничего не знает о диске. Поставив 50 ГБ на диске с
    16 ГБ, его можно переполнить -- а рядом база."""
    import collections
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(sar_staging.shutil, "disk_usage",
                        lambda p: Usage(100 * 10**9, 99 * 10**9, 1 * 10**9))
    st = sar_staging.Staging(str(tmp_path / "staging"), cap_bytes=50 * 10**9)
    with pytest.raises(sar_staging.NoRoomError) as e:
        st.free_space_for(3 * 10**9)
    assert "на диске" in str(e.value)


def test_the_reserve_protects_the_database(tmp_path, monkeypatch):
    """Файл влезает впритык, но тогда базе не останется ничего."""
    import collections
    Usage = collections.namedtuple("Usage", "total used free")
    # свободно ровно столько, сколько весит файл -- запаса нет
    monkeypatch.setattr(sar_staging.shutil, "disk_usage",
                        lambda p: Usage(100 * 10**9, 97 * 10**9, 3 * 10**9))
    st = sar_staging.Staging(str(tmp_path / "staging"), cap_bytes=50 * 10**9)
    with pytest.raises(sar_staging.NoRoomError):
        st.free_space_for(3 * 10**9)


def test_room_is_granted_when_the_disk_really_has_it(tmp_path, monkeypatch):
    import collections
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(sar_staging.shutil, "disk_usage",
                        lambda p: Usage(100 * 10**9, 10 * 10**9, 90 * 10**9))
    st = sar_staging.Staging(str(tmp_path / "staging"), cap_bytes=50 * 10**9)
    assert st.free_space_for(3 * 10**9) == 0


def test_evictable_files_count_towards_free_space(tmp_path, monkeypatch):
    """Незакреплённое можно убрать -- значит место под файл есть, и
    отказывать было бы неправдой."""
    import collections
    Usage = collections.namedtuple("Usage", "total used free")
    root = tmp_path / "staging"
    st = sar_staging.Staging(str(root), cap_bytes=10 * 10**9)
    os.makedirs(str(root), exist_ok=True)
    with open(str(root / "old.mp4"), "wb") as f:
        f.write(b"x" * 5_000_000)
    monkeypatch.setattr(sar_staging.shutil, "disk_usage",
                        lambda p: Usage(10 * 10**9, 10 * 10**9, 2_100_000_000))
    # 2,1 ГБ свободно + 5 МБ вытесняемых, нужно 50 МБ плюс 2 ГБ запаса
    st.free_space_for(50_000_000)


def test_unreadable_disk_does_not_block_the_download(tmp_path, monkeypatch):
    """Не смогли узнать про диск -- не выдумываем цифру и не отказываем:
    выдуманная тревога учит людей игнорировать настоящие."""
    def boom(p):
        raise OSError("нет доступа")
    monkeypatch.setattr(sar_staging.shutil, "disk_usage", boom)
    st = sar_staging.Staging(str(tmp_path / "staging"), cap_bytes=10 * 10**9)
    assert st.free_space_for(1000) == 0


# --- сам конвейер ---------------------------------------------------------

@pytest.fixture
def worker(tmp_path, monkeypatch):
    import sar_worker
    monkeypatch.setattr(sar_worker, "DATA_DIR", str(tmp_path / "data"),
                        raising=False)
    monkeypatch.setattr(sar_worker, "_prefetch",
                        {"thread": None, "name": None}, raising=False)
    monkeypatch.setattr(sar_worker, "_may_try", lambda k: True)
    monkeypatch.setattr(sar_worker, "_note_success", lambda k: None)
    monkeypatch.setattr(sar_worker, "_note_failure", lambda k, w: None)
    return sar_worker


class RecordingFetcher:
    def __init__(self):
        self.asked = []
        self.gate = threading.Event()

    def ensure_local(self, rel_path, file_id=None, expected_size=0, pin=True):
        self.asked.append(rel_path)
        self.gate.set()
        return "/tmp/" + rel_path

    def release(self, rel_path):
        pass


def test_next_file_is_fetched_while_the_current_one_encodes(worker, monkeypatch):
    """Ради чего всё: канал не должен простаивать во время кодирования."""
    f = RecordingFetcher()
    monkeypatch.setattr(worker, "get_fetcher", lambda: f)
    jobs = [("a.mp4", ("f1", 100)), ("b.mp4", ("f2", 200))]
    worker._start_prefetch(jobs, busy_name="a.mp4")
    assert f.gate.wait(timeout=10), "фоновая закачка не стартовала"
    worker._prefetch["thread"].join(timeout=10)
    assert f.asked == ["b.mp4"]


def test_the_file_being_encoded_is_not_fetched_again(worker, monkeypatch):
    f = RecordingFetcher()
    monkeypatch.setattr(worker, "get_fetcher", lambda: f)
    worker._start_prefetch([("a.mp4", ("f1", 100))], busy_name="a.mp4")
    th = worker._prefetch["thread"]
    if th:
        th.join(timeout=10)
    assert f.asked == []


def test_only_one_file_is_fetched_ahead(worker, monkeypatch):
    """Каждый следующий -- ещё гигабайты под тем же потолком, а выигрыш
    даёт уже первый."""
    f = RecordingFetcher()
    f_started = threading.Event()

    class Blocking(RecordingFetcher):
        def ensure_local(self, rel_path, file_id=None, expected_size=0,
                         pin=True):
            self.asked.append(rel_path)
            f_started.set()
            time.sleep(0.5)
            return "/tmp/" + rel_path

    b = Blocking()
    monkeypatch.setattr(worker, "get_fetcher", lambda: b)
    jobs = [("a.mp4", ("f1", 1)), ("b.mp4", ("f2", 2)), ("c.mp4", ("f3", 3))]
    worker._start_prefetch(jobs, busy_name="a.mp4")
    assert f_started.wait(timeout=10)
    worker._start_prefetch(jobs, busy_name="a.mp4")   # пока первая идёт
    worker._prefetch["thread"].join(timeout=10)
    assert b.asked == ["b.mp4"], "качаем вперёд больше одного файла"


def test_already_built_files_are_skipped(worker, monkeypatch):
    """У файла уже есть лёгкая копия -- качать оригинал незачем."""
    import sar_common
    f = RecordingFetcher()
    monkeypatch.setattr(worker, "get_fetcher", lambda: f)
    done = sar_common.proxy_video_path(worker.DATA_DIR, "b.mp4")
    os.makedirs(os.path.dirname(done), exist_ok=True)
    with open(done, "wb") as fh:
        fh.write(b"proxy")
    jobs = [("a.mp4", ("f1", 1)), ("b.mp4", ("f2", 2)), ("c.mp4", ("f3", 3))]
    worker._start_prefetch(jobs, busy_name="a.mp4")
    worker._prefetch["thread"].join(timeout=10)
    assert f.asked == ["c.mp4"]


def test_no_cloud_connected_means_no_prefetch(worker, monkeypatch):
    monkeypatch.setattr(worker, "get_fetcher", lambda: None)
    worker._start_prefetch([("b.mp4", ("f2", 2))], busy_name="a.mp4")
    assert worker._prefetch["thread"] is None


def test_prefetch_failure_is_reported_not_swallowed(worker, monkeypatch, capsys):
    """Тихий отказ здесь означает: конвейер не работает, а выглядит
    работающим -- ровно та категория багов, что уже пережила боевое
    применение в этом проекте."""
    class Failing(RecordingFetcher):
        def ensure_local(self, rel_path, **kw):
            raise RuntimeError("места нет")

    monkeypatch.setattr(worker, "get_fetcher", lambda: Failing())
    worker._start_prefetch([("b.mp4", ("f2", 2))], busy_name="a.mp4")
    worker._prefetch["thread"].join(timeout=10)
    assert "места нет" in capsys.readouterr().out


def test_prefetch_only_takes_requested_material():
    """Страж. Список для упреждающей закачки обязан приходить из того же
    отбора proxy_requested=1, что и основной: иначе подключение диска
    начнёт молча качать все 150 файлов."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "sar_worker.py"
           ).read_text(encoding="utf-8")
    i = src.index("cloud_jobs = [")
    head = src[:i]
    assert "proxy_requested=1" in head[head.rindex("SELECT"):], (
        "cloud_jobs собирается не из попрошенного материала")
