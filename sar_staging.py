"""Временная папка для скачанных оригиналов.

ЗАЧЕМ. Материал уезжает в облако, но детектор и ffmpeg умеют читать только
файл на диске: `cv2.VideoCapture` и `ffmpeg -i` берут ПУТЬ. Значит перед
обработкой файл надо положить локально. Оригинал при этом нужен ровно
дважды -- при обработке и при сборке лёгкой копии; дальше зрители смотрят
копию, и держать исходник незачем.

ГЛАВНОЕ ПРАВИЛО: ПЕРЕПОЛНИТЬ ДИСК НЕЛЬЗЯ. На этом же диске лежит база
операции. Поэтому у папки жёсткий потолок, и если освободить место нечем --
скачивание НЕ НАЧИНАЕТСЯ и об этом говорится вслух. Отказать в обработке
одного файла неприятно; уронить машину, на которой хранится вся работа
поисковой группы, несравнимо хуже.

ЧТО ВЫТЕСНЯЕМ. Самые давно не нужные. Закреплённые (те, что прямо сейчас
обрабатываются) не трогаются никогда -- иначе детектор потеряет файл на
середине и, что хуже, сделает это молча: `cap.read()` вернёт False, и
получится отчёт по половине видео (см. IncompleteReadError в
sar_video_review.py).

ПОЧЕМУ ВРЕМЯ ОБРАЩЕНИЯ ХРАНИТСЯ КАК mtime ФАЙЛА, А НЕ В ОТДЕЛЬНОМ ИНДЕКСЕ.
Отдельный индекс -- это второй источник правды, который рассинхронизируется
с папкой при любом сбое: файл удалили руками, процесс убили между записью
файла и записью индекса. Здесь правда одна и та же -- содержимое папки.
"""
import os
import shutil
import threading
import time

STAGING_DIR_NAME = "staging"

# Сколько места на диске беречь сверх нужд временной папки.
#
# Потолок папки задаётся человеком в настройках и НИЧЕГО не знает о том,
# сколько на диске реально свободно: поставив 50 ГБ на диске с 16 ГБ, его
# можно было переполнить -- а рядом лежит база, и ей для WAL нужно место.
# Отказ в скачивании переживаемый, потеря базы посреди операции -- нет.
DISK_RESERVE_BYTES = 2 * 1000 ** 3


def staging_dir(data_dir):
    """Где лежат скачанные оригиналы -- ОДНА точка правды.

    Считается от data_dir, а не от watch_dir: watch_dir может оказаться
    сетевой или облачной папкой, а временные копии должны лежать на
    локальном диске рядом с базой и отчётами.
    """
    return os.path.join(data_dir, STAGING_DIR_NAME)


class NoRoomError(RuntimeError):
    """Освободить место нечем. Скачивание не начинается."""


