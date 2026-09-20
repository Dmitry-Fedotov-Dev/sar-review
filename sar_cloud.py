"""Облачные хранилища: листинг, чтение по частям, скачивание.

ЗАЧЕМ. Материал операций исторически заливают в Google Диск и Яндекс.Диск.
Пока платформа умеет читать только локальную папку, файлы приходится
держать ещё и на ноутбуке -- 21 ГБ на операцию.

ТРИ РЕЖИМА ДОСТУПА, И ОНИ РАЗНЫЕ ПО ЦЕНЕ:

  1. Листинг -- имена, размеры, даты. Ничего не качается. Дёшево.
  2. Чтение КУСКА файла -- для превью (первый кадр) и длительности
     (заголовок контейнера). Несколько мегабайт вместо 600.
  3. Скачивание целиком -- только перед обработкой детектором.

Пункт 2 -- не оптимизация «на потом». Без него первый же проход по
подключённой папке утянет всю библиотеку ради картинок по 30 КБ.

ПОЧЕМУ ССЫЛКУ ОТДАЁМ ffmpeg, А НЕ КАЧАЕМ КУСОК САМИ. У MP4 индекс (moov)
лежит то в начале, то в конце файла -- у записей с дрона обычно в конце.
Скачав «первые 5 МБ», получим кусок, который не открывается ничем. ffmpeg
же умеет ходить по HTTP с Range сам: он возьмёт заголовок там, где тот
реально лежит, и ровно столько, сколько нужно. Наше дело -- дать ему
адрес и заголовок авторизации.

ЗАВИСИМОСТЕЙ НЕТ. Только стандартная библиотека, как и в sar_tunnel.py:
лишняя зависимость в проекте, который ставят в поле на чужой ноутбук, --
это лишний способ не запуститься.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# Все внешние вызовы обязаны завершаться. Сторож туннеля однажды повис на
# восемь суток именно потому, что ждал без таймаута -- см. _run() в
# sar_tunnel.py и раздел про молчаливые отказы в CLAUDE.md.
HTTP_TIMEOUT_SEC = 60

# Сколько раз повторяем при 429/5xx и сколько ждём, если сервер не сказал
# Retry-After. Дальше -- честный отказ: бесконечные повторы против квоты
# облака только усугубляют положение.
MAX_RETRIES = 4
DEFAULT_RETRY_WAIT_SEC = 5


class CloudError(RuntimeError):
    """Облако не отдало то, что просили."""


class AuthExpired(CloudError):
    """Токен протух. Нужно обновить или переподключить диск."""


class RateLimited(CloudError):
    """Квота исчерпана или слишком частые запросы."""


class FileInfo:
    """Файл или папка в облаке, приведённые к общему виду.

    У Google и Яндекса формат ответа разный вплоть до мелочей (id против
    пути, camelCase против snake_case). Приводим здесь, чтобы весь
    остальной код не знал, с каким облаком работает.
    """

    __slots__ = ("id", "name", "size", "modified", "is_folder", "mime")

    def __init__(self, id, name, size=0, modified=None, is_folder=False, mime=""):
        self.id = id
        self.name = name
        self.size = int(size or 0)
        self.modified = modified
        self.is_folder = bool(is_folder)
        self.mime = mime or ""

    def __repr__(self):
        kind = "папка" if self.is_folder else "%.1f МБ" % (self.size / 1e6)
        return "<%s %s (%s)>" % (self.__class__.__name__, self.name, kind)


def _sleep(seconds):
    """Вынесено отдельной функцией, чтобы тесты не ждали по-настоящему."""
    time.sleep(seconds)


def http_json(url, headers=None, data=None, method=None):
    """GET/POST с разбором JSON и понятными ошибками.

    Повторяет при 429 и 5xx с уважением к Retry-After: облако само говорит,
    когда возвращаться, и игнорировать это -- верный способ получить бан
    вместо замедления.
    """
    last = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=data, method=method,
                                      headers=dict(headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
                body = r.read()
            return json.loads(body.decode("utf-8")) if body else {}
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403) and _is_auth_problem(e):
                raise AuthExpired("доступ к облаку отклонён (код %d)" % e.code)
            if e.code == 429 or 500 <= e.code < 600:
                wait = _retry_after(e) or DEFAULT_RETRY_WAIT_SEC * (2 ** attempt)
                if attempt < MAX_RETRIES - 1:
                    _sleep(wait)
                    continue
                if e.code == 429:
                    raise RateLimited(
                        "облако ограничивает частоту запросов и не успокоилось "
                        "за %d попыток. Загрузки приостановлены." % MAX_RETRIES)
            raise CloudError("облако ответило %d на %s" % (e.code, url))
        except urllib.error.URLError as e:
            last = e
            if attempt < MAX_RETRIES - 1:
                _sleep(DEFAULT_RETRY_WAIT_SEC * (2 ** attempt))
                continue
            raise CloudError("не удалось связаться с облаком: %s" % e)
    raise CloudError("облако недоступно: %s" % last)


def _retry_after(err):
    """Сколько просил подождать сервер. None, если не сказал."""
    try:
        v = err.headers.get("Retry-After")
    except Exception:
        return None
    if not v:
        return None
    try:
        return max(1, int(float(v)))
    except (TypeError, ValueError):
        return None


def _is_auth_problem(err):
    """Отличает «токен протух» от «слишком часто просите».

    Google отвечает 403 и на исчерпанную квоту, и на отозванный доступ.
    Перепутать нельзя: в первом случае надо подождать, во втором --
    попросить человека переподключить диск. Различаем по тексту ответа,
    другого признака в протоколе нет.
    """
    if err.code == 401:
        return True
    try:
        body = err.read().decode("utf-8", "replace").lower()
    except Exception:
        return False
    for marker in ("invalid_grant", "invalid credentials", "unauthorized",
                   "autherror", "insufficientpermissions", "unauthorizederror"):
        if marker in body:
            return True
    return False


def folder_ref(provider, text):
    """Приводит то, что вставил человек, к идентификатору папки.

    ЗАЧЕМ. В поле «папка» естественнее всего вставить ССЫЛКУ -- её видно в
    адресной строке, её копируют и пересылают. Идентификатор отдельно никто
    не выковыривает. Раньше вставленная ссылка не распознавалась, поле
    считалось пустым, и платформа молча бралась за КОРЕНЬ ДИСКА: на боевом
    подключении это означало 288 личных фотографий, затянутых в платформу
    поисковой операции.

    Отказ был бы лучше, но правильнее просто понять ссылку.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if provider == "google":
        # https://drive.google.com/drive/folders/<ID>?usp=drive_link
        m = re.search(r"/folders/([A-Za-z0-9_-]+)", text)
        if m:
            return m.group(1)
        # https://drive.google.com/drive/u/0/folders/<ID> тоже сюда попадает,
        # а вот ссылка на ФАЙЛ -- нет: это другая сущность, и молча
        # подставлять её как папку значит обещать то, чего не будет.
        if text.startswith("http"):
            raise CloudError(
                "это ссылка не на папку Google Диска. Нужен адрес вида "
                "drive.google.com/drive/folders/... -- откройте нужную папку "
                "и скопируйте адрес из строки браузера.")
        return text
    if provider == "yandex":
        # Человек может вставить и путь, и ссылку на публичную папку.
        if text.startswith("http"):
            raise CloudError(
                "для Яндекс.Диска нужен путь вида disk:/Папка, а не ссылка.")
        if not text.startswith("disk:"):
            return "disk:/" + text.lstrip("/")
        return text
    return text


