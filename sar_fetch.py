"""Получение файла материала: единственная дверь к байтам.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Байты материала нужны трём разным местам --
детектору, сборке лёгкой копии, генерации превью. Пока материал лежал на
локальном диске, каждое просто открывало путь. С облаком открытие пути
превращается в скачивание, и если оставить три двери, ограничители придётся
ставить в трёх местах. Они разойдутся -- это в проекте уже случалось с
путём к папке резервных копий, который считался в трёх местах и в двух
неверно; мониторинг годами смотрел не туда.

Поэтому здесь ОДНА функция `ensure_local()`, и все ходят через неё.

ОГРАНИЧИТЕЛИ, КОТОРЫЕ ОНА СОБЛЮДАЕТ:

  * одновременных загрузок -- не больше настроенного;
  * место во временной папке -- жёсткий потолок, вытеснение по давности,
    и отказ вместо переполнения диска, на котором лежит база;
  * суточный лимит трафика -- страховка от исчерпания квоты облака;
  * повторы после неудачи -- с нарастающей паузой, а не каждые 15 секунд.

ЧЕГО ОНА НЕ ДЕЛАЕТ. Не качает ради превью и длительности: для них есть
`stream_source()` у провайдера -- ffmpeg сходит по HTTP и возьмёт только
нужные куски. Скачивание целиком оправдано лишь перед обработкой
детектором, который читает всё видео подряд.
"""
import os
import threading
import time
from datetime import date

import sar_cloud
import sar_staging


class FetchRefused(RuntimeError):
    """Скачивание не начато. Причина -- в тексте, и она всегда конкретна."""


class TrafficBudget:
    """Сколько скачано за сутки и можно ли ещё.

    Считается в памяти процесса: воркер один, перезапуск -- осмысленная
    точка сброса, а хранить в базе счётчик, который обновляется на каждом
    куске файла, значит писать в неё тысячи раз на одно видео.
    """

    def __init__(self):
        self._day = date.today()
        self._used = 0
        self._lock = threading.Lock()

    def _roll(self):
        today = date.today()
        if today != self._day:
            self._day, self._used = today, 0

    def used_bytes(self):
        with self._lock:
            self._roll()
            return self._used

    def add(self, n):
        with self._lock:
            self._roll()
            self._used += int(n)

    def check(self, need_bytes, limit_gb):
        """Бросает FetchRefused, если лимит уже выбран.

        Лимит 0 означает «без лимита» -- так он и задан в настройках.
        """
        if not limit_gb:
            return
        with self._lock:
            self._roll()
            used = self._used
        limit = float(limit_gb) * 1e9
        if used >= limit:
            raise FetchRefused(
                "суточный лимит трафика исчерпан: скачано %.1f ГБ из %.1f ГБ. "
                "Загрузки возобновятся завтра или после изменения лимита в "
                "настройках." % (used / 1e9, limit / 1e9))