class Staging:
    """Папка скачанных оригиналов с потолком и вытеснением."""

    def __init__(self, root, cap_bytes):
        self.root = root
        self.cap_bytes = int(cap_bytes)
        self._pinned = set()
        self._lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)

    # --- пути -------------------------------------------------------------

    def path_for(self, rel_path):
        """Локальный путь, по которому файл окажется после скачивания.

        Структура папок сохраняется: два разных видео с одинаковым именем
        в разных операциях иначе затирали бы друг друга -- а на материале
        дрона одинаковые имена обычное дело.
        """
        parts = [p for p in str(rel_path or "").replace("\\", "/").split("/") if p]
        return os.path.join(self.root, *parts) if parts else self.root

    def has(self, rel_path):
        return os.path.exists(self.path_for(rel_path))

    # --- учёт -------------------------------------------------------------

    def _entries(self):
        """(путь, размер, когда_последний_раз_обращались) по всем файлам."""
        out = []
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((p, st.st_size, st.st_mtime))
        return out

    def size(self):
        return sum(e[1] for e in self._entries())

    def touch(self, rel_path):
        """Отметить обращение. Без этого вытеснение выбросит файл, который
        читают прямо сейчас, просто потому что он скачан давно."""
        p = self.path_for(rel_path)
        if os.path.exists(p):
            now = time.time()
            try:
                os.utime(p, (now, now))
            except OSError:
                pass

    # --- закрепление ------------------------------------------------------

    def pin(self, rel_path):
        with self._lock:
            self._pinned.add(self._key(rel_path))

    def unpin(self, rel_path):
        with self._lock:
            self._pinned.discard(self._key(rel_path))

    def is_pinned(self, rel_path):
        with self._lock:
            return self._key(rel_path) in self._pinned

    def _key(self, rel_path):
        return os.path.normcase(os.path.abspath(self.path_for(rel_path)))

    # --- освобождение места ----------------------------------------------

    def free_space_for(self, need_bytes):
        """Освобождает место под need_bytes. Бросает NoRoomError, если не
        получилось.

        Исключение, а не False: молчаливый отказ здесь означал бы, что
        вызывающий начнёт скачивание «на всякий случай» и переполнит диск.
        """
        need_bytes = int(need_bytes)
        if need_bytes > self.cap_bytes:
            raise NoRoomError(
                "файл (%.2f ГБ) больше всей временной папки (%.2f ГБ). "
                "Увеличьте потолок в настройках платформы."
                % (need_bytes / 1e9, self.cap_bytes / 1e9))

        with self._lock:
            pinned = set(self._pinned)

        entries = self._entries()
        used = sum(e[1] for e in entries)

        # МЕСТО НА САМОМ ДИСКЕ, а не только под потолком папки. Вытеснение
        # освобождает и его, поэтому в запас считаем и то, что можно убрать.
        evictable = sum(
            e[1] for e in entries
            if os.path.normcase(os.path.abspath(e[0])) not in pinned)
        try:
            free_now = shutil.disk_usage(self.root).free
        except OSError:
            free_now = None     # диска не видно -- не выдумываем, пропускаем
        if free_now is not None and \
                need_bytes + DISK_RESERVE_BYTES > free_now + evictable:
            raise NoRoomError(
                "на диске свободно %.2f ГБ (и ещё %.2f ГБ можно освободить), "
                "а под файл нужно %.2f ГБ плюс %.2f ГБ запаса для базы. "
                "Скачивание не начато."
                % (free_now / 1e9, evictable / 1e9, need_bytes / 1e9,
                   DISK_RESERVE_BYTES / 1e9))

        if used + need_bytes <= self.cap_bytes:
            return 0

        # самые давние -- первыми
        entries.sort(key=lambda e: e[2])
        freed = 0
        for path, size, _ in entries:
            if used + need_bytes - freed <= self.cap_bytes:
                break
            if os.path.normcase(os.path.abspath(path)) in pinned:
                continue        # обрабатывается прямо сейчас -- не трогаем
            try:
                os.remove(path)
                freed += size
            except OSError:
                continue

        if used + need_bytes - freed > self.cap_bytes:
            raise NoRoomError(
                "во временной папке нет места под %.2f ГБ и освободить "
                "нечем: занято %.2f ГБ из %.2f ГБ, и всё это файлы в "
                "работе. Скачивание не начато."
                % (need_bytes / 1e9, (used - freed) / 1e9, self.cap_bytes / 1e9))
        self._prune_empty_dirs()
        return freed

    def _prune_empty_dirs(self):
        """Убирает опустевшие подпапки операций -- иначе за месяцы работы
        накопится дерево пустых каталогов."""
        for dirpath, dirnames, names in os.walk(self.root, topdown=False):
            if dirpath == self.root or names or dirnames:
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    # --- запись -----------------------------------------------------------

    def open_for_write(self, rel_path):
        """Временный путь для скачивания. Публикация -- через publish().

        Пишем во временный файл и переименовываем: без этого оборванная
        закачка оставляет файл нормального вида, но неполный, и детектор
        честно обработает половину видео. Тот же приём, что и везде в
        проекте (flush_partial_detections, публикация прокси-копий).
        """
        dst = self.path_for(rel_path)
        os.makedirs(os.path.dirname(dst) or self.root, exist_ok=True)
        return dst + ".part"

    def publish(self, rel_path):
        """Делает скачанный файл видимым под настоящим именем."""
        dst = self.path_for(rel_path)
        tmp = dst + ".part"
        os.replace(tmp, dst)
        self.touch(rel_path)
        return dst

    def discard(self, rel_path):
        """Убирает недокачанный остаток после неудачи."""
        for p in (self.path_for(rel_path) + ".part",):
            try:
                os.remove(p)
            except OSError:
                pass

    def remove(self, rel_path):
        try:
            os.remove(self.path_for(rel_path))
            return True
        except OSError:
            return False

    def clear(self):
        """Полная очистка -- для ручного обслуживания."""
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root, exist_ok=True)