# ---------------------------------------------------------------------------
# ПРОДЛЕНИЕ ДОСТУПА К GOOGLE
#
# У Google access token живёт РОВНО ЧАС. Это не сбой и не настройка -- так
# устроен протокол: предполагается, что приложение получило вместе с ним
# refresh token и меняет протухший на свежий само.
#
# Без этого диск приходится подключать заново каждый час. На боевых данных
# подготовка 150 файлов занимает около трёх часов -- то есть без продления
# она не может завершиться в принципе, сколько ни начинай.
#
# ЧТО НУЖНО ОТ ЧЕЛОВЕКА. Обменять refresh token на новый доступ может
# только то приложение, которому он выдан, -- нужны его client_id и
# client_secret. У OAuth Playground они свои, чужие, и Google их не
# отдаёт (справедливо). Поэтому для постоянной работы человек заводит
# СВОЁ приложение в консоли Google и вставляет его ключи в админку.
#
# У Яндекса такой заботы нет: там токен действует около года.
# ---------------------------------------------------------------------------

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Обновляем чуть заранее: запрос, отправленный за секунду до конца жизни
# токена, успеет протухнуть по дороге.
REFRESH_MARGIN_SEC = 120


def refresh_google_token(client_id, client_secret, refresh_token):
    """Меняет refresh token на свежий доступ.

    Возвращает (access_token, когда_истечёт_в_секундах_эпохи).
    """
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("ascii")
    try:
        out = http_json(GOOGLE_TOKEN_URL, data=data, method="POST",
                        headers={"Content-Type":
                                 "application/x-www-form-urlencoded"})
    except AuthExpired:
        # Отозванный доступ выглядит так же, как истёкший, но чинится
        # иначе: продлевать нечего, человеку надо переподключить диск.
        raise AuthExpired(
            "продлить доступ не вышло: Google отклонил refresh token. "
            "Обычно это значит, что доступ отозвали или ключи приложения "
            "не те. Подключите диск заново.")
    token = out.get("access_token")
    if not token:
        raise CloudError("Google не вернул новый токен доступа")
    lifetime = int(out.get("expires_in") or 3600)
    return token, time.time() + lifetime