class Fetcher:
    """Единственная дверь к байтам материала."""

    def __init__(self, staging, provider=None, settings=None, log=None):
        self.staging = staging
        self.provider = provider
        self.settings = settings or {}
        self.traffic = TrafficBudget()
        self._log = log or (lambda msg: print(msg, flush=True))
        # Ограничение одновременных загрузок. Семафор, а не очередь:
        # лишние вызывающие просто ждут своей очереди, а не копят задания.
        limit = int(self.settings.get("downloads_in_flight", 1) or 1)
        self._slots = threading.Semaphore(limit)
        self._limit = limit
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        # Очереди на отдельные файлы -- чтобы один файл не качался дважды.
        self._keys = {}
        self._keys_lock = threading.Lock()

    # --- наблюдаемость ----------------------------------------------------

    def stats(self):
        """Для метрик и админки. Без этого исчерпание квоты выглядит как
        «платформа странно тормозит»."""
        with self._inflight_lock:
            inflight = self._inflight
        return {
            "downloads_in_flight": inflight,
            "downloads_limit": self._limit,
            "staging_bytes": self.staging.size(),
            "staging_cap_bytes": self.staging.cap_bytes,
            "traffic_today_bytes": self.traffic.used_bytes(),
        }

    # --- главное ----------------------------------------------------------

    def _file_lock(self, rel_path):
        """Очередь на КОНКРЕТНЫЙ файл -- одна на всех, кто его просит."""
        with self._keys_lock:
            lk = self._keys.get(rel_path)
            if lk is None:
                lk = threading.Lock()
                self._keys[rel_path] = lk
            return lk

    def ensure_local(self, rel_path, file_id=None, expected_size=0, pin=True):
        """Возвращает путь к файлу НА ДИСКЕ, скачав его при необходимости.

        pin=True закрепляет файл: пока идёт обработка, вытеснение его не
        тронет. Снимать закрепление обязан вызывающий -- через release().
        Не снятое закрепление не потеряет файл, но займёт место навсегда,
        поэтому release() стоит вызывать в finally.

        ОДИН ФАЙЛ КАЧАЕТСЯ ОДИН РАЗ. Временный путь считается из rel_path,
        поэтому два потока, попросившие одно и то же, писали бы в общий
        `.part` вперемешку. Пока фоновой докачки не было, это не
        проявлялось; с ней -- проявилось бы сразу и выглядело бы как
        битый файл неизвестно откуда. Второй ждёт первого и получает уже
        скачанное, а не качает заново.
        """
        with self._file_lock(rel_path):
            return self._ensure_local_locked(rel_path, file_id,
                                              expected_size, pin)

    def _ensure_local_locked(self, rel_path, file_id, expected_size, pin):
        local = self.staging.path_for(rel_path)
        if os.path.exists(local):
            self.staging.touch(rel_path)
            if pin:
                self.staging.pin(rel_path)
            return local

        if self.provider is None:
            raise FetchRefused(
                "материал не найден на диске, а облачное хранилище не "
                "подключено: %s" % rel_path)
        if not file_id:
            raise FetchRefused("неизвестно, что качать: у %s нет "
                               "идентификатора в облаке" % rel_path)

        self.traffic.check(expected_size, self.settings.get("daily_traffic_gb"))

        # Место освобождаем ДО занятия слота: иначе загрузка, которой
        # заведомо некуда лечь, будет держать слот и мешать остальным.
        try:
            freed = self.staging.free_space_for(expected_size or 0)
        except sar_staging.NoRoomError as e:
            raise FetchRefused(str(e))
        if freed:
            self._log("[облако] освобождено %.2f ГБ во временной папке"
                      % (freed / 1e9))

        with self._slots:
            with self._inflight_lock:
                self._inflight += 1
            try:
                return self._download(rel_path, file_id, expected_size, pin)
            finally:
                with self._inflight_lock:
                    self._inflight -= 1

    def _download(self, rel_path, file_id, expected_size, pin):
        tmp = self.staging.open_for_write(rel_path)
        started = time.time()
        self._log("[облако] качаю %s (%.0f МБ)"
                  % (rel_path, (expected_size or 0) / 1e6))
        try:
            got = self.provider.download(file_id, tmp,
                                          expected_size=expected_size)
        except (sar_cloud.CloudError, OSError) as e:
            # Недокачанный остаток убираем СРАЗУ. Оставленный .part-файл
            # занимает место и при следующей попытке будет докачан как
            # продолжение -- но если файл в облаке подменили, докачка
            # склеит два разных видео, и такой файл откроется.
            self.staging.discard(rel_path)
            raise FetchRefused("не удалось получить %s: %s" % (rel_path, e))

        self.traffic.add(got)
        path = self.staging.publish(rel_path)
        if pin:
            self.staging.pin(rel_path)
        secs = max(time.time() - started, 0.001)
        self._log("[облако] готово %s: %.0f МБ за %.0f с (%.1f Мбит/с)"
                  % (rel_path, got / 1e6, secs, got * 8 / secs / 1e6))
        return path

    def release(self, rel_path):
        """Снимает закрепление: файл снова можно вытеснить."""
        self.staging.unpin(rel_path)

    # --- чтение без скачивания -------------------------------------------

    def stream_source(self, rel_path, file_id=None):
        """(url, headers) для чтения по частям -- или локальный путь.

        Именно этим путём делаются превью и читается длительность: качать
        600 МБ ради одного кадра незачем. Если файл уже лежит локально,
        возвращаем путь -- он всяко быстрее сети.
        """
        local = self.staging.path_for(rel_path)
        if os.path.exists(local):
            self.staging.touch(rel_path)
            return local, {}
        if self.provider is None or not file_id:
            return None, {}
        return self.provider.stream_source(file_id)