def _check_token(token):
    """Отсекает то, что заведомо не является токеном.

    ЗАЧЕМ. Заголовки HTTP кодируются в latin-1, и токен с кириллицей роняет
    запрос невнятным UnicodeEncodeError из недр http.client -- человек в
    ответ на вставленный не тот текст получает трассировку вместо
    объяснения. А случается это ровно в момент настройки, когда человек и
    так не уверен, что делает правильно.

    Настоящие токены Google и Яндекса -- латиница, цифры и несколько
    знаков. Проверяем именно это, а не длину и не формат: формат у
    провайдеров разный и со временем меняется.
    """
    token = (token or "").strip()
    if not token:
        raise CloudError("токен пустой")
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        raise CloudError(
            "в токене есть символы, которых в нём быть не может "
            "(кириллица или похожее). Обычно это значит, что скопирован "
            "не тот текст -- нужен сам токен доступа, без подписей и "
            "кавычек.")
    if any(c.isspace() for c in token):
        raise CloudError(
            "в токене есть пробелы или перенос строки -- скорее всего "
            "скопирован лишний текст вокруг него.")
    return token


class Provider:
    """Общий интерфейс облачного хранилища."""

    name = "?"
    label = "?"

    def __init__(self, token, renew=None, on_renew=None):
        """renew -- как получить новый токен: вызываемое без аргументов,
        возвращает (token, expires_at). on_renew -- куда его сохранить.

        Провайдер НЕ знает про базу: он получает две функции и вызывает их.
        Иначе сетевой модуль пришлось бы учить схеме хранения, а его
        импортируют и разовые скрипты, которым база не нужна вовсе.
        """
        self.token = _check_token(token)
        self._renew = renew
        self._on_renew = on_renew

    def renew_access(self):
        """Продлевает доступ. True, если получилось."""
        if not self._renew:
            return False
        token, expires_at = self._renew()
        self.token = _check_token(token)
        if self._on_renew:
            self._on_renew(self.token, expires_at)
        return True

    def _with_renew(self, call):
        """Выполняет запрос, продлевая доступ при отказе -- ОДИН раз.

        Повторять бесконечно нельзя: если продление само не помогает
        (доступ отозван), получится бесконечный цикл обращений к Google
        вместо честной ошибки.
        """
        try:
            return call()
        except AuthExpired:
            if not self.renew_access():
                raise
            return call()

    def auth_headers(self):
        raise NotImplementedError

    def list_folder(self, folder_id):
        raise NotImplementedError

    def stream_source(self, file_id):
        """(url, headers) для чтения файла по частям.

        Отдаётся ffmpeg: он сам сходит за индексом туда, где тот лежит, и
        возьмёт ровно столько, сколько нужно для кадра или длительности.
        """
        raise NotImplementedError

    def download(self, file_id, dst_path, expected_size=0, progress=None,
                 resume=True):
        """Качает файл, продлевая доступ при отказе.

        Продление нужно именно здесь, а не только в листинге: 3-гигабайтное
        видео качается дольше часа, то есть токен может умереть ПРЯМО В
        ПРОЦЕССЕ. Докачка с продлением доводит такой файл до конца, а без
        неё он не скачается никогда, сколько ни начинай.
        """
        return self._with_renew(
            lambda: self._download(file_id, dst_path, expected_size,
                                    progress, resume))

    def _download(self, file_id, dst_path, expected_size=0, progress=None,
                  resume=True):
        """Качает файл целиком во временный путь dst_path.

        Докачивает, если файл частично скачан: обрыв на 600-мегабайтном
        видео иначе означает начать сначала, и на плохом канале закачка
        не завершится никогда.
        """
        url, headers = self.stream_source(file_id)
        headers = dict(headers)
        have = 0
        if resume and os.path.exists(dst_path):
            have = os.path.getsize(dst_path)
            if expected_size and have >= expected_size:
                return have
            if have:
                headers["Range"] = "bytes=%d-" % have

        req = urllib.request.Request(url, headers=headers)
        mode = "ab" if have else "wb"
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
                # Сервер мог проигнорировать Range и отдать файл целиком --
                # тогда дописывать в конец нельзя, получится каша из двух
                # копий. Признак -- код 200 вместо 206.
                if have and getattr(r, "status", r.getcode()) != 206:
                    have, mode = 0, "wb"
                done = have
                with open(dst_path, mode) as f:
                    while True:
                        chunk = r.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, expected_size)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited("облако ограничивает скачивание")
            if e.code in (401, 403) and _is_auth_problem(e):
                raise AuthExpired("доступ к файлу отклонён")
            raise CloudError("не удалось скачать файл: код %d" % e.code)
        except urllib.error.URLError as e:
            raise CloudError("обрыв связи при скачивании: %s" % e)

        # ФАЙЛ НЕ ТОГО РАЗМЕРА -- ЭТО ОШИБКА, А НЕ МЕЛОЧЬ. Недокачанное
        # видео открывается и читается, просто кончается раньше времени:
        # получится отчёт по половине материала, и никто этого не заметит.
        if expected_size and done != expected_size:
            raise CloudError(
                "скачано %d байт из %d -- файл неполный, обработка по нему "
                "дала бы отчёт по части видео" % (done, expected_size))
        return done


class GoogleDrive(Provider):
    name = "google"
    label = "Google Диск"

    API = "https://www.googleapis.com/drive/v3"

    def auth_headers(self):
        return {"Authorization": "Bearer %s" % self.token}

    def list_folder(self, folder_id):
        return self._with_renew(lambda: self._list_folder(folder_id))

    def _list_folder(self, folder_id):
        out, page = [], None
        while True:
            params = {
                "q": "'%s' in parents and trashed = false" % folder_id,
                "fields": "nextPageToken,files(id,name,size,mimeType,modifiedTime)",
                "pageSize": "200",
            }
            if page:
                params["pageToken"] = page
            url = "%s/files?%s" % (self.API, urllib.parse.urlencode(params))
            data = http_json(url, headers=self.auth_headers())
            for f in data.get("files", []):
                folder = f.get("mimeType") == "application/vnd.google-apps.folder"
                out.append(FileInfo(
                    id=f.get("id"), name=f.get("name", ""),
                    size=f.get("size", 0), modified=f.get("modifiedTime"),
                    is_folder=folder, mime=f.get("mimeType", "")))
            page = data.get("nextPageToken")
            if not page:
                return out

    def stream_source(self, file_id):
        return ("%s/files/%s?alt=media" % (self.API, file_id),
                self.auth_headers())


class YandexDisk(Provider):
    name = "yandex"
    label = "Яндекс.Диск"

    API = "https://cloud-api.yandex.net/v1/disk"

    def auth_headers(self):
        return {"Authorization": "OAuth %s" % self.token}

    def list_folder(self, folder_id):
        """У Яндекса «идентификатор» -- это путь вида disk:/Папка."""
        out, offset = [], 0
        while True:
            params = {"path": folder_id, "limit": "200", "offset": str(offset),
                      "fields": "_embedded.items.name,_embedded.items.path,"
                                "_embedded.items.type,_embedded.items.size,"
                                "_embedded.items.modified,_embedded.total"}
            url = "%s/resources?%s" % (self.API, urllib.parse.urlencode(params))
            data = http_json(url, headers=self.auth_headers())
            emb = data.get("_embedded") or {}
            items = emb.get("items") or []
            for f in items:
                out.append(FileInfo(
                    id=f.get("path"), name=f.get("name", ""),
                    size=f.get("size", 0), modified=f.get("modified"),
                    is_folder=f.get("type") == "dir"))
            offset += len(items)
            if not items or offset >= int(emb.get("total") or 0):
                return out

    def stream_source(self, file_id):
        """Яндекс отдаёт ОДНОРАЗОВУЮ ссылку, её надо сначала запросить.

        Ссылка живёт минуты и уже содержит авторизацию, поэтому заголовок
        к ней не нужен -- и слать его туда не следует.
        """
        url = "%s/resources/download?%s" % (
            self.API, urllib.parse.urlencode({"path": file_id}))
        data = http_json(url, headers=self.auth_headers())
        href = data.get("href")
        if not href:
            raise CloudError("Яндекс.Диск не выдал ссылку на скачивание")
        return href, {}


PROVIDERS = {p.name: p for p in (GoogleDrive, YandexDisk)}


def make_provider(name, token, renew=None, on_renew=None):
    if name not in PROVIDERS:
        raise CloudError("неизвестное хранилище: %s" % name)
    return PROVIDERS[name](token, renew=renew, on_renew=on_renew)
