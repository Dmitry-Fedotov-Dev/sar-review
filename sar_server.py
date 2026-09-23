#!/usr/bin/env python3
"""
SAR Server — self-hosted многопользовательский веб-интерфейс поверх
sar_video_review.py / sar_photo_review.py.

С этой версии обработка видео/фото вынесена в ОТДЕЛЬНЫЙ процесс —
sar_worker.py. sar_server.py теперь только читает общую БД и отдаёт
страницы; он ничего не запускает и не следит за папкой сам. Это значит:
перезапуск/обновление sar_server.py НЕ прерывает текущую обработку видео —
она продолжается в sar_worker.py независимо, сервер просто на пару секунд
перестаёт отвечать на HTTP-запросы, а потом снова видит актуальное
состояние из БД как ни в чём не бывало.

Что делает:
  - отдаёт список файлов (данные о них собирает sar_worker.py) со статусом
    обработки, прогрессом, живым логом
  - после готовности — интерфейс сцен (report.html) с трекингом просмотров,
    и отдельный ручной плеер с разметкой боксов и наблюдениями
  - доступ по общему паролю + свободное имя (для атрибуции статистики)

Запуск (нужны ОБА процесса одновременно, в двух отдельных окнах/сессиях):
    python sar_worker.py
    python sar_server.py

Зависимости:
    pip install flask ultralytics opencv-python numpy --break-system-packages
"""

import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta

from flask import (Flask, request, session, redirect, url_for, jsonify,
                    send_from_directory, send_file, make_response, g, Response)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_cloud
import sar_common
import sar_health
from sar_video_review import (parse_srt_telemetry, lookup_telemetry, load_config as load_detection_config,
                               Hit, group_hits_into_scenes)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Индекс папки telemetry/ (см. sar_common.build_telemetry_index), строится
# один раз при старте сервера в main() -- не на каждый запрос плеера.
_TELEMETRY_INDEX = {"by_stem": {}, "by_timestamp": []}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
    return g.db



# ---------------------------------------------------------------------------
# Покрытие сцен по времени (для полоски на странице списка файлов)
# ---------------------------------------------------------------------------

def merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def coverage_buckets(merged, duration, n_buckets=50):
    if duration <= 0:
        return [0.0] * n_buckets
    buckets = [0.0] * n_buckets
    bucket_len = duration / n_buckets
    for s, e in merged:
        b_start = max(0, int(s / bucket_len))
        b_end = min(n_buckets - 1, int(e / bucket_len))
        for b in range(b_start, b_end + 1):
            buckets[b] = 1.0
    return buckets


def get_report_stats(conn, report_id, duration_sec):
    rows = conn.execute(
        "SELECT viewer_name, start_sec, end_sec FROM watch_segments WHERE report_id=?",
        (report_id,)).fetchall()
    viewers = set(r["viewer_name"] for r in rows)
    intervals = [(r["start_sec"], r["end_sec"]) for r in rows]
    merged = merge_intervals(intervals)
    covered = sum(e - s for s, e in merged)
    percent = round(100.0 * covered / duration_sec, 1) if duration_sec and duration_sec > 0 else None
    buckets = coverage_buckets(merged, duration_sec or 0) if duration_sec else []
    return {"viewer_count": len(viewers), "percent": percent, "buckets": buckets}


# ---------------------------------------------------------------------------
# FLASK APP
# ---------------------------------------------------------------------------

ONLINE_WINDOW_SEC = 40  # heartbeat раз в 15с -> 40с прощает одну пропущенную отправку

# кто угодно с общим паролем может ввести это имя при входе (полей "роль"/
# "аккаунт" в системе пока нет вообще -- см. UPLOADER_NAME ниже) -- это
# осознанный временный барьер "от случайности", а не настоящая авторизация.
# Планируется заменить нормальной ролевой моделью.
UPLOADER_NAME = "uploader"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 ГБ -- щедро, но не бесконечно


def init_secret_key():
    key_path = os.path.join(DATA_DIR, "secret.key")
    if os.path.exists(key_path):
        return open(key_path, "r", encoding="utf-8").read().strip()
    key = secrets.token_hex(32)
    with open(key_path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def _metrics_start():
    g._t0 = time.perf_counter()


@app.after_request
def _metrics_done(response):
    """Учёт каждого запроса.

    Группируем по ПРАВИЛУ маршрута (request.endpoint), а не по фактическому
    адресу: иначе /report/<id>/ породит отдельную метрику на каждый отчёт, и
    в Prometheus окажутся десятки тысяч рядов вместо одного -- это его
    известным образом убивает.

    Сам учёт не должен ронять ответ ни при каких обстоятельствах: метрика
    полезна, но не настолько, чтобы из-за неё человек не увидел страницу.
    """
    try:
        t0 = getattr(g, "_t0", None)
        if t0 is not None:
            sar_health.record_request(
                request.endpoint, request.method,
                response.status_code, time.perf_counter() - t0)
    except Exception:                    # noqa: BLE001
        pass
    return response


# Скрипт перевода подключается ОДНОЙ точкой, а не правкой четырнадцати
# шаблонов: heartbeat присутствия однажды стоял на 3 страницах из 6 ровно
# потому, что его разносили копированием. Здесь же любая новая страница
# получает переключатель сама, без отдельного действия.
_I18N_TAG = '<script src="/static/i18n.js" defer></script>'


@app.after_request
def _inject_i18n(response):
    """Дописывает тег скрипта перевода в HTML-ответы.

    Только HTML и только при наличии </body>: на JSON, картинки и видео
    это не должно влиять никак. Сбой внедрения не имеет права уронить
    ответ -- страница без переключателя языка работает, страница с
    ошибкой 500 не работает.
    """
    try:
        ctype = (response.headers.get("Content-Type") or "")
        if "text/html" not in ctype:
            return response
        if response.direct_passthrough or not response.is_sequence:
            return response
        body = response.get_data(as_text=True)
        if "</body>" not in body or _I18N_TAG in body:
            return response
        response.set_data(body.replace("</body>", _I18N_TAG + "</body>", 1))
    except Exception:                    # noqa: BLE001
        # Проглатываем осознанно: перевод -- удобство, а не работа
        # платформы. Но молча не оставляем -- пишем в журнал сервера.
        app.logger.warning("i18n: не удалось внедрить скрипт", exc_info=True)
    return response


@app.before_request
def require_login():
    # /healthz и /metrics открыты намеренно: их опрашивает внешний монитор,
    # у которого нет и не должно быть пароля от платформы. Отдают только
    # эксплуатационные счётчики -- ни имён, ни координат, ни содержимого
    # находок; проверено тестом, чтобы это не расползлось при правках.
    open_paths = ("/login", "/static", "/guide", "/healthz", "/metrics")
    if request.path.startswith(open_paths):
        return None
    if not session.get("authed"):
        return redirect(url_for("login_page", next=request.path))
    return None


# --- пульс присутствия ---
#
# Один и тот же код нужен на КАЖДОЙ странице, где работает вошедший
# человек. Раньше он был вписан руками в три страницы (список файлов,
# страница обработки, плеер), а страницы операций появились позже -- и
# пульса им никто не добавил. В итоге "онлайн" считал только тех, кто
# открыл плеер: человек, зашедший на платформу, выбирающий операцию и
# читающий список материалов, числился отсутствующим. Именно туда после
# входа и попадает большинство.
#
# Поэтому реализация здесь одна и подставляется во все страницы разом --
# чтобы следующая новая страница не завела четвёртую копию и не завела
# заодно тот же баг.
#
# На /guide пульса намеренно нет: эта страница открыта БЕЗ входа (см.
# open_paths выше), и присутствие оттуда означало бы "онлайн" для того,
# кого мы не опознали.
HEARTBEAT_JS = """<script>
async function heartbeat() {
  try {
    const res = await fetch('/api/heartbeat', { method: 'POST' });
    const data = await res.json();
    // Счётчик есть не на каждой странице -- присутствие всё равно должно
    // отмечаться. Поэтому проверяем элемент, а не полагаемся на catch.
    const el = document.getElementById('online-count');
    if (el && data.count !== undefined) el.textContent = data.count;
  } catch (e) {
    // НЕ глухой catch: ровно пустой catch однажды спрятал сломанный
    // счётчик онлайна, и баг нашёл человек, а не тест (см. CLAUDE.md).
    console.warn('пульс присутствия не прошёл', e);
  }
}
heartbeat();
setInterval(heartbeat, 15000);
</script>"""

# Тот же код для шаблонов, проходящих через .format(): там фигурные скобки
# JS обязаны быть удвоены, иначе format примет их за подстановку. Версия
# выводится из основной, чтобы две не разъехались при правке.
HEARTBEAT_JS_FORMAT = HEARTBEAT_JS.replace("{", "{{").replace("}", "}}")


# --- страницы ---

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>SAR — вход</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; background:#111; color:#eee;
       display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
form {{ background:#1b1b1b; padding:30px; border-radius:10px; border:1px solid #333; width:280px; }}
h1 {{ font-size:18px; margin-top:0; }}
input {{ width:100%; padding:10px; margin-bottom:12px; border-radius:6px; border:1px solid #444;
         background:#0d0d0d; color:#eee; box-sizing:border-box; }}
button {{ width:100%; padding:10px; border-radius:6px; border:none; background:#3355aa;
          color:#fff; font-weight:bold; cursor:pointer; }}
.err {{ color:#ff6666; font-size:13px; margin-bottom:10px; }}
</style></head>
<body>
<form method="post">
  <h1>SAR Review — вход</h1>
  {error_html}
  <input name="name" placeholder="Ваше имя" required>
  <input name="password" type="password" placeholder="Пароль" required>
  <button type="submit">Войти</button>
</form>
</body></html>"""


def _login_response(name, next_url=None):
    # После входа человек попадает в СПИСОК ОПЕРАЦИЙ, а не в общий список
    # файлов. Иначе ссылка из бота приводила в кучу всех материалов сразу,
    # мимо той структуры, ради которой операции и заводились.
    resp = make_response(redirect(next_url or url_for("operations_page")))
    resp.set_cookie("sar_viewer_name", urllib.parse.quote(name), httponly=False, samesite="Lax")
    return resp



@app.route("/login", methods=["GET", "POST"])
def login_page():
    error_html = ""

    # 1) Персональная ссылка из Telegram-бота: /login?key=<токен>.
    # Человек попадает внутрь уже ОПОЗНАННЫМ -- имя не вводится руками, роль
    # берётся из записи бота (см. sar_common: ROLE_*). Именно это делает
    # модерацию осмысленной: аноним не может выдать себя за другого.
    key = (request.args.get("key") or "").strip()
    if key:
        person = sar_common.find_person_by_token(get_db(), key)
        if person is not None:
            session["authed"] = True
            session["verified"] = True
            session["tg_chat_id"] = person["chat_id"]
            session["role"] = person["role"] or sar_common.DEFAULT_ROLE
            name = sar_common.display_name_for(person)
            session["viewer_name"] = name
            return _login_response(name, request.args.get("next"))
        error_html = ('<div class="err">Ссылка недействительна или доступ отозван.<br>'
                      'Запросите новую командой /help у бота.</div>')

    # 2) Общий пароль -- вход остаётся для работы в поле с чужого устройства,
    # но такой человек АНОНИМЕН: смотреть и размечать может, комментировать нет
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:60]
        password = request.form.get("password") or ""
        if password == SERVER_CFG["shared_password"] and name:
            session["authed"] = True
            session["verified"] = False
            session["role"] = sar_common.DEFAULT_ROLE
            session.pop("tg_chat_id", None)
            session["viewer_name"] = name
            return _login_response(name, request.args.get("next"))
        error_html = '<div class="err">Неверный пароль или не указано имя</div>'
    return LOGIN_HTML.format(error_html=error_html)


def current_role():
    """Роль текущего посетителя. Аноним (вход по общему паролю) роли не имеет
    -- ему доступно только чтение и разметка находок."""
    if not session.get("verified"):
        return None
    return session.get("role") or sar_common.DEFAULT_ROLE


def can_comment():
    return current_role() in sar_common.ROLES_CAN_COMMENT


def is_moderator():
    """Модератор ИЛИ администратор -- админ по определению может всё, что
    может модератор (см. ROLES_CAN_MODERATE)."""
    return current_role() in sar_common.ROLES_CAN_MODERATE


def is_admin():
    return current_role() == sar_common.ROLE_ADMIN


def can_upload():
    """Загрузка файлов: роль администратора ИЛИ старый вход под именем
    'uploader'.

    Имя 'uploader' сохранено намеренно, как переходный вариант: по нему
    заходят с общего пароля в поле, где личной ссылки может не быть под
    рукой. Но это по-прежнему не настоящая проверка -- любой, кто знает
    общий пароль, может так назваться (см. UPLOADER_NAME). Роль admin --
    честная замена: её нельзя присвоить себе, её выдаёт координатор."""
    if is_admin():
        return True
    return session.get("viewer_name") == UPLOADER_NAME


# --- гид волонтёра и презентация -- статические страницы, открыты БЕЗ входа
# (см. open_paths в require_login выше). Раньше жили как приватные claude.ai
# artifact-страницы -- пришлось убрать эту зависимость: доступ там нужно
# было включать отдельно через Share, и это дважды всплыло как "ссылки не
# действительны" у людей, переходящих из Telegram-бота. Теперь это часть
# самого сервиса, доступна по тому же туннелю, без отдельной настройки. Ни
# .format(), ни {{}}-плейсхолдеров тут не нужно -- содержимое статичное,
# фигурные скобки в CSS можно не экранировать.
GUIDE_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Полевой гид SAR Review</title>
<style>
:root {
  --ground: #F4F6F5;
  --surface: #E7EBE9;
  --surface-2: #DCE2DF;
  --ink: #1A2229;
  --ink-soft: #4B565C;
  --contour: #746B5E;
  --contour-soft: #A79E8F;
  --glacier: #1B6B79;
  --glacier-tint: #E3EEEF;
  --rescue: #C8501C;
  --rescue-tint: #FBE7DC;
  --rescue-strong: #A83E12;
  --line: #CFD6D2;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
  --sans: -apple-system, "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #10161A;
    --surface: #1A2227;
    --surface-2: #212B31;
    --ink: #E7EDEB;
    --ink-soft: #A9B4B1;
    --contour: #8A8172;
    --contour-soft: #5C554A;
    --glacier: #5FB8C7;
    --glacier-tint: #16282C;
    --rescue: #FF8A52;
    --rescue-tint: #2A1D14;
    --rescue-strong: #FFA574;
    --line: #2A343A;
  }
}
:root[data-theme="dark"] {
  --ground: #10161A;
  --surface: #1A2227;
  --surface-2: #212B31;
  --ink: #E7EDEB;
  --ink-soft: #A9B4B1;
  --contour: #8A8172;
  --contour-soft: #5C554A;
  --glacier: #5FB8C7;
  --glacier-tint: #16282C;
  --rescue: #FF8A52;
  --rescue-tint: #2A1D14;
  --rescue-strong: #FFA574;
  --line: #2A343A;
}

* { box-sizing: border-box; }
body {
  background: var(--ground);
  color: var(--ink);
  font-family: var(--sans);
  margin: 0;
  padding: 0 20px 80px;
  line-height: 1.6;
}
.wrap { max-width: 720px; margin: 0 auto; }

.masthead {
  padding: 48px 0 28px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 40px;
}
.eyebrow {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--contour);
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.eyebrow::before { content: "◆"; color: var(--glacier); font-size: 10px; }
h1 {
  font-size: 34px;
  font-weight: 800;
  letter-spacing: -0.01em;
  margin: 0 0 10px;
  text-wrap: balance;
}
.masthead p {
  color: var(--ink-soft);
  font-size: 16px;
  margin: 0;
  max-width: 58ch;
}

.contents {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 18px 22px;
  margin-bottom: 44px;
}
.contents .label {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--contour);
  margin-bottom: 10px;
}
.contents ol {
  margin: 0;
  padding-left: 20px;
  columns: 2;
  column-gap: 24px;
  font-size: 14px;
}
.contents li { margin-bottom: 6px; break-inside: avoid; }
.contents a { color: var(--ink); text-decoration: none; }
.contents a:hover { color: var(--glacier); }

section.step {
  margin-bottom: 52px;
  scroll-margin-top: 20px;
}
.step-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  margin-bottom: 14px;
}
.step-num {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
  color: var(--glacier);
  background: var(--glacier-tint);
  border-radius: 5px;
  padding: 3px 9px;
  flex-shrink: 0;
}
h2 {
  font-size: 21px;
  font-weight: 700;
  margin: 0;
  text-wrap: balance;
}
.step p { margin: 0 0 12px; }
.step ul, .step ol.sub { margin: 0 0 14px; padding-left: 22px; }
.step li { margin-bottom: 6px; }
.step h3 {
  font-size: 15.5px;
  font-weight: 700;
  margin: 20px 0 6px;
  color: var(--ink);
}
.step h3:first-of-type { margin-top: 14px; }

.divider {
  display: flex;
  align-items: center;
  gap: 14px;
  margin: 0 0 44px;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--contour);
}
.divider::before, .divider::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--line);
}

kbd, .ui {
  font-family: var(--mono);
  font-size: 0.92em;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 1px 6px;
  color: var(--ink);
}

.callout {
  border-radius: 10px;
  padding: 16px 18px;
  margin: 16px 0;
  border: 1px solid var(--line);
  display: flex;
  gap: 12px;
}
.callout .icon { font-size: 18px; flex-shrink: 0; line-height: 1.4; }
.callout .body { font-size: 14.5px; }
.callout .body strong { display: block; margin-bottom: 4px; font-size: 15px; }
.callout.warn {
  background: var(--rescue-tint);
  border-color: color-mix(in srgb, var(--rescue) 35%, var(--line));
}
.callout.warn .body strong { color: var(--rescue-strong); }
.callout.info {
  background: var(--glacier-tint);
  border-color: color-mix(in srgb, var(--glacier) 30%, var(--line));
}
.callout.info .body strong { color: var(--glacier); }

.critical {
  background: var(--rescue-tint);
  border: 1.5px solid var(--rescue);
  border-radius: 12px;
  padding: 22px 24px;
  margin: 20px 0 24px;
}
.critical .tag {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--rescue-strong);
  font-weight: 700;
  margin-bottom: 10px;
}
.critical h3 { margin: 0 0 8px; font-size: 18px; color: var(--rescue-strong); }
.critical p { margin: 0 0 8px; font-size: 15px; }
.critical p:last-child { margin-bottom: 0; }

.badge {
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
  padding: 2px 8px;
  border-radius: 4px;
  color: #fff;
}
.badge.model { background: #5533aa; }
.badge.color { background: #aa7a00; }
.badge.done { background: #22703a; }
.badge.processing { background: #8a6d00; }
.badge.queued { background: #6b6b6b; }
.badge.error { background: #7a1f1f; }

.mock {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 14px 16px;
  margin: 14px 0;
  font-size: 13.5px;
}
.mock-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--line);
}
.mock-row:last-child { border-bottom: none; }
.mock-thumb {
  width: 40px; height: 24px; border-radius: 4px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--surface-2), var(--line));
}
.mock-name { flex: 1; font-family: var(--mono); font-size: 12.5px; }

.coord-line { display: flex; align-items: baseline; gap: 8px; font-family: var(--mono); font-size: 13px; margin: 4px 0; }
.coord-line .tag2 { font-size: 15px; }
.coord-line.est { color: var(--glacier); }

table.field {
  width: 100%;
  border-collapse: collapse;
  font-size: 13.5px;
  margin: 14px 0;
}
table.field th, table.field td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
}
table.field th { color: var(--contour); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }

.faq {
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-bottom: 8px;
  overflow: hidden;
}
.faq summary {
  padding: 12px 16px;
  cursor: pointer;
  font-weight: 600;
  font-size: 14.5px;
  background: var(--surface);
  list-style: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.faq summary::-webkit-details-marker { display: none; }
.faq summary::after { content: "+"; color: var(--glacier); font-weight: 400; font-size: 18px; }
.faq[open] summary::after { content: "–"; }
.faq .a { padding: 4px 16px 14px; font-size: 14px; color: var(--ink-soft); }

footer {
  margin-top: 60px;
  padding-top: 20px;
  border-top: 1px solid var(--line);
  font-size: 12.5px;
  color: var(--contour);
  font-family: var(--mono);
}
</style></head>
<body>
<div class="wrap">

  <div class="masthead">
    <div class="eyebrow">SAR Review — для волонтёров</div>
    <h1>Как смотреть видео с дрона и не пропустить находку</h1>
    <p>Сначала — методика отсмотра: как смотреть, куда смотреть и по каким признакам искать. Потом — работа с самой платформой. Разделы <a href="#color">05</a> и <a href="#coords">09</a> прочитайте обязательно: первый про то, почему нельзя искать «яркое», второй — до того как начнёте передавать координаты дальше.</p>
  </div>

  <nav class="contents">
    <div class="label">Методика отсмотра</div>
    <ol>
      <li><a href="#how">Как смотреть</a></li>
      <li><a href="#where">Куда смотреть в первую очередь</a></li>
      <li><a href="#signs">По каким признакам искать</a></li>
      <li><a href="#what">Что именно ищем</a></li>
      <li><a href="#color">Про цвет отдельно</a></li>
    </ol>
    <div class="label" style="margin-top:16px">Работа с платформой</div>
    <ol>
      <li><a href="#login">Вход в систему</a></li>
      <li><a href="#list">Список файлов</a></li>
      <li><a href="#scenes">Автоматический отчёт</a></li>
      <li><a href="#coords">Координаты — прочитать обязательно</a></li>
      <li><a href="#telemetry">Полная телеметрия кадра</a></li>
      <li><a href="#player">Ручной плеер и разметка</a></li>
      <li><a href="#upload">Загрузка файлов</a></li>
      <li><a href="#help">Если что-то не работает</a></li>
    </ol>
  </nav>

  <section class="step" id="how">
    <div class="step-head"><span class="step-num">01</span><h2>Как смотреть</h2></div>

    <h3>Темп — не быстрее 0.5×</h3>
    <p>Участки со сложным рельефом (трещины, скалы, кулуары) проходить <strong>покадрово</strong>.
    Блик от металла живёт один-два кадра — на обычной скорости он теряется полностью.</p>

    <h3>Сетка</h3>
    <p>Мысленно делите кадр на 6 или 9 секторов и просматривайте каждый отдельно.
    Без этого взгляд залипает в центре, а находки чаще всего <strong>у краёв</strong>.</p>

    <div class="callout warn">
      <div class="icon">⏱</div>
      <div class="body"><strong>Смены каждые 20–30 минут</strong>После 40 минут непрерывного
      просмотра человек перестаёт видеть физически — даже если уверен в обратном. Это не про
      усталость и силу воли, это про физиологию зрения.</div>
    </div>

    <h3>Отмечать всё просмотренное, а не только находки</h3>
    <p>Пустые участки тоже отмечайте, со статусом:</p>
    <ul>
      <li><strong>просмотрено чисто</strong> — condition хорошие, ничего нет;</li>
      <li><strong>просмотрено плохо</strong> — пересвет, туман, смаз, шли слишком быстро.</li>
    </ul>
    <p>Знать, <em>где смотрели плохо</em>, так же важно, как знать, где нашли: по этим участкам
    планируют повторный облёт.</p>

    <h3>Формат заявки на кандидата</h3>
    <ol class="sub">
      <li>имя файла</li>
      <li>таймкод</li>
      <li>координаты из телеметрии</li>
      <li>скриншот</li>
      <li>короткое описание</li>
      <li>уверенность по шкале 1–5</li>
      <li>нужен ли повторный облёт</li>
    </ol>

    <div class="callout info">
      <div class="icon">ⓘ</div>
      <div class="body"><strong>Сомнительное фиксируем всегда</strong>Отсеивают потом, при
      сведении. Лучше лишняя отметка, чем пропущенный человек.</div>
    </div>
  </section>

  <section class="step" id="where">
    <div class="step-head"><span class="step-num">02</span><h2>Куда смотреть в первую очередь</h2></div>
    <p>На этих участках замедляйтесь:</p>
    <ul>
      <li><strong>Бергшрунд, ранклюфт, подгорные трещины, мульды.</strong></li>
      <li><strong>Устья и выкаты кулуаров</strong>, всё под скальными сбросами.</li>
      <li><strong>Конусы выноса целиком</strong>, включая края и низ. Тело обычно ниже и глубже.
        Лёгкие вещи (пуховка, каремат, каска) уходят дальше и вбок — их разносит ветром.</li>
      <li><strong>Трещины ледника, ледопад, краевые разломы.</strong> На кадрах сверху это чёрные
        щели, внутри ничего не читается. Такие места помечайте как <em>требующие пересъёмки
        сбоку</em>.</li>
      <li><strong>Скальные полки, ниши, места под нависаниями</strong> — возможный вынужденный
        бивуак.</li>
      <li><strong>Промоины и русла.</strong> Вода тянет вниз.</li>
    </ul>
  </section>

  <section class="step" id="signs">
    <div class="step-head"><span class="step-num">03</span><h2>По каким признакам искать</h2></div>
    <p><strong>Цвет ненадёжен</strong>, поэтому опирайтесь на пять признаков сразу.</p>

    <table class="field">
      <tr><th>Признак</th><th>На что это похоже</th></tr>
      <tr><td><strong>Геометрия</strong></td>
        <td>Прямая линия, прямой угол, правильная окружность в горах почти не встречаются.
            Верёвка — тонкая прямая или ровная дуга. Палатка — многоугольник. Лямка, оттяжка,
            петля на скале.</td></tr>
      <tr><td><strong>Блик</strong></td>
        <td>Кошки, ледоруб, карабины, ледобур, стекло очков, фольга спасодеяла. Короткая яркая
            вспышка не на своём месте.</td></tr>
      <tr><td><strong>Тень</strong></td>
        <td>При низком солнце любой предмет выше уровня снега даёт тень длиннее себя. Часто
            аномальная тень видна лучше самого предмета.</td></tr>
      <tr><td><strong>Тёмное пятно на снегу</strong></td>
        <td>Самый заметный сигнал вообще — сильнее любого яркого цвета.</td></tr>
      <tr><td><strong>Изменение и движение</strong></td>
        <td>Если участок снят дважды — сравнивайте кадры между собой. Ткань полощется на ветру,
            это даёт шевеление между соседними кадрами.</td></tr>
    </table>
  </section>

  <section class="step" id="what">
    <div class="step-head"><span class="step-num">04</span><h2>Что именно ищем</h2></div>
    <p>Рюкзаки, верёвки, фрагменты одежды, цветные ботинки, следы, тени на снегу, линии отрыва,
    конусы, свежие камни. И отдельно:</p>
    <ul>
      <li><strong>Утоптанные площадки</strong>, следы копания, снежная пещера, вход в неё,
        вентиляционное отверстие.</li>
      <li><strong>Борозда скольжения или срыва</strong>, воронка от удара, дорожка вещей вниз по
        линии падения.</li>
      <li><strong>Оставленное снаряжение как маркер пути</strong>: петли на скалах, крюк, ледобур,
        станция, фиксированная верёвка.</li>
      <li><strong>Следы кошек и ледоруба на льду</strong>, свежие сколы льда.</li>
      <li><strong>Бивуачный мусор</strong>: обёртки, газовый баллон, жёлтые пятна на снегу.</li>
      <li><strong>Свежесть камня.</strong> Свежий лежит поверх снега, без изморози, часто с
        бороздой позади. Старый вмёрз, вокруг ореол протаивания.</li>
      <li><strong>Снежные комья и шары</strong> в теле выноса, вырванный грунт.</li>
    </ul>
  </section>

  <section class="step" id="color">
    <div class="step-head"><span class="step-num">05</span><h2>Про цвет отдельно</h2></div>

    <div class="critical">
      <div class="tag">Главная установка</div>
      <h3>Ищем не «яркое», а «не снег и не камень»</h3>
      <p>Синее, зелёное и чёрное сливаются с породой и тенью сильнее всего, а исходные цвета
      курток нам <strong>достоверно неизвестны</strong>. Установка «ищем яркое» приводит к тому,
      что тёмная одежда в тени проходит мимо взгляда.</p>
    </div>

    <h3>В тени на снегу всё уходит в синий</h3>
    <p>Красный пуховик в тени выглядит тёмно-серым пятном. Не полагайтесь на глаз — поднимайте
    насыщенность и уровни на подозрительных участках.</p>

    <h3>Снег пересвечен, в светах деталей нет</h3>
    <p>Если объект лежит на ярко освещённом склоне, он может быть просто выжжен. Такие зоны
    помечайте в отчёте как «просмотрено плохо».</p>

    <div class="callout warn">
      <div class="icon">⚠</div>
      <div class="body"><strong>Ложные срабатывания, которые будут точно</strong>
      Оранжевые и жёлтые лишайники на скалах · красная водоросль на снегу · охристые и железистые
      породы · тень от камня-останца.</div>
    </div>
  </section>

  <div class="divider">Работа с платформой</div>

  <section class="step" id="login">
    <div class="step-head"><span class="step-num">06</span><h2>Вход в систему</h2></div>
    <p>Адрес сайта и общий пароль вам даёт координатор операции — это локальный сервис, работающий на месте, обычно без выхода в открытый интернет.</p>
    <ul>
      <li><strong>Откройте адрес в браузере на ноутбуке или компьютере.</strong> С телефона
        сервис тоже работает, но для разбора видео он плохо подходит: человека на снегу
        видно как несколько пикселей, и на маленьком экране находку легко пропустить.
        На большом экране удобнее зум, перемотка и разметка находок мышью.</li>
      <li>Введите <span class="ui">пароль</span> и своё <span class="ui">имя</span> — любое, по нему система отличает, кто что уже посмотрел.</li>
      <li>Имя можно сменить в любой момент ссылкой «сменить» наверху страницы.</li>
    </ul>
    <div class="callout info">
      <div class="icon">ⓘ</div>
      <div class="body"><strong>Пароль общий на всю команду</strong>Это осознанное упрощение для работы в закрытой сети операции. Не пересылайте пароль за пределы команды.</div>
    </div>
  </section>

  <section class="step" id="list">
    <div class="step-head"><span class="step-num">07</span><h2>Список файлов</h2></div>
    <p>Главная страница — таблица всех видео и фото с дрона. У каждого файла статус и превью первого кадра (для видео):</p>
    <div class="mock">
      <div class="mock-row"><div class="mock-thumb"></div><div class="mock-name">DJI_20260812140054_0003_Z.MP4</div><span class="badge done">готово</span></div>
      <div class="mock-row"><div class="mock-thumb"></div><div class="mock-name">DJI_20260812133836_0008_Z.MP4</div><span class="badge processing">обрабатывается</span></div>
      <div class="mock-row"><div class="mock-thumb"></div><div class="mock-name">DJI_20260812135747_0002_Z.MP4</div><span class="badge queued">в очереди</span></div>
    </div>
    <table class="field">
      <tr><th>Статус</th><th>Что доступно</th></tr>
      <tr><td><span class="badge queued">в очереди</span></td><td>Ждёт обработки. Ручной плеер уже открывается — можно начинать смотреть и отмечать находки, не дожидаясь модели.</td></tr>
      <tr><td><span class="badge processing">обрабатывается</span></td><td>Идёт анализ. Автоматический отчёт уже открывается, рамки модели появляются по ходу дела.</td></tr>
      <tr><td><span class="badge done">готово</span></td><td>Анализ полностью завершён.</td></tr>
      <tr><td><span class="badge error">ошибка</span></td><td>Автоматический анализ не удался — ручной плеер всё равно работает как обычно.</td></tr>
    </table>
    <p>Кнопка <span class="ui">▶ плеер</span> рядом с файлом открывает ручной просмотр — доступна всегда, независимо от статуса.</p>
  </section>

  <section class="step" id="scenes">
    <div class="step-head"><span class="step-num">08</span><h2>Автоматический отчёт</h2></div>
    <p>Клик по готовому или ещё обрабатывающемуся файлу открывает отчёт — сетку карточек. Каждая карточка — не один кадр, а <strong>сцена</strong>: модель сама сгруппировала все кадры, где, вероятно, один и тот же объект.</p>
    <p>Бейдж на карточке говорит, чем это найдено:</p>
    <p><span class="badge model">МОДЕЛЬ</span> — распознано как объект (человек, снаряжение и т.п.) &nbsp; <span class="badge color">ЦВЕТ</span> — яркое пятно нетипичного цвета (часто одежда/палатка, которую модель не опознала как объект, но которое выделяется на фоне скал и снега)</p>
    <p>Клик по карточке → все кадры сцены. Клик по кадру → полноэкранный просмотр: колесо мыши приближает, можно включать/выключать рамки поверх фото.</p>
    <div class="callout info">
      <div class="icon">ⓘ</div>
      <div class="body"><strong>Это приоритизация внимания, а не готовый вердикт</strong>Модель подсвечивает, куда посмотреть в первую очередь. Решение — за человеком, как и всегда.</div>
    </div>
  </section>

  <section class="step" id="coords">
    <div class="step-head"><span class="step-num">09</span><h2>Координаты</h2></div>
    <p>На карточке сцены и в полноэкранном просмотре — два разных набора координат. Это два разных факта, не опечатка:</p>
    <div class="mock">
      <div class="coord-line"><span class="tag2">📍</span> дрон: 39.480800, 73.592544 <span class="ui">🗺 карта</span></div>
      <div class="coord-line est"><span class="tag2">🎯</span> вероятные координаты объекта: 39.476466, 73.595053 <span class="ui">🗺 карта</span></div>
    </div>
    <div class="critical">
      <div class="tag">Прочитать перед тем, как передавать координаты дальше</div>
      <h3>«Вероятные координаты» — это расчёт, а не измерение</h3>
      <p><strong>📍 Координаты дрона</strong> — где находился сам дрон в момент кадра. Это точные данные GPS с телеметрии.</p>
      <p><strong>🎯 Вероятные координаты объекта</strong> — программа пытается прикинуть, куда именно смотрела камера, по высоте полёта и углу наклона подвеса. Это <strong>оценка с реальной погрешностью</strong>, которая растёт на больших расстояниях и при съёмке почти вдоль горизонта. Иногда оценки не будет вообще — программа специально не показывает её, если расчёт получается ненадёжным, вместо того чтобы гадать.</p>
      <p>Прежде чем отправлять наземную группу по «вероятным координатам» — сверьтесь с картой на глаз (кнопка <span class="ui">🗺 карта</span> у каждой пары координат) и трезво оцените обстановку. При сомнении ориентируйтесь на координаты дрона плюс то, что видно на кадре.</p>
    </div>
  </section>

  <section class="step" id="telemetry">
    <div class="step-head"><span class="step-num">10</span><h2>Полная телеметрия кадра</h2></div>
    <p>Под координатами — раскрывающийся блок <span class="ui">📡 Вся телеметрия кадра</span>. Внутри — всё, что записал дрон на этот момент: высота (относительная и над уровнем моря), углы подвеса камеры, приближение (зум) и другие технические параметры.</p>
    <ul>
      <li>Клик раскрывает блок и <strong>сразу копирует весь текст в буфер обмена</strong> — можно сразу вставить в сообщение координатору.</li>
      <li>Кнопка <span class="ui">📋 копировать</span> внутри — если нужно скопировать ещё раз.</li>
    </ul>
  </section>

  <section class="step" id="player">
    <div class="step-head"><span class="step-num">11</span><h2>Ручной плеер и разметка</h2></div>
    <p>Кнопка <span class="ui">▶ плеер</span> открывает обычное видео с перемоткой — свой собственный осмотр, не полагаясь только на модель.</p>
    <ol class="sub">
      <li>Включите <span class="ui">🖊 Режим разметки</span>.</li>
      <li>Потяните мышью/пальцем по видео вокруг находки — видео само поставится на паузу.</li>
      <li>Впишите, что это, и заметку — сохранится с привязкой к таймкоду, вашему имени и координатам (если есть телеметрия).</li>
    </ol>
    <p>Чекбокс <span class="ui">показывать рамки модели</span> накладывает поверх видео и автоматические находки — можно сверять их со своими на ходу.</p>
    <div class="callout info">
      <div class="icon">ⓘ</div>
      <div class="body"><strong>Полоса покрытия — общая для всей команды</strong>Система запоминает, какие участки видео уже реально посмотрели (кем угодно из команды) — это помогает не пересматривать одно и то же и не пропустить неосмотренные куски.</div>
    </div>
  </section>

  <section class="step" id="upload">
    <div class="step-head"><span class="step-num">12</span><h2>Загрузка файлов</h2></div>
    <p>Загружать новые видео и телеметрию с браузера может только тот, кто вошёл под именем <span class="ui">uploader</span> — обычно это координатор. Если у вас есть файлы для загрузки, но нет доступа — передайте их координатору напрямую (или физически скопируйте в папку на сервере, если есть доступ к машине).</p>
  </section>

  <section class="step" id="help">
    <div class="step-head"><span class="step-num">13</span><h2>Если что-то не работает</h2></div>
    <details class="faq">
      <summary>Файл долго висит «в очереди» и не начинает обрабатываться</summary>
      <div class="a">Обработка идёт по одному видео за раз. Если это не первый файл в очереди — дождитесь своей очереди, либо позовите координатора проверить, что фоновая обработка вообще запущена.</div>
    </details>
    <details class="faq">
      <summary>Нет превью у видео в списке</summary>
      <div class="a">Превью считается один раз и появляется не мгновенно после добавления файла. Подождите немного и обновите страницу.</div>
    </details>
    <details class="faq">
      <summary>У сцены совсем нет координат — ни дрона, ни оценки</summary>
      <div class="a">Значит для этого конкретного видео не нашлось телеметрии полёта (файл с данными GPS/высоты). Это не ошибка программы — просто такой файл телеметрии не был загружен либо не сохранился. Уточните у координатора.</div>
    </details>
    <details class="faq">
      <summary>Ничего не открывается, страница не отвечает</summary>
      <div class="a">Позовите координатора — возможно, нужно перезапустить сервис на месте. Ваша разметка и заметки при этом не теряются, они уже сохранены.</div>
    </details>
  </section>

  <footer>SAR Review · полевой гид для волонтёров</footer>
</div>
</body></html>"""


def _health_snapshot():
    """Факты и их оценка. Общий источник для /healthz, /metrics и тревог
    в боте -- чтобы дашборд, HTTP и телеграм не разошлись в показаниях."""
    conn = get_db()
    watch = os.path.abspath(SERVER_CFG["watch_dir"])
    _, _, db_path, _ = sar_common.resolve_paths(watch, SERVER_CFG.get("data_dir"))
    data_dir = os.path.dirname(db_path)
    # Папку копий НЕ передаём: её знает sar_common.backups_dir(), и это
    # единственное место, где она считается. Здесь стоял третий по счёту
    # самостоятельный расчёт того же пути ("на уровень выше watch_dir"), и
    # именно он перебивал общее значение -- проверка докладывала "последняя
    # копия 56 ч назад" сразу после свежей копии.
    facts = sar_health.collect(
        conn, watch,
        reports_dir=os.path.join(data_dir, "reports"))
    return facts, sar_health.evaluate(facts)


@app.route("/healthz")
def healthz():
    """Состояние платформы для человека и для внешнего монитора.

    Код ответа несёт смысл: 200 -- всё в порядке или есть предупреждения,
    503 -- критично. Внешние пингеры смотрят именно на код, поэтому
    предупреждение НЕ должно поднимать тревогу: диск на 8 ГБ -- повод
    заняться, а не повод будить дежурного ночью.
    """
    facts, checks = _health_snapshot()
    level = sar_health.overall(checks)
    body = {
        "status": level,
        "checks": {k: {"level": lv, "text": txt} for k, (lv, txt) in checks.items()},
        "facts": facts,
    }
    return jsonify(body), (503 if level == sar_health.CRIT else 200)


@app.route("/metrics")
def metrics():
    facts, checks = _health_snapshot()
    return Response(sar_health.render_prometheus(facts, checks),
                     mimetype="text/plain; version=0.0.4; charset=utf-8")


@app.route("/guide")
def guide_page():
    return GUIDE_PAGE_HTML


ABOUT_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>SAR Review в деле</title>
<style>
:root {
  --ground: #F4F6F5;
  --surface: #EAEEEC;
  --surface-2: #DCE2DF;
  --ink: #1A2229;
  --ink-soft: #4B565C;
  --contour: #746B5E;
  --glacier: #1B6B79;
  --glacier-tint: #E3EEEF;
  --glacier-strong: #0F4B56;
  --rescue: #C8501C;
  --rescue-tint: #FBE7DC;
  --rescue-strong: #A83E12;
  --line: #CFD6D2;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
  --sans: -apple-system, "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0E1417;
    --surface: #171F23;
    --surface-2: #1F282D;
    --ink: #E9EEEC;
    --ink-soft: #A9B4B1;
    --contour: #8A8172;
    --glacier: #5FB8C7;
    --glacier-tint: #142529;
    --glacier-strong: #8FD3DE;
    --rescue: #FF8A52;
    --rescue-tint: #2A1D14;
    --rescue-strong: #FFA574;
    --line: #283034;
  }
}
:root[data-theme="dark"] {
  --ground: #0E1417;
  --surface: #171F23;
  --surface-2: #1F282D;
  --ink: #E9EEEC;
  --ink-soft: #A9B4B1;
  --contour: #8A8172;
  --glacier: #5FB8C7;
  --glacier-tint: #142529;
  --glacier-strong: #8FD3DE;
  --rescue: #FF8A52;
  --rescue-tint: #2A1D14;
  --rescue-strong: #FFA574;
  --line: #283034;
}

* { box-sizing: border-box; }
body {
  background: var(--ground);
  color: var(--ink);
  font-family: var(--sans);
  margin: 0;
  padding: 0 20px 90px;
  line-height: 1.6;
}
.wrap { max-width: 760px; margin: 0 auto; }

.topo {
  height: 3px;
  margin: 0 0 0;
  background: repeating-linear-gradient(90deg, var(--contour) 0 18px, transparent 18px 30px);
  opacity: 0.55;
}

.hero {
  padding: 56px 0 40px;
}
.eyebrow {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--contour);
  margin-bottom: 18px;
}
.hero h1 {
  font-size: 40px;
  line-height: 1.15;
  font-weight: 800;
  letter-spacing: -0.015em;
  margin: 0 0 20px;
  text-wrap: balance;
  max-width: 15ch;
}
.hero h1 em {
  font-style: normal;
  color: var(--glacier-strong);
}
.hero .lede {
  font-size: 18px;
  color: var(--ink-soft);
  max-width: 56ch;
  margin: 0;
}

.provenance {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  background: var(--surface);
  border: 1px solid var(--line);
  border-left: 3px solid var(--glacier);
  border-radius: 8px;
  padding: 16px 18px;
  margin-top: 28px;
  font-size: 14.5px;
}
.provenance .mark { font-family: var(--mono); font-size: 18px; color: var(--glacier); }
.provenance strong { display: block; margin-bottom: 3px; }
.provenance span.loc { color: var(--ink-soft); }

section { margin: 56px 0; }
.head-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 10px;
}
.head-row .num { font-family: var(--mono); font-size: 12px; color: var(--contour); }
h2 { font-size: 22px; font-weight: 800; margin: 0; letter-spacing: -0.01em; }

.problem-line {
  font-size: 19px;
  line-height: 1.55;
  color: var(--ink);
  max-width: 58ch;
}
.problem-line b { color: var(--rescue-strong); font-weight: 700; }
.stat-row { display: flex; gap: 28px; margin-top: 22px; flex-wrap: wrap; }
.stat { min-width: 140px; }
.stat .n { font-family: var(--mono); font-size: 28px; font-weight: 700; color: var(--glacier-strong); font-variant-numeric: tabular-nums; }
.stat .d { font-size: 13px; color: var(--ink-soft); margin-top: 2px; }

.grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 14px;
}
@media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }
.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 18px 20px;
}
.card .icon { font-size: 20px; margin-bottom: 10px; display: block; }
.card h3 { font-size: 15.5px; margin: 0 0 6px; font-weight: 700; }
.card p { font-size: 13.5px; color: var(--ink-soft); margin: 0; }

.honest {
  background: var(--rescue-tint);
  border: 1px solid color-mix(in srgb, var(--rescue) 30%, var(--line));
  border-radius: 10px;
  padding: 22px 24px;
}
.honest ul { margin: 0; padding-left: 20px; }
.honest li { margin-bottom: 10px; font-size: 14.5px; }
.honest li:last-child { margin-bottom: 0; }
.honest li b { color: var(--rescue-strong); }

.status-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 15px;
  margin-bottom: 10px;
}
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--glacier); flex-shrink: 0; }

.closing {
  margin-top: 64px;
  padding-top: 28px;
  border-top: 1px solid var(--line);
  text-align: left;
}
.closing p { font-size: 16px; max-width: 54ch; color: var(--ink-soft); }
footer {
  margin-top: 40px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--contour);
}
</style></head>
<body>
<div class="topo"></div>
<div class="wrap">

  <div class="hero">
    <div class="eyebrow">SAR Review — для МЧС Кыргызской Республики</div>
    <h1>Часы видео с дрона. <em>Одна пара глаз.</em> Это и есть проблема.</h1>
    <p class="lede">Инструмент приоритизации внимания для поиска людей и снаряжения на видео и фото с дрона — не замена наземным группам и повторным облётам, а способ быстрее находить, куда именно им нужно смотреть.</p>

    <div class="provenance">
      <div class="mark">◆</div>
      <div>
        <strong>Разработан не в кабинете, а в поле</strong>
        <span class="loc">Во время реальной поисково-спасательной операции — поиск пропавших альпинистов в Алайском районе, у пика Корумду. Каждая функция здесь появилась в ответ на конкретную проблему, с которой команда столкнулась в процессе поиска, а не была придумана заранее.</span>
      </div>
    </div>
  </div>

  <section>
    <div class="head-row"><span class="num">01</span><h2>Проблема</h2></div>
    <p class="problem-line">Один вылет дрона — час и больше отснятого материала. Просмотреть его внимательно, кадр за кадром, способен человек, но не <b>уставший</b> человек на третьем часу поисковой операции, не <b>один и тот же</b> человек по десятому видео за день.</p>
    <div class="stat-row">
      <div class="stat"><div class="n">×1</div><div class="d">пара глаз на человека, без инструмента</div></div>
      <div class="stat"><div class="n">↓</div><div class="d">внимание падает с каждым часом и каждым видео</div></div>
      <div class="stat"><div class="n">?</div><div class="d">какие участки уже точно кто-то посмотрел</div></div>
    </div>
  </section>

  <section>
    <div class="head-row"><span class="num">02</span><h2>Что делает SAR Review</h2></div>
    <div class="grid">
      <div class="card">
        <span class="icon">🎯</span>
        <h3>Приоритизирует кадры</h3>
        <p>Модель просматривает всё видео и подсвечивает кадры, где, вероятно, есть человек или снаряжение — команда смотрит в первую очередь туда, а не всё подряд с нуля.</p>
      </div>
      <div class="card">
        <span class="icon">🟠</span>
        <h3>Ловит то, что пропустит модель</h3>
        <p>Отдельный детектор цветовых аномалий подсвечивает яркие пятна нетипичного цвета — часто одежда или снаряжение, которое стандартная модель на мелком масштабе не распознаёт как объект.</p>
      </div>
      <div class="card">
        <span class="icon">📍</span>
        <h3>Считает вероятные координаты</h3>
        <p>По GPS и телеметрии подвеса камеры прикидывает не только где был дрон, но и куда примерно смотрела камера — честно, с указанием, когда оценке нельзя доверять.</p>
      </div>
      <div class="card">
        <span class="icon">👥</span>
        <h3>Работает как командный инструмент</h3>
        <p>Несколько человек одновременно смотрят разные видео с разных устройств в локальной сети; система помнит, какие участки уже реально просмотрены.</p>
      </div>
      <div class="card">
        <span class="icon">⚡</span>
        <h3>Показывает результат по ходу дела</h3>
        <p>Не нужно ждать конца обработки часового видео — рамки модели появляются в отчёте постепенно, а ручной просмотр доступен с первой секунды.</p>
      </div>
      <div class="card">
        <span class="icon">🔌</span>
        <h3>Работает без интернета</h3>
        <p>Полностью локальный сервис в сети операции — не зависит от связи со внешним миром, что на месте поисковых работ в горах часто и есть главное ограничение.</p>
      </div>
    </div>
  </section>

  <section>
    <div class="head-row"><span class="num">03</span><h2>Честно о границах</h2></div>
    <div class="honest">
      <ul>
        <li><b>Это не замена наземным группам и повторным облётам</b> — инструмент приоритизирует внимание человека, финальное решение всегда за людьми.</li>
        <li><b>«Вероятные координаты объекта» — расчётная оценка</b>, а не измерение; программа честно не показывает её, если расчёт получается ненадёжным, вместо того чтобы гадать.</li>
        <li><b>Нужна локальная сеть на месте операции</b> — сервер разворачивается на одном компьютере в зоне действия операции, доступ у всей команды через Wi-Fi/VPN.</li>
      </ul>
    </div>
  </section>

  <section>
    <div class="head-row"><span class="num">04</span><h2>Статус</h2></div>
    <div class="status-row"><span class="dot"></span> Собран и обкатан прямо во время реальной операции, а не в отрыве от неё</div>
    <div class="status-row"><span class="dot"></span> Каждое изменение проверяется тестами перед тем, как попасть в работу</div>
    <div class="status-row"><span class="dot"></span> Готов к развёртыванию для следующей операции — разворачивается на одной машине за несколько минут</div>
  </section>

  <div class="closing">
    <p>Инструмент не ищет за человека — он помогает команде не терять время и внимание на том объёме видео, который иначе просто некому было бы полностью просмотреть.</p>
  </div>

  <footer>SAR Review</footer>
</div>
</body></html>"""


# /about временно снята с публикации по решению пользователя -- текущий
# текст "не пойдёт" для показа МЧС, нужна доработка. Контент (ABOUT_PAGE_HTML
# выше) НЕ удалён специально, чтобы было что править, но маршрут отдаёт 404,
# пока страницу не решат вернуть.
@app.route("/about")
def about_page():
    return "Страница временно недоступна", 404


TREE_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>SAR Review — файлы</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; background:#111; color:#eee; margin:0; padding:20px; }}
h1 {{ font-size:18px; display:flex; justify-content:space-between; align-items:center; }}
.whoami {{ font-size:13px; color:#999; font-weight:normal; }}
.header-right {{ display:flex; align-items:center; gap:16px; }}
.online-indicator {{ display:flex; align-items:center; gap:6px; font-size:13px; color:#ccc; font-weight:normal; }}
.online-dot {{ width:6px; height:6px; border-radius:50%; background:#2f9e44; flex-shrink:0; }}
.toolbar {{ display:flex; align-items:center; gap:10px; margin:14px 0; font-size:13px; color:#ccc; }}
.toolbar select {{ background:#1b1b1b; color:#eee; border:1px solid #333; border-radius:6px; padding:6px 10px; }}
.toolbar button {{ background:#1b1b1b; color:#eee; border:1px solid #333; border-radius:6px;
                    padding:6px 10px; cursor:pointer; }}
/* кнопка направления сортировки -- только значок ⇅, поэтому фиксированная
   ширина и крупнее шрифт, иначе значок теряется в широкой кнопке */
#sort-dir {{ width:38px; font-size:16px; line-height:1; text-align:center; padding:6px 0; }}
#search {{ flex:1; min-width:180px; max-width:340px; background:#0d0d0d; color:#eee;
           border:1px solid #333; border-radius:6px; padding:7px 10px; font-size:13px; }}
#search:focus {{ outline:none; border-color:#3355aa; }}
.search-count {{ font-size:12px; color:#888; white-space:nowrap; }}
.toolbar button:hover {{ border-color:#555; }}
/* СТРОГАЯ СЕТКА. Раньше строка выкладывалась флексом, и каждый элемент
   вставал по своей естественной ширине -- полоса покрытия, статистика и
   счётчики оказывались в разных местах на каждой строке, правый край
   "плясал". Теперь ширины колонок заданы жёстко и одинаковы для всех строк,
   поэтому цифры выстраиваются по вертикали и их можно сравнивать взглядом.
   Колонки: превью | имя | покрытие | просмотры | находки | статус | кнопки */
.item-row {{ display:grid;
             grid-template-columns: 64px minmax(0, 1fr) 120px 130px 96px 110px auto;
             align-items:center; gap:10px;
             background:#1b1b1b; border:1px solid #2a2a2a; border-radius:8px;
             padding:6px 12px; margin-bottom:6px; transition: border-color .1s; }}
.item-row:hover {{ border-color:#555; }}
/* шапка -- без рамки и фона, только подписи столбцов */
.list-head {{ background:none; border-color:transparent; padding-top:0; padding-bottom:2px;
              margin-bottom:2px; font-size:11px; color:#666; text-transform:uppercase;
              letter-spacing:.05em; }}
.list-head:hover {{ border-color:transparent; }}
/* сама ссылка-строка больше не рисует фон/рамку -- это делает .item-row,
   иначе получилась бы рамка внутри рамки */
.item {{ display:contents; color:#eee; text-decoration:none; cursor:pointer; }}
.item.disabled {{ cursor:default; }}
.fname {{ font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }}
/* колонки с числами -- моноширинные цифры, чтобы столбец не дёргался */
.col-num {{ font-variant-numeric: tabular-nums; }}
.badge {{ font-size:11px; padding:3px 8px; border-radius:5px; white-space:nowrap; }}
.badge.idle {{ background:#2a2a2a; color:#999; border:1px solid #444; }}
.badge.queued {{ background:#444; color:#ccc; }}
.badge.processing {{ background:#8a6d00; color:#fff; }}
.badge.done {{ background:#22703a; color:#fff; }}
.badge.error {{ background:#7a1f1f; color:#fff; }}
.stat {{ font-size:12px; color:#999; white-space:nowrap; }}
.progress-mini {{ width:100%; height:6px; background:#333; border-radius:3px; overflow:hidden; margin-bottom:3px; }}
.progress-mini-bar {{ height:100%; background:#8a6d00; }}
.covbar {{ display:flex; gap:1px; width:100%; height:6px; transition: height .15s; overflow:hidden; }}
.item-row:hover .covbar {{ height:16px; }}
/* ячейки сетки: выравнивание внутри своей колонки */
.cell-cov {{ min-width:0; }}
.cell-stat {{ font-size:12px; color:#999; text-align:right; white-space:nowrap;
              overflow:hidden; text-overflow:ellipsis; }}
.cell-det {{ font-size:12px; color:#999; text-align:right; white-space:nowrap; }}
.cell-status {{ display:flex; flex-direction:column; align-items:flex-end; gap:2px; min-width:0; }}
.cell-actions {{ display:flex; gap:6px; justify-content:flex-end; }}
.covseg {{ flex:1; background:#333; border-radius:1px; }}
.covseg.on {{ background:#2f9e44; }}
.kind-icon {{ width:20px; text-align:center; opacity:0.6; }}
/* превью -- отдельная ссылка рядом с основной строкой (НЕ внутри неё:
   вложенные <a> невалидны), поэтому у неё своя рамка и свой ховер */
.thumb-wrap {{ width:64px; height:36px; flex-shrink:0; border-radius:5px; overflow:hidden;
                background:#0d0d0d; display:flex; align-items:center; justify-content:center;
                border:1px solid #2a2a2a; align-self:center; text-decoration:none;
                transition: border-color .1s; }}
.thumb-wrap:hover {{ border-color:#3355aa; }}
.thumb-wrap .kind-icon {{ width:auto; font-size:16px; }}
.thumb-img {{ width:100%; height:100%; object-fit:cover; display:block; }}
/* Ожидание превью -- тот же спиннер, что на карточке операции. Класс
   снимается и при загрузке, и при отказе: крутилка над тем, чего не
   будет, врёт. Файл свой, из static/ -- в поле интернета нет. */
.thumb-wrap.spin {{ background:url(/static/spinner.gif) center/78% auto no-repeat; }}
@media (prefers-reduced-motion:reduce) {{ .thumb-wrap.spin {{ background-image:none; }} }}
.playerlink {{ font-size:14px; padding:4px 12px; border-radius:5px; border:1px solid #3355aa;
               color:#8ecbff; text-decoration:none; white-space:nowrap; display:flex; align-items:center;
               justify-content:center; flex-shrink:0; }}
.playerlink:hover {{ background:#3355aa; color:#fff; }}
.runlink {{ font-size:14px; padding:4px 10px; border-radius:5px; border:1px solid #5533aa;
            background:transparent; color:#c3b3ff; cursor:pointer; flex-shrink:0; }}
.runlink:hover {{ background:#5533aa; color:#fff; }}
.runlink:disabled {{ opacity:0.5; cursor:default; }}
.upload-box {{ display:flex; align-items:center; gap:10px; margin:14px 0; padding:12px 14px;
                background:#1b1b1b; border:1px solid #3355aa; border-radius:8px; flex-wrap:wrap; }}
.upload-box input[type=file] {{ color:#ccc; font-size:13px; }}
.upload-box button {{ background:#3355aa; color:#fff; border:none; border-radius:6px;
                       padding:8px 14px; cursor:pointer; font-size:13px; }}
.upload-box button:hover {{ background:#3f66c9; }}
.upload-box button:disabled {{ opacity:0.6; cursor:default; }}
.upload-status {{ font-size:12px; color:#999; }}
.badge.cloud {{ background:#1d3a52; color:#9ecbf0; border-color:#2b5473;
  font-size:13px; line-height:1; padding:3px 7px; }}
</style></head>
<body>
<h1><span id="page-title">SAR Review — файлы</span>
  <span class="header-right">
    <a id="ops-link" href="/operations" style="color:#8bd;text-decoration:none;font-size:13px">← Операции</a>
    <span class="online-indicator"><span class="online-dot"></span><span id="online-count">—</span> онлайн</span>
    <span class="whoami">{viewer_name} · <a href="/login" style="color:#999">сменить</a></span>
  </span>
</h1>
<div class="toolbar">
  Сортировать по:
  <select id="sort-key">
    <option value="date">дате создания файла</option>
    <option value="name">имени</option>
    <option value="type">типу файла</option>
    <option value="detections">числу детекций</option>
  </select>
  <button id="sort-dir" title="Изменить направление">⇅</button>
  <input id="search" type="search" placeholder="Поиск по имени файла…" autocomplete="off">
  <span id="search-count" class="search-count"></span>
</div>
{upload_section}
<div id="tree">Загрузка...</div>
<script>
let sortKey = localStorage.getItem('sar_sort_key') || 'date';
let sortDesc = (localStorage.getItem('sar_sort_desc') ?? 'true') === 'true';
document.getElementById('sort-key').value = sortKey;
updateSortDirLabel();

document.getElementById('sort-key').addEventListener('change', e => {{
  sortKey = e.target.value;
  localStorage.setItem('sar_sort_key', sortKey);
  updateSortDirLabel();
  render(lastData);
}});
document.getElementById('sort-dir').addEventListener('click', () => {{
  sortDesc = !sortDesc;
  localStorage.setItem('sar_sort_desc', sortDesc);
  updateSortDirLabel();
  render(lastData);
}});

function updateSortDirLabel() {{
  // Кнопка -- только значок ⇅ (две стрелки в разные стороны). Раньше на ней
  // был текст вроде "↓ фото→видео", который занимал место и читался хуже
  // самой сортировки. Смысл текущего направления -- во всплывающей подсказке.
  const btn = document.getElementById('sort-dir');
  const hints = {{
    date: sortDesc ? 'сначала новые' : 'сначала старые',
    name: sortDesc ? 'по имени: Я→А' : 'по имени: А→Я',
    type: sortDesc ? 'сначала фото' : 'сначала видео',
    detections: sortDesc ? 'сначала с большим числом детекций'
                          : 'сначала с меньшим числом детекций',
  }};
  btn.textContent = '⇅';
  btn.title = (hints[sortKey] || 'изменить направление') + ' — нажмите, чтобы перевернуть';
}}

let lastData = {{ items: [] }};

// Какая операция открыта -- берём из адреса, чтобы ссылка была
// поделимой: человек может кинуть коллеге /?op=3 и тот увидит то же самое.
const OP = new URLSearchParams(location.search).get('op') || '';

async function loadTree() {{
  const res = await fetch('/api/tree' + (OP ? ('?op=' + encodeURIComponent(OP)) : ''));
  lastData = await res.json();
  // Заголовок называет операцию, если открыта одна: иначе непонятно, почему
  // в списке часть файлов, и выглядит как пропажа материала.
  const t = document.getElementById('page-title');
  if (t) {{
    t.textContent = lastData.operation
      ? ('Материалы: ' + lastData.operation)
      : 'SAR Review — файлы';
  }}
  render(lastData);
}}

// --- загрузка видео (кнопка видна только пользователю 'uploader', см. index()
// в sar_server.py; но реальная защита -- на сервере в /api/upload, не здесь) ---
const uploadBtn = document.getElementById('upload-btn');
if (uploadBtn) {{
  uploadBtn.addEventListener('click', async () => {{
    const input = document.getElementById('upload-file-input');
    const status = document.getElementById('upload-status');
    if (!input.files.length) {{ status.textContent = 'выберите файл'; return; }}
    const fd = new FormData();
    fd.append('video', input.files[0]);
    uploadBtn.disabled = true;
    status.textContent = 'загружается, не закрывайте страницу...';
    try {{
      const res = await fetch('/api/upload', {{ method: 'POST', body: fd }});
      const data = await res.json();
      if (data.ok) {{
        status.textContent = `готово: ${{data.filename}}`;
        input.value = '';
        loadTree();
      }} else {{
        status.textContent = 'ошибка: ' + (data.error || res.status);
      }}
    }} catch (e) {{
      status.textContent = 'ошибка сети при загрузке';
    }} finally {{
      uploadBtn.disabled = false;
    }}
  }});
}}

// --- загрузка телеметрии (SRT), та же серверная защита, что и у видео;
// поддерживает несколько файлов за раз -- реальная телеметрия обычно
// приходит целой пачкой с одного дня полётов ---
const uploadTelemetryBtn = document.getElementById('upload-telemetry-btn');
if (uploadTelemetryBtn) {{
  uploadTelemetryBtn.addEventListener('click', async () => {{
    const input = document.getElementById('upload-telemetry-input');
    const status = document.getElementById('upload-telemetry-status');
    if (!input.files.length) {{ status.textContent = 'выберите файл(ы)'; return; }}
    const fd = new FormData();
    for (const f of input.files) fd.append('telemetry', f);
    uploadTelemetryBtn.disabled = true;
    status.textContent = 'загружается...';
    try {{
      const res = await fetch('/api/upload_telemetry', {{ method: 'POST', body: fd }});
      const data = await res.json();
      if (data.ok) {{
        status.textContent = `готово: ${{data.saved.length}} файл(ов)`;
        input.value = '';
      }} else {{
        const errText = (data.errors && data.errors.length) ? data.errors.join('; ') : (data.error || res.status);
        status.textContent = 'ошибка: ' + errText;
      }}
    }} catch (e) {{
      status.textContent = 'ошибка сети при загрузке';
    }} finally {{
      uploadTelemetryBtn.disabled = false;
    }}
  }});
}}

function badgeLabel(status) {{
  return {{idle:'без анализа', queued:'в очереди', processing:'обрабатывается',
           done:'готово', error:'ошибка'}}[status] || status;
}}

// Отправить файл в очередь на анализ моделью вручную (когда авто-обработка
// выключена). Кнопка есть у файлов, которые модель ещё не смотрела.
async function enqueue(reportId, btn) {{
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '...';
  try {{
    const res = await fetch(`/api/report/${{reportId}}/enqueue`, {{ method: 'POST' }});
    const data = await res.json();
    if (!data.ok) {{ btn.textContent = 'ошибка'; btn.disabled = false; return; }}
    loadTree();
  }} catch (e) {{
    btn.textContent = old;
    btn.disabled = false;
  }}
}}

// --- поиск по имени файла ---
// Список сам обновляется каждые 5 секунд, поэтому фильтр применяется при
// КАЖДОЙ отрисовке, а не разово к текущим строкам -- иначе набранный запрос
// сбрасывался бы при первом же автообновлении.
let searchQuery = '';

function matchesSearch(it) {{
  if (!searchQuery) return true;
  // ищем по всем словам запроса независимо от их порядка: "0003 mp4"
  // найдёт DJI_20260812140054_0003_Z.MP4
  const name = it.name.toLowerCase();
  return searchQuery.split(/\\s+/).every(part => name.includes(part));
}}

document.getElementById('search').addEventListener('input', e => {{
  searchQuery = e.target.value.trim().toLowerCase();
  render(lastData);
}});

function detectionTotal(it) {{
  // ai_count -- null, пока видео ещё не done (см. /api/tree в sar_server.py:
  // группировка на лету слишком дорогая, чтобы гонять её при каждом опросе
  // не готового отчёта) -- для сортировки считаем как 0, не как "неизвестно"
  return (it.manual_count || 0) + (it.ai_count || 0);
}}

function sortItems(items) {{
  const arr = [...items];
  arr.sort((a, b) => {{
    let cmp = 0;
    if (sortKey === 'date') cmp = (a.file_ctime || 0) - (b.file_ctime || 0);
    else if (sortKey === 'name') cmp = a.name.localeCompare(b.name, 'ru');
    else if (sortKey === 'type') cmp = a.kind.localeCompare(b.kind);
    else if (sortKey === 'detections') cmp = detectionTotal(a) - detectionTotal(b);
    return sortDesc ? -cmp : cmp;
  }});
  return arr;
}}

function render(data) {{
  const root = document.getElementById('tree');
  if (!data.items || data.items.length === 0) {{
    root.innerHTML = '<p style="color:#888">Видео и фото не найдены в корне папки запуска сервера '
      + '(подпапки не сканируются — см. README).</p>';
    return;
  }}
  // шапка колонок -- та же сетка, что и у строк, поэтому подписи стоят
  // ровно над своими столбцами
  const header = `<div class="item-row list-head">
      <span></span>
      <span>файл</span>
      <span>просмотр</span>
      <span class="cell-stat">кто смотрел</span>
      <span class="cell-det">находки</span>
      <span class="cell-status">статус</span>
      <span></span>
    </div>`;
  const shown = sortItems(data.items).filter(matchesSearch);
  const counter = document.getElementById('search-count');
  counter.textContent = searchQuery
    ? `найдено: ${{shown.length}} из ${{data.items.length}}` : '';

  if (!shown.length) {{
    root.innerHTML = `<p style="color:#888">По запросу «${{escapeHtmlTree(searchQuery)}}» ничего не найдено.</p>`;
    return;
  }}
  root.innerHTML = header + renderItems(shown);
}}

function escapeHtmlTree(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function renderItems(items) {{
  return items.map(it => {{
    const icon = it.kind === 'video' ? '🎬' : '🖼️';
    // превью -- только для видео (первый кадр, генерируется воркером, см.
    // watcher_loop в sar_worker.py); если ещё не готово (файл только что
    // добавлен) или не грузится -- откатываемся на эмодзи-иконку как раньше
    // Превью есть у обоих типов: первый кадр у видео, уменьшенная копия у
    // фото. Клик по превью открывает сам материал (плеер / просмотр снимка),
    // поэтому это ОТДЕЛЬНАЯ ссылка, ВНЕ основной <a class="item"> -- вложенные
    // <a> внутри <a> невалидны, браузер разбирает их непредсказуемо и может
    // оборвать родительскую ссылку (эти грабли в проекте уже были, см. CLAUDE.md).
    const mediaHref = it.kind === 'video'
      ? `/report/${{it.report_id}}/player/`
      : `/report/${{it.report_id}}/viewer/`;
    const thumb = `<a class="thumb-wrap spin" href="${{mediaHref}}" title="Открыть">
           <img class="thumb-img" src="/api/thumbnail/${{encodeURIComponent(it.name)}}" loading="lazy"
                onload="this.parentNode.classList.remove('spin')"
                onerror="this.parentNode.classList.remove('spin'); this.style.display='none'; this.nextElementSibling.style.display='';">
           <span class="kind-icon" style="display:none">${{icon}}</span>
         </a>`;
    let right = `<span class="badge ${{it.status}}">${{badgeLabel(it.status)}}</span>`;
    // Файл лежит в облаке, а не на этой машине. Отметка нужна, чтобы
    // человек понимал, почему у него нет превью и почему открытие может
    // занять время: иначе это выглядит как неисправность.
    if (it.in_cloud) {{
      right = `<span class="badge cloud" title="файл в облаке, ещё не скачан">☁</span>` + right;
    }}
    if (it.status === 'processing') {{
      right = `<div class="progress-mini"><div class="progress-mini-bar" style="width:${{it.progress_pct||0}}%"></div></div>` + right;
    }}
    let cov = '';
    // полоса покрытия -- для видео в любом статусе, а не только 'done':
    // ручной просмотр доступен сразу, и видеть отсмотренные куски важнее
    // всего именно пока детектор до файла ещё не дошёл
    if (it.kind === 'video' && it.buckets) {{
      cov = '<div class="covbar">' + it.buckets.map(b => `<div class="covseg ${{b>0?'on':''}}"></div>`).join('') + '</div>';
    }}
    // статистика просмотра -- для видео в любом статусе (см. полосу выше):
    // человек может смотреть файл вручную, пока тот ещё стоит в очереди
    // Ничего не показываем, пока смотреть нечего: строка "0 чел. · —
    // просмотрено" выглядела как поломка, хотя означала просто "ещё никто
    // не открывал". Тот же принцип, что и с прочерком у счётчика модели.
    let stat = '';
    if ((it.kind === 'video' || it.status === 'done') && it.viewer_count) {{
      const pct = (it.percent !== null && it.percent !== undefined)
        ? ` · просмотрено ${{it.percent}}%` : '';
      stat = `<span class="stat">${{it.viewer_count}} чел.${{pct}}</span>`;
    }}
    // счётчики детекций -- модель и ручные отдельно, как и просили; ai_count
    // = null, пока отчёт не done (см. detectionTotal выше)
    const aiCountTxt = it.ai_count === null || it.ai_count === undefined ? '—' : it.ai_count;
    const detStat = `<span class="stat det-stat">🤖 ${{aiCountTxt}} · ✍️ ${{it.manual_count || 0}}</span>`;
    // Для ФОТО вся строка ведёт в просмотр снимка, в любом статусе: смотреть
    // снимок глазами можно с первой секунды, не дожидаясь детектора, а из
    // самого просмотра есть ссылка на отчёт, когда тот появится. Раньше
    // строка фото в очереди была мёртвой (href='#'), и открыть снимок из
    // таблицы было нельзя вообще.
    // Для ВИДЕО поведение прежнее: строка -- в отчёт со сценами (он тяжёлый и
    // осмысленен только после обработки), а ручной плеер -- отдельной кнопкой.
    const clickable = it.kind === 'photo'
      || it.status === 'done' || it.status === 'processing' || it.status === 'error';
    const href = it.kind === 'photo'
      ? mediaHref
      : (clickable ? `/report/${{it.report_id}}/` : '#');
    const cls = clickable ? 'item' : 'item disabled';
    // ВАЖНО: playerLink -- ОТДЕЛЬНАЯ ссылка, не вложенная в основную <a class="item">.
    // Вложенные <a> внутри <a> -- невалидный HTML, браузер разбирает его
    // непредсказуемо (может обрубить родительскую ссылку раньше времени) --
    // поэтому обе ссылки идут РЯДОМ, как соседние элементы одной строки.
    // Плеер доступен для видео в ЛЮБОМ статусе, включая 'queued' и 'error' --
    // исходный файл физически на диске с момента появления в очереди, ручная
    // разметка от результата детектора не зависит вообще. Раньше ждали
    // 'done'/'processing' -- значит, только что добавленный, ещё не взятый
    // в работу файл (или файл, на котором детектор упал с ошибкой) не
    // давал начать ручной разбор, хотя для этого не было technической причины.
    // Рамки модели (если появятся) подгружаются в самом плеере на лету.
    // Для фото -- своя ссылка на просмотр, тоже доступная в любом статусе.
    // Без неё снимок в очереди нельзя было открыть вообще ничем: строка не
    // кликабельна, пока нет отчёта, а отчёта нет, пока файл не обработан.
    // кнопка выглядит одинаково для видео и фото -- просто знак "играть",
    // ведёт в плеер или в просмотр снимка соответственно
    const playerLink =
      `<a class="playerlink" href="${{mediaHref}}" title="${{it.kind === 'video' ? 'Ручной просмотр с плеером' : 'Посмотреть снимок'}}">▶</a>`;
    // кнопка ручного запуска модели -- для файлов, которые она ещё не смотрела
    const runLink = (it.status === 'idle' || it.status === 'error')
      ? `<button class="runlink" onclick="enqueue('${{it.report_id}}', this)" title="Прогнать через модель">🤖</button>`
      : '';
    // ВАЖНО для сетки: пустые ячейки нельзя пропускать -- иначе следующие
    // элементы сдвинутся в чужие колонки и выравнивание развалится. Поэтому
    // каждая колонка всегда выводится, пусть и пустым <span>.
    // Столбцов ровно 7: превью | имя | покрытие | просмотры | находки |
    // статус | кнопки. Статус и кнопки собраны каждый в ОДНУ ячейку, иначе
    // строка "обрабатывается" (полоска + бейдж) заняла бы две колонки.
    // В списке показываем ИМЯ ФАЙЛА, а не весь путь. it.name -- это rel_path,
    // и с появлением папок операций он стал длинным: приставка «Курумды
    // август 2026/» съедала всю ширину строки, а само имя обрезалось --
    // на телефоне вылеты стало невозможно отличить друг от друга.
    // Путь целиком остаётся в подсказке при наведении, так что информация
    // не теряется, а поиск по-прежнему идёт по полному пути.
    const shortName = String(it.name).split('/').pop();
    return `<div class="item-row">
      ${{thumb}}
      <a class="${{cls}}" href="${{href}}" title="${{it.name}}">
        <span class="fname">${{shortName}}</span>
        <span class="cell-cov">${{cov}}</span>
        <span class="cell-stat col-num">${{stat}}</span>
        <span class="cell-det col-num">${{detStat}}</span>
        <span class="cell-status">${{right}}</span>
      </a>
      <span class="cell-actions">${{runLink}}${{playerLink}}</span>
    </div>`;
  }}).join('');
}}

loadTree();
setInterval(loadTree, 5000);

</script>
</body></html>"""


UPLOAD_SECTION_HTML = """<div class="upload-box">
  <input type="file" id="upload-file-input" accept="video/*">
  <button id="upload-btn">📤 Загрузить видео</button>
  <span class="upload-status" id="upload-status"></span>
</div>
<div class="upload-box">
  <input type="file" id="upload-telemetry-input" accept=".srt" multiple>
  <button id="upload-telemetry-btn">📤 Загрузить телеметрию (SRT)</button>
  <span class="upload-status" id="upload-telemetry-status"></span>
</div>"""


@app.route("/")
def index():
    upload_section = UPLOAD_SECTION_HTML if can_upload() else ""
    return TREE_PAGE_HTML.format(viewer_name=session.get("viewer_name", ""), upload_section=upload_section)


OPERATIONS_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Операции — SAR Review</title>
<style>
:root {{
  --bg:#12171c; --card:#1a2129; --card2:#212a33; --line:#2b353f;
  --ink:#e8eeec; --soft:#9aa8a5; --dim:#6f7d7a;
  --accent:#5fb8c7; --warm:#e07a3f;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,"Segoe UI",system-ui,sans-serif;line-height:1.55}}
.wrap{{max-width:960px;margin:0 auto;padding:20px 16px 80px}}
.top{{display:flex;align-items:center;justify-content:space-between;
  gap:12px;flex-wrap:wrap;margin-bottom:22px}}
h1{{font-size:21px;margin:0;font-weight:700}}
.who{{font-size:13px;color:var(--soft)}}
.btn{{background:var(--accent);color:#0d1418;border:0;border-radius:7px;
  padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer}}
.btn:hover{{filter:brightness(1.08)}}
.btn.ghost{{background:transparent;color:var(--soft);border:1px solid var(--line)}}

.grid{{display:grid;gap:12px}}
.op{{display:block;text-decoration:none;color:inherit;background:var(--card);
  border:1px solid var(--line);border-radius:10px;padding:16px 18px}}
.op:hover{{border-color:var(--accent)}}
.op h2{{margin:0 0 4px;font-size:17px;font-weight:650}}
.op .area{{font-size:13px;color:var(--soft);margin-bottom:12px}}
.nums{{display:flex;flex-wrap:wrap;gap:6px 20px;font-size:13px;color:var(--soft)}}
.nums b{{color:var(--ink);font-weight:650}}
.cov{{margin-top:12px;height:6px;background:var(--card2);border-radius:3px;overflow:hidden}}
.cov i{{display:block;height:100%;background:var(--accent)}}
.covnote{{font-size:12px;color:var(--dim);margin-top:5px}}

.unsorted{{background:transparent;border:1px dashed var(--line)}}
.unsorted h2{{color:var(--warm)}}

.empty{{text-align:center;color:var(--soft);padding:48px 20px;
  border:1px dashed var(--line);border-radius:10px}}
.empty p{{margin:0 0 14px}}

dialog{{background:var(--card);color:var(--ink);border:1px solid var(--line);
  border-radius:12px;padding:0;max-width:440px;width:calc(100% - 32px)}}
dialog::backdrop{{background:rgba(0,0,0,.6)}}
.dlg{{padding:22px 24px}}
.dlg h3{{margin:0 0 4px;font-size:18px}}
.dlg .hint{{font-size:13px;color:var(--soft);margin:0 0 18px}}
label{{display:block;font-size:13px;color:var(--soft);margin:12px 0 5px}}
input[type=text]{{width:100%;background:var(--bg);color:var(--ink);
  border:1px solid var(--line);border-radius:7px;padding:9px 11px;font-size:15px}}
input[type=text]:focus{{outline:2px solid var(--accent);outline-offset:-1px}}
.row{{display:flex;gap:10px;justify-content:flex-end;margin-top:22px}}
.err{{color:#ff9d7a;font-size:13px;margin-top:10px;min-height:18px}}
</style></head><body>
<div class="wrap">
  <div class="top">
    <h1>Операции</h1>
    <div style="display:flex;align-items:center;gap:14px">
      <span class="who">{viewer_name}</span>
      <a class="btn ghost" href="/guide" style="text-decoration:none">📖 Как смотреть</a>
      <a class="btn ghost" href="/" style="text-decoration:none">Все материалы</a>
      <button class="btn" id="new" style="display:none">Новая операция</button>
    </div>
  </div>
  <div class="grid" id="list"></div>
</div>

<dialog id="dlg"><form method="dialog" class="dlg">
  <h3>Новая операция</h3>
  <p class="hint">Рядом появится папка с этим названием — складывайте материал
     туда, можно прямо распакованной папкой из облака.</p>
  <label for="t">Название</label>
  <input type="text" id="t" placeholder="Курумды, август 2026" maxlength="120" required>
  <label for="a">Район работ</label>
  <input type="text" id="a" placeholder="Алайский район, пик Курумды">
  <label for="c">Заказчик</label>
  <input type="text" id="c" placeholder="необязательно">
  <div class="err" id="err"></div>
  <div class="row">
    <!-- type="button" обязателен: кнопка внутри формы по умолчанию считается
         отправкой, и браузер требует заполнить обязательное поле ПРЕЖДЕ чем
         закрыть окно. Человек не мог отменить создание, не придумав название. -->
    <button class="btn ghost" type="button" id="cancel">Отмена</button>
    <button class="btn" id="create" value="ok">Создать</button>
  </div>
</form></dialog>

<script>
const hhmm = s => {{
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${{h}} ч ${{m}} мин` : `${{m}} мин`;
}};

async function load() {{
  const r = await fetch('/api/operations');
  const d = await r.json();
  document.getElementById('new').style.display = d.can_manage ? '' : 'none';
  const list = document.getElementById('list');

  if (!d.operations.length && !d.unsorted) {{
    list.innerHTML = `<div class="empty"><p>Операций пока нет.</p>` +
      (d.can_manage ? `<p>Создайте первую — рядом появится папка,
        куда складывать съёмку.</p>` : `<p>Их создаёт координатор.</p>`) + `</div>`;
    return;
  }}

  let html = d.operations.map(o => {{
    // Покрытие -- отношение отсмотренного к отснятому. Главная цифра для
    // заказчика: "мы просмотрели столько-то процентов материала".
    // Процент считает СЕРВЕР: там объединяются пересечения сегментов.
    // Считать его здесь делением было бы неверно -- watched_sec это уже
    // уникальное покрытие, а человеко-часы лежат отдельно в viewer_sec.
    const pct = o.coverage_pct || 0;
    return `<a class="op" href="/operation/${{o.id}}/">
      <h2>${{esc(o.title)}}</h2>
      <div class="area">${{esc(o.area || '')}}</div>
      <div class="nums">
        <span>материалов <b>${{o.materials}}</b></span>
        <span>отснято <b>${{hhmm(o.footage_sec)}}</b></span>
        <span>просмотрено <b>${{hhmm(o.watched_sec)}}</b></span>
        <span>человеко-часов <b>${{hhmm(o.viewer_sec)}}</b></span>
        <span>пометок <b>${{o.marks}}</b></span>
      </div>
      <div class="cov"><i style="width:${{pct}}%"></i></div>
      <div class="covnote">просмотрено ${{pct}}% отснятого материала</div>
    </a>`;
  }}).join('');

  if (d.unsorted) {{
    html += `<a class="op unsorted" href="/">
      <h2>Не разобрано</h2>
      <div class="area">материалы, которые ещё не отнесены к операции</div>
      <div class="nums"><span>файлов <b>${{d.unsorted}}</b></span></div>
    </a>`;
  }}
  list.innerHTML = html;
}}

function esc(s) {{
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

const dlg = document.getElementById('dlg');
document.getElementById('new').onclick = () => {{
  document.getElementById('err').textContent = '';
  dlg.showModal();
  document.getElementById('t').focus();
}};

// Отмена закрывает окно ВСЕГДА, независимо от заполненности полей, и
// очищает их: иначе брошенный черновик всплывёт при следующем открытии.
document.getElementById('cancel').onclick = () => {{
  ['t','a','c'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('err').textContent = '';
  dlg.close();
}};

document.getElementById('create').addEventListener('click', async (e) => {{
  const title = document.getElementById('t').value.trim();
  if (!title) {{ e.preventDefault(); document.getElementById('err').textContent =
    'Название обязательно'; return; }}
  e.preventDefault();
  const r = await fetch('/api/operations', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{title,
      area: document.getElementById('a').value.trim(),
      client: document.getElementById('c').value.trim()}})
  }});
  const d = await r.json();
  if (!d.ok) {{ document.getElementById('err').textContent = d.error || 'Не вышло'; return; }}
  dlg.close();
  document.getElementById('t').value = '';
  document.getElementById('a').value = '';
  document.getElementById('c').value = '';
  load();
}});

load();
</script></body></html>"""


@app.route("/operations")
def operations_page():
    return OPERATIONS_PAGE_HTML.format(viewer_name=session.get("viewer_name", ""))


OPERATION_CARD_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Операция — SAR Review</title>
<!-- Leaflet лежит СВОЙ, а не с CDN: платформа обязана работать в поле без
     интернета, и внешняя ссылка означала бы пустую страницу там, где она
     нужнее всего. Подложка карты (тайлы) без сети всё равно не придёт, но
     точки, треки и разметка останутся видны. BSD-2, лицензия рядом. -->
<link rel="stylesheet" href="/static/leaflet/leaflet.css">
<script src="/static/leaflet/leaflet.js"></script>
<style>
:root {{
  --bg:#12171c; --card:#1a2129; --card2:#212a33; --line:#2b353f;
  --ink:#e8eeec; --soft:#9aa8a5; --dim:#6f7d7a;
  --accent:#5fb8c7; --warm:#e07a3f;
  /* Размер превью материала.
     Привязан к высоте экрана, а не задан жёстко: на 1920x1080 выходит
     примерно 56px, и тогда в список без прокрутки помещается ровно
     десять строк. На экране повыше превью само становится крупнее, на
     низком ноутбучном -- мельче, но не ниже 44px: меньше этого кадр с
     дрона перестаёт узнаваться, а ради него всё и делается. */
  --thumb:clamp(44px,5.2vh,76px);
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,"Segoe UI",system-ui,sans-serif;line-height:1.5}}
.wrap{{max-width:1000px;margin:0 auto;padding:16px 14px 70px}}
a{{color:inherit}}

.top{{display:flex;align-items:center;justify-content:space-between;gap:10px;
  flex-wrap:wrap;font-size:13px;color:var(--soft);margin-bottom:14px}}
.top a{{color:var(--accent);text-decoration:none}}
h1{{font-size:20px;margin:0 0 3px;font-weight:700}}
.area{{font-size:13px;color:var(--soft);margin-bottom:12px}}

.sum{{display:flex;flex-wrap:wrap;gap:4px 22px;font-size:13px;
  color:var(--soft);margin-bottom:8px}}
.sum b{{color:var(--ink);font-weight:650}}
.cov{{height:6px;background:var(--card2);border-radius:3px;overflow:hidden;
  margin-bottom:18px}}
.cov i{{display:block;height:100%;background:var(--accent)}}

.tabs{{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:14px;
  overflow-x:auto}}
.tab{{padding:9px 15px;font-size:14px;color:var(--soft);cursor:pointer;
  border-bottom:2px solid transparent;white-space:nowrap;background:none;
  border-top:0;border-left:0;border-right:0;font-family:inherit}}
.tab.on{{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}}
/* Вкладка «на будущее»: отодвинута вправо и приглушена.
   .tab.later.on -- специфичность ВЫШЕ, чем у .tab.later: иначе выбранная
   вкладка осталась бы блёклой и читалась как неактивная. */
.tab.later{{margin-left:auto;color:var(--dim)}}
.tab.later:hover{{color:var(--soft)}}
.tab.later.on{{color:var(--ink)}}

/* --- карта ------------------------------------------------------------ */
#map{{height:min(70vh,640px);border-radius:10px;border:1px solid var(--line);
  background:#0d0d0d}}
/* Подписи Leaflet светлые по умолчанию -- на тёмной странице они слепят. */
.leaflet-container{{background:#0d0d0d;font-family:inherit}}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{{
  background:var(--card2);color:var(--ink);border:1px solid var(--line)}}
.leaflet-popup-content{{margin:11px 13px;font-size:13.5px;line-height:1.5}}
/* Когда карточка упирается в maxHeight, Leaflet добавляет свою прокрутку
   и светлую рамку -- на тёмной странице она выглядит как артефакт. */
.leaflet-popup-scrolled{{border-top:1px solid var(--line);
  border-bottom:1px solid var(--line)}}
.leaflet-popup-content a{{color:var(--accent)}}
/* Подсказка при наведении: светлая по умолчанию, на тёмной карте слепит. */
.leaflet-tooltip{{background:var(--card2);color:var(--ink);
  border:1px solid var(--line);box-shadow:none;font-size:13px;
  padding:4px 9px;border-radius:6px}}
.leaflet-tooltip-top:before{{border-top-color:var(--line)}}
.leaflet-tooltip-bottom:before{{border-bottom-color:var(--line)}}
.leaflet-tooltip-left:before{{border-left-color:var(--line)}}
.leaflet-tooltip-right:before{{border-right-color:var(--line)}}
.leaflet-control-attribution{{background:rgba(0,0,0,.55);color:var(--dim)}}
.leaflet-control-attribution a{{color:var(--soft)}}
.mapbar{{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;
  margin:0 0 10px;font-size:13.5px;color:var(--soft)}}
.mapbar label{{display:flex;align-items:center;gap:6px;cursor:pointer}}
.mapnote{{margin:10px 0 0;font-size:13px;color:var(--dim);line-height:1.6}}
.mapnote b{{color:var(--soft)}}
.key{{display:inline-block;width:12px;height:12px;border-radius:50%;
  vertical-align:-1px;margin-right:5px}}
.key.obj{{background:var(--accent)}}
.key.drone{{background:transparent;border:2px dashed var(--soft)}}
.key.mark{{background:#e0a33a;border-radius:2px}}
.key.track{{background:transparent;border-bottom:2px solid #4b9fd5;
  border-radius:0;height:6px}}
.addmode{{padding:7px 13px;border-radius:7px;border:1px solid var(--line);
  background:var(--card2);color:var(--ink);cursor:pointer;font-family:inherit;
  font-size:13.5px}}
.addmode.on{{background:var(--accent);color:#0d0d0d;font-weight:600}}
#map.adding{{cursor:crosshair}}

/* --- отчёт ------------------------------------------------------------ */
.rep-bar{{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;
  margin:0 0 16px;font-size:13.5px;color:var(--soft)}}
.rep-bar input[type=date]{{background:#0d0d0d;color:var(--ink);
  border:1px solid var(--line);border-radius:6px;padding:6px 9px;
  font-family:inherit;font-size:13.5px}}
.rep-bar button{{padding:6px 12px;border-radius:6px;border:1px solid var(--line);
  background:var(--card2);color:var(--ink);cursor:pointer;font-family:inherit;
  font-size:13.5px}}
.rep-sec{{margin:0 0 26px}}
.rep-sec h3{{font-size:15px;margin:0 0 10px;color:var(--ink)}}
.rep-grid{{display:grid;gap:10px;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}}
.rep-kpi{{background:var(--card2);border:1px solid var(--line);border-radius:9px;
  padding:12px 14px}}
.rep-kpi b{{display:block;font-size:22px;color:var(--ink);
  font-variant-numeric:tabular-nums;line-height:1.25}}
.rep-kpi span{{font-size:12.5px;color:var(--soft)}}
.rep-kpi.warn b{{color:#e0a33a}}
.rep-tbl-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:9px}}
table.rep{{width:100%;border-collapse:collapse;font-size:13px}}
table.rep th,table.rep td{{padding:7px 11px;text-align:left;
  border-bottom:1px solid var(--line);white-space:nowrap}}
table.rep th{{background:var(--card2);color:var(--soft);cursor:pointer;
  user-select:none;position:sticky;top:0}}
table.rep th:hover{{color:var(--ink)}}
table.rep th.num,table.rep td.num{{text-align:right;
  font-variant-numeric:tabular-nums}}
table.rep tbody tr:hover{{background:var(--card2)}}
table.rep td.dim{{color:var(--dim)}}
.covcell{{display:flex;align-items:center;gap:7px;justify-content:flex-end}}
.covbar{{width:54px;height:6px;border-radius:3px;background:#0d0d0d;
  overflow:hidden;flex:none}}
.covbar i{{display:block;height:100%;background:var(--accent)}}
.rep-note{{font-size:13px;color:var(--dim);line-height:1.65;margin:9px 0 0}}
.rep-note b{{color:var(--soft)}}
.rep-filter{{background:#0d0d0d;color:var(--ink);border:1px solid var(--line);
  border-radius:6px;padding:6px 10px;font-size:13px;font-family:inherit;
  margin:0 0 9px;width:min(320px,100%)}}
@media print{{
  .tabs,#searchbar,.rep-bar button,.rep-filter{{display:none}}
  body{{background:#fff;color:#000}}
  .rep-kpi,table.rep th{{background:#f4f4f4}}
  table.rep th,table.rep td{{border-color:#ccc}}
}}

#searchbar{{display:none;align-items:center;gap:12px;margin:14px 0 2px}}
#searchbar.on{{display:flex}}
#q{{flex:1;max-width:420px;background:#0d0d0d;color:var(--ink);
  border:1px solid var(--line);border-radius:7px;padding:8px 12px;font-size:14px}}
#q:focus{{outline:none;border-color:var(--accent)}}
#qhint{{color:var(--dim);font-size:12.5px;white-space:nowrap}}
.hit-where{{color:var(--soft);font-size:12px;display:block;margin-top:2px}}
.hit-mark{{background:#4a3a12;color:#ffd479;border-radius:3px;padding:0 1px}}
.noqres{{color:var(--soft);padding:22px 4px}}
.crumbs{{font-size:13px;color:var(--soft);margin-bottom:10px;
  word-break:break-word}}
.crumbs a{{color:var(--accent);text-decoration:none}}
.crumbs .sep{{color:var(--dim);margin:0 5px}}

/* Отступ маленький намеренно: высоту строки задаёт превью, и лишние
   вертикальные поля здесь стоят прямо строк на экране. */
.row{{display:flex;align-items:center;gap:12px;padding:4px 11px;
  border-bottom:1px solid var(--line);text-decoration:none;color:inherit}}
.row:hover{{background:var(--card)}}
/* Квадрат, а не 16:9 по форме кадра. Кадр с дрона -- это местность, и
   квадратный кроп оставляет центр кадра, по которому видео и узнают;
   к тому же одинаковая ширина держит имена файлов в одной колонке,
   независимо от того, папка это, видео или фото. */
.row .ic,.row .thumb{{width:var(--thumb);height:var(--thumb);flex-shrink:0;
  border-radius:7px;display:flex;align-items:center;justify-content:center}}
.row .ic{{background:var(--card2);font-size:calc(var(--thumb)*.42);opacity:.85}}
.row .thumb{{position:relative;overflow:hidden;background:var(--card2)}}
.row .thumb img{{width:100%;height:100%;object-fit:cover;display:block;
  position:relative;z-index:1}}
/* Значок лежит ПОД картинкой, а не вместо неё: пока превью грузится или
   если его вовсе нет (файл только появился, воркер до него не дошёл),
   строка выглядит так же и не прыгает по высоте. */
.row .thumb .fb{{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-size:calc(var(--thumb)*.42);opacity:.45}}
.row:hover .thumb{{outline:1px solid var(--accent);outline-offset:-1px}}
.row .nm{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-size:14px}}
.row .meta{{font-size:12px;color:var(--soft);flex-shrink:0}}
.bar{{width:74px;height:6px;background:var(--card2);border-radius:3px;
  overflow:hidden;flex-shrink:0}}
.bar i{{display:block;height:100%;background:var(--accent)}}

.empty{{color:var(--soft);padding:34px 14px;text-align:center;
  border:1px dashed var(--line);border-radius:9px}}
.note{{font-size:12px;color:var(--dim);margin:10px 2px}}
.find{{display:flex;align-items:center;gap:13px;padding:8px 11px;
  border-bottom:1px solid var(--line);text-decoration:none;color:inherit}}
.find:hover{{background:var(--card)}}
.find-body{{display:flex;flex-direction:column;min-width:0;flex:1}}
/* Кнопка "кадр" -- ИМЕННО span, а не ссылка: вся строка находки уже
   обёрнута в <a> (ведёт в плеер), а вложенная ссылка внутри ссылки
   невалидна и разбирается браузерами непредсказуемо. На этом в проекте
   уже обжигались с кнопкой плеера в списке файлов. */
/* Рамка в миниатюре. Раньше она была впечатана в саму картинку -- и
   при увеличении в окне предпросмотра рассыпалась на пиксели. */
.exp{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  margin:0 0 12px;font-size:13px}}
.exp-lbl{{color:var(--soft)}}
.exp-btn{{border:1px solid var(--line);border-radius:6px;padding:5px 11px;
  color:var(--fg);text-decoration:none}}
.exp-btn:hover{{border-color:var(--accent);color:var(--accent)}}
.exp-note{{color:var(--soft);font-size:12px}}
.find-open{{flex:none;align-self:center;font-size:12px;color:var(--soft);
  border:1px solid var(--line);border-radius:6px;padding:4px 9px;
  white-space:nowrap;cursor:pointer;text-align:center}}
/* Кнопка-значок: фиксированная ширина, чтобы подтверждение "✓" не меняло
   размер и не дёргало соседнюю кнопку. */
.find-open.ic{{min-width:30px;padding:4px 6px;font-size:13px}}
.find-open:hover{{color:var(--fg);border-color:var(--soft)}}
.find .lbl{{font-size:14px}}
.find .sub{{font-size:12px;color:var(--soft);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.find .when{{color:var(--dim);font-size:11.5px}}
.tag{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:4px;
  background:var(--card2);color:var(--soft);margin-right:6px}}
/* Статус триажа -- отдельной меткой, а не в подписи. Подпись говорит, ЧТО
   человек увидел ("резко чёрное"), статус -- к какому выводу пришли
   ("точно человек"); склеенные в строку, они теряют и то, и другое. */
.tag.st{{margin-left:7px;margin-right:0}}
.tag.st.confirmed_person,.tag.st.likely_person{{color:#8fe0b0}}
.tag.st.confirmed_object,.tag.st.likely_object{{color:#e0c07a}}
.tag.st.anomaly{{color:#9ec8ff}}
.tag.st.rejected{{color:var(--dim)}}

/* Отбор по статусу. Считать находки по рубрикам полезно само по себе:
   видно, сколько разобрано и сколько ещё нет. */
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}}
.chip{{font-family:inherit;font-size:12px;padding:4px 10px;border-radius:14px;
  border:1px solid var(--line);background:transparent;color:var(--soft);
  cursor:pointer;display:inline-flex;align-items:center;gap:6px}}
.chip:hover{{border-color:var(--accent);color:var(--ink)}}
.chip.on{{border-color:var(--accent);color:var(--ink);background:var(--card)}}
.chip .n{{font-size:11px;color:var(--dim)}}
.chip.on .n{{color:var(--accent)}}

/* Кадр находки. Тот же размер, что у превью материалов -- список находок и
   список материалов стоят на одной странице, и разнобой в размере читался
   бы как разная важность. */
.shot{{width:var(--thumb);height:var(--thumb);flex-shrink:0;border-radius:7px;
  overflow:hidden;background:var(--card2);display:block;position:relative}}
.shot img{{width:100%;height:100%;object-fit:cover;display:block}}
/* Рамка в миниатюре. Раньше она была впечатана в саму картинку -- и при
   увеличении в окне предпросмотра рассыпалась на пиксели, складываясь с
   нарисованной поверх. Теперь рисуем только здесь. */
.shot svg{{position:absolute;inset:0;width:100%;height:100%;
  pointer-events:none;overflow:visible}}
.shot svg rect{{fill:none;stroke:#ffd24a;stroke-width:1;
  vector-effect:non-scaling-stroke}}
.shot svg rect.under{{stroke:rgba(0,0,0,.75);stroke-width:2.5}}
/* Пока кадра нет (воркер до пометки не дошёл) -- ровный прямоугольник, а
   не пустая дыра и не значок битой картинки. */
.shot.noshot::after{{content:'▭';position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;color:var(--dim);
  font-size:calc(var(--thumb)*.4)}}

/* ОЖИДАНИЕ КАДРА -- один спиннер на всю платформу (static/spinner.gif).
   Лежит ПОД картинкой, как и запасной значок: строка не прыгает по высоте
   и выглядит одинаково до и после загрузки.

   Класс снимается И при успехе, И при отказе. При успехе -- чтобы браузер
   не крутил анимацию под непрозрачной картинкой (в списке таких строк
   двести). При отказе -- потому что крутилка над тем, что уже никогда не
   загрузится, врёт: человек ждёт вместо того, чтобы понять, что ждать
   нечего. Ровно тот молчаливый отказ, против которого весь проект.

   Файл отдаётся своей же статикой, а не из сети: платформа обязана
   работать в поле без интернета. */
.spin{{background-image:url(/static/spinner.gif);background-repeat:no-repeat;
  background-position:center;background-size:78% auto}}
/* Анимацию GIF нельзя остановить из CSS, поэтому при просьбе убрать
   движение просто не показываем её -- под ней остаётся статичный значок. */
/* Скобки разнесены по строкам НАМЕРЕННО: страж test_no_template_braces_leaked
   ищет в готовой странице «}}» как признак ошибки экранирования и не может
   отличить её от двух подряд закрывающих скобок вложенного CSS. */
@media (prefers-reduced-motion:reduce){{
  .spin{{background-image:none}}
}}
.find:hover .shot{{outline:1px solid var(--accent);outline-offset:-1px}}

/* Окно предпросмотра. Появляется по наведению на кадр, закрывается
   крестиком или когда курсор ушёл. Зум колесом и щипком. */
.peek{{position:fixed;z-index:60;background:var(--bg);border:1px solid var(--line);
  border-radius:10px;box-shadow:0 18px 50px rgba(0,0,0,.55);overflow:hidden;
  width:20vw;min-width:280px;display:none}}
.peek.on{{display:block}}
.peek-view{{position:relative;overflow:hidden;background:#000;
  touch-action:none;cursor:zoom-in}}
.peek-zoom{{transform-origin:0 0;will-change:transform;position:relative}}
.peek-view img{{display:block;width:100%}}
/* Рамка находки рисуется поверх кадра, а не вжигается в него: остаётся
   чёткой на любом увеличении (non-scaling-stroke) и не мешает смотреть
   на саму находку. Кликов не перехватывает -- иначе съела бы зум и
   перетаскивание. */
.peek-box{{position:absolute;inset:0;width:100%;height:100%;
  pointer-events:none;overflow:visible}}
/* Толщина в ЭКРАННЫХ пикселях и не растёт при увеличении -- за это
   отвечает non-scaling-stroke. Полторы точки: рамка обязана быть видна,
   но находки бывают мелкие, и жирная линия закрывает собой то самое,
   ради чего её открыли. */
.peek-box rect{{fill:none;stroke:#ffd24a;stroke-width:1.5;
  vector-effect:non-scaling-stroke}}
.peek-box rect.under{{stroke:rgba(0,0,0,.75);stroke-width:3}}
.peek-busy{{position:absolute;top:6px;left:6px;z-index:2;font-size:10.5px;
  color:#e8eeec;background:rgba(0,0,0,.5);padding:2px 6px;border-radius:4px;
  display:none;align-items:center;gap:5px}}
.peek-busy.on{{display:flex}}
.peek-busy i.spin{{width:20px;height:20px;flex-shrink:0;background-size:contain}}
.peek-cap{{font-size:12px;color:var(--soft);padding:7px 10px;
  border-top:1px solid var(--line);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}}
.peek-x{{position:absolute;top:6px;right:6px;z-index:2;width:26px;height:26px;
  border-radius:50%;border:none;background:rgba(0,0,0,.55);color:#fff;
  font-size:15px;line-height:1;cursor:pointer}}
.peek-x:hover{{background:rgba(0,0,0,.85)}}
.peek-hint{{position:absolute;left:6px;bottom:6px;z-index:2;font-size:10.5px;
  color:#e8eeec;background:rgba(0,0,0,.5);padding:2px 6px;border-radius:4px}}
@media (max-width:760px){{
  /* На телефоне окно во всю ширину: 20% экрана там -- это ничто. */
  .peek{{width:100vw;min-width:0;left:0 !important;right:0;
    top:auto !important;bottom:0;border-radius:12px 12px 0 0}}
}}
</style></head><body>
<div class="wrap">
  <div class="top">
    <a href="/operations">← Все операции</a>
    <span><a href="/guide">📖 Как смотреть</a> &nbsp;·&nbsp; {viewer_name}</span>
  </div>
  <h1 id="title">…</h1>
  <div class="area" id="area"></div>
  <div class="sum" id="sum"></div>
  <div class="cov"><i id="covbar" style="width:0%"></i></div>

  <div class="tabs">
    <button class="tab on" data-t="mat">Материалы</button>
    <button class="tab" data-t="find">Находки</button>
    <button class="tab" data-t="map">Карта</button>
    <button class="tab" data-t="rep">Отчёт</button>
    <!-- Эфиры пока заглушка: отодвинуты вправо и приглушены, чтобы не
         стояли в одном ряду с работающими вкладками и не обещали лишнего. -->
    <button class="tab later" data-t="live">Эфиры</button>
  </div>
  <div id="searchbar">
    <input id="q" type="search" placeholder="Поиск по всей операции…"
           autocomplete="off" spellcheck="false">
    <span id="qhint"></span>
  </div>
  <div id="body"></div>
</div>

<script>
const OP = Number(location.pathname.split('/').filter(Boolean)[1]);
let path = new URLSearchParams(location.search).get('path') || '';
// Вкладка берётся из адреса: без этого на находки нельзя было дать
// прямую ссылку -- человек открывал страницу и должен был догадаться
// нажать нужную вкладку сам.
let tab = new URLSearchParams(location.search).get('tab') || 'mat';

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
const hhmm = s => {{
  s = Math.round(s || 0);
  const h = Math.floor(s/3600), m = Math.round((s%3600)/60);
  return h ? `${{h}} ч ${{m}} мин` : `${{m}} мин`;
}};

// Когда находку записали. Год обязателен: поиски идут годами, и «16.08»
// без года не отличить от прошлогоднего. Никакого внешнего сервиса
// времени -- toLocaleString встроен в браузер, работает офлайн и в любой
// стране, ничего никуда не передаёт.
const fmtStamp = iso => {{
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).replace('T', ' ').slice(0, 16);
  return d.toLocaleString('ru-RU', {{ day:'2-digit', month:'2-digit',
                                      year:'numeric', hour:'2-digit',
                                      minute:'2-digit' }});
}};

document.querySelectorAll('.tab').forEach(b => b.onclick = () => {{
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  tab = b.dataset.t;
  // Адрес обновляем без перезагрузки: ссылку на вкладку можно скопировать
  // прямо из строки браузера, а кнопка "назад" возвращает куда ожидается.
  const u = new URL(location.href);
  if (tab === 'mat') u.searchParams.delete('tab'); else u.searchParams.set('tab', tab);
  history.replaceState(null, '', u);
  render();
}});
// Отметить активной ту вкладку, что пришла из адреса.
document.querySelectorAll('.tab').forEach(x => {{
  x.classList.toggle('on', x.dataset.t === tab);
}});

let data = null, findings = null;

async function loadBrowse() {{
  const r = await fetch(`/api/operations/${{OP}}/browse?path=` + encodeURIComponent(path));
  data = await r.json();
  const o = data.operation, s = data.summary;
  document.getElementById('title').textContent = o.title;
  document.getElementById('area').textContent = o.area || '';
  document.getElementById('sum').innerHTML =
    `<span>материалов <b>${{s.materials}}</b></span>` +
    `<span>отснято <b>${{hhmm(s.footage_sec)}}</b></span>` +
    `<span>просмотрено <b>${{hhmm(s.watched_sec)}}</b></span>` +
    `<span>человеко-часов <b>${{hhmm(s.viewer_sec)}}</b></span>` +
    `<span>пометок <b>${{s.marks}}</b></span>`;
  document.getElementById('covbar').style.width = (s.coverage_pct || 0) + '%';
  render();
}}

// Крошки: путь с возвратом на любой уровень. Без них человек, провалившись
// в третью вложенную папку, не понимает, где он и как выйти.
function crumbs() {{
  const parts = path ? path.split('/') : [];
  let acc = '', html = `<a href="#" onclick="go('');return false">${{esc(data.operation.title)}}</a>`;
  parts.forEach((p, i) => {{
    acc = acc ? acc + '/' + p : p;
    const last = i === parts.length - 1;
    html += `<span class="sep">›</span>` + (last
      ? esc(p)
      : `<a href="#" onclick="go('${{esc(acc)}}');return false">${{esc(p)}}</a>`);
  }});
  return `<div class="crumbs">${{html}}</div>`;
}}

function go(p) {{
  path = p;
  history.replaceState(null, '', `/operation/${{OP}}/` + (p ? '?path=' + encodeURIComponent(p) : ''));
  loadBrowse();
}}

function fileRow(f) {{
  const href = f.kind === 'video' ? `/report/${{f.report_id}}/player/`
                                   : `/report/${{f.report_id}}/viewer/`;
  const bar = (f.percent != null)
    ? `<span class="bar"><i style="width:${{f.percent}}%"></i></span>` : '';
  // Превью просится по ПОЛНОМУ пути материала (rel_path), а не по одному
  // имени файла: в разных папках операции лежат файлы с совпадающими
  // именами, и по короткому имени превью досталось бы не тому.
  const key = encodeURIComponent(f.rel_path || f.name);
  const glyph = f.kind === 'video' ? '▭' : '🖼';
  // onerror убирает картинку, и из-под неё показывается значок. Превью
  // может не быть законно: файл только положили, воркер до него не дошёл.
  const thumb = `<span class="thumb spin"><span class="fb">${{glyph}}</span>` +
    `<img src="/api/thumbnail/${{key}}" alt="" loading="lazy" ` +
    `decoding="async" onload="this.parentNode.classList.remove('spin')" ` +
    `onerror="this.parentNode.classList.remove('spin');this.remove()"></span>`;
  return `<a class="row" href="${{href}}">
    ${{thumb}}
    <span class="nm">${{esc(f.name)}}</span>
    ${{bar}}
    <span class="meta">✍ ${{f.manual_count || 0}}</span></a>`;
}}

function render() {{
  const b = document.getElementById('body');
  if (!data) return;

  // Поиск нужен только там, где есть по чему искать -- на вкладке
  // материалов. На находках и отчёте он был бы полем, которое ничего не
  // делает.
  document.getElementById('searchbar').classList.toggle('on', tab === 'mat');
  if (tab !== 'mat' && query) {{ query = ''; document.getElementById('q').value = ''; }}

  if (tab === 'mat') {{
    let html = crumbs();
    const rows = data.folders.map(f =>
      `<a class="row" href="#" onclick="go('${{esc(path ? path + '/' + f.name : f.name)}}');return false">
        <span class="ic">📁</span><span class="nm">${{esc(f.name)}}</span>
        <span class="meta">${{f.files ? f.files + ' файл(ов)' : '—'}}</span></a>`
      // значок папки того же размера, что превью: иначе имена файлов и
      // имена папок встанут в разные колонки и список поедет
    ).concat(data.files.map(fileRow));

    if (data.outside && data.outside.length) {{
      rows.push(`<div class="note">Вне папки операции — добавлены вручную:</div>`);
      data.outside.forEach(f => rows.push(fileRow(f)));
    }}
    html += rows.length ? rows.join('') : `<div class="empty">Папка пуста</div>`;
    b.innerHTML = html;

  }} else if (tab === 'find') {{
    if (!findings) {{ b.innerHTML = '<div class="empty">Загружаю…</div>'; loadFindings(); return; }}
    const shown = findings.filter(passesFilter);
    b.innerHTML =
      `<div class="note">Только размеченное человеком: ручные пометки и
         статусы триажа. Сами по себе сцены модели сюда не попадают — их
         тысячи, и настоящие находки в них потерялись бы.</div>` +
      exportButtons() +
      findFilters() +
      (shown.length
      ? shown.map(f => {{
          // Находка ведёт ТУДА, ГДЕ ОНА НАЙДЕНА: в плеер на её таймкод.
          // Список без переходов бесполезен -- человек видит «верёвка,
          // 66 с» и не может посмотреть, что там на самом деле.
          const href = `/report/${{f.report_id}}/player/` +
            (f.seconds != null ? `?t=${{Math.max(0, Math.floor(f.seconds))}}` : '');
          const tc = f.seconds != null
            ? String(Math.floor(f.seconds / 60)).padStart(2,'0') + ':' +
              String(Math.floor(f.seconds % 60)).padStart(2,'0')
            : '';
          // Кадр находки. Имена файлов с дрона неразличимы, подпись вроде
          // «резко чёрное» тоже мало что говорит -- узнаётся именно кадр.
          const cap = esc(f.label || '') + (tc ? ' · ' + tc : '');
          // Спиннер ставим ТОЛЬКО когда кадр реально ожидается (f.preview
          // есть). Если превью нет вовсе -- сразу ровный прямоугольник:
          // крутить над тем, чего не будет, значит обещать несбыточное.
          const shot = f.preview
            ? `<span class="shot spin" onmouseenter="showPeek(this,'${{f.preview}}','${{cap}}',${{f.obs_id || 'null'}},${{JSON.stringify(f.bbox || null)}})">
                 <img src="${{f.preview}}" alt="" loading="lazy" decoding="async"
                      onload="this.parentNode.classList.remove('spin')"
                      onerror="this.parentNode.classList.remove('spin');this.parentNode.classList.add('noshot');this.remove()">${{boxSvg(f.bbox)}}</span>`
            : `<span class="shot noshot"></span>`;
          const status = f.status
            ? `<span class="tag st ${{f.priority}}">${{esc(f.status)}}</span>` : '';
          return `<a class="find" href="${{href}}">
            ${{shot}}
            <span class="find-body">
              <span class="lbl"><span class="tag">${{f.kind === 'manual' ? '✍ пометка' : '🏷 триаж'}}</span>${{esc(f.label) || '—'}}${{status}}</span>
              <span class="sub">${{esc(f.file)}}${{tc ? ' · ' + tc : ''}}${{f.viewer ? ' · ' + esc(f.viewer) : ''}}${{f.lat ? ' · 📍' : ''}}</span>
              <span class="sub when">записано ${{fmtStamp(f.created_at)}}</span>
            </span>
            ${{f.obs_id ? `<span class="find-open" title="Открыть кадр находки"
                 onclick="openFrame(event, ${{f.obs_id}})">кадр</span>` : ''}}
            <span class="find-open ic" title="Скопировать ссылку на находку"
              onclick="copyFindingLink(event, ${{f.obs_id || 'null'}}, '${{f.report_id}}', ${{f.seconds === null || f.seconds === undefined ? 'null' : f.seconds}})">🔗</span>
          </a>`;
        }}).join('')
      : `<div class="empty">${{findings.length
            ? 'В этом отборе находок нет'
            : 'Находок пока нет'}}</div>`);

  }} else if (tab === 'map') {{
    b.innerHTML = `
      <div class="mapbar">
        <label><input type="checkbox" id="l-obj" checked>
          <span class="key obj"></span>точка объекта</label>
        <label><input type="checkbox" id="l-drone" checked>
          <span class="key drone"></span>позиция дрона</label>
        <label><input type="checkbox" id="l-mark" checked>
          <span class="key mark"></span>отметка человека</label>
        <label><input type="checkbox" id="l-track">
          <span class="key track"></span>треки дрона</label>
        <button class="addmode" id="addbtn">＋ поставить отметку</button>
      </div>
      <div id="map"></div>
      <div class="mapnote" id="mapnote">Загружаю…</div>`;
    drawMap();

  }} else if (tab === 'live') {{
    b.innerHTML = `<div class="empty">Эфиры появятся, когда включим трансляции.<br>
      Записи эфиров будут попадать сюда же, в материалы операции.</div>`;
  }} else {{
    b.innerHTML = `
      <div class="rep-bar">
        <label>с <input type="date" id="r-from" value="${{repFrom}}"></label>
        <label>по <input type="date" id="r-to" value="${{repTo}}"></label>
        <button id="r-all">весь период</button>
        <label><input type="checkbox" id="r-anon" ${{repAnon ? 'checked' : ''}}>
          обезличить</label>
        <button id="r-print">🖨 печать / PDF</button>
      </div>
      <div id="rep"><div class="empty">Собираю…</div></div>`;
    document.getElementById('r-from').onchange = reloadReport;
    document.getElementById('r-to').onchange = reloadReport;
    document.getElementById('r-anon').onchange = reloadReport;
    document.getElementById('r-all').onclick = () => {{
      repFrom = repTo = ''; render();
    }};
    document.getElementById('r-print').onclick = () => window.print();
    drawReport();
  }}
}}

// --- отчёт ----------------------------------------------------------------
//
// Главное в этом отчёте -- не найденное, а НЕ ПРОСМОТРЕННОЕ. Отчёт,
// показывающий только находки, льстит: он отвечает на вопрос «что мы
// нашли», тогда как решение принимается по вопросу «куда ещё не смотрели».
// Поэтому непросмотренное вынесено в заметные числа, а не спрятано в
// таблицу.

let repFrom = '', repTo = '', repAnon = false, repData = null;
let repSort = {{ video: ['coverage_pct', 1], photo: ['findings', 1] }};
let repFilter = {{ video: '', photo: '' }};

function reloadReport() {{
  repFrom = document.getElementById('r-from').value || '';
  repTo = document.getElementById('r-to').value || '';
  repAnon = document.getElementById('r-anon').checked;
  drawReport();
}}

async function drawReport() {{
  const host = document.getElementById('rep');
  const qs = new URLSearchParams();
  if (repFrom) qs.set('from', repFrom);
  if (repTo) qs.set('to', repTo);
  if (repAnon) qs.set('anon', '1');
  try {{
    repData = await (await fetch(
      `/api/operations/${{OP}}/report?` + qs.toString())).json();
  }} catch (e) {{
    host.innerHTML = `<div class="empty">Отчёт не собрался: ${{esc(String(e))}}</div>`;
    return;
  }}
  paintReport();
}}

function kpi(value, label, warn) {{
  return `<div class="rep-kpi${{warn ? ' warn' : ''}}"><b>${{value}}</b>`
    + `<span>${{label}}</span></div>`;
}}

function paintReport() {{
  const d = repData, v = d.volume, c = d.coverage, f = d.findings;
  const period = d.period.full
    ? 'за всю операцию'
    : `за период ${{d.period.from || '…'}} — ${{d.period.to || '…'}}`;

  let h = `<div class="rep-sec"><h3>${{esc(d.operation.title)}} — ${{period}}`
    + (d.anonymized ? ' <span style="color:#9aa8a5">(обезличено)</span>' : '')
    + `</h3></div>`;

  h += `<div class="rep-sec"><h3>Объём</h3><div class="rep-grid">`
    + kpi(v.materials, 'материалов')
    + kpi(v.videos, 'видео')
    + kpi(v.photos, 'фотографий')
    + kpi(hhmm(v.footage_sec), 'отснято')
    + kpi(hhmm(v.viewer_sec), 'человеко-часов разбора')
    + `</div>`;
  if (v.footage_known < v.videos) {{
    h += `<div class="rep-note">Длительность известна у <b>${{v.footage_known}}</b> `
      + `видео из ${{v.videos}} — «отснято» считается только по ним.</div>`;
  }}
  h += `</div>`;

  // САМЫЙ ВАЖНЫЙ БЛОК. Непросмотренное -- первым и с подсветкой.
  h += `<div class="rep-sec"><h3>Покрытие</h3><div class="rep-grid">`
    + kpi(c.videos_untouched, 'видео НЕ открывал никто', c.videos_untouched > 0)
    + kpi(c.videos_touched, 'видео открывал хоть кто-то')
    + kpi(hhmm(c.watched_sec), 'просмотрено')
    + `</div>`;
  if (!c.photos_tracked && c.photos_total) {{
    h += `<div class="rep-note">Просмотр фотографий платформа <b>не отслеживает</b>: `
      + `отрезки пишет только плеер видео. Про ${{c.photos_total}} снимков нельзя `
      + `сказать ни что их смотрели, ни что нет — это не ноль, это отсутствие `
      + `измерения.</div>`;
  }}
  h += `</div>`;

  h += `<div class="rep-sec"><h3>Второй проход</h3><div class="rep-grid">`;
  d.second_pass.forEach(x => {{
    h += kpi(x.materials, x.viewers === 0
      ? 'материалов не смотрел никто'
      : `материалов смотрели ${{x.viewers}} чел.`, x.viewers === 0);
  }});
  h += `</div><div class="rep-note">Учёт второго прохода: важно не сколько `
    + `посмотрели, а сколько посмотрели <b>дважды</b>.</div></div>`;

  h += `<div class="rep-sec"><h3>Находки</h3><div class="rep-grid">`
    + kpi(f.total, 'пометок всего')
    + kpi(f.with_object_point, 'с расчётной точкой объекта')
    + kpi(f.drone_only, 'только позиция дрона', f.drone_only > 0)
    + kpi(f.without_coords, 'без координат', f.without_coords > 0)
    + `</div>`;
  const st = Object.entries(f.by_status || {{}});
  if (st.length) {{
    h += `<div class="rep-grid" style="margin-top:10px">`
      + st.map(([k, n]) => kpi(n, esc(k))).join('') + `</div>`;
  }}
  h += `<div class="rep-note">Выгрузка: `
    + `<a href="/api/operations/${{OP}}/findings.kml">KML</a> · `
    + `<a href="/api/operations/${{OP}}/findings.gpx">GPX</a></div></div>`;

  h += `<div class="rep-sec"><h3>Кто работал</h3>`
    + repTable('people', d.people) + `</div>`;

  h += `<div class="rep-sec"><h3>Покрытие по видео</h3>`
    + `<input class="rep-filter" id="fv" placeholder="фильтр по имени…" `
    + `value="${{esc(repFilter.video)}}">`
    + repTable('video', d.materials.video) + `</div>`;

  h += `<div class="rep-sec"><h3>Фотографии</h3>`
    + `<input class="rep-filter" id="fp" placeholder="фильтр по имени…" `
    + `value="${{esc(repFilter.photo)}}">`
    + repTable('photo', d.materials.photo) + `</div>`;

  h += `<div class="rep-sec"><h3>Чего этот отчёт не показывает</h3>`
    + `<div class="rep-note">`
    + `• «Просмотрено» значит «кто-то проиграл этот отрезок», а не «увидел `
    + `всё, что там было».<br>`
    + `• У <b>${{f.without_coords}}</b> находок координат нет вовсе, `
    + `у <b>${{f.drone_only}}</b> известна только позиция дрона — на этом `
    + `материале расхождение доходит до 653 метров.<br>`
    + `• Телеметрия есть у <b>${{d.geography.tracks}}</b> видео из `
    + `${{d.geography.videos}}: находки на остальных на земле не локализовать.<br>`
    + `• Детектор — вспомогательный сигнал, а не заключение.`
    + (c.photos_tracked ? '' : `<br>• Просмотр фотографий не измеряется.`)
    + `</div></div>`;

  document.getElementById('rep').innerHTML = h;
  wireReportTables();
}}

const REP_COLS = {{
  people: [
    ['name', 'кто', 0], ['seconds', 'времени', 1],
    ['materials', 'материалов', 1], ['marks', 'пометок', 1]],
  video: [
    ['name', 'файл', 0], ['folder', 'папка', 0],
    ['coverage_pct', 'просмотрено', 1], ['views', 'просмотров', 1],
    ['viewers', 'человек', 1], ['findings', 'находок', 1]],
  photo: [
    ['name', 'файл', 0], ['folder', 'папка', 0], ['findings', 'находок', 1]],
}};

function repTable(kind, rows) {{
  const cols = REP_COLS[kind];
  const q = (repFilter[kind] || '').toLowerCase();
  let list = rows.slice();
  if (q && kind !== 'people') {{
    list = list.filter(r => ((r.folder || '') + '/' + r.name).toLowerCase().includes(q));
  }}
  const [key, dir] = repSort[kind] || [cols[0][0], 1];
  list.sort((a, x) => {{
    const A = a[key], B = x[key];
    if (A === B) return 0;
    if (A === null || A === undefined) return 1;   // «нет данных» -- в конец
    if (B === null || B === undefined) return -1;
    return (A > B ? 1 : -1) * (dir ? -1 : 1);
  }});

  let h = `<div class="rep-tbl-wrap"><table class="rep"><thead><tr>`;
  cols.forEach(([k, label, num]) => {{
    const on = k === key ? (dir ? ' ↓' : ' ↑') : '';
    h += `<th class="${{num ? 'num' : ''}}" data-k="${{k}}" data-t="${{kind}}">`
      + `${{esc(label)}}${{on}}</th>`;
  }});
  h += `</tr></thead><tbody>`;
  if (!list.length) h += `<tr><td colspan="${{cols.length}}" class="dim">пусто</td></tr>`;
  list.forEach(r => {{
    h += '<tr>';
    cols.forEach(([k, , num]) => {{
      let val = r[k];
      if (k === 'seconds') val = hhmm(val);
      else if (k === 'coverage_pct') {{
        // Пустая длительность -- НЕ ноль процентов: посчитать не из чего.
        val = (val === null || val === undefined)
          ? '<span class="dim">нет длительности</span>'
          : `<span class="covcell">${{val}}%<span class="covbar">`
            + `<i style="width:${{val}}%"></i></span></span>`;
      }} else if (k === 'folder') val = `<span class="dim">${{esc(val || '—')}}</span>`;
      else val = esc(val);
      h += `<td class="${{num ? 'num' : ''}}">${{val}}</td>`;
    }});
    h += '</tr>';
  }});
  return h + `</tbody></table></div>`;
}}

function wireReportTables() {{
  document.querySelectorAll('table.rep th').forEach(th => {{
    th.onclick = () => {{
      const k = th.dataset.k, t = th.dataset.t;
      const [cur, dir] = repSort[t] || [];
      repSort[t] = [k, cur === k ? !dir : true];
      paintReport();
    }};
  }});
  [['fv', 'video'], ['fp', 'photo']].forEach(([id, kind]) => {{
    const el = document.getElementById(id);
    if (!el) return;
    el.oninput = () => {{
      repFilter[kind] = el.value;
      paintReport();
      const again = document.getElementById(id);
      if (again) {{ again.focus(); again.setSelectionRange(9999, 9999); }}
    }};
  }});
}}

// --- карта ----------------------------------------------------------------
//
// ЧЕСТНОСТЬ КАРТЫ -- главное требование, а не украшение. Из 32 пометок
// операции только у 8 посчитана точка объекта, у 7 известна лишь позиция
// дрона, у 17 координат нет вовсе. На материале этой операции дрон и
// объект расходятся до 653 метров -- это соседнее ущелье. Поэтому:
//
//   * «точка объекта» и «позиция дрона» -- РАЗНЫЕ значки, как в выгрузке KML;
//   * между ними рисуется линия, чтобы разнос было видно, а не прочитать;
//   * сколько находок на карту НЕ попало -- написано прямо под ней.
//
// Карта, молча скрывающая половину пометок, хуже отсутствия карты: по ней
// делают вывод, что искать больше негде.

let mapObj = null, mapData = null, mapLayers = {{}}, tracksData = null;
let addingMark = false;

async function drawMap() {{
  const note = document.getElementById('mapnote');
  if (!mapData) {{
    try {{
      mapData = await (await fetch(`/api/operations/${{OP}}/findings-map`)).json();
    }} catch (e) {{
      note.textContent = 'Не удалось загрузить точки: ' + e;
      return;
    }}
  }}

  if (mapObj) {{ mapObj.remove(); mapObj = null; }}
  mapObj = L.map('map', {{ zoomControl: true }});
  // Подпись Leaflet -- обычной ссылкой, без встроенной в неё картинки.
  //
  // По умолчанию Leaflet 1.9 подставляет в prefix свой SVG-значок. Это
  // высказывание разработчиков библиотеки, а не требование лицензии:
  // BSD-2 обязывает сохранять текст лицензии при распространении (он
  // лежит в static/leaflet/LICENSE), про интерфейс там нет ничего.
  // Платформа поисково-спасательная, и лишних высказываний на её карте
  // быть не должно -- ни в одну сторону.
  //
  // Подпись OpenStreetMap ниже трогать НЕЛЬЗЯ: вот она как раз
  // обязательна по ODbL. См. THIRD-PARTY.md.
  mapObj.attributionControl.setPrefix(
    '<a href="https://leafletjs.com" title="Библиотека карт">Leaflet</a>');
  L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19,
    // Указание авторства ODbL обязательно и убирать его нельзя -- см.
    // THIRD-PARTY.md. Без интернета подложки не будет, точки останутся.
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }}).addTo(mapObj);

  mapLayers = {{
    obj: L.layerGroup(), drone: L.layerGroup(),
    mark: L.layerGroup(), track: L.layerGroup()
  }};

  const bounds = [];
  (mapData.points || []).forEach(p => {{
    bounds.push([p.lat, p.lon]);
    if (p.estimated) {{
      L.circleMarker([p.lat, p.lon], {{
        radius: 7, color: '#3ecf8e', weight: 2, fillOpacity: .85, fillColor: '#3ecf8e'
      }}).bindTooltip(tipFor(p))
        .bindPopup(findingPopup(p), POPUP_OPTS)
        .addTo(mapLayers.obj);
      // Линия к позиции дрона: разнос виден глазом, а не в подписи.
      if (p.drone) {{
        L.polyline([[p.lat, p.lon], [p.drone.lat, p.drone.lon]], {{
          color: '#9aa8a5', weight: 1, dashArray: '4,5', opacity: .7
        }}).addTo(mapLayers.obj);
      }}
    }} else {{
      L.circleMarker([p.lat, p.lon], {{
        radius: 6, color: '#9aa8a5', weight: 2, dashArray: '3,3', fillOpacity: 0
      }}).bindTooltip(tipFor(p))
        .bindPopup(findingPopup(p), POPUP_OPTS)
        .addTo(mapLayers.drone);
    }}
  }});

  (mapData.marks || []).forEach(m => {{
    bounds.push([m.lat, m.lon]);
    L.marker([m.lat, m.lon], {{
      icon: L.divIcon({{
        className: '', iconSize: [14, 14], iconAnchor: [7, 7],
        html: '<div style="width:14px;height:14px;background:#e0a33a;'
            + 'border:2px solid #0d0d0d;border-radius:3px"></div>'
      }})
    }}).bindTooltip(esc(m.label || 'отметка'))
      .bindPopup(markPopup(m), POPUP_OPTS)
      .addTo(mapLayers.mark);
  }});

  mapLayers.obj.addTo(mapObj);
  mapLayers.drone.addTo(mapObj);
  mapLayers.mark.addTo(mapObj);

  if (bounds.length) mapObj.fitBounds(bounds, {{ padding: [40, 40], maxZoom: 16 }});
  else mapObj.setView([39.48, 73.59], 12);

  ['obj', 'drone', 'mark', 'track'].forEach(k => {{
    const cb = document.getElementById('l-' + k);
    cb.onchange = () => {{
      if (!cb.checked) {{ mapObj.removeLayer(mapLayers[k]); return; }}
      mapLayers[k].addTo(mapObj);
      if (k === 'track') loadTracks();
    }};
  }});

  document.getElementById('addbtn').onclick = toggleAdding;
  mapObj.on('click', onMapClick);
  updateMapNote();
}}

// Наведение -- ПОДСКАЗКА С НАЗВАНИЕМ, клик -- карточка.
//
// Раньше наведение открывало карточку целиком: с кадром, оговорками и
// ссылкой. На карте с полутора десятками точек это означало, что карточка
// выскакивает при каждом движении мыши и закрывает соседние точки.
// Подсказка отвечает на вопрос «что это», карточка -- на вопрос
// «расскажи подробнее», и второй задаётся осознанно.
//
// У точки, где известна только позиция ДРОНА, в подсказке это сказано:
// иначе при наведении видно «рюкзак» над местом, где рюкзака нет.
// Карточка не должна вылезать за рамку карты.
//
// Leaflet сам подвигает карту, чтобы вписать открытую карточку -- но
// только если ей есть куда вписаться. Высокая карточка (кадр + текст)
// упиралась в верхний край и обрезалась. Поэтому: предел высоты со своей
// прокруткой и отступ, внутри которого карта двигаться не пытается.
const POPUP_OPTS = {{
  maxWidth: 270,
  maxHeight: 340,
  autoPanPadding: [24, 24],
}};

function tipFor(p) {{
  return esc(p.title) + (p.estimated ? '' : ' — позиция дрона');
}}

function findingPopup(p) {{
  // Кадр находки прямо в карточке: без него точка на карте -- просто
  // кружок, и чтобы понять, что там, надо уходить на страницу находки.
  //
  // onerror прячет картинку, а не оставляет значок битого изображения:
  // превью может не быть (воркер не дошёл, находка старая), и это не
  // ошибка -- карточка должна остаться читаемой.
  // max-height обязателен: без него вертикальный кадр делает карточку
  // выше карты, и Leaflet не может её вписать -- она уезжает за верхнюю
  // рамку и обрезается. Ровно это и было видно на боевой карте.
  const pic = `<img src="/api/finding/${{p.id}}/preview" alt="" `
    + `style="width:100%;max-width:240px;max-height:150px;object-fit:cover;`
    + `border-radius:6px;display:block;margin-bottom:9px;background:#0d0d0d" `
    + `onerror="this.style.display='none'">`;
  const head = p.estimated
    ? '<b style="color:#3ecf8e">Вероятная точка объекта</b>'
    : '<b style="color:#9aa8a5">Позиция ДРОНА, не объекта</b>';
  // Одной строкой НАМЕРЕННО. Внутри бэктиков перенос -- это перенос, а не
  // склейка: попытка продолжить строку кавычкой-плюсом-кавычкой отправляет
  // эти символы прямо в карточку как текст. На боевой карте так и было
  // видно -- лишние знаки между словом «дальность» и числом.
  const dist = p.distance_m ? Math.round(p.distance_m) : null;
  const warn = p.estimated
    ? (dist ? `<br><span style="color:#9aa8a5">расчётная дальность ${{dist}} м — это расчёт, а не измерение</span>` : '')
    : `<br><span style="color:#9aa8a5">объект в стороне: точка на земле не посчитана</span>`;
  return `${{pic}}${{head}}${{warn}}<br><br><b>${{esc(p.title)}}</b>`
    + (p.note ? `<br>${{esc(p.note)}}` : '')
    + (p.status ? `<br>статус: ${{esc(p.status)}}` : '')
    + (p.file ? `<br><span style="color:#6f7d7a">${{esc(p.file)}}`
        + (p.tc ? ` ${{esc(p.tc)}}` : '') + '</span>' : '')
    + (p.viewer ? `<br><span style="color:#6f7d7a">отметил: ${{esc(p.viewer)}}</span>` : '')
    + `<br><br><a href="/finding/${{p.id}}/">открыть находку →</a>`;
}}

function markPopup(m) {{
  return `<b style="color:#e0a33a">Отметка на карте</b><br>`
    + `<span style="color:#9aa8a5">поставлена человеком, не расчёт</span><br><br>`
    + (m.label ? `<b>${{esc(m.label)}}</b><br>` : '')
    + (m.note ? `${{esc(m.note)}}<br>` : '')
    + `<span style="color:#6f7d7a">${{esc(m.viewer_name)}}</span><br>`
    + `${{m.lat.toFixed(6)}}, ${{m.lon.toFixed(6)}}<br><br>`
    + `<a href="#" onclick="delMark(${{m.id}});return false" `
    + `style="color:#c8553d">убрать отметку</a>`;
}}

function updateMapNote() {{
  const d = mapData, note = document.getElementById('mapnote');
  const bits = [`На карте <b>${{d.points.length}}</b> из <b>${{d.total}}</b> находок: `
    + `${{d.estimated}} с точкой объекта, ${{d.drone_only}} только с позицией дрона.`];
  if (d.without_coords) {{
    // Самое важное предложение на этой вкладке.
    bits.push(`<b>У ${{d.without_coords}} находок координат нет вовсе</b> — `
      + `их на карте не видно. Пустое место здесь не значит «там не искали».`);
  }}
  if (d.drone_only) {{
    bits.push(`Пунктирные кружки — это где был ДРОН, а не где находка. `
      + `Расхождение на материале операции доходит до 653 метров.`);
  }}
  if (tracksData) {{
    let t = `Треки: ${{tracksData.tracks.length}} видео с телеметрией, `
      + `у ${{tracksData.without_telemetry}} её нет.`;
    // «Ещё не разобрано» и «телеметрии нет» -- разные вещи: первое пройдёт
    // само, второе не изменится никогда. Смешать их значит обещать треки,
    // которых не будет.
    if (tracksData.pending) {{
      t += ` Ещё ${{tracksData.pending}} в разборе — обновите страницу позже.`;
    }}
    bits.push(t);
  }}
  note.innerHTML = bits.join('<br>');
}}

async function loadTracks() {{
  if (tracksData) return;
  const note = document.getElementById('mapnote');
  note.innerHTML = 'Загружаю треки…';
  try {{
    tracksData = await (await fetch(`/api/operations/${{OP}}/tracks`)).json();
  }} catch (e) {{
    note.textContent = 'Треки не загрузились: ' + e;
    return;
  }}
  tracksData.tracks.forEach(t => {{
    L.polyline(t.points, {{ color: '#4b9fd5', weight: 2, opacity: .65 }})
      .bindPopup(`<b>${{esc(t.name)}}</b><br>трек дрона по телеметрии`)
      .addTo(mapLayers.track);
  }});
  updateMapNote();
}}

function toggleAdding() {{
  addingMark = !addingMark;
  document.getElementById('addbtn').classList.toggle('on', addingMark);
  document.getElementById('map').classList.toggle('adding', addingMark);
}}

async function onMapClick(e) {{
  if (!addingMark) return;
  const label = prompt('Что здесь? (коротко)');
  if (label === null) return;
  const note = prompt('Пояснение (можно пропустить)') || '';
  const r = await fetch(`/api/operations/${{OP}}/map-marks`, {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ lat: e.latlng.lat, lon: e.latlng.lng, label, note }})
  }});
  const d = await r.json();
  if (!r.ok) {{ alert(d.error || 'не вышло'); return; }}
  mapData = null; tracksData = null;   // перечитаем вместе с новой отметкой
  toggleAdding();
  drawMap();
}}

async function delMark(id) {{
  const r = await fetch(`/api/operations/${{OP}}/map-marks/${{id}}`, {{ method: 'DELETE' }});
  const d = await r.json();
  if (!r.ok) {{ alert(d.error || 'не вышло'); return; }}
  mapData = null; tracksData = null;
  drawMap();
}}

// --- окно предпросмотра находки ------------------------------------------
//
// Кадр в строке маленький: он нужен, чтобы отличить одну находку от другой
// в списке. Чтобы РАЗГЛЯДЕТЬ находку, нужен масштаб -- ради этого окно и
// существует. Открывается по наведению, а не по клику, потому что клик по
// строке уже занят переходом в плеер на таймкод находки.
let peekEl = null, peekScale = 1, peekX = 0, peekY = 0, peekPinch = 0;
let peekDrag = null;      // {{x, y}} последней точки при перетаскивании
let peekPress = null;     // {{x, y, moved}} нажатия -- клик или таскание
let peekLoadId = 0;       // номер загрузки крупного кадра: отсекает опоздавшие

function peek() {{
  if (peekEl) return peekEl;
  peekEl = document.createElement('div');
  peekEl.className = 'peek';
  peekEl.innerHTML =
    `<div class="peek-view">
       <button class="peek-x" title="Закрыть">×</button>
       <span class="peek-busy"><i class="spin"></i>загружаю кадр…</span>
       <span class="peek-hint">клик и колесо — масштаб, перетаскивание — сдвиг</span>
       <div class="peek-zoom">
         <img alt="">
         <svg class="peek-box" viewBox="0 0 1 1" preserveAspectRatio="none">
           <rect class="under"></rect><rect></rect>
         </svg>
       </div>
     </div>
     <div class="peek-cap"></div>`;
  document.body.appendChild(peekEl);

  peekEl.querySelector('.peek-x').onclick = hidePeek;
  // Уводя курсор с окна, человек его и закрывает -- отдельного действия
  // для этого не нужно. Но НЕ во время перетаскивания: при быстром сдвиге
  // курсор легко выскакивает за край, и окно захлопывалось бы прямо
  // посреди движения.
  peekEl.addEventListener('mouseleave', () => {{ if (!peekDrag) hidePeek(); }});

  const view = peekEl.querySelector('.peek-view');
  view.addEventListener('wheel', e => {{
    e.preventDefault();
    zoomPeek(e.deltaY < 0 ? 1.25 : 1 / 1.25, e);
  }}, {{ passive: false }});

  // Перетаскивание левой кнопкой. Без него увеличенный кадр можно было
  // только зумить: край снимка становился недостижим, а находка нередко
  // как раз с краю.
  view.addEventListener('mousedown', e => {{
    if (e.button !== 0) return;
    e.preventDefault();
    // Точку нажатия запоминаем ВСЕГДА, даже когда тащить ещё нечего
    // (масштаб 1): по ней на отпускании отличаем клик от перетаскивания.
    peekPress = {{ x: e.clientX, y: e.clientY, moved: 0 }};
    if (peekScale > 1) {{
      peekDrag = {{ x: e.clientX, y: e.clientY }};
      view.style.cursor = 'grabbing';
    }}
  }});
  // Слушаем на документе, а не на окне: если курсор при быстром движении
  // выскочил за край, перетаскивание не должно застревать.
  document.addEventListener('mousemove', e => {{
    if (peekPress) {{
      peekPress.moved = Math.max(peekPress.moved,
        Math.hypot(e.clientX - peekPress.x, e.clientY - peekPress.y));
    }}
    if (!peekDrag) return;
    panPeek(e.clientX - peekDrag.x, e.clientY - peekDrag.y);
    peekDrag = {{ x: e.clientX, y: e.clientY }};
  }});
  document.addEventListener('mouseup', e => {{
    const press = peekPress;
    peekPress = null;
    if (peekDrag) {{
      peekDrag = null;
      view.style.cursor = peekScale > 1 ? 'grab' : 'zoom-in';
    }}
    // Курсор показывал лупу, но клик не делал ничего: увеличить можно было
    // только колесом, а на ноутбучном тачпаде это неудобно. Порог в 5 px --
    // чтобы дрожание руки на нажатии не считалось перетаскиванием.
    if (!press || press.moved > 5) return;
    if (!peekEl || !peekEl.classList.contains('on')) return;
    if (e.target.closest('.peek-x')) return;
    if (!e.target.closest('.peek-view')) return;
    // Shift -- отдалить: без него из глубокого зума пришлось бы выкручиваться
    // колесом, а на тачпаде это и есть исходная жалоба.
    zoomPeek(e.shiftKey ? 1 / 1.6 : 1.6, e);
  }});

  // Тот же жест одним пальцем на тач-экране.
  view.addEventListener('touchstart', e => {{
    if (e.touches.length !== 1 || peekScale <= 1) return;
    peekDrag = {{ x: e.touches[0].clientX, y: e.touches[0].clientY }};
  }}, {{ passive: true }});

  // Щипок на тач-экране. Дистанция между пальцами -> масштаб.
  view.addEventListener('touchmove', e => {{
    if (e.touches.length === 1 && peekDrag) {{
      e.preventDefault();
      panPeek(e.touches[0].clientX - peekDrag.x,
              e.touches[0].clientY - peekDrag.y);
      peekDrag = {{ x: e.touches[0].clientX, y: e.touches[0].clientY }};
      return;
    }}
    if (e.touches.length !== 2) return;
    e.preventDefault();
    const dx = e.touches[0].clientX - e.touches[1].clientX;
    const dy = e.touches[0].clientY - e.touches[1].clientY;
    const dist = Math.hypot(dx, dy);
    if (peekPinch) zoomPeek(dist / peekPinch, {{
      clientX: (e.touches[0].clientX + e.touches[1].clientX) / 2,
      clientY: (e.touches[0].clientY + e.touches[1].clientY) / 2,
    }});
    peekPinch = dist;
  }}, {{ passive: false }});
  view.addEventListener('touchend', () => {{ peekPinch = 0; peekDrag = null; }});
  return peekEl;
}}

function applyPeekTransform() {{
  const view = peekEl.querySelector('.peek-view');
  const zoom = peekEl.querySelector('.peek-zoom');
  // Не даём утащить картинку за края окна: иначе легко "потерять" её и
  // смотреть в пустоту, не понимая, куда всё делось.
  const w = view.clientWidth, h = view.clientHeight;
  peekX = Math.min(0, Math.max(peekX, w - w * peekScale));
  peekY = Math.min(0, Math.max(peekY, h - h * peekScale));
  zoom.style.transform =
    `translate(${{peekX}}px, ${{peekY}}px) scale(${{peekScale}})`;
  view.style.cursor = peekScale > 1 ? (peekDrag ? 'grabbing' : 'grab') : 'zoom-in';
}}

function panPeek(dx, dy) {{
  peekX += dx;
  peekY += dy;
  applyPeekTransform();
}}

function zoomPeek(factor, at) {{
  const view = peekEl.querySelector('.peek-view');
  const before = peekScale;
  peekScale = Math.min(8, Math.max(1, peekScale * factor));
  if (peekScale === before) return;

  // Точка под курсором должна остаться на месте -- иначе при увеличении
  // уезжает как раз то, что человек хотел рассмотреть.
  const r = view.getBoundingClientRect();
  const cx = (at.clientX - r.left - peekX) / before;
  const cy = (at.clientY - r.top - peekY) / before;
  peekX = at.clientX - r.left - cx * peekScale;
  peekY = at.clientY - r.top - cy * peekScale;
  applyPeekTransform();
}}

// --- ссылка на находку -----------------------------------------------------
//
// Адрес абсолютный и берётся с сервера, а не из location: платформа живёт за
// быстрым туннелем, и его имя меняется при каждом падении канала. Ссылка,
// собранная из location, была бы верной только пока открыта эта вкладка.
// Внешний адрес спрашиваем у сервера, а не подставляем в страницу:
// туннель меняет имя при каждом падении канала, и вкладка, открытая до
// падения, продолжила бы копировать мёртвые ссылки. Один запрос на
// загрузку страницы, дальше держим в памяти.
let EXTERNAL_BASE = '';
let BOT_NAME = '';
fetch('/api/external_base')
  .then(r => r.json())
  .then(d => {{ EXTERNAL_BASE = d.base || ''; BOT_NAME = d.bot || ''; }})
  .catch(() => {{}});

function findingLink(obsId, reportId, seconds) {{
  // ВЕЧНАЯ ссылка идёт через бота: адрес t.me не меняется никогда, а имя
  // быстрого туннеля -- при каждом перезапуске, и старое исчезает из DNS
  // без всякой возможности перенаправить.
  if (obsId && BOT_NAME) return `https://t.me/${{BOT_NAME}}?start=finding_${{obsId}}`;
  const base = EXTERNAL_BASE || location.origin;
  if (obsId) return `${{base}}/finding/${{obsId}}/`;
  if (!reportId) return '';
  const t = (seconds !== null && seconds !== undefined)
    ? `?t=${{Math.max(0, Math.floor(seconds))}}` : '';
  return `${{base}}/report/${{reportId}}/player/${{t}}`;
}}

async function copyFindingLink(e, obsId, reportId, seconds) {{
  if (e) {{ e.preventDefault(); e.stopPropagation(); }}
  const link = findingLink(obsId, reportId, seconds);
  if (!link) return;
  try {{
    await navigator.clipboard.writeText(link);
    flashCopied(e && e.target);
  }} catch (err) {{
    // Буфер обмена браузер отдаёт только по https, а платформа внутри сети
    // ходит по http. Молчаливое "ничего не произошло" -- худший исход,
    // поэтому показываем ссылку для ручного копирования.
    window.prompt('Скопируйте ссылку:', link);
  }}
}}

function flashCopied(el) {{
  if (!el) return;
  // Значком, а не словом: кнопка шириной в один символ, и "скопировано"
  // растянуло бы строку, сдвинув соседние кнопки на время подсказки.
  const was = el.textContent;
  el.textContent = '✓';
  setTimeout(() => {{ el.textContent = was; }}, 1400);
}}

// Рамка находки как SVG поверх картинки. Координаты нормализованы 0..1,
// поэтому viewBox "0 0 1 1" и preserveAspectRatio="none" ложатся ровно на
// кадр независимо от его размера и пропорций.
function boxSvg(bbox) {{
  if (!bbox || bbox.length !== 4) return '';
  const x1 = Math.min(bbox[0], bbox[2]), x2 = Math.max(bbox[0], bbox[2]);
  const y1 = Math.min(bbox[1], bbox[3]), y2 = Math.max(bbox[1], bbox[3]);
  const w = Math.max(0, x2 - x1), h = Math.max(0, y2 - y1);
  const r = `x="${{x1}}" y="${{y1}}" width="${{w}}" height="${{h}}"`;
  return `<svg viewBox="0 0 1 1" preserveAspectRatio="none">` +
         `<rect class="under" ${{r}}></rect><rect ${{r}}></rect></svg>`;
}}

// Строка находки ведёт в плеер, а эта кнопка -- на страницу кадра.
// Гасим и переход по внешней ссылке, и всплытие: иначе сработали бы оба.
function openFrame(e, obsId) {{
  e.preventDefault();
  e.stopPropagation();
  window.open(`/finding/${{obsId}}/`, '_blank', 'noopener');
}}

function showPeek(anchor, src, caption, obsId, bbox) {{
  const el = peek();
  peekScale = 1; peekX = 0; peekY = 0; peekDrag = null; peekPress = null;
  const img = el.querySelector('img');
  el.querySelector('.peek-zoom').style.transform = '';

  // Мелкий кадр показываем сразу -- он уже в кэше браузера, потому что
  // тот же файл стоит в сетке находок. Иначе окно открывалось бы пустым
  // на время загрузки крупного.
  img.src = src;

  // Крупный кадр (2560 px, без вжённой рамки) подгружаем следом и
  // подменяем. Именно ради него всё и затевалось: на мелком при
  // увеличении видна каша.
  const busy = el.querySelector('.peek-busy');
  peekLoadId++;
  const myLoad = peekLoadId;
  if (obsId) {{
    busy.classList.add('on');
    const big = new Image();
    big.onload = () => {{
      // Пока грузили, человек мог навести на другую находку -- тогда
      // подменять нельзя, иначе в окне окажется чужой кадр.
      if (myLoad !== peekLoadId) return;
      img.src = big.src;
      busy.classList.remove('on');
    }};
    big.onerror = () => {{ if (myLoad === peekLoadId) busy.classList.remove('on'); }};
    big.src = `/api/finding/${{obsId}}/preview?full=1`;
  }} else {{
    busy.classList.remove('on');
  }}

  // Рамка поверх кадра. У мелкого она вжжена внутрь, у крупного нет --
  // поэтому рисуем свою: после подмены картинки рамка остаётся на месте.
  const svg = el.querySelector('.peek-box');
  if (bbox && bbox.length === 4) {{
    const x1 = Math.min(bbox[0], bbox[2]), x2 = Math.max(bbox[0], bbox[2]);
    const y1 = Math.min(bbox[1], bbox[3]), y2 = Math.max(bbox[1], bbox[3]);
    svg.querySelectorAll('rect').forEach(r => {{
      r.setAttribute('x', x1); r.setAttribute('y', y1);
      r.setAttribute('width', Math.max(0, x2 - x1));
      r.setAttribute('height', Math.max(0, y2 - y1));
    }});
    svg.style.display = '';
  }} else {{
    svg.style.display = 'none';
  }}

  el.querySelector('.peek-cap').textContent = caption || '';
  el.classList.add('on');

  // Ставим рядом со строкой, но не за краем экрана.
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  let left = r.right + 12;
  if (left + w > window.innerWidth - 8) left = Math.max(8, r.left - w - 12);
  el.style.left = left + 'px';
  const top = Math.min(Math.max(8, r.top - 30),
                       window.innerHeight - el.offsetHeight - 8);
  el.style.top = Math.max(8, top) + 'px';
}}

function hidePeek() {{
  if (peekEl) peekEl.classList.remove('on');
}}

// Прокрутка списка уводит строку из-под окна -- закрываем, иначе оно
// остаётся висеть над другой находкой и вводит в заблуждение.
window.addEventListener('scroll', hidePeek, {{ passive: true }});
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') hidePeek();
}});

// --- отбор находок по статусу --------------------------------------------
//
// Отбор живёт в браузере, а не в запросе: находок десятки, а не тысячи,
// и переключение получается мгновенным, без похода на сервер. Данные при
// этом приходят ПОЛНЫЕ -- прятать отклонённое на сервере значило бы лишить
// человека возможности пересмотреть отбракованное, а в поиске к
// отвергнутому возвращаются.
//
// По умолчанию показываем всё, кроме отклонённого: больше половины
// статусов -- именно "отклонено", и с ними список превращается в перечень
// того, что находкой НЕ оказалось.
const FIND_STATUS = {{
  'confirmed_person': '✅ точно человек',
  'likely_person': '👤 предположительно человек',
  'confirmed_object': '🎒 предмет',
  'likely_object': '🎒 предположительно предмет',
  'anomaly': '❓ аномалия',
  'rejected': '❌ отклонено',
  '': '— без статуса —',
}};
let findFilter = 'active';

function passesFilter(f) {{
  if (findFilter === 'all') return true;
  if (findFilter === 'active') return f.priority !== 'rejected';
  return (f.priority || '') === findFilter;
}}

// Выгрузка координатору. Он работает не в нашей платформе, а в своей
// карте или навигаторе -- пока координаты живут только здесь, они
// бесполезны ровно там, где нужны.
function exportButtons() {{
  // считать здесь "есть ли координата" самостоятельно нельзя: счётчик
  // и содержимое файла разошлись бы молча. Признак ставит сервер.
  const withGeo = findings.filter(f => f.exportable).length;
  if (!withGeo) return '';
  return `<div class="exp">
    <span class="exp-lbl">Выгрузить координаты (${{withGeo}}):</span>
    <a class="exp-btn" href="/api/operations/${{OP}}/findings.kml">KML для карты</a>
    <a class="exp-btn" href="/api/operations/${{OP}}/findings.gpx">GPX для навигатора</a>
    <span class="exp-note">точки объектов и позиции дрона разделены</span>
  </div>`;
}}

function findFilters() {{
  const counts = {{}};
  findings.forEach(f => {{
    const k = f.priority || '';
    counts[k] = (counts[k] || 0) + 1;
  }});
  const active = findings.filter(f => f.priority !== 'rejected').length;

  // Кнопки только для статусов, которые реально встречаются: пустые
  // рубрики создают ощущение, что чего-то не хватает.
  const chips = [
    ['active', 'Актуальные', active],
    ['all', 'Все', findings.length],
  ].concat(Object.keys(FIND_STATUS)
    .filter(k => counts[k])
    .map(k => [k, FIND_STATUS[k], counts[k]]));

  return `<div class="chips">` + chips.map(([key, name, n]) =>
    `<button class="chip ${{findFilter === key ? 'on' : ''}}"
       onclick="setFindFilter('${{key}}')">${{name}}<span class="n">${{n}}</span></button>`
  ).join('') + `</div>`;
}}

function setFindFilter(key) {{
  findFilter = key;
  render();
}}

async function loadFindings() {{
  const r = await fetch(`/api/operations/${{OP}}/findings`);
  findings = (await r.json()).findings || [];
  render();
}}


// --- ПОИСК ПО ВСЕЙ ОПЕРАЦИИ ------------------------------------------------
//
// Поиск по ТЕКУЩЕЙ папке бесполезен: человек ищет файл ровно тогда, когда
// не помнит, в какой он папке. Поэтому список материалов операции берётся
// целиком -- на сотнях файлов это несколько килобайт -- и дальше
// фильтруется в браузере: мгновенно и без похода на сервер на каждую букву.

let ALL = null;          // весь список, грузится один раз
let query = '';

async function ensureAll() {{
  if (ALL) return ALL;
  const r = await fetch(`/api/operations/${{OP}}/materials`);
  ALL = r.ok ? (await r.json()).items : [];
  return ALL;
}}

function matches(it, parts) {{
  // Ищем и по имени, и по пути: "helicopter saykal" должно находить, даже
  // если этих слов нет в самом имени файла.
  const hay = ((it.folder || '') + '/' + it.name).toLowerCase();
  return parts.every(p => hay.includes(p));
}}

function mark(text, parts) {{
  // Подсветка совпавших кусков. Имена вроде DJI_20260814143659_0015_Z.MP4
  // отличаются серединой, и без подсветки глазами их не различить.
  //
  // Обходимся БЕЗ регулярного выражения: запрос печатает человек, в нём
  // спокойно окажется точка или скобка, и собранная из него регулярка
  // либо сломается, либо начнёт совпадать не с тем. Обычный поиск
  // подстроки этой беды не знает.
  const src = String(text == null ? '' : text);
  const low = src.toLowerCase();
  const hits = [];
  parts.forEach(p => {{
    if (!p) return;
    let from = 0;
    for (;;) {{
      const i = low.indexOf(p, from);
      if (i < 0) break;
      hits.push([i, i + p.length]);
      from = i + p.length;
    }}
  }});
  if (!hits.length) return esc(src);
  // Совпадения разных слов могут пересекаться -- склеиваем, иначе теги
  // подсветки вложатся друг в друга и разметка поедет.
  hits.sort((a, b) => a[0] - b[0]);
  const merged = [hits[0]];
  for (let k = 1; k < hits.length; k++) {{
    const last = merged[merged.length - 1];
    if (hits[k][0] <= last[1]) last[1] = Math.max(last[1], hits[k][1]);
    else merged.push(hits[k]);
  }}
  let out = '', pos = 0;
  merged.forEach(([a, b]) => {{
    out += esc(src.slice(pos, a))
        + '<span class="hit-mark">' + esc(src.slice(a, b)) + '</span>';
    pos = b;
  }});
  return out + esc(src.slice(pos));
}}

function hitRow(it, parts) {{
  const href = it.kind === 'video' ? `/report/${{it.report_id}}/player/`
                                   : `/report/${{it.report_id}}/viewer/`;
  const where = it.folder
    ? `<span class="hit-where">${{mark(it.folder, parts)}}</span>` : '';
  const cloud = it.in_cloud ? ' <span class="meta">☁</span>' : '';
  return `<a class="row" href="${{href}}">
    <span class="ic">${{it.kind === 'video' ? '🎬' : '🖼'}}</span>
    <span class="nm">${{mark(it.name, parts)}}${{cloud}}${{where}}</span>
    <span class="meta">${{esc(it.status)}}</span></a>`;
}}

function renderSearch() {{
  const b = document.getElementById('body');
  const parts = query.split(/\\s+/).filter(Boolean);
  const hits = (ALL || []).filter(it => matches(it, parts));
  document.getElementById('qhint').textContent =
    hits.length ? `найдено: ${{hits.length}}` : '';
  if (!hits.length) {{
    b.innerHTML = `<div class="noqres">Ничего не найдено по запросу
      «${{esc(query)}}». Ищите по части имени файла или по названию папки.</div>`;
    return;
  }}
  // Потолок на выдачу: показать 200 строк разом значит подвесить страницу
  // на слабой машине, а искать среди двухсот результатов всё равно нельзя.
  const shown = hits.slice(0, 60);
  let html = shown.map(it => hitRow(it, parts)).join('');
  if (hits.length > shown.length) {{
    html += `<div class="note">…и ещё ${{hits.length - shown.length}}.
      Уточните запрос.</div>`;
  }}
  b.innerHTML = html;
}}

async function onQuery(v) {{
  query = (v || '').trim().toLowerCase();
  document.getElementById('qhint').textContent = '';
  if (!query) {{ render(); return; }}
  await ensureAll();
  renderSearch();
}}

document.getElementById('q').addEventListener('input', e => onQuery(e.target.value));
document.getElementById('q').addEventListener('keydown', e => {{
  if (e.key === 'Escape') {{ e.target.value = ''; onQuery(''); e.target.blur(); }}
}});

// «/» ставит курсор в поиск. Проверяем ТИП поля, а не тег: проверка
// «это input?» ломала бы горячую клавишу, стоило чекбоксу получить фокус --
// на этих граблях в плеере уже стояли.
document.addEventListener('keydown', e => {{
  if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
  const el = document.activeElement;
  const typing = el && (el.isContentEditable
    || el.tagName === 'TEXTAREA'
    || (el.tagName === 'INPUT'
        && !['checkbox','radio','button','submit'].includes(el.type)));
  if (typing) return;
  e.preventDefault();
  document.getElementById('q').focus();
}});

loadBrowse();
</script></body></html>"""


@app.route("/operation/<int:op_id>/")
def operation_card_page(op_id):
    conn = get_db()
    if sar_common.get_operation(conn, op_id) is None:
        return "Операции нет", 404
    return OPERATION_CARD_HTML.format(viewer_name=session.get("viewer_name", ""))


def can_manage_operations():
    """Кто заводит операции.

    Модератор и администратор -- те же права, что и на модерацию находок:
    операция это организация работы команды, а не личное действие. Заводить
    поиски всем подряд не нужно, иначе список зарастёт черновиками.
    """
    return is_moderator()


@app.route("/api/operations", methods=["GET", "POST"])
def api_operations():
    conn = get_db()
    if request.method == "GET":
        out = []
        for o in sar_common.list_operations(conn):
            item = dict(o)
            item.update(sar_common.operation_summary(conn, o["id"]))
            out.append(item)
        return jsonify({"operations": out,
                         "unsorted": len(sar_common.unsorted_materials(conn)),
                         "can_manage": can_manage_operations()})

    if not can_manage_operations():
        return jsonify({"ok": False, "error": "нужны права модератора"}), 403

    data = request.get_json(silent=True) or request.form
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "не задано название"}), 400
    if len(title) > 120:
        return jsonify({"ok": False, "error": "название слишком длинное"}), 400

    watch_dir = os.path.abspath(SERVER_CFG["watch_dir"])
    op_id, folder = sar_common.create_operation_with_folder(
        conn, watch_dir, title,
        area=(data.get("area") or "").strip() or None,
        client=(data.get("client") or "").strip() or None,
        coordinator=session.get("viewer_name"))
    return jsonify({"ok": True, "id": op_id, "title": title, "folder": folder})


@app.route("/api/operations/<int:op_id>/materials")
def api_operation_materials(op_id):
    """Плоский список материалов операции -- для поиска.

    Отдаётся целиком и один раз: на сотнях материалов это несколько
    килобайт, а фильтрация в браузере отвечает мгновенно и не гоняет
    запрос на каждую нажатую букву.
    """
    conn = get_db()
    items = sar_common.operation_materials_flat(conn, op_id)
    if items is None:
        return jsonify({"error": "операции нет"}), 404
    return jsonify({"items": items})


@app.route("/api/operations/<int:op_id>/browse")
def api_operation_browse(op_id):
    """Одна папка операции: подпапки и материалы. Ходим как в проводнике."""
    conn = get_db()
    watch_dir = os.path.abspath(SERVER_CFG["watch_dir"])
    subpath = (request.args.get("path") or "").strip("/")
    # путь приходит из адресной строки -- не пускаем его выше папки операции
    if ".." in subpath.split("/"):
        return jsonify({"error": "недопустимый путь"}), 400

    data = sar_common.browse_operation(conn, watch_dir, op_id, subpath)
    if data is None:
        return jsonify({"error": "операции нет"}), 404

    def short(r):
        rel = (r["rel_path"] or "").replace("\\", "/")
        item = {"report_id": r["report_id"], "name": rel.split("/")[-1],
                "rel_path": rel, "kind": r["kind"], "status": r["status"],
                "duration_sec": r.get("duration_sec"),
                "manual_count": conn.execute(
                    "SELECT COUNT(*) c FROM manual_observations WHERE report_id=?",
                    (r["report_id"],)).fetchone()["c"]}
        if r["kind"] == "video":
            item.update(get_report_stats(conn, r["report_id"], r.get("duration_sec")))
        return item

    return jsonify({
        "operation": {"id": data["operation"]["id"],
                       "title": data["operation"]["title"],
                       "area": data["operation"]["area"],
                       "folder": data["operation"]["folder"]},
        "path": data["path"],
        "folders": data["folders"],
        "files": [short(r) for r in data["files"]],
        "outside": [short(r) for r in data["outside"]],
        "summary": sar_common.operation_summary(conn, op_id),
    })


@app.route("/api/finding/<int:observation_id>/preview")
def api_finding_preview(observation_id):
    """Кадр ручной пометки. Сервер только отдаёт готовый файл -- вырезает
    его воркер (см. ensure_finding_previews в sar_worker.py), как и все
    прочие картинки в проекте.

    ?full=1 -- крупный кадр БЕЗ рамки, для окна предпросмотра и страницы
    кадра, где картинку увеличивают. Если крупного ещё нет (воркер не
    дошёл, или находка старая), молча отдаём мелкий: пустое окно
    предпросмотра хуже, чем окно с картинкой похуже.
    """
    want_full = request.args.get("full") in ("1", "true", "yes")
    if want_full:
        path = sar_common.finding_preview_path(DATA_DIR, observation_id, full=True)
        if not os.path.exists(path):
            path = sar_common.finding_preview_path(DATA_DIR, observation_id)
    else:
        path = sar_common.finding_preview_path(DATA_DIR, observation_id)
    if not os.path.exists(path):
        # воркер ещё не дошёл до этой пометки -- не ошибка
        return "", 404
    return send_file(path, mimetype="image/jpeg")


def _finding_bbox(f):
    """Рамка находки как список из четырёх чисел 0..1, либо None.

    В базе она лежит строкой JSON. Битую строку молча считаем отсутствующей:
    находка без рамки полезна, а падение списка из-за одной кривой записи --
    нет (тот же урок, что и с превью).
    """
    raw = f.get("bbox")
    if not raw:
        return None
    try:
        box = json.loads(raw) if isinstance(raw, str) else raw
        vals = [float(v) for v in list(box)[:4]]
        return vals if len(vals) == 4 else None
    except (ValueError, TypeError):
        return None


def _finding_obs_id(f):
    """Id ручной пометки, стоящей за находкой, либо None.

    Находка -- это либо сама пометка, либо триаж, поставленный НА пометку;
    в обоих случаях кадр, рамка и страница кадра у них общие. У триажа
    сцены модели своей пометки нет, и обратиться не к чему.

    Логика повторялась в подборе превью -- вынесена сюда, чтобы адрес
    картинки и адрес страницы кадра не разъехались между собой.
    """
    if f.get("kind") == "manual":
        obs_id = f.get("id")
    elif f.get("target_kind") == "manual":
        obs_id = f.get("ref_key")
    else:
        return None
    try:
        return int(obs_id)
    except (TypeError, ValueError):
        return None


def _finding_preview_url(conn, f):
    """Адрес картинки находки, если она есть.

    Два источника, и оба уже существуют на диске:
      * ручная пометка -- кадр, вырезанный воркером;
      * триаж сцены модели -- готовый кроп из отчёта, его и показываем,
        генерировать ничего не нужно.

    Никогда не поднимает исключение. Картинка -- украшение строки, а список
    находок -- рабочий инструмент: одна битая запись не должна уносить весь
    список, как это уже было с отчётом без out_dir, который клал главную
    страницу целиком.
    """
    try:
        return _finding_preview_url_inner(conn, f)
    except Exception:                                   # noqa: BLE001
        return None


def _finding_preview_url_inner(conn, f):
    if f["kind"] == "manual":
        obs_id = f.get("id")
    elif f.get("target_kind") == "manual":
        # триаж, поставленный на ручную пометку -- картинка у них общая
        obs_id = f.get("ref_key")
    else:
        obs_id = None

    if obs_id is not None:
        try:
            obs_id = int(obs_id)
        except (TypeError, ValueError):
            return None
        if os.path.exists(sar_common.finding_preview_path(DATA_DIR, obs_id)):
            return f"/api/finding/{obs_id}/preview"
        return None

    # Триаж сцены модели: ищем кроп по тому же ref_key, что стоит в базе.
    ref = f.get("ref_key")
    if not ref:
        return None
    report = get_report_row(f["report_id"])
    if report is None:
        return None
    for scene in _get_ai_scenes_for_report(report):
        if scene.get("ref_key") != ref:
            continue
        crop = scene.get("image_path") or scene.get("full_image_path")
        if not crop:
            return None
        return f"/report/{f['report_id']}/{str(crop).replace(chr(92), '/')}"
    return None


def _finding_label(f):
    """Подпись находки человеческими словами.

    У ручной пометки это её собственный текст. У сцены модели своей подписи
    нет -- показываем статус, который ей поставил человек. Служебный ключ
    вроде "confirmed_person" человеку не показываем никогда, для того и
    существует PRIORITY_LABELS.
    """
    if f.get("label"):
        return f["label"]
    priority = f.get("priority")
    if not priority:
        return ""
    return sar_common.PRIORITY_LABELS.get(priority, priority)


def _finding_status(f):
    """Статус триажа отдельной подписью.

    У ручной пометки подпись своя ("резко чёрное"), а статус ("точно
    человек") -- это другое измерение: что именно человек написал и к
    какому выводу пришли. Склеивать их в одну строку значит терять и то,
    и другое; поэтому статус идёт отдельной меткой.
    """
    priority = f.get("priority")
    if not priority or not f.get("label"):
        # у сцены модели статус уже стал подписью -- дублировать не надо
        return ""
    return sar_common.PRIORITY_LABELS.get(priority, priority)


@app.route("/api/operations/<int:op_id>/findings")
def api_operation_findings(op_id):
    conn = get_db()
    if sar_common.get_operation(conn, op_id) is None:
        return jsonify({"error": "операции нет"}), 404
    out = []
    for f in sar_common.operation_findings(conn, op_id):
        rel = (f.get("rel_path") or "").replace("\\", "/")
        out.append({
            "kind": f["kind"],
            "report_id": f["report_id"],
            "file": rel.split("/")[-1],
            "label": _finding_label(f),
            "status": _finding_status(f),
            # служебный ключ статуса -- по нему отбирает интерфейс; человеку
            # он не показывается никогда, для этого есть label/status
            "priority": f.get("priority") or "",
            # у ручной пометки автор в viewer_name, у триажа -- в set_by
            "viewer": (f.get("viewer_name") or f.get("author")
                        or f.get("set_by") or ""),
            # у сцены модели своего таймкода нет: в ключе сцены его не
            # хранят. Оставляем пустым, а не нулём -- ноль выглядел бы как
            # "в самом начале видео", то есть врал бы.
            "seconds": f.get("timestamp_sec"),
            "lat": f.get("lat"), "lon": f.get("lon"),
            # попадёт ли эта находка в KML/GPX -- решает сервер тем же
            # правилом, что и сама выгрузка. Интерфейс только считает.
            "exportable": _finding_export_point(f) is not None,
            # время постановки: created_at у пометки, set_at у триажа
            "created_at": (f.get("created_at") or f.get("set_at")
                            or f.get("updated_at")),
            "preview": _finding_preview_url(conn, f),
            # id пометки: по нему открывается страница кадра и строится
            # постоянная ссылка на находку
            "obs_id": _finding_obs_id(f),
            # рамка нормализована 0..1 -- рисуется поверх кадра в SVG,
            # поэтому остаётся чёткой при увеличении и выключается
            "bbox": _finding_bbox(f),
        })
    return jsonify({"findings": out})


# ---------------------------------------------------------------------------
# Кэш списка материалов
#
# /api/tree -- самый дорогой запрос платформы, и его дёргает КАЖДАЯ открытая
# вкладка раз в 5 секунд, включая забытые. Замерено нагрузочным тестом: при
# восьми одновременных он отвечает 1.5 секунды против десятков миллисекунд у
# всего остального, а суммарная пропускная способность платформы падает со
# 105 до 59 запросов в секунду. Это не насыщение, а очередь: запросы дерутся
# за один и тот же обход диска и три сотни обращений к базе.
#
# Причём в ночь пика операции (8 человек одновременно) люди в это уже
# упирались -- список тормозил ровно тогда, когда работы было больше всего.
#
# Кэш превращает нагрузку от N вкладок в нагрузку одной, сколько бы вкладок
# ни было открыто.
#
# ПОЧЕМУ ОДИН КЭШ НА ВСЕХ. В ответе нет ничего, что зависит от конкретного
# человека: статусы файлов, счётчики пометок и полосы покрытия одинаковы для
# всех зрителей. Если когда-нибудь в ответ добавится поле, своё у каждого
# (например "смотрел ли ИМЕННО Я это видео"), кэш придётся либо разделять по
# зрителю, либо убирать -- иначе один человек увидит данные другого. На это
# есть тест.
#
# Инвалидации по записи намеренно НЕТ. Живые правки идут постоянно: плеер
# шлёт просмотренный отрезок раз в 5 секунд с каждого играющего видео, и
# сброс кэша на каждую такую запись обнулил бы весь смысл. Вместо этого срок
# жизни короткий: список и так опрашивается раз в 5 секунд, поэтому задержка
# в пару секунд незаметна, а выигрыш кратный.
# ---------------------------------------------------------------------------

TREE_CACHE_TTL_SEC = 3.0
_tree_cache = {}
_tree_cache_lock = threading.Lock()


def _tree_cache_key(op_param):
    """Ключ включает НАБОР ДАННЫХ, а не только фильтр по операции.

    Ответ описывает конкретную пару «папка + база». В бою они не меняются,
    и соблазн ключевать только по операции велик -- но тогда кэш молча
    отдаёт ответ, посчитанный для другого набора данных, как только эта
    пара всё-таки сменится. Ровно это и вскрылось: восемь тестов, каждый со
    своей временной папкой и базой, начали получать чужие списки файлов.
    Кэш не должен зависеть от того, что окружение "обычно не меняется".
    """
    return (os.path.abspath(SERVER_CFG.get("watch_dir") or ""),
            DB_PATH, op_param)


# Ключи, по которым пересчёт идёт прямо сейчас.
#
# Без этого кэш чинил медиану, но портил худший случай -- и это было
# измерено. Пока значение свежее, все запросы отвечают за миллисекунды; в
# момент протухания ВОСЕМЬ запросов промахиваются одновременно и каждый
# честно считает всё заново. Восемь одновременных обходов диска дерутся за
# те же ядра, и p95 списка вырос с 1517 до 2079 мс -- хуже, чем было без
# кэша вообще.
#
# Поэтому пересчитывает только ОДИН запрос, а остальные в этот момент
# получают предыдущий ответ -- сразу, без ожидания. Список опрашивается
# раз в 5 секунд, и ответ, устаревший на пару секунд, здесь ничем не хуже
# свежего; а вот ожидание в две секунды человек чувствует.
# ключ -> событие, которым считающий запрос сообщит остальным, что готово
_tree_refreshing = {}

# Сколько ждать чужой пересчёт на холодном старте. Больше самого долгого
# наблюдавшегося обхода с запасом; если не дождались -- считаем сами, чтобы
# запрос не завис вовсе.
TREE_COLD_WAIT_SEC = 20.0


def _tree_cache_take(key):
    """Что делать с запросом: (готовый ответ, считать ли самому, чего ждать).

    Решение принимается под одним замком -- иначе два запроса одновременно
    решат, что считать некому, и мы вернёмся к одновременному пересчёту.
    """
    now = time.time()
    with _tree_cache_lock:
        hit = _tree_cache.get(key)
        if hit and now - hit[0] < TREE_CACHE_TTL_SEC:
            return hit[1], False, None
        waiter = _tree_refreshing.get(key)
        if waiter is not None:
            # Пересчёт уже идёт у соседнего запроса.
            if hit is not None:
                return hit[1], False, None     # отдаём предыдущий, не ждём
            # Прежнего ответа нет вовсе -- холодный старт. Раньше здесь
            # каждый считал сам, и это было ошибкой: при восьми
            # одновременных зрителях восемь обходов диска дрались за одни
            # ядра, и первые запросы после перезапуска отвечали 12, 10 и
            # 8.6 секунды (замерено). Ждать чужой пересчёт -- полторы.
            return None, False, waiter
        _tree_refreshing[key] = threading.Event()
        return None, True, None


def _tree_refresh_done(key):
    with _tree_cache_lock:
        event = _tree_refreshing.pop(key, None)
    if event is not None:
        event.set()


def _tree_wait_for_refresh(key, event):
    """Дождаться чужого пересчёта и забрать результат.

    Возвращает готовый ответ либо None -- тогда вызвавший считает сам:
    висеть бесконечно из-за чужого сбоя запрос не должен.
    """
    event.wait(TREE_COLD_WAIT_SEC)
    with _tree_cache_lock:
        hit = _tree_cache.get(key)
    return hit[1] if hit is not None else None


class _OperationMissing(Exception):
    """Запрошена операция, которой нет. Отдельным исключением, а не
    возвратом ответа: сборка списка вынесена в помощника, и Flask-ответы
    внутри него мешали бы кэшировать результат."""


def _tree_cache_put(key, payload):
    with _tree_cache_lock:
        _tree_cache[key] = (time.time(), payload)
        # ключей ровно столько, сколько операций плюс два особых -- расти
        # им неоткуда, но пусть словарь не растёт молча при ошибке в ключе
        if len(_tree_cache) > 64:
            oldest = min(_tree_cache, key=lambda k: _tree_cache[k][0])
            del _tree_cache[oldest]


def _tree_cache_clear():
    """Сброс. Нужен тестам и на случай ручной правки данных на месте."""
    with _tree_cache_lock:
        _tree_cache.clear()
        _tree_refreshing.clear()


@app.route("/api/tree")
def api_tree():
    # Фильтр по операции. Отбор идёт по СВЯЗЯМ в базе, а не по префиксу пути:
    # материал может быть добавлен в операцию, физически лежа где угодно, и
    # может принадлежать двум операциям сразу. Путь на диске -- удобство
    # раскладки, а не источник истины о принадлежности.
    op_param = (request.args.get("op") or "").strip()

    # Кэш проверяется ДО обхода диска -- иначе смысла в нём нет: дорого
    # именно сканирование папок и три сотни запросов к базе ниже.
    cache_key = _tree_cache_key(op_param)
    cached, must_compute, waiter = _tree_cache_take(cache_key)
    if waiter is not None:
        cached = _tree_wait_for_refresh(cache_key, waiter)
        if cached is not None:
            return jsonify(cached)
        # Тот, кого ждали, не справился -- считаем сами, но уже без
        # признака "идёт пересчёт": его снял тот запрос.
        must_compute = True
    if not must_compute:
        return jsonify(cached)

    try:
        return jsonify(_build_tree_payload(cache_key, op_param))
    except _OperationMissing:
        # В кэш не кладём: несуществующая операция -- это ответ про запрос,
        # а не про данные, и держать его 3 секунды незачем.
        return jsonify({"items": [], "error": "операции нет"}), 404
    finally:
        # Снимать признак обязательно даже при исключении: иначе после
        # одной ошибки ключ навсегда останется "в пересчёте", и список
        # застынет на последнем удачном ответе.
        _tree_refresh_done(cache_key)


def _build_tree_payload(cache_key, op_param):
    conn = get_db()
    watch_dir = os.path.abspath(SERVER_CFG["watch_dir"])
    # Папки операций обходятся рекурсивно (заказчик раскладывает материал
    # так же, как у себя в облаке), корень -- плоско, там «Не разобрано».
    found = sar_common.scan_all_materials(watch_dir)
    # Какой материал показывать -- настройка администратора, см.
    # material_sources в SETTINGS_SCHEMA. На сам материал не влияет:
    # ничего не удаляется и не отвязывается от операции.
    sources = sar_common.get_settings(conn).get("material_sources", "all")
    if sources == "cloud":
        found = []

    only = None
    op_title = None
    if op_param == "unsorted":
        only = {r["report_id"] for r in sar_common.unsorted_materials(conn)}
        op_title = "Не разобрано"
    elif op_param.isdigit():
        op = sar_common.get_operation(conn, int(op_param))
        if op is None:
            raise _OperationMissing()
        only = {r["report_id"]
                for r in sar_common.materials_of_operation(conn, int(op_param))}
        op_title = op["title"]

    items = []
    for name, abs_path, kind in found:
        # ищем ПО ИМЕНИ ФАЙЛА (rel_path), а не пересчитывая report_id из
        # текущего ctime -- см. подробное объяснение в watcher_loop()
        # (sar_worker.py): ctime у больших/ещё дописывающихся файлов может
        # разойтись между сканами, и тогда пересчитанный report_id не найдёт
        # запись, которую воркер реально обрабатывает под старым report_id --
        # именно так "новый файл обрабатывается, но прогресс не виден и
        # нельзя перейти на страницу обработки" и проявлялось в реальности.
        # ORDER BY updated_at DESC -- на случай уже накопленных дублей с До
        # этого фикса, берём самую свежую запись, а не первую попавшуюся.
        file_ctime = sar_common.get_file_ctime(abs_path)
        row = conn.execute(
            "SELECT * FROM reports WHERE rel_path=? ORDER BY updated_at DESC LIMIT 1", (name,)).fetchone()
        if row is None:
            report_id = sar_common.make_report_id(name, abs_path)
            item = {"report_id": report_id, "name": name, "kind": kind,
                     "status": "queued", "progress_pct": 0, "viewer_count": 0, "percent": None,
                     "buckets": None, "file_ctime": file_ctime, "manual_count": 0, "ai_count": None}
        else:
            r = dict(row)
            report_id = r["report_id"]
            item = {"report_id": report_id, "name": name, "kind": kind,
                     "status": r["status"], "progress_pct": r["progress_pct"] or 0,
                     "viewer_count": 0, "percent": None, "buckets": None,
                     "file_ctime": r["file_ctime"] or file_ctime,
                     "manual_count": conn.execute(
                         "SELECT COUNT(*) c FROM manual_observations WHERE report_id=?",
                         (report_id,)).fetchone()["c"],
                     # AI-счётчик только для готовых отчётов -- группировка
                     # detections.json на каждый файл при каждом опросе
                     # /api/tree была бы слишком дорогой для ещё обрабатывающихся
                     # видео; для "processing"/"queued" фронт покажет "—"
                     "ai_count": len(_get_ai_scenes_for_report(r)) if r["status"] == "done" else None}
            # Покрытие ручного просмотра считается для видео в ЛЮБОМ статусе,
            # не только 'done'. Ручной плеер доступен с момента появления
            # файла в очереди (см. player_page), то есть люди смотрят видео
            # ЗАДОЛГО до того, как до него дойдёт детектор -- на CPU очередь
            # может быть на сутки вперёд. Показывать при этом пустое место
            # вместо полосы просмотра означало бы, что команда не видит,
            # какие куски уже отсмотрены, именно тогда, когда это нужнее
            # всего -- пока автоматических подсказок ещё нет вообще.
            if kind == "video":
                stats = get_report_stats(conn, report_id, r["duration_sec"])
                item.update(stats)
            elif r["status"] == "done" and kind == "photo":
                stats = get_report_stats(conn, report_id, None)
                item["viewer_count"] = stats["viewer_count"]
        if only is not None and item["report_id"] not in only:
            continue
        items.append(item)

    # МАТЕРИАЛ ИЗ ОБЛАКА. Обход выше ходит по локальной папке и облачные
    # записи не находит: их нет на диске. Без этого блока подключённый диск
    # выглядел бы неработающим -- файлы зарегистрированы, привязаны к
    # операции, а в списке их нет, и понять почему невозможно.
    seen_ids = {i["report_id"] for i in items}
    cloud_rows = [] if sources == "local" else conn.execute(
        "SELECT * FROM reports WHERE cloud_file_id IS NOT NULL "
        "ORDER BY rel_path").fetchall()
    for r in cloud_rows:
        r = dict(r)
        report_id = r["report_id"]
        if report_id in seen_ids:
            continue          # тот же файл уже нашёлся локально
        if only is not None and report_id not in only:
            continue
        name = r["rel_path"]
        kind = r["kind"] or "video"
        item = {"report_id": report_id, "name": name, "kind": kind,
                "status": r["status"], "progress_pct": r["progress_pct"] or 0,
                "viewer_count": 0, "percent": None, "buckets": None,
                # Даты создания у облачного файла нет: локально его не было.
                # Ставим 0, а не выдумываем -- сортировка по дате просто
                # положит такие файлы в конец, и это честно.
                "file_ctime": r["file_ctime"] or 0,
                "in_cloud": True,
                "size_bytes": r["cloud_size"] or 0,
                "manual_count": conn.execute(
                    "SELECT COUNT(*) c FROM manual_observations WHERE report_id=?",
                    (report_id,)).fetchone()["c"],
                "ai_count": len(_get_ai_scenes_for_report(r))
                if r["status"] == "done" else None}
        if kind == "video":
            item.update(get_report_stats(conn, report_id, r["duration_sec"]))
        items.append(item)

    payload = {"items": items, "operation": op_title, "op": op_param or None}
    _tree_cache_put(cache_key, payload)
    return payload


@app.route("/api/thumbnail/<path:filename>")
def api_thumbnail(filename):
    """Отдаёт уже сгенерированное превью (первый кадр видео) -- сервер тут
    ничего не обрабатывает сам, только читает готовый файл с диска (см.
    watcher_loop/_generate_thumbnail в sar_worker.py) -- та же граница
    ответственности, что и для report.html/detections.json/crops.

    filename проверяется по РЕАЛЬНОМУ списку файлов из scan_watch_dir, а не
    просто join'ится с диском -- защита от path traversal (тот же принцип,
    что и у report_asset() ниже)."""
    watch_dir = os.path.abspath(SERVER_CFG["watch_dir"])
    found = {name: kind for name, _abs_path, kind in sar_common.scan_all_materials(watch_dir)}
    # превью есть и у видео (первый кадр), и у фото (уменьшенная копия) --
    # см. _generate_thumbnail в sar_worker.py
    kind = found.get(filename)

    if kind not in ("video", "photo"):
        # ОБЛАЧНОГО МАТЕРИАЛА НА ДИСКЕ НЕТ ПО ОПРЕДЕЛЕНИЮ, и проверка по
        # обходу папки его не пропускала -- 404 на каждое облачное превью,
        # хотя JPEG лежал рядом готовый. Тот же класс ошибки, что был с
        # отдачей видео: «настоящий ли это материал» выяснялось у диска.
        #
        # Защита от подстановки пути не ослабевает: сверяем с ТОЧНЫМ
        # rel_path из базы, а не склеиваем присланное с каталогом.
        row = get_db().execute(
            "SELECT kind FROM reports WHERE rel_path = ?", (filename,)).fetchone()
        kind = row["kind"] if row else None

    if kind not in ("video", "photo"):
        return "", 404

    thumb_path = sar_common.get_thumbnail_path(DATA_DIR, filename)
    if not os.path.exists(thumb_path):
        # воркер ещё не успел сгенерировать (новый файл, следующий скан через
        # poll_interval_sec) -- не ошибка, просто пока нечего отдавать
        return "", 404
    return send_file(thumb_path, mimetype="image/jpeg")


def _sanitize_upload_filename(original_name):
    """Возвращает (safe_stem, ext) из имени, которое прислал браузер.
    os.path.basename срезает любые компоненты пути (защита от path traversal
    вида '../../evil.mp4' в имени файла) -- дальше остаётся только сам stem,
    очищенный тем же паттерном, что и в sar_common.make_report_id (кириллица
    разрешена осознанно -- проект и так работает с русскими именами файлов)."""
    original_name = os.path.basename(original_name.replace("\\", "/"))
    stem, ext = os.path.splitext(original_name)
    ext = ext.lower()
    safe_stem = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", stem)[:100] or "video"
    return safe_stem, ext


def _unique_upload_path(watch_dir, safe_stem, ext):
    """Не даём загрузке молча перетереть уже существующий файл (может быть
    уже обработанное или обрабатывающееся видео) -- подбираем свободное имя."""
    candidate = os.path.join(watch_dir, safe_stem + ext)
    if not os.path.exists(candidate):
        return candidate
    for i in range(1, 1000):
        candidate = os.path.join(watch_dir, f"{safe_stem}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError("не удалось подобрать свободное имя файла после 1000 попыток")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Загрузка видео через браузер вместо ручного копирования в watch_dir.

    ВРЕМЕННЫЙ барьер доступа: пока в системе нет ролей вообще (общий пароль +
    свободное имя на вход), доступ разрешён только тому, кто вошёл под именем
    UPLOADER_NAME. Это НЕ настоящая авторизация -- любой, кто знает общий
    пароль, может ввести это же имя и получить доступ к загрузке. Осознанный
    временный компромисс до нормальной ролевой модели, а не забытая дыра."""
    if not can_upload():
        return jsonify({"ok": False, "error": "загрузка доступна администратору "
                        f"или пользователю '{UPLOADER_NAME}'"}), 403

    file = request.files.get("video")
    if file is None or not file.filename:
        return jsonify({"ok": False, "error": "файл не передан"}), 400

    safe_stem, ext = _sanitize_upload_filename(file.filename)
    if ext not in sar_common.VIDEO_EXTS:
        return jsonify({"ok": False, "error": f"неподдерживаемое расширение '{ext}' "
                                                f"(ожидается видео: {sorted(sar_common.VIDEO_EXTS)})"}), 400

    watch_dir = os.path.abspath(SERVER_CFG["watch_dir"])
    final_path = _unique_upload_path(watch_dir, safe_stem, ext)

    # пишем во временное имя с расширением, которого нет в MEDIA_EXTS --
    # watcher_loop в sar_worker.py (отдельный процесс!) сканирует watch_dir
    # каждые poll_interval_sec и по расширению отбирает файлы; без этого он
    # мог бы подхватить ЕЩЁ дозаписывающийся файл и поставить в очередь
    # битое/недокачанное видео. os.replace -- атомарная замена на уровне ОС,
    # тот же паттерн уже используется в flush_partial_detections()
    # (sar_video_review.py) для detections.json.
    tmp_path = final_path + ".uploading"
    try:
        file.save(tmp_path)
        os.replace(tmp_path, final_path)
    except OSError as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({"ok": False, "error": f"не удалось сохранить файл: {e}"}), 500

    return jsonify({"ok": True, "filename": os.path.basename(final_path)})


@app.route("/api/upload_telemetry", methods=["POST"])
def api_upload_telemetry():
    """Загрузка SRT-телеметрии через браузер -- тот же временный барьер
    доступа (UPLOADER_NAME), что и у /api/upload, см. комментарий там.

    В отличие от видео, поддерживает МНОЖЕСТВЕННУЮ загрузку за один раз --
    реальная телеметрия обычно приходит целой пачкой файлов с одного дня
    полётов (см. sar_common.build_telemetry_index), а не по одному файлу."""
    if not can_upload():
        return jsonify({"ok": False, "error": "загрузка доступна администратору "
                        f"или пользователю '{UPLOADER_NAME}'"}), 403

    files = request.files.getlist("telemetry")
    if not files or all(not f.filename for f in files):
        return jsonify({"ok": False, "error": "файлы не переданы"}), 400

    detection_cfg = load_detection_config(
        os.path.join(SCRIPT_DIR, "sar_config.json") if os.path.exists(
            os.path.join(SCRIPT_DIR, "sar_config.json")) else None)
    telemetry_dir_name = detection_cfg.get("telemetry_dir", sar_common.DEFAULT_TELEMETRY_DIR_NAME)
    telemetry_dir = sar_common.resolve_telemetry_dir(
        os.path.abspath(SERVER_CFG["watch_dir"]), telemetry_dir_name)

    saved, errors = [], []
    for file in files:
        if not file.filename:
            continue
        safe_stem, ext = _sanitize_upload_filename(file.filename)
        if ext != ".srt":
            errors.append(f"{file.filename}: неподдерживаемое расширение '{ext}' (ожидается .srt)")
            continue
        final_path = _unique_upload_path(telemetry_dir, safe_stem, ext)
        tmp_path = final_path + ".uploading"
        try:
            file.save(tmp_path)
            os.replace(tmp_path, final_path)
            saved.append(os.path.basename(final_path))
        except OSError as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            errors.append(f"{file.filename}: не удалось сохранить ({e})")

    # индекс telemetry/ строится один раз при старте сервера (см. main()) --
    # без перестройки здесь загруженные файлы не заработали бы до рестарта.
    # Уже обработанные отчёты этим не подтягиваются автоматически -- для
    # них по-прежнему нужен отдельный прогон backfill_telemetry.py.
    global _TELEMETRY_INDEX
    _TELEMETRY_INDEX = sar_common.build_telemetry_index(telemetry_dir)

    return jsonify({"ok": len(errors) == 0, "saved": saved, "errors": errors})


PROCESSING_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Обработка — {name}</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; background:#111; color:#eee; margin:0; padding:24px; }}
h1 {{ font-size:16px; }}
a {{ color:#8ecbff; }}
.progress {{ width:100%; height:18px; background:#222; border-radius:9px; overflow:hidden; margin:14px 0; }}
.progress-bar {{ height:100%; background:#3355aa; transition:width .3s; text-align:center; font-size:12px;
                  line-height:18px; color:#fff; }}
#console {{ background:#000; color:#9fef9f; font-family: 'Consolas', monospace; font-size:12px;
            padding:14px; border-radius:8px; height:400px; overflow-y:auto; white-space:pre-wrap; }}
.status {{ font-size:13px; color:#999; margin-bottom:6px; }}
.errbox {{ background:#3a1414; border:1px solid #7a1f1f; padding:12px; border-radius:8px; margin-top:12px; }}
.header-row {{ display:flex; justify-content:space-between; align-items:center; }}
.online-indicator {{ display:flex; align-items:center; gap:6px; font-size:13px; color:#ccc; }}
.online-dot {{ width:6px; height:6px; border-radius:50%; background:#2f9e44; flex-shrink:0; }}
/* Крошки: путь назад в операцию материала. Раньше здесь стояла ссылка
   "к списку файлов" -- в общую кучу всех материалов, мимо операции, из
   которой человек пришёл. */
.crumbs {{ font-size:13px; color:#888; margin:0 0 10px; }}
.crumbs a {{ color:#6bb; text-decoration:none; }}
.crumbs a:hover {{ text-decoration:underline; }}
</style></head>
<body>
<div class="header-row">
  <p class="crumbs">{crumbs}</p>
  <span class="online-indicator"><span class="online-dot"></span><span id="online-count">—</span> онлайн</span>
</div>
<h1>{name}</h1>
<div class="status" id="status">статус: {status}</div>
<div class="progress"><div class="progress-bar" id="progress-bar" style="width:{progress}%">{progress}%</div></div>
<div id="console"></div>
<div id="err"></div>
<script>
const reportId = "{report_id}";
let lastLogId = 0;
const consoleEl = document.getElementById('console');
const statusEl = document.getElementById('status');
const barEl = document.getElementById('progress-bar');

async function poll() {{
  const [statusRes, logRes] = await Promise.all([
    fetch(`/api/report/${{reportId}}/status`),
    fetch(`/api/report/${{reportId}}/log?since=${{lastLogId}}`),
  ]);
  const st = await statusRes.json();
  const log = await logRes.json();

  statusEl.textContent = 'статус: ' + st.status + (st.phase ? ' (' + st.phase + ')' : '')
    + (st.error ? ' — ' + st.error : '');
  barEl.style.width = (st.progress_pct || 0) + '%';
  barEl.textContent = Math.round(st.progress_pct || 0) + '%';

  if (log.lines.length) {{
    for (const l of log.lines) {{
      consoleEl.textContent += l.line + '\\n';
    }}
    consoleEl.scrollTop = consoleEl.scrollHeight;
    lastLogId = log.last_id;
  }}

  if (st.status === 'done') {{
    window.location.reload();
    return;
  }}
  if (st.status === 'error') {{
    document.getElementById('err').innerHTML =
      '<div class="errbox">Обработка завершилась с ошибкой. Проверьте консоль выше.</div>';
  }}
  setTimeout(poll, 1200);
}}
poll();

</script>
</body></html>"""


# Корни, от которых вычисляются пути отчёта. Берутся из SERVER_CFG, а не
# из глобалей DATA_DIR/REPORTS_DIR: те заполняются только в main(), а тесты
# поднимают приложение без него и подменяют именно SERVER_CFG.
#
# Ключ кэша -- сам watch_dir. Без этого тесты с временными каталогами
# увидели бы чужие пути: ровно та же причина, по которой ключ кэша
# /api/tree обязан включать watch_dir и путь к базе.
_PATH_ROOTS_CACHE = {}


def _data_dir():
    """Папка служебных данных -- устойчиво к тому, что main() не запускался.

    Глобаль DATA_DIR заполняется только в main(), а тесты поднимают
    приложение без него. Обращение к ней напрямую роняло страницу плеера
    с NameError -- то есть правка, задуманная как «пускать в плеер, когда
    есть лёгкая копия», ломала плеер вообще.
    """
    d = globals().get("DATA_DIR")
    if d:
        return d
    return sar_common.resolve_paths(SERVER_CFG["watch_dir"],
                                     SERVER_CFG.get("data_dir"))[1]


def _path_roots():
    """(watch_dir, reports_dir) -- корни для material_path и report_dir."""
    watch = SERVER_CFG["watch_dir"]
    roots = _PATH_ROOTS_CACHE.get(watch)
    if roots is None:
        resolved = sar_common.resolve_paths(watch, SERVER_CFG.get("data_dir"))
        roots = (resolved[0], resolved[3])
        _PATH_ROOTS_CACHE[watch] = roots
    return roots


def get_report_row(report_id):
    """Строка отчёта с ВЫЧИСЛЕННЫМИ путями.

    abs_path и out_dir в базе -- абсолютные пути, записанные на той машине,
    где файл впервые увидели. Они прибивают базу к букве диска и к ОС, и
    при любом переезде (материал в облако, платформа на VPS) превращаются
    в ссылки в никуда -- молча: строка есть, файла по ней нет.

    Поэтому здесь они ПЕРЕКРЫВАЮТСЯ расчётом от rel_path и report_id.
    Это единственный аксессор отчёта в веб-слое, так что достаточно одной
    правки здесь -- все потребители получают правильный путь, не зная об
    этом. Сверено на боевой базе: расчёт совпал с хранимым в 58 записях
    из 58.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    watch, reports = _path_roots()
    d["abs_path"] = sar_common.material_path(watch, d.get("rel_path"))
    d["out_dir"] = sar_common.report_dir(reports, d.get("report_id"))
    return d


# ---------------------------------------------------------------------------
# Ручной режим: видео-плеер, рисование боксов, наблюдения, реальный прогресс
# просмотра (не по сценам, а по факту проигранных секунд самого видео)
# ---------------------------------------------------------------------------

# Простые кэши на report_id, живущие всё время работы процесса. Без верхней
# границы за долгую полевую операцию (десятки обработанных видео за сессию,
# сервер не перезапускается сутками) память бы росла безостановочно и молча.
# Не LRU в строгом смысле, а "не более N последних report_id", этого достаточно:
# вытесняем самый старый по порядку вставки при превышении лимита.
_CACHE_MAX_REPORTS = 200


def _cache_put(cache, key, value):
    if key not in cache and len(cache) >= _CACHE_MAX_REPORTS:
        oldest_key = next(iter(cache))
        del cache[oldest_key]
    cache[key] = value


_telemetry_cache = {}


def get_telemetry_for_report(report):
    """Кэшированный парсинг SRT-телеметрии. Сначала пробуем файл рядом с
    видео (то же имя + .srt, старое поведение), потом -- индекс папки
    telemetry/ (см. sar_common.build_telemetry_index). Переиспользует
    парсер из sar_video_review.py."""
    report_id = report["report_id"]
    if report_id not in _telemetry_cache:
        # ПУТЬ ВЫЧИСЛЯЕТСЯ, а не берётся из abs_path.
        #
        # У облачного материала воркер пишет в abs_path пустую строку, и
        # дальше всё рушилось молча: os.path.splitext("")[0] + ".srt" даёт
        # ".srt", которого нет, а find_telemetry_for_video() сопоставляет по
        # Path(video_path).stem -- у пустой строки он пустой и не совпадает
        # ни с чем. То есть SRT не находился НИКОГДА: ни рядом с видео, ни в
        # папке telemetry/, даже если человек положил его туда руками.
        # Пометки на облачном видео оставались без координат, без ошибки.
        #
        # Пятый случай одной и той же ошибки -- «где файл» спрашивают у
        # диска, а облако на это не отвечает. См. CLAUDE.md.
        video_path = sar_common.material_path(
            os.path.abspath(SERVER_CFG["watch_dir"]), report["rel_path"])
        srt_path = os.path.splitext(video_path)[0] + ".srt"
        if not os.path.exists(srt_path):
            match, reason = sar_common.find_telemetry_for_video(video_path, _TELEMETRY_INDEX)
            srt_path = str(match) if match else None
        if srt_path and os.path.exists(srt_path):
            detection_cfg = load_detection_config(
                os.path.join(SCRIPT_DIR, "sar_config.json") if os.path.exists(
                    os.path.join(SCRIPT_DIR, "sar_config.json")) else None)
            gps_order = detection_cfg.get("srt_gps_tuple_order", "lat_lon")
            _cache_put(_telemetry_cache, report_id, parse_srt_telemetry(srt_path, gps_tuple_order=gps_order))
        else:
            _cache_put(_telemetry_cache, report_id, [])
    return _telemetry_cache[report_id]


@app.route("/report/<report_id>/video")
def report_video(report_id):
    report = get_report_row(report_id)
    if report is None:
        return "Отчёт не найден", 404

    # По умолчанию отдаём лёгкую копию: оригинал идёт на 30 Мбит/с, и
    # столько нужно КАЖДОМУ зрителю через один канал наружу. Копия того же
    # разрешения весит в разы меньше -- см. proxy_video в sar_common.
    #
    # ?original=1 -- когда надо разглядеть вплотную. Копию всегда можно
    # обойти, поэтому сжатие не отнимает у человека ничего, а только
    # ускоряет обычный просмотр.
    #
    # ПОРЯДОК ЗДЕСЬ ВАЖЕН. Раньше наличие ОРИГИНАЛА проверялось первым, до
    # подстановки копии. Пока весь материал лежал на локальном диске, это
    # было незаметно. Но материал уезжает в облако, и там оригинала на
    # месте может не быть вовсе -- он нужен ровно дважды, при обработке и
    # при сборке копии. Со старым порядком плеер отдавал бы 404, ИМЕЯ
    # готовую копию под рукой: человек видит "видео не найдено" при
    # полностью рабочем материале.
    #
    # Поэтому оригинал обязателен только тогда, когда его прямо попросили.
    want_original = request.args.get("original") == "1"
    path = None
    if not want_original:
        proxy = sar_common.proxy_video_path(DATA_DIR, report["rel_path"])
        if os.path.exists(proxy):
            path = proxy
    if path is None:
        path = sar_common.find_material_file(
            _path_roots()[0], _data_dir(), report["rel_path"])             or report["abs_path"]
        if not os.path.exists(path):
            if want_original:
                return ("Оригинал недоступен: файла нет на месте. "
                        "Уберите «оригинал», чтобы смотреть лёгкую копию."), 404
            return "Видеофайл больше не найден на диске", 404

    # conditional=True -> Flask/Werkzeug сам обрабатывает Range-заголовки,
    # это и даёт перемотку в <video> без ручной реализации потоковой отдачи
    return send_file(path, conditional=True)


@app.route("/api/report/<report_id>/video_info")
def api_video_info(report_id):
    """Есть ли лёгкая копия и насколько она легче. Нужно плееру, чтобы
    честно показать, что именно человек сейчас смотрит."""
    report = get_report_row(report_id)
    if report is None or report["kind"] != "video":
        return jsonify({"proxy": False})
    proxy = sar_common.proxy_video_path(DATA_DIR, report["rel_path"])
    if not os.path.exists(proxy):
        return jsonify({"proxy": False})
    try:
        orig_mb = os.path.getsize(report["abs_path"]) / 1e6
        proxy_mb = os.path.getsize(proxy) / 1e6
    except OSError:
        return jsonify({"proxy": False})
    return jsonify({"proxy": True, "original_mb": round(orig_mb),
                     "proxy_mb": round(proxy_mb)})


@app.route("/report/<report_id>/photo")
def report_photo(report_id):
    """Оригинал снимка. Аналог /report/<id>/video для фото -- без него снимок
    до обработки детектором нельзя было посмотреть через сервис ВООБЩЕ:
    строка в списке не кликабельна, пока нет готового отчёта, а отчёта нет,
    пока файл стоит в очереди (а очередь на CPU бывает на сутки)."""
    report = get_report_row(report_id)
    if report is None:
        return "Отчёт не найден", 404
    if report["kind"] != "photo":
        return "Это не фото", 400
    found = sar_common.find_material_file(
        _path_roots()[0], _data_dir(), report["rel_path"])
    if found:
        report = dict(report, abs_path=found)
    if not os.path.exists(report["abs_path"]):
        return "Исходный файл больше не найден на диске", 404
    return send_file(report["abs_path"], conditional=True)


PHOTO_VIEWER_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Снимок — {name}</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; background:#111; color:#eee; margin:0; padding:20px; }}
a {{ color:#8ecbff; }}
h1 {{ font-size:16px; margin:12px 0; }}
.banner {{ background:#3a2f00; border:1px solid #8a6d00; color:#ffcc66; padding:10px 14px;
           border-radius:8px; margin-bottom:14px; font-size:13px; }}
.banner.done {{ background:#14301c; border-color:#22703a; color:#9fe8b5; }}
.toolbar {{ display:flex; align-items:center; gap:14px; margin:10px 0; flex-wrap:wrap; font-size:13px; }}
.zoom-controls button {{ width:30px; height:30px; border-radius:5px; border:1px solid #444;
                          background:#252525; color:#eee; cursor:pointer; font-size:16px; }}
.zoom-controls button:hover {{ background:#333; }}
.hint {{ color:#777; font-size:12px; }}
.viewport {{ position:relative; width:100%; height:78vh; overflow:hidden; border-radius:6px;
             background:#000; cursor:grab; touch-action:none; border:1px solid #333; }}
.viewport.dragging {{ cursor:grabbing; }}
.stage {{ position:absolute; top:50%; left:50%; transform-origin:50% 50%; will-change:transform; }}
.stage img {{ display:block; max-width:none; user-select:none; -webkit-user-drag:none; }}
/* Крошки: путь назад в операцию материала. Раньше здесь стояла ссылка
   "к списку файлов" -- в общую кучу всех материалов, мимо операции, из
   которой человек пришёл. */
.crumbs {{ font-size:13px; color:#888; margin:0 0 10px; }}
.crumbs a {{ color:#6bb; text-decoration:none; }}
.crumbs a:hover {{ text-decoration:underline; }}
</style></head>
<body>
<p class="crumbs">{crumbs}{report_link}</p>
<h1>Снимок — {short_name}</h1>
{banner}
<div class="toolbar">
  <span class="zoom-controls">
    <button onclick="zoomBy(1.3)" title="Приблизить">+</button>
    <button onclick="zoomBy(1/1.3)" title="Отдалить">&minus;</button>
    <button onclick="resetZoom()" title="Вписать в экран">⤾</button>
  </span>
  <span class="hint">колесо мыши — зум, зажать и тянуть — панорама, двойной клик — сброс</span>
</div>
<div class="viewport" id="viewport">
  <div class="stage" id="stage"><img id="img" src="/report/{report_id}/photo"></div>
</div>
<script>
let st = {{ scale: 1, tx: 0, ty: 0, dragging: false, lastX: 0, lastY: 0 }};
const img = document.getElementById('img');
const viewport = document.getElementById('viewport');
const stage = document.getElementById('stage');

function apply() {{
  stage.style.transform = `translate(${{st.tx}}px, ${{st.ty}}px) scale(${{st.scale}})`;
  stage.style.marginLeft = (-img.naturalWidth / 2) + 'px';
  stage.style.marginTop = (-img.naturalHeight / 2) + 'px';
}}
function fit() {{
  const s = Math.min(viewport.clientWidth / img.naturalWidth,
                     viewport.clientHeight / img.naturalHeight, 1);
  st = {{ scale: s || 1, tx: 0, ty: 0, dragging: false, lastX: 0, lastY: 0 }};
  apply();
}}
function zoomBy(f, cx, cy) {{
  const r = viewport.getBoundingClientRect();
  const px = cx ?? r.width / 2, py = cy ?? r.height / 2;
  const ns = Math.min(20, Math.max(0.05, st.scale * f));
  const af = ns / st.scale;
  const dx = px - r.width / 2, dy = py - r.height / 2;
  st.tx = (st.tx - dx) * af + dx;
  st.ty = (st.ty - dy) * af + dy;
  st.scale = ns;
  apply();
}}
function resetZoom() {{ fit(); }}

img.onload = fit;
if (img.complete && img.naturalWidth) fit();

viewport.addEventListener('wheel', e => {{
  e.preventDefault();
  const r = viewport.getBoundingClientRect();
  zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top);
}}, {{ passive: false }});
viewport.addEventListener('mousedown', e => {{
  st.dragging = true; st.lastX = e.clientX; st.lastY = e.clientY;
  viewport.classList.add('dragging');
}});
window.addEventListener('mousemove', e => {{
  if (!st.dragging) return;
  st.tx += e.clientX - st.lastX; st.ty += e.clientY - st.lastY;
  st.lastX = e.clientX; st.lastY = e.clientY; apply();
}});
window.addEventListener('mouseup', () => {{ st.dragging = false; viewport.classList.remove('dragging'); }});
viewport.addEventListener('dblclick', resetZoom);
</script>
</body></html>"""


@app.route("/report/<report_id>/viewer/")
def photo_viewer_page(report_id):
    """Просмотр снимка с зумом, доступен в ЛЮБОМ статусе -- по той же логике,
    что и ручной плеер для видео (см. player_page): файл лежит на диске с
    момента появления в очереди, и смотреть его глазами можно, не дожидаясь
    детектора."""
    report = get_report_row(report_id)
    if report is None:
        return "Отчёт не найден", 404
    if report["kind"] != "photo":
        return "Просмотр снимка доступен только для фото", 400
    found = sar_common.find_material_file(
        _path_roots()[0], _data_dir(), report["rel_path"])
    if not found:
        if report.get("cloud_file_id"):
            return _not_ready_page(report)
    if not found and not os.path.exists(report["abs_path"]):
        return "Исходный файл больше не найден на диске", 404

    if report["status"] == "done":
        banner = ('<div class="banner done">✅ Снимок обработан — в отчёте есть найденные '
                  'моделью кандидаты с координатами.</div>')
        report_link = f' &nbsp;·&nbsp; <a href="/report/{report_id}/">к отчёту с детекциями</a>'
    elif report["status"] == "error":
        banner = ('<div class="banner" style="background:#3a1414;border-color:#7a1f1f;color:#ff9999;">'
                  '⚠ Автоматическая обработка завершилась с ошибкой — рамок модели не будет, '
                  'но сам снимок доступен для просмотра.</div>')
        report_link = ""
    else:
        banner = ('<div class="banner">⚙️ Снимок ещё не обработан моделью — его можно '
                  'смотреть уже сейчас, кандидаты появятся в отчёте позже.</div>')
        report_link = ""

    # Возврат -- в ОПЕРАЦИЮ материала, а не в общий список всех файлов.
    # Раньше отсюда вела ссылка "к списку файлов" -- в кучу, мимо операции,
    # из которой человек пришёл; на этом он и споткнулся.
    rel = (report["rel_path"] or "").replace("\\", "/")
    return PHOTO_VIEWER_HTML.format(
        report_id=report_id, name=rel, short_name=rel.split("/")[-1],
        crumbs=material_crumbs(get_db(), report),
        banner=banner, report_link=report_link)


@app.route("/api/report/<report_id>/coverage")
def api_coverage(report_id):
    report = get_report_row(report_id)
    if report is None:
        return jsonify({"error": "not found"}), 404
    conn = get_db()
    stats = get_report_stats(conn, report_id, report["duration_sec"])
    return jsonify(stats)


_ai_detections_cache = {}


def _read_detections_file(det_path):
    """Читает detections.json. Файл может в этот самый момент переписываться
    воркером (он пишет через временный файл + os.replace, так что "битого"
    полузаписанного JSON быть не должно) — но на всякий случай, если что-то
    пойдёт не так (гонка на файловой системе, странности конкретной ОС),
    не роняем запрос, а просто отдаём пусто и попробуем на следующий опрос.

    Резервная попытка через cp1251 -- страховка на файлы, записанные ДО
    исправления пропущенного encoding="utf-8" в save_outputs()
    (sar_video_review.py): такие файлы физически лежат в cp1251 (кодировка
    консоли Windows по умолчанию), а не в UTF-8. Правильное решение --
    перегенерировать их (см. backfill_telemetry.py), это просто чтобы
    старые отчёты не роняли API 500-й ошибкой, пока их не перегенерировали."""
    try:
        with open(det_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except UnicodeDecodeError:
        try:
            with open(det_path, "r", encoding="cp1251") as f:
                return json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
    except (OSError, json.JSONDecodeError):
        return None


@app.route("/api/report/<report_id>/ai_detections")
def api_ai_detections(report_id):
    """Отдаёт то, что нашла модель (те же данные, что лежат в
    detections.json рядом с отчётом) — для опционального показа этих рамок
    поверх видео в ручном плеере.

    Пока видео ЕЩЁ обрабатывается, detections.json пополняется на лету
    (см. flush_partial_detections в sar_video_review.py) — поэтому кэшируем
    результат только когда отчёт уже 'done' (тогда файл точно final и
    неизменный). Пока идёт обработка — каждый раз читаем заново, чтобы
    отдавать актуальную, а не устаревшую картину."""
    report = get_report_row(report_id)
    if report is None:
        return jsonify({"error": "not found"}), 404

    if report["status"] == "done" and report_id in _ai_detections_cache:
        return jsonify(_ai_detections_cache[report_id])

    det_path = os.path.join(report["out_dir"], "detections.json")
    hits = _read_detections_file(det_path) if os.path.exists(det_path) else []
    if hits is None:  # временная ошибка чтения -- отдаём то, что было закэшировано раньше, если было
        hits = _ai_detections_cache.get(report_id, [])
        return jsonify(hits)

    result = [
        {"timestamp_sec": h["seconds"], "bbox": h["bbox"],
         "object_class": h["object_class"], "confidence": h["confidence"],
         "source": h["source"]}
        for h in hits
    ]
    if report["status"] == "done":
        _cache_put(_ai_detections_cache, report_id, result)  # теперь уже финальные данные, можно кэшировать
    return jsonify(result)


_ai_scenes_cache = {}


def _get_ai_scenes_for_report(report):
    """То же, что видно в report.html как карточки сцен -- сырые AI-детекции
    (тот же detections.json), сгруппированные в компактные "сцены" (см.
    group_hits_into_scenes в sar_video_review.py). Общая логика для
    /api/report/<id>/ai_scenes (плеер) и для счётчика на главной странице
    (/api/tree) -- вынесена в отдельную функцию, чтобы не дублировать
    группировку и кэш.

    Группировка считается на лету при каждом запросе, не кэшируется отдельным
    файлом на диске -- это позволяет отдавать актуальный список сцен и во
    время ещё идущей обработки, когда detections.json пополняется на лету
    (кэшируется в памяти процесса только после статуса done, см. ниже)."""
    report_id = report["report_id"]
    if report["status"] == "done" and report_id in _ai_scenes_cache:
        return _ai_scenes_cache[report_id]

    # Папка отчёта ВЫЧИСЛЯЕТСЯ, а не берётся из столбца out_dir: хранимый
    # абсолютный путь привязывает базу к машине и к букве диска, а сюда
    # приходят строки и из /api/tree (сырые, мимо get_report_row), и из
    # самого get_report_row -- то есть если считать по-разному, разойдутся.
    #
    # Пустой report_id по-прежнему означает "сцен нет": раньше пустой
    # out_dir ронял ВЕСЬ список файлов с TypeError -- одна плохая строка
    # делала страницу недоступной целиком.
    if not report_id:
        return []
    _, reports_root = _path_roots()
    det_path = os.path.join(sar_common.report_dir(reports_root, report_id),
                            "detections.json")
    raw_hits = _read_detections_file(det_path) if os.path.exists(det_path) else []
    if not raw_hits:
        return []

    hits = [Hit(**h) for h in raw_hits]

    # Готовые отчёты УЖЕ несут финальный group_id на каждом hit (проставлен
    # один раз в group_hits_into_scenes() при завершении обработки, см.
    # process_video/save_outputs в sar_video_review.py) -- группируем по
    # нему напрямую, а не гоняем заново весь алгоритм трекинга по близости.
    # Разница на практике оказалась не теоретической: на реальном видео с
    # 25000+ детекциями повторная группировка занимала 7+ секунд НА КАЖДЫЙ
    # холодный запрос (после каждого рестарта сервера, плюс конкуренция за
    # CPU с sar_worker.py, который в это же время может обрабатывать другое
    # видео) -- отсюда и жалоба "список видео на главной грузится очень
    # долго". Для ЕЩЁ обрабатывающихся отчётов group_id пока не финален
    # (проставляется только в конце), поэтому для них по-прежнему считаем
    # группировку на лету.
    if report["status"] == "done" and all(h.group_id for h in hits):
        by_gid = defaultdict(list)
        for h in hits:
            by_gid[h.group_id].append(h)
        groups = []
        for gid, g_hits in by_gid.items():
            g_hits.sort(key=lambda x: x.frame_idx)
            groups.append({"id": gid, "hits": g_hits})
    else:
        detection_cfg = load_detection_config(
            os.path.join(SCRIPT_DIR, "sar_config.json") if os.path.exists(
                os.path.join(SCRIPT_DIR, "sar_config.json")) else None)
        grouping_cfg = detection_cfg.get("grouping", {})
        groups = group_hits_into_scenes(
            hits, max_frame_gap=grouping_cfg.get("max_frame_gap", 360),
            distance_multiplier=grouping_cfg.get("distance_multiplier", 2.5))

    def _first_not_none(hits_list, attr):
        return next((getattr(h, attr) for h in hits_list if getattr(h, attr) is not None), None)

    scenes = []
    for grp in groups:  # НЕ "g" -- имя уже занято импортом flask.g (application context)
        g_hits = grp["hits"]  # уже в порядке возрастания frame_idx
        peak = max(g_hits, key=lambda h: h.confidence)
        first, last = g_hits[0], g_hits[-1]
        # координаты ДРОНА и отдельно "вероятные координаты" объекта (оценка,
        # см. sar_common.estimate_ground_point) -- то же, что и в report.html,
        # чтобы в плеере не было расхождений с тем, что уже видели в отчёте
        scenes.append({
            # ref_key -- СТАБИЛЬНЫЙ отпечаток содержимого сцены для триажа
            # (detection_priorities), нарочно НЕ используем позиционный
            # grp["id"] ("g00001", ...) -- он зависит от порядка обхода при
            # группировке и меняется при переобработке видео. Общая функция
            # с sar_dataset_export.py -- см. sar_common.ai_scene_ref_key
            # (там же объяснение, почему одного класса+кадра недостаточно)
            "ref_key": sar_common.ai_scene_ref_key(
                peak.object_class, peak.source, first.frame_idx, first.bbox),
            "id": grp["id"], "object_class": peak.object_class, "source": peak.source,
            "confidence": peak.confidence, "count": len(g_hits),
            "time_start_sec": first.seconds, "time_end_sec": last.seconds,
            "drone_lat": peak.lat if peak.lat is not None else _first_not_none(g_hits, "lat"),
            "drone_lon": peak.lon if peak.lon is not None else _first_not_none(g_hits, "lon"),
            "est_lat": peak.est_lat if peak.est_lat is not None else _first_not_none(g_hits, "est_lat"),
            "est_lon": peak.est_lon if peak.est_lon is not None else _first_not_none(g_hits, "est_lon"),
            "est_distance_m": peak.est_distance_m,
            "alt": peak.alt, "alt_abs": peak.alt_abs,
            "yaw": peak.yaw, "pitch": peak.pitch, "roll": peak.roll,
            "focal_len": peak.focal_len,
            "raw_telemetry": peak.raw_telemetry,
        })
    scenes.sort(key=lambda s: s["time_start_sec"])

    if report["status"] == "done":
        _cache_put(_ai_scenes_cache, report_id, scenes)
    return scenes


@app.route("/api/report/<report_id>/ai_scenes")
def api_ai_scenes(report_id):
    report = get_report_row(report_id)
    if report is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_get_ai_scenes_for_report(report))


@app.route("/api/report/<report_id>/playback_watched", methods=["POST"])
def api_playback_watched(report_id):
    """Пинг от плеера: реально просмотренный диапазон секунд самого видео
    (не диапазон сцены, как в автоматическом режиме, а буквально то, что
    проиграл <video>). Пишется в ту же watch_segments — метрика покрытия
    получается объединённой для обоих режимов, что и правильно: и то,
    и то — подтверждённый живой просмотр человеком."""
    data = request.get_json(force=True, silent=True) or {}
    start_sec, end_sec = data.get("start_sec"), data.get("end_sec")
    if start_sec is None or end_sec is None or float(end_sec) <= float(start_sec):
        return jsonify({"ok": False, "error": "bad range"}), 400
    viewer_name = session.get("viewer_name", "аноним")
    conn = get_db()
    conn.execute(
        "INSERT INTO watch_segments (report_id, viewer_name, start_sec, end_sec, ts) VALUES (?,?,?,?,?)",
        (report_id, viewer_name, float(start_sec), float(end_sec), datetime.now().isoformat()))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/report/<report_id>/observations", methods=["GET", "POST"])
def api_observations(report_id):
    report = get_report_row(report_id)
    if report is None:
        return jsonify({"error": "not found"}), 404
    conn = get_db()

    if request.method == "GET":
        rows = conn.execute(
            "SELECT * FROM manual_observations WHERE report_id=? ORDER BY timestamp_sec",
            (report_id,)).fetchall()
        return jsonify([dict(r) | {"bbox": json.loads(r["bbox"])} for r in rows])

    data = request.get_json(force=True, silent=True) or {}
    timestamp_sec = data.get("timestamp_sec")
    bbox = data.get("bbox")
    if timestamp_sec is None or not isinstance(bbox, list) or len(bbox) != 4:
        return jsonify({"ok": False, "error": "missing timestamp_sec or bbox"}), 400

    viewer_name = session.get("viewer_name", "аноним")
    label = (data.get("label") or "").strip()[:100]
    note = (data.get("note") or "").strip()[:2000]

    lat = lon = None
    est_lat = est_lon = est_distance_m = None
    raw_telemetry = None
    telemetry = get_telemetry_for_report(report)
    if telemetry:
        telem = lookup_telemetry(telemetry, float(timestamp_sec))
        lat, lon = telem.get("lat"), telem.get("lon")
        raw_telemetry = telem.get("raw_telemetry")

        # "вероятные координаты" объекта -- та же оценка, что и для AI-детекций
        # (см. sar_common.estimate_ground_point) -- bbox ручного наблюдения уже
        # нормализован 0..1 (см. схему manual_observations), центр считаем так
        # же, как при разметке в плеере
        detection_cfg = load_detection_config(
            os.path.join(SCRIPT_DIR, "sar_config.json") if os.path.exists(
                os.path.join(SCRIPT_DIR, "sar_config.json")) else None)
        max_estimate_distance_m = detection_cfg.get(
            "max_estimate_distance_m", sar_common.DEFAULT_MAX_ESTIMATE_DISTANCE_M)
        bbox_cx_frac = (bbox[0] + bbox[2]) / 2.0
        bbox_cy_frac = (bbox[1] + bbox[3]) / 2.0
        # FOV по фактическому зуму этого кадра, как и для AI-детекций
        frame_hfov, frame_vfov = sar_common.resolve_frame_fov(
            telem.get("focal_len"), detection_cfg)
        estimate = sar_common.estimate_ground_point(
            drone_lat=lat, drone_lon=lon, altitude_m=telem.get("alt"),
            gimbal_yaw_deg=telem.get("yaw"), gimbal_pitch_deg=telem.get("pitch"),
            bbox_center_frac_x=bbox_cx_frac, bbox_center_frac_y=bbox_cy_frac,
            horizontal_fov_deg=frame_hfov, vertical_fov_deg=frame_vfov,
            max_distance_m=max_estimate_distance_m)
        if estimate:
            est_lat, est_lon, est_distance_m = estimate

    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, label, "
        "note, lat, lon, est_lat, est_lon, est_distance_m, raw_telemetry, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (report_id, viewer_name, float(timestamp_sec), json.dumps(bbox), label, note, lat, lon,
         est_lat, est_lon, est_distance_m, raw_telemetry, now))
    obs_id = cur.lastrowid

    # Статус ставится ТУТ ЖЕ, одной операцией с самой пометкой.
    #
    # Иначе пришлось бы делать второй запрос из браузера, а между ними
    # существует промежуток, в котором находка уже есть, а её статус ещё
    # нет: оборвётся связь -- и пометка "точно человек" останется
    # неразмеченной, причём человек будет уверен, что отметил.
    priority = (data.get("priority") or "").strip()
    if priority and priority in sar_common.VALID_PRIORITIES:
        conn.execute(
            "INSERT OR REPLACE INTO detection_priorities "
            "(report_id, kind, ref_key, priority, set_by, set_at) "
            "VALUES (?, 'manual', ?, ?, ?, ?)",
            (report_id, str(obs_id), priority, viewer_name, now))
    conn.commit()
    return jsonify({"ok": True, "id": obs_id})


@app.route("/api/report/<report_id>/observations/<int:obs_id>", methods=["DELETE"])
def api_delete_observation(report_id, obs_id):
    conn = get_db()
    conn.execute("DELETE FROM manual_observations WHERE id=? AND report_id=?", (obs_id, report_id))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/report/<report_id>/enqueue", methods=["POST"])
def api_enqueue(report_id):
    """Поставить файл в очередь на анализ моделью вручную.

    Нужно, когда авто-обработка выключена (auto_process=false): файлы
    появляются в списке со статусом 'idle' и доступны для ручного просмотра,
    а через модель прогоняются только те, что человек выбрал сам. На CPU
    минута видео считается ~30 минут, поэтому выбирать осмысленно."""
    conn = get_db()
    row = conn.execute("SELECT status FROM reports WHERE report_id=?", (report_id,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    if row["status"] in ("queued", "processing"):
        return jsonify({"ok": True, "status": row["status"], "note": "уже в работе"})

    # 'done'/'error' тоже можно отправить заново -- это осознанный повтор
    # (сменили модель/настройки детектора и хотят пересчитать)
    conn.execute(
        "UPDATE reports SET status='queued', progress_pct=0, phase=NULL, error=NULL, updated_at=? "
        "WHERE report_id=?", (datetime.now().isoformat(), report_id))
    conn.commit()
    return jsonify({"ok": True, "status": "queued"})


MAX_COMMENT_LEN = 2000


@app.route("/api/report/<report_id>/comments", methods=["GET", "POST"])
def api_comments(report_id):
    """Обсуждение конкретной находки (сцены модели или ручной отметки).

    Читается ЦЕЛИКОМ за один запрос на отчёт, а не по запросу на карточку:
    карточек на видео бывают сотни, и запрос на каждую превратил бы открытие
    плеера в сотни обращений к серверу.

    Привязка -- та же пара (kind, ref_key), что и у статусов находок, поэтому
    обсуждение переживает переобработку видео (см. ai_scene_ref_key)."""
    conn = get_db()

    if request.method == "GET":
        rows = conn.execute(
            "SELECT id, kind, ref_key, author, text, created_at FROM detection_comments "
            "WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
        return jsonify([dict(r) for r in rows])

    # Писать могут только опознанные (вошедшие по персональной ссылке из
    # бота) и не лишённые слова. Аноним по общему паролю смотрит и размечает
    # находки, но в обсуждении не участвует -- иначе заблокированный просто
    # зашёл бы по общему паролю и продолжил, и модерация ничего не значила бы.
    if not can_comment():
        if current_role() == sar_common.ROLE_MUTED:
            return jsonify({"ok": False, "error": "muted",
                            "message": "Координатор ограничил вам участие в обсуждениях."}), 403
        return jsonify({"ok": False, "error": "not_verified",
                        "message": "Чтобы писать в обсуждении, войдите по персональной "
                                   "ссылке из бота (команда /help)."}), 403

    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind")
    ref_key = data.get("ref_key")
    text = (data.get("text") or "").strip()[:MAX_COMMENT_LEN]
    if kind not in ("ai_scene", "manual") or not ref_key:
        return jsonify({"ok": False, "error": "missing kind or ref_key"}), 400
    if not text:
        return jsonify({"ok": False, "error": "пустой комментарий"}), 400

    author = session.get("viewer_name", "аноним")
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO detection_comments (report_id, kind, ref_key, author, text, created_at) "
        "VALUES (?,?,?,?,?,?)", (report_id, kind, ref_key, author, text, now))
    conn.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "author": author,
                    "text": text, "created_at": now})


@app.route("/api/report/<report_id>/comments/<int:comment_id>", methods=["DELETE"])
def api_delete_comment(report_id, comment_id):
    """Своё сообщение может удалить автор, любое -- модератор.

    Для опознанных (вход по персональной ссылке) это настоящее правило:
    имя берётся из записи бота и подделать его нельзя. Для анонимов по
    общему паролю сверка по имени остаётся тем, чем была -- вежливостью,
    а не защитой."""
    conn = get_db()
    row = conn.execute(
        "SELECT author FROM detection_comments WHERE id=? AND report_id=?",
        (comment_id, report_id)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    if row["author"] != session.get("viewer_name", "аноним") and not is_moderator():
        return jsonify({"ok": False, "error": "это не ваш комментарий"}), 403
    conn.execute("DELETE FROM detection_comments WHERE id=?", (comment_id,))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/report/<report_id>/priorities", methods=["GET", "POST"])
def api_priorities(report_id):
    """Ранжирование детекций (ручных и модели) -- см. detection_priorities в
    sar_common.init_db и обсуждение с пользователем. Только человек может
    поставить любой из этих статусов, включая "точно человек" -- сервер
    сюда не пишет ни при какой автоматической обработке, только по этому
    эндпоинту, вызванному из браузера залогиненным человеком."""
    conn = get_db()

    if request.method == "GET":
        rows = conn.execute(
            "SELECT kind, ref_key, priority, set_by, set_at FROM detection_priorities WHERE report_id=?",
            (report_id,)).fetchall()
        return jsonify([dict(r) for r in rows])

    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind")
    ref_key = data.get("ref_key")
    priority = (data.get("priority") or "").strip()
    if kind not in ("ai_scene", "manual") or not ref_key:
        return jsonify({"ok": False, "error": "missing kind or ref_key"}), 400

    if not priority:
        # пустое значение = "снять метку", а не "500 ошибка валидации" --
        # обычное действие в UI (выбрать "— не размечено —" обратно)
        conn.execute(
            "DELETE FROM detection_priorities WHERE report_id=? AND kind=? AND ref_key=?",
            (report_id, kind, ref_key))
        conn.commit()
        return jsonify({"ok": True, "priority": None})

    if priority not in sar_common.VALID_PRIORITIES:
        return jsonify({"ok": False, "error": "unknown priority"}), 400

    viewer_name = session.get("viewer_name", "аноним")
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO detection_priorities (report_id, kind, ref_key, priority, set_by, set_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(report_id, kind, ref_key) DO UPDATE SET "
        "priority=excluded.priority, set_by=excluded.set_by, set_at=excluded.set_at",
        (report_id, kind, ref_key, priority, viewer_name, now))
    conn.commit()
    return jsonify({"ok": True, "priority": priority, "set_by": viewer_name, "set_at": now})


PLAYER_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Плеер — {name}</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; background:#111; color:#eee; margin:0; padding:20px; }}
a {{ color:#8ecbff; }}
h1 {{ font-size:16px; margin:12px 0; }}
.header-row {{ display:flex; justify-content:space-between; align-items:baseline;
               gap:16px; margin-bottom:6px; }}
.online-indicator {{ display:flex; align-items:center; gap:6px; font-size:13px;
                     color:#ccc; flex-shrink:0; white-space:nowrap; }}
.online-dot {{ width:6px; height:6px; border-radius:50%; background:#2f9e44; flex-shrink:0; }}
.layout {{ display:flex; gap:18px; align-items:flex-start; flex-wrap:wrap; }}
.video-col {{ flex:2; min-width:480px; }}
.obs-col {{ flex:1; min-width:320px; }}

.video-wrap {{ position:relative; width:100%; background:#000; border-radius:6px;
               overflow:hidden; }}
/* Сцена -- то, что масштабируется. Видео и слой разметки внутри неё, поэтому
   при зуме рамки едут вместе с картинкой, а не отстают от неё. */
/* will-change:transform здесь БЫЛО и это оказалось вредно.
   Оно поднимает сцену в отдельный слой, который браузер растрирует ОДИН
   раз, а потом просто увеличивает получившуюся картинку. Видео -- растр,
   ему всё равно, а вот рамки разметки -- вектор, и они превращались в
   лесенку при первом же зуме. Без will-change браузер перерисовывает
   вектор в текущем масштабе, и линии остаются чистыми. */
#stage {{ transform-origin:0 0; }}
#stage video {{ width:100%; display:block; }}

/* В полном экране разворачивается ОБЁРТКА, а не <video>: иначе слой
   разметки остаётся в обычном документе (см. комментарий в разметке).
   Здесь же выравниваем видео по центру -- у экрана и кадра разные
   пропорции, и без этого видео прилипало бы к верхнему краю. */
.video-wrap:fullscreen {{ border-radius:0; display:flex; align-items:center;
                          justify-content:center; }}
.video-wrap:fullscreen #stage {{ width:100%; }}
.video-wrap:fullscreen #stage video {{ max-height:100vh; object-fit:contain; }}


/* pointer-events:none по умолчанию -- иначе оверлей перехватывает клики по
   нативным элементам управления видео (play/пауза/перемотка/громкость),
   и ими становится невозможно пользоваться. Включаем перехват кликов
   ТОЛЬКО когда реально идёт рисование рамки. */
/* Слой разметки НИКОГДА не ловит указатель.
   Раньше в режиме разметки ему включали pointer-events:auto, и он накрывал
   собой нативную полосу управления: кнопки плеера переставали нажиматься,
   пока включена разметка. Ровно эта грабля описана в CLAUDE.md, и она
   вернулась. Теперь клики ловит ОТДЕЛЬНЫЙ прозрачный слой (#draw-catch),
   который до полосы управления не достаёт. */
#overlay {{ position:absolute; top:0; left:0; width:100%; height:100%;
            pointer-events:none; }}

/* Ловушка кликов для рисования. Отдельный элемент, а не сам #overlay,
   потому что у #overlay нельзя менять размер: по нему считаются
   нормализованные координаты рамок, и укоротишь его -- поедут все
   сохранённые пометки. */
/* Тот же приём, что и у ловушки разметки: не достаём до нативной полосы
   управления, иначе перехватим нажатия на её кнопки. */
#click-catch {{ position:absolute; top:0; left:0; right:0;
                bottom:var(--controls-h); }}
#click-catch.off {{ display:none; }}

#draw-catch {{ position:absolute; top:0; left:0; right:0;
               bottom:var(--controls-h); display:none; cursor:crosshair; }}
#draw-catch.on {{ display:block; }}

/* Высота нативной полосы управления. Её точного значения браузер не
   сообщает, поэтому берём с запасом: лучше отдать разметке на несколько
   пикселей меньше, чем снова перекрыть кнопки. */
:root {{ --controls-h: 52px; }}

/* Толщина линии и размер подписи НЕ масштабируются вместе с кадром.
   При восьмикратном увеличении обводка в 2px превращалась в 16px и
   закрывала собой то, что обводит, -- а закрывать находку рамкой на
   поисковом инструменте нельзя.

   vector-effect:non-scaling-stroke -- штатное средство SVG ровно для
   этого: линия остаётся заданной толщины в экранных пикселях при любом
   преобразовании. У текста такого свойства нет, поэтому размер шрифта
   делим на текущий масштаб (--zoom ставит applyStage). */
#overlay rect {{ vector-effect:non-scaling-stroke; }}
#overlay text {{ font-size:calc(14px / var(--zoom, 1));
                 stroke-width:calc(3px / var(--zoom, 1)); }}
#overlay rect.obs-box {{ fill:none; stroke:#ff3b3b; stroke-width:2; }}
#overlay rect.temp-box {{ fill:rgba(255,59,59,0.15); stroke:#ff3b3b; stroke-width:2; stroke-dasharray:5,4; }}
#overlay text.obs-label {{ fill:#ff3b3b; font-weight:bold; paint-order:stroke; stroke:#000; }}

.toolbar {{ display:flex; align-items:center; gap:10px; margin:10px 0; flex-wrap:wrap; }}
/* Легенда управления. Тихая: она справочная, читается один раз и дальше
   не должна тянуть взгляд с кадра. */
.legend {{ display:flex; flex-wrap:wrap; gap:4px 16px; margin:8px 0 2px;
  font-size:11.5px; color:#7d8a87; }}
.legend b {{ color:#b9c4c1; font-weight:600; }}
/* Нативная кнопка полного экрана убрана: она разворачивает САМ <video>,
   а слой разметки -- его сосед, и в полноэкранном видео его не
   существует. Починить на лету нельзя -- requestFullscreen требует
   свежего действия пользователя, а после асинхронного выхода оно уже
   истекло. Полный экран открывается двойным кликом по кадру и клавишей F.

   Меню "⋮" оставлено как есть: в нём живут параметры воспроизведения,
   включая замедление, которое требует методика отсмотра. */
video::-webkit-media-controls-fullscreen-button {{ display:none !important; }}
.src-switch {{ display:inline-flex; align-items:center; gap:5px; cursor:pointer;
  color:#8d9a97; }}
.src-switch input {{ margin:0; cursor:pointer; }}
.src-note {{ color:#6f7d7a; }}
.toolbar button {{ background:#1b1b1b; color:#eee; border:1px solid #333; border-radius:6px;
                    padding:7px 12px; cursor:pointer; font-size:13px; }}
.toolbar button:hover {{ border-color:#555; }}
.toolbar button.active {{ background:#3355aa; border-color:#3355aa; }}
.hint {{ font-size:12px; color:#777; }}
.processing-banner {{ background:#3a2f00; border:1px solid #8a6d00; color:#ffcc66; padding:10px 14px;
                       border-radius:8px; margin-bottom:14px; font-size:13px; display:flex;
                       align-items:center; gap:10px; }}
.processing-banner .spin {{ animation: spin 1.2s linear infinite; display:inline-block; }}
@keyframes spin {{ from {{ transform:rotate(0deg); }} to {{ transform:rotate(360deg); }} }}

.covbar {{ display:flex; gap:1px; width:100%; height:10px; border-radius:3px; overflow:hidden; margin:8px 0; }}
.covseg {{ flex:1; background:#333; }}
.covseg.on {{ background:#2f9e44; }}
.cov-stat {{ font-size:12px; color:#999; margin-bottom:14px; }}

/* Форма заметки -- рядом с нарисованной рамкой, поверх кадра.
   Раньше она жила под плеером: в полноэкранном режиме рамку нарисовать
   можно, а заполнить заметку нечем. Плюс глаз всё равно на кадре, и
   уводить его вниз страницы незачем.

   Цвета -- как на страницах операций, чтобы плеер не выглядел отдельной
   программой. */
#draw-form {{ position:absolute; z-index:5; width:270px; max-width:calc(100% - 24px);
  background:#161d22; border:1px solid #2b353f; border-radius:9px;
  padding:11px; box-shadow:0 14px 38px rgba(0,0,0,.55); }}
#draw-form[hidden] {{ display:none; }}
#draw-form .ttl {{ font-size:11.5px; letter-spacing:.03em; color:#6f7d7a;
  margin-bottom:7px; }}
.draw-form input, .draw-form textarea {{ width:100%; box-sizing:border-box; background:#0d0d0d; color:#eee;
    border:1px solid #333; border-radius:5px; padding:8px; margin-bottom:8px; font-family:inherit; font-size:13px; }}
.draw-form textarea {{ resize:vertical; min-height:50px; }}
.draw-form input, .draw-form textarea {{ border-color:#2b353f; border-radius:7px; }}
.draw-form select {{ width:100%; box-sizing:border-box; margin-bottom:8px;
  background:#0d0d0d; color:#eee; border:1px solid #2b353f; border-radius:7px;
  padding:7px; font-family:inherit; font-size:13px; }}
.draw-form select:focus {{ outline:none; border-color:#5fb8c7; }}
.draw-form input:focus, .draw-form textarea:focus {{ outline:none; border-color:#5fb8c7; }}
.draw-form .row {{ display:flex; gap:8px; }}
.draw-form button {{ flex:1; padding:7px; border-radius:7px; cursor:pointer;
  font-size:13px; font-family:inherit; border:1px solid #2b353f;
  background:transparent; color:#9aa8a5; }}
.draw-form button:hover {{ border-color:#5fb8c7; color:#e8eeec; }}
.draw-form .btn-save {{ border-color:#5fb8c7; color:#5fb8c7; }}
.draw-form .btn-save:hover {{ background:#5fb8c7; color:#101417; }}
.draw-form .hint {{ font-size:10.5px; color:#6f7d7a; margin-top:6px; display:block; }}
.btn-save {{ background:#2f9e44; color:#fff; }}
.btn-cancel {{ background:#444; color:#eee; }}

.obs-item {{ background:#1b1b1b; border:1px solid #2a2a2a; border-radius:8px; padding:10px 12px; margin-bottom:8px; }}
/* Слева -- таймкод и автор, справа -- дата и удаление. space-between на
   четырёх элементах разносил их по всей ширине, и дата зависала посередине. */
.obs-head {{ display:flex; align-items:center; gap:10px; margin-bottom:4px; }}
.obs-head .obs-when {{ margin-left:auto; }}
.obs-time {{ color:#8ecbff; font-weight:bold; cursor:pointer; font-size:14px; }}
/* Ссылка и кадр -- рядом с удалением, в шапке карточки. Кнопка ссылки
   именно button: карточка не обёрнута в <a>, но привычка вкладывать
   ссылки в этом проекте уже приводила к неработающим кнопкам. */
.obs-link, .obs-frame {{ background:none; border:none; cursor:pointer;
  color:#8a949c; font-size:13px; padding:0 3px; line-height:1;
  text-decoration:none; }}
.obs-link:hover, .obs-frame:hover {{ color:#cfe6ef; }}
/* когда пометка СДЕЛАНА -- отдельно от таймкода в видео, иначе их путают */
.obs-when {{ color:#777; font-size:11.5px; margin-left:auto; white-space:nowrap; }}
.obs-author {{ font-size:11px; color:#888; }}
.obs-label {{ font-weight:bold; margin-bottom:2px; }}
.obs-note {{ font-size:13px; color:#ccc; white-space:pre-wrap; }}
.obs-gps {{ font-size:11px; color:#888; margin-top:4px; }}
.obs-gps.est {{ color:#c3b3ff; }}
.raw-telemetry {{ margin-top:6px; }}
.raw-telemetry summary {{ cursor:pointer; color:#8ecbff; font-size:11px; user-select:none; }}
.raw-telemetry summary:hover {{ color:#b3d9ff; }}
.raw-telemetry pre {{ background:#0d0d0d; border:1px solid #2a2a2a; border-radius:5px; padding:8px 10px;
                       margin:6px 0; font-size:11px; color:#9fef9f; white-space:pre-wrap; word-break:break-word; }}
/* Удаление наблюдения -- в его собственной шапке, рядом с датой.
   Раньше кнопка стояла ПОСЛЕ блока обсуждения, сразу под полем ввода
   комментария, и читалась как "удалить комментарий" -- о чём и сообщил
   пользователь. Место кнопки и есть её подпись: рядом с автором и датой
   наблюдения понятно, что удаляется наблюдение. */
.obs-del {{ background:none; border:none; color:#6f7d7a;
  cursor:pointer; font-size:13px; line-height:1; padding:0 2px;
  opacity:0; transition:opacity .12s; }}
.obs-item:hover .obs-del, .obs-del:focus {{ opacity:1; }}
.obs-del:hover {{ color:#e0776a; }}
.obs-actions {{ margin-top:6px; display:flex; gap:8px; }}
.obs-actions button {{ font-size:11px; padding:3px 8px; border-radius:5px; border:1px solid #444;
                        background:#252525; color:#ccc; cursor:pointer; }}
.obs-actions button:hover {{ background:#333; }}
.priority-control {{ margin-top:8px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.priority-control select {{ font-size:12px; padding:4px 6px; border-radius:5px; border:1px solid #444;
                             background:#0d0d0d; color:#eee; }}
.priority-who {{ font-size:11px; color:#888; }}
/* --- Комментарии -----------------------------------------------------
   Обычный разговор: список реплик, под ним поле ввода.

   Раньше блок жил в СВОЕЙ палитре -- синие ссылки #8ecbff, зелёные имена
   #9fe8b5, синяя рамка кнопки #3355aa -- на странице, где акцент всей
   платформы бирюзовый. Три разных акцента на одном экране читаются как
   чужой виджет, приклеенный сбоку. Вход в обсуждение к тому же выглядел
   переключателем "💬 обсуждение (2)", то есть элементом управления, а не
   началом разговора.

   Цвета взяты те же, что на страницах операций, чтобы плеер и операции
   выглядели одной системой. Объявлены локально для блока: перекрасить
   плеер целиком -- отдельная работа, и мешать её с этой значит менять
   всё сразу и вслепую. */
.discussion {{
  --c-line:#2b353f; --c-card:#1a2129; --c-ink:#e8eeec;
  --c-soft:#9aa8a5; --c-dim:#6f7d7a; --c-accent:#5fb8c7;
  margin-top:10px; border-top:1px solid var(--c-line); padding-top:8px;
}}
/* Подпись, а не кнопка. Треугольник-маркер убран намеренно: он делал из
   раздела орган управления, хотя это просто заголовок разговора. */
.discussion summary {{ cursor:pointer; list-style:none; user-select:none;
  font-size:11.5px; letter-spacing:.03em; color:var(--c-dim); padding:1px 0; }}
.discussion summary::-webkit-details-marker {{ display:none; }}
.discussion summary:hover {{ color:var(--c-soft); }}
.discussion[open] summary {{ margin-bottom:9px; }}

/* Реплики без рамок и подложек: разделяет их воздух, а не border. Десяток
   реплик в рамочках рябит и читается как стопка карточек, а не как
   разговор. */
.cmt-list {{ display:flex; flex-direction:column; gap:10px; margin-bottom:11px; }}
.cmt {{ display:flex; flex-direction:column; gap:2px; }}
.cmt-head {{ display:flex; align-items:baseline; gap:8px; }}
.cmt-author {{ font-size:12px; font-weight:650; color:var(--c-soft); }}
.cmt-time {{ font-size:11px; color:var(--c-dim); }}
.crumbs {{ font-size:13px; color:#888; margin:0; min-width:0;
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.crumbs a {{ color:#6bb; text-decoration:none; }}
.crumbs a:hover {{ text-decoration:underline; }}

/* Памятка по отсмотру. Стоит НАД видео, потому что именно здесь человек
   смотрит, и именно здесь методика имеет значение. Раньше гид существовал,
   но ссылок на него не было нигде -- найти можно было только зная адрес. */
/* Памятка -- ОТДЕЛЬНАЯ полоса во всю ширину, а не элемент флекс-строки.
   Вложенная в header-row, она вставала третьей колонкой между крошками и
   счётчиком онлайн, и шапка разъезжалась. */
.howto {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 18px;
          background:#16202a; border:1px solid #24323d;
          border-left:3px solid #5fb8c7; border-radius:7px;
          padding:9px 14px; margin:0 0 14px; font-size:13px; color:#98a6a3; }}
.howto b {{ color:#dfe8e6; font-weight:650; }}
.howto-head {{ display:flex; align-items:baseline; gap:10px; }}
.howto a {{ color:#5fb8c7; text-decoration:none; white-space:nowrap; }}
.howto a:hover {{ text-decoration:underline; }}
.howto .pts {{ display:flex; flex-wrap:wrap; gap:4px 18px; }}
.howto .pts span {{ white-space:nowrap; }}
/* Крестик удаления виден только при наведении на саму реплику: ряд
   постоянных крестиков превращает разговор в панель управления. */
.cmt-del {{ margin-left:auto; background:none; border:none; color:var(--c-dim);
  cursor:pointer; font-size:14px; line-height:1; padding:0 2px;
  opacity:0; transition:opacity .12s; }}
.cmt:hover .cmt-del, .cmt-del:focus {{ opacity:1; }}
.cmt-del:hover {{ color:#e0776a; }}
.cmt-text {{ font-size:13px; line-height:1.5; color:var(--c-ink);
  white-space:pre-wrap; word-break:break-word; }}

/* Поле ввода неяркое по умолчанию: акцентом рамка загорается только
   когда человек в него встал. */
.cmt-form {{ display:flex; flex-direction:column; align-items:stretch; gap:7px; }}
.cmt-form textarea {{ background:var(--c-card); color:var(--c-ink);
  border:1px solid var(--c-line); border-radius:7px; padding:8px 10px;
  font-family:inherit; font-size:13px; line-height:1.45;
  resize:vertical; min-height:38px; }}
.cmt-form textarea::placeholder {{ color:var(--c-dim); }}
.cmt-form textarea:focus {{ outline:none; border-color:var(--c-accent); }}
.cmt-actions {{ display:flex; align-items:center; gap:11px; }}
/* Кнопка тихая, и пока писать нечего -- неактивна, а не молча
   проглатывает пустую отправку. */
.cmt-form button {{ font-size:12px; padding:5px 13px; border-radius:6px;
  border:1px solid var(--c-line); background:transparent; color:var(--c-soft);
  cursor:pointer; font-family:inherit; white-space:nowrap; }}
.cmt-form button:hover:not(:disabled) {{ border-color:var(--c-accent);
  color:var(--c-accent); }}
.cmt-form button:disabled {{ opacity:.4; cursor:default; }}
.cmt-hint {{ font-size:11px; color:var(--c-dim); }}
.cmt-locked {{ font-size:12px; color:var(--c-dim); line-height:1.5;
  border-left:2px solid var(--c-line); padding:2px 0 2px 10px; }}
.empty {{ color:#777; font-size:13px; }}
.ai-scene-badge {{ font-size:10px; padding:2px 6px; border-radius:4px; color:#fff; white-space:nowrap; }}
.ai-scene-badge.model {{ background:#5533aa; }}
.ai-scene-badge.color {{ background:#aa7a00; }}
.section-sep {{ border:none; border-top:1px solid #2a2a2a; margin:18px 0; }}
</style></head>
<body>
<div class="header-row">
  <p class="crumbs">{crumbs}</p>
  <span class="online-indicator"><span class="online-dot"></span><span id="online-count">—</span> онлайн</span>
</div>
<h1>{short_name}</h1>
<div class="howto">
  <span class="howto-head"><b>Как смотреть</b><a href="/guide" target="_blank">полная методика →</a></span>
  <span class="pts">
    <span>темп <b>не быстрее 0.5×</b></span>
    <span>делить кадр на <b>9 секторов</b></span>
    <span>смена каждые <b>20–30 мин</b></span>
    <span>сомнительное <b>отмечать всегда</b></span>
  </span>
</div>
{processing_banner}

<div class="layout">
  <div class="video-col">
    <div class="video-wrap" id="video-wrap">
      <!-- Видео и слой разметки лежат в ОДНОЙ сцене и масштабируются
           вместе. Раньше <svg> был соседом <video>, и нативная кнопка
           полного экрана разворачивала только видео: слой разметки
           оставался в обычном документе, поэтому в полном экране рамки
           пропадали, а рисовать было нечем. -->
      <div id="stage">
        <video id="video" controls controlsList="nofullscreen"
               disablePictureInPicture src="/report/{report_id}/video"></video>
        <svg id="overlay"></svg>
        <!-- Ловушка кликов для рисования. Не достаёт до нативной полосы
             управления, поэтому кнопки плеера остаются нажимаемыми и при
             включённой разметке. -->
        <!-- Слой для кликов по кадру: клик -- пауза, двойной -- полный
             экран. Отдельный слой, а не сам <video>, потому что клики по
             нативной полосе управления приходят на тот же элемент, и
             нажатие на громкость заодно ставило бы видео на паузу.
             Этот слой до полосы не достаёт. -->
        <div id="click-catch"></div>
        <div id="draw-catch"></div>
      </div>
      <!-- Форма заметки живёт ВНУТРИ обёртки видео, а не под плеером.
           Под плеером её не видно в полноэкранном режиме: нарисовать рамку
           можно, а заполнить заметку нечем -- на этом и споткнулся
           пользователь.

           Снаружи #stage, а не внутри: иначе зум масштабировал бы и саму
           форму вместе с кадром. -->
      <div id="draw-form" hidden></div>

    </div>

    <!-- Легенда под видео, а не поверх кадра. Кнопки масштаба убраны с
         самого кадра: они отнимали угол картинки, ради которой всё и
         затевалось. Значит про масштаб и клавиши надо сказать здесь,
         иначе о них никто не узнает. -->
    <div class="legend">
      <span><b>колесо мыши</b> — масштаб</span>
      <span><b>тянуть мышью</b> — двигать увеличенный кадр</span>
      <span><b>пробел</b> — пауза</span>
      <span><b>←/→</b> — ±5 с, с Shift ±10</span>
      <span><b>Ctrl+←/→</b> — на кадр назад/вперёд</span>
      <span><b>+ &minus; 0</b> — масштаб с клавиатуры</span>
      <span><b>M</b> — разметка</span>
      <span><b>F</b> или <b>двойной клик</b> — во весь экран</span>
      <span><b>Esc</b> — отменить рамку</span>
      <!-- Плеер по умолчанию играет лёгкую копию. Человек должен видеть,
           что смотрит именно её, и уметь переключиться на оригинал --
           иначе сжатие превращается в тихое ухудшение инструмента. -->
      <label class="src-switch" id="src-switch" hidden>
        <input type="checkbox" id="use-original"> оригинал
        <span class="src-note" id="src-note"></span>
      </label>
    </div>

    <div class="toolbar">
      <button id="draw-toggle">🖊 Режим разметки: выкл</button>
      <span class="hint">включите режим и потяните мышью по видео, чтобы отметить область</span>
    </div>
    <div class="toolbar">
      <label style="display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer;">
        <input type="checkbox" id="ai-toggle"> показывать находки модели (<span id="ai-count">0</span>)
      </label>
      <span class="hint" style="color:#7a5cff;">— рамки на видео и список сцен справа. Снимите галку,
        чтобы смотреть своими глазами, без подсказок модели</span>
    </div>


    <div class="covbar" id="covbar"></div>
    <div class="cov-stat" id="cov-stat">Загрузка статистики просмотра...</div>
  </div>

  <div class="obs-col">
    <div id="ai-scenes-block">
      <h1 style="font-size:14px; margin-top:0;">Сцены (модель) (<span id="ai-scenes-count">0</span>)</h1>
      <div id="ai-scenes-list"><div class="empty">Загрузка...</div></div>
      <hr class="section-sep">
    </div>
    <h1 style="font-size:14px;">Наблюдения (<span id="obs-count">0</span>)</h1>
    <div id="obs-list"><div class="empty">Загрузка...</div></div>
  </div>
</div>

<script>
const reportId = "{report_id}";
// имя текущего зрителя -- чтобы показать кнопку удаления только у СВОИХ
// сообщений (это удобство, а не защита: аккаунтов в системе нет)
const VIEWER_NAME = {viewer_name_js};
// может ли этот посетитель писать в обсуждениях (и почему нет, если нет)
const CAN_COMMENT = {can_comment_js};
const COMMENT_LOCK_REASON = {comment_lock_js};
const IS_MODERATOR = {is_moderator_js};
let isProcessing = {is_processing_js};
const video = document.getElementById('video');
const overlay = document.getElementById('overlay');
const drawCatch = document.getElementById('draw-catch');
const drawToggle = document.getElementById('draw-toggle');
let drawMode = false;
let observations = [];
let drawing = null; // {{startX, startY, rectEl}}
let pendingBox = null; // нормализованный bbox, ждущий сохранения формы

// --- режим разметки: включение/выключение ---
drawToggle.addEventListener('click', () => {{
  drawMode = !drawMode;
  drawCatch.classList.toggle('on', drawMode);
  syncClickCatch();
  // Снимаем фокус с кнопки: иначе она остаётся "нажимаемой пробелом" и
  // перехватывает его у видео.
  drawToggle.blur();
  drawToggle.textContent = '🖊 Режим разметки: ' + (drawMode ? 'вкл' : 'выкл');
  drawToggle.classList.toggle('active', drawMode);
}});

// Размер слоя разметки в ЕГО СОБСТВЕННЫХ координатах.
//
// getBoundingClientRect() возвращает размер НА ЭКРАНЕ, то есть уже
// умноженный на масштаб сцены. А SVG рисует в своих непреобразованных
// единицах. Если смешать одно с другим, при любом зуме рамки уезжают:
// экранные координаты попадают в SVG как есть.
// --- лёгкая копия или оригинал -------------------------------------------
//
// Плеер по умолчанию играет лёгкую копию: оригинал идёт на 30 Мбит/с, и
// столько нужно каждому зрителю через один канал наружу.
//
// Переключатель обязателен. Сжатие, которое нельзя обойти, -- это тихое
// ухудшение инструмента: человек не знает, что смотрит копию, и не может
// проверить сомнительное место на оригинале.
async function initVideoSource() {{
  let info;
  try {{
    const res = await fetch(`/api/report/${{reportId}}/video_info`);
    info = await res.json();
  }} catch (e) {{
    console.warn('не удалось узнать про лёгкую копию', e);
    return;
  }}
  if (!info.proxy) return;      // копии ещё нет -- играет оригинал, молчим

  const box = document.getElementById('src-switch');
  const note = document.getElementById('src-note');
  const cb = document.getElementById('use-original');
  note.textContent = `(копия ${{info.proxy_mb}} МБ вместо ${{info.original_mb}} МБ)`;
  box.hidden = false;

  cb.addEventListener('change', () => {{
    // Место в видео сохраняем: переключение источника не должно
    // отбрасывать человека в начало -- он смотрит конкретный момент.
    const at = video.currentTime;
    const wasPlaying = !video.paused;
    video.src = `/report/${{reportId}}/video` + (cb.checked ? '?original=1' : '');
    video.addEventListener('loadedmetadata', () => {{
      video.currentTime = at;
      if (wasPlaying) video.play();
    }}, {{ once: true }});
  }});
}}
initVideoSource();

// --- клавиатура ----------------------------------------------------------
//
// Пробел не работал: нативные горячие клавиши <video> действуют, только
// когда фокус на самом видео, а человек его туда не ставит -- он кликает
// по странице, по комментарию, по списку наблюдений. Поэтому слушаем на
// документе.
//
// Главное условие: горячие клавиши НЕ должны срабатывать, когда человек
// печатает. Пробел посреди комментария обязан ставить пробел, а не паузу.
// Поля, где НЕЛЬЗЯ перехватывать клавиши: там человек набирает текст.
//
// Проверять просто "это INPUT" оказалось неверно. Флажок "оригинал" --
// тоже <input>, и после клика по нему фокус остаётся на флажке; все
// горячие клавиши разом переставали работать, включая M и пробел.
// Симптом выглядел как "разметка по M не работает", хотя дело было
// в фокусе.
//
// Набирают текст только текстовые поля. У флажка, переключателя, ползунка
// и кнопки свои клавиши (пробел, стрелки), но они не текст, и отбирать у
// плеера управление из-за них незачем.
const TEXT_INPUT_TYPES = new Set([
  'text', 'search', 'url', 'tel', 'email', 'password', 'number',
  'date', 'time', 'datetime-local', 'month', 'week',
]);

function typingNow() {{
  const el = document.activeElement;
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag !== 'INPUT') return false;
  // у <input> без type по умолчанию text
  return TEXT_INPUT_TYPES.has((el.type || 'text').toLowerCase());
}}

// Длительность одного кадра. Покадровый шаг -- это перемотка ровно на
// неё: отдельного "шага" у <video> нет.
const VIDEO_FPS = {fps_js} || 30;
const FRAME_SEC = 1 / VIDEO_FPS;

function stepFrame(direction) {{
  // Пока идёт предыдущая перемотка, новую не начинаем.
  //
  // Иначе быстрые нажатия ставят перемотки одну на другую, и <video>
  // может застрять в состоянии seeking: тогда перестают работать не
  // только шаги, но и пауза с воспроизведением -- элемент не отвечает
  // ни нам, ни собственным кнопкам браузера. Именно так это и выглядело.
  if (video.seeking) return;

  // Без загруженных метаданных перематывать некуда: длительность ещё
  // не известна, и любое значение будет наугад.
  if (!video.duration || !isFinite(video.duration)) return;

  video.pause();

  let at = video.currentTime + direction * FRAME_SEC;
  at = Math.max(0, Math.min(video.duration, at));
  if (!isFinite(at)) return;

  // Не выходим за пределы того, что браузер вообще может перемотать:
  // у частично загруженного файла это не весь ролик.
  if (video.seekable && video.seekable.length) {{
    const lo = video.seekable.start(0);
    const hi = video.seekable.end(video.seekable.length - 1);
    at = Math.max(lo, Math.min(hi, at));
  }}

  try {{
    video.currentTime = at;
  }} catch (e) {{
    // Не глухой catch: если перемотка не удалась, это надо видеть, а не
    // гадать, почему кнопка "не работает".
    console.warn('покадровый шаг не удался', e);
    return;
  }}

  // На паузе timeupdate не приходит, а рамки рисуются по нему -- без
  // явной перерисовки они застынут на прежнем кадре.
  video.addEventListener('seeked', renderVisibleObservations, {{ once: true }});
}}

function nudge(seconds) {{
  video.currentTime = Math.max(
    0, Math.min(video.duration || 0, video.currentTime + seconds));
}}

// Пробел обрабатывается ОТДЕЛЬНО и в фазе перехвата -- раньше всех
// остальных. Причина: пробел -- это ещё и "нажать кнопку в фокусе". Нажав
// кнопку "Режим разметки" мышью, человек оставляет на ней фокус, и
// следующий пробел переключал режим вместо паузы. То же с любой другой
// кнопкой панели.
//
// Единственное исключение -- когда человек печатает: пробел посреди
// заметки обязан ставить пробел.
document.addEventListener('keydown', e => {{
  if (e.key !== ' ' && e.code !== 'Space') return;
  if (typingNow() || e.ctrlKey || e.metaKey || e.altKey) return;
  e.preventDefault();
  e.stopPropagation();
  togglePlayback();
}}, true);

function togglePlayback() {{
  if (!video.paused) {{ video.pause(); return; }}
  // play() возвращает обещание, которое браузер может отклонить -- чаще
  // всего когда элемент занят перемоткой. Без обработки отказ уходит в
  // никуда, и со стороны это выглядит как "кнопка не работает".
  const started = video.play();
  if (started && started.catch) {{
    started.catch(err => console.warn('воспроизведение не началось', err));
  }}
}}

document.addEventListener('keydown', e => {{
  if (typingNow()) return;

  // Покадрово -- Ctrl со стрелками. Обрабатывается ДО общей проверки
  // модификаторов: она отсекает сочетания браузера, а это как раз
  // сочетание, и без отдельной ветки оно бы туда не дошло.
  if ((e.ctrlKey || e.metaKey) && !e.altKey &&
      (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {{
    e.preventDefault();
    stepFrame(e.key === 'ArrowRight' ? 1 : -1);
    return;
  }}
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  switch (e.key) {{
    case 'ArrowLeft':
      e.preventDefault(); nudge(e.shiftKey ? -10 : -5); return;
    case 'ArrowRight':
      e.preventDefault(); nudge(e.shiftKey ? 10 : 5); return;
    case 'f': case 'F': case 'а': case 'А':
      e.preventDefault(); toggleFullscreen(); return;
    case 'm': case 'M': case 'ь': case 'Ь':
      e.preventDefault(); drawToggle.click(); return;
    case '+': case '=':
      e.preventDefault(); zoomCentre(1.4); return;
    case '-': case '_':
      e.preventDefault(); zoomCentre(1 / 1.4); return;
    case '0':
      e.preventDefault(); vz.scale = 1; vz.x = 0; vz.y = 0; applyStage(); return;
    case 'Escape':
      // Бросаем и недорисованную рамку, и уже нарисованную с открытой
      // формой -- фокус мог быть где угодно, а Esc должен работать всегда.
      // Из полного экрана браузер выходит сам.
      if (drawing) {{ drawing.rectEl.remove(); drawing = null; }}
      if (pendingBox) cancelDraw();
      return;
  }}
}});

// --- масштаб видео и полный экран ----------------------------------------
//
// Оба бага, о которых сообщил пользователь, растут из одного места:
// нативная кнопка полного экрана разворачивает САМ <video>, а слой
// разметки -- его сосед. В полном экране слой оставался в обычном
// документе: рамки пропадали, рисовать было нечем. Зума же в плеере не
// было вовсе -- ни в окне, ни в полном экране.
//
// Поэтому: видео и слой лежат в одной сцене (#stage), масштабируется
// сцена целиком, а в полный экран уходит ОБЁРТКА, внутри которой оба.
const videoWrap = document.getElementById('video-wrap');
const stage = document.getElementById('stage');
let vz = {{ scale: 1, x: 0, y: 0, drag: null, pinch: 0 }};

function applyStage() {{
  // Не даём утащить кадр за края: увеличенное видео легко "потерять" и
  // смотреть в чёрное поле, не понимая, куда всё делось.
  const w = videoWrap.clientWidth, h = stage.clientHeight;
  const maxX = 0, minX = w - w * vz.scale;
  const maxY = 0, minY = h - h * vz.scale;
  vz.x = Math.min(maxX, Math.max(minX, vz.x));
  vz.y = Math.min(maxY, Math.max(minY, vz.y));
  if (vz.scale === 1) {{ vz.x = 0; vz.y = 0; }}
  stage.style.transform =
    `translate(${{vz.x}}px, ${{vz.y}}px) scale(${{vz.scale}})`;
  // Слой разметки узнаёт масштаб, чтобы делить на него размер подписей:
  // иначе при 8x шрифт в 14px становится 112px и закрывает пол-кадра.
  overlay.style.setProperty('--zoom', vz.scale);
  // Тянуть за кадр можно только когда есть что тянуть и когда мы не
  // рисуем: в режиме разметки перетаскивание -- это рисование рамки.
  videoWrap.style.cursor =
    (vz.scale > 1 && !drawMode) ? (vz.drag ? 'grabbing' : 'grab') : '';
  renderVisibleObservations();
  // Рамка при зуме едет -- форма должна ехать за ней, иначе она укажет
  // не на то место.
  placeDrawForm();
}}

function zoomAt(factor, clientX, clientY) {{
  const before = vz.scale;
  const next = Math.min(8, Math.max(1, before * factor));
  if (next === before) return;
  const r = videoWrap.getBoundingClientRect();
  // Точка под курсором остаётся на месте -- иначе при увеличении уезжает
  // ровно то, что человек хотел рассмотреть.
  const cx = (clientX - r.left - vz.x) / before;
  const cy = (clientY - r.top - vz.y) / before;
  vz.scale = next;
  vz.x = clientX - r.left - cx * next;
  vz.y = clientY - r.top - cy * next;
  applyStage();
}}

function zoomCentre(factor) {{
  const r = videoWrap.getBoundingClientRect();
  zoomAt(factor, r.left + r.width / 2, r.top + r.height / 2);
}}

videoWrap.addEventListener('wheel', e => {{
  e.preventDefault();
  zoomAt(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX, e.clientY);
}}, {{ passive: false }});

// Перетаскивание кадра. Слушаем на обёртке, а рисование -- на слое
// разметки, поэтому в режиме разметки сюда просто не доходит: слой
// перехватывает событие первым.
videoWrap.addEventListener('mousedown', e => {{
  if (e.button !== 0 || vz.scale <= 1 || drawMode) return;
  if (e.target.closest('.vid-tools')) return;
  vz.drag = {{ x: e.clientX, y: e.clientY }};
  applyStage();
}});
document.addEventListener('mousemove', e => {{
  if (!vz.drag) return;
  vz.x += e.clientX - vz.drag.x;
  vz.y += e.clientY - vz.drag.y;
  vz.drag = {{ x: e.clientX, y: e.clientY }};
  applyStage();
}});
document.addEventListener('mouseup', () => {{
  if (!vz.drag) return;
  vz.drag = null;
  applyStage();
}});

// Щипок на тач-экране.
videoWrap.addEventListener('touchmove', e => {{
  if (e.touches.length !== 2) return;
  e.preventDefault();
  const dx = e.touches[0].clientX - e.touches[1].clientX;
  const dy = e.touches[0].clientY - e.touches[1].clientY;
  const dist = Math.hypot(dx, dy);
  if (vz.pinch) zoomAt(dist / vz.pinch,
                        (e.touches[0].clientX + e.touches[1].clientX) / 2,
                        (e.touches[0].clientY + e.touches[1].clientY) / 2);
  vz.pinch = dist;
}}, {{ passive: false }});
videoWrap.addEventListener('touchend', () => {{ vz.pinch = 0; }});

// --- полный экран ---
function toggleFullscreen() {{
  if (document.fullscreenElement) {{
    document.exitFullscreen();
  }} else if (videoWrap.requestFullscreen) {{
    videoWrap.requestFullscreen().catch(err => {{
      console.warn('полный экран не открылся', err);
    }});
  }}
}}
// Двойной клик по кадру -- во весь экран и обратно. Привычный жест, и он
// не требует целиться в маленькую кнопку.
//
// В режиме разметки не срабатывает: там протягивание мышью рисует рамку,
// и двойной клик легко получается случайно. Ловушка кликов перекрывает
// кадр и до этого обработчика событие просто не доходит, но проверку
// оставляем явной -- она объясняет намерение.
// Клик по кадру -- пауза, двойной -- полный экран.
//
// Chrome не переключает воспроизведение по клику на встроенное в страницу
// видео: это делают своими руками все плееры, где такое поведение есть.
// Ожидание при этом совершенно естественное, поэтому делаем и мы.
//
// Одиночное действие откладывается на четверть секунды и отменяется, если
// пришёл двойной клик. Иначе двойной клик успевал бы дважды дёрнуть
// воспроизведение по дороге к полному экрану -- заметно и раздражает.
const clickCatch = document.getElementById('click-catch');
let clickTimer = null;

clickCatch.addEventListener('click', () => {{
  if (clickTimer) return;                 // второй клик пары -- ждём dblclick
  clickTimer = setTimeout(() => {{
    clickTimer = null;
    togglePlayback();
  }}, 250);
}});

clickCatch.addEventListener('dblclick', e => {{
  e.preventDefault();
  if (clickTimer) {{ clearTimeout(clickTimer); clickTimer = null; }}
  toggleFullscreen();
}});

// В режиме разметки слой кликов убираем: там протягивание мышью рисует
// рамку, и ловушка разметки должна получать события первой.
function syncClickCatch() {{ clickCatch.classList.toggle('off', drawMode); }}

document.addEventListener('fullscreenchange', () => {{
  // Штатная кнопка плеера разворачивает САМ <video>, а слой разметки --
  // его сосед, и он остаётся в обычном документе: рамки пропадают, рисовать
  // нечем. Поэтому ловим момент и разворачиваем обёртку целиком.
  if (document.fullscreenElement === video) {{
    // Сюда попадаем, только если браузер всё-таки развернул само видео
    // мимо нашей кнопки. Чинить это на лету нельзя: requestFullscreen
    // требует свежего действия пользователя, а оно к этому моменту уже
    // истекло, и запрос молча отклоняется. Поэтому просто выходим и
    // говорим вслух -- молчаливый отказ выглядит как сломанная кнопка.
    console.warn('видео развернулось без слоя разметки, выхожу из полного экрана');
    document.exitFullscreen().catch(e => console.warn('выход не удался', e));
    return;
  }}
  // Размер сцены сменился -- пересчитываем рамки, иначе они останутся
  // нарисованными по старому размеру кадра.
  vz.scale = 1; vz.x = 0; vz.y = 0;
  applyStage();
}});

function overlaySize() {{
  // Поля называются width/height НЕ случайно: эта функция заменила собой
  // overlay.getBoundingClientRect(), и весь код отрисовки читает именно
  // rect.width / rect.height.
  //
  // Первая версия возвращала {{w, h}} -- и отрисовка молча получала
  // undefined, координата становилась NaN, а рамка не появлялась ВООБЩЕ
  // НИКОГДА. Ошибка не бросается и в консоль не пишет: setAttribute
  // спокойно принимает "NaN".
  return {{ width: overlay.clientWidth, height: overlay.clientHeight }};
}}

function overlayPoint(evt) {{
  const rect = overlay.getBoundingClientRect();
  const size = overlaySize();
  // фактический масштаб берём из отношения экранного размера к
  // собственному -- так он верен и при зуме, и в полном экране, и при
  // любом будущем преобразовании сцены
  const kx = rect.width ? size.width / rect.width : 1;
  const ky = rect.height ? size.height / rect.height : 1;
  return {{
    x: Math.max(0, Math.min(size.width, (evt.clientX - rect.left) * kx)),
    y: Math.max(0, Math.min(size.height, (evt.clientY - rect.top) * ky)),
    w: size.width, h: size.height,
  }};
}}

drawCatch.addEventListener('mousedown', e => {{
  if (!drawMode) return;
  video.pause();  // фиксируем таймкод на момент начала разметки, не даём ему уехать
  const p = overlayPoint(e);
  const rectEl = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rectEl.setAttribute('class', 'temp-box');
  overlay.appendChild(rectEl);
  drawing = {{ startX: p.x, startY: p.y, rectEl, w: p.w, h: p.h, atTime: video.currentTime }};
}});

document.addEventListener('mousemove', e => {{
  if (!drawing) return;
  const p = overlayPoint(e);
  const x = Math.min(drawing.startX, p.x), y = Math.min(drawing.startY, p.y);
  const w = Math.abs(p.x - drawing.startX), h = Math.abs(p.y - drawing.startY);
  drawing.rectEl.setAttribute('x', x); drawing.rectEl.setAttribute('y', y);
  drawing.rectEl.setAttribute('width', w); drawing.rectEl.setAttribute('height', h);
  drawing.lastX = p.x; drawing.lastY = p.y;
}});

document.addEventListener('mouseup', () => {{
  if (!drawing) return;
  const x1 = Math.min(drawing.startX, drawing.lastX ?? drawing.startX);
  const y1 = Math.min(drawing.startY, drawing.lastY ?? drawing.startY);
  const x2 = Math.max(drawing.startX, drawing.lastX ?? drawing.startX);
  const y2 = Math.max(drawing.startY, drawing.lastY ?? drawing.startY);
  if (x2 - x1 < 5 || y2 - y1 < 5) {{
    // слишком маленький бокс -- скорее всего случайный клик, не сохраняем
    drawing.rectEl.remove();
    drawing = null;
    return;
  }}
  pendingBox = {{
    bbox: [x1 / drawing.w, y1 / drawing.h, x2 / drawing.w, y2 / drawing.h],
    timestamp_sec: drawing.atTime,
    rectEl: drawing.rectEl,
  }};
  drawing = null;
  showDrawForm();
}});

// Варианты статуса берём из того же словаря, что и остальной интерфейс:
// свой список тут неминуемо разошёлся бы с серверным при первой правке.
//
// Считается ПРИ ОТКРЫТИИ формы, а не один раз при загрузке: PRIORITY_LABELS
// объявлен ниже по файлу, и обращение к нему на верхнем уровне падает
// с ReferenceError -- const в temporal dead zone. Уронило бы весь скрипт
// плеера целиком.
function priorityOptions() {{
  return Object.keys(PRIORITY_LABELS)
    .map(v => `<option value="${{v}}">${{PRIORITY_LABELS[v]}}</option>`).join('');
}}

function showDrawForm() {{
  const form = document.getElementById('draw-form');
  form.innerHTML = `
    <div class="draw-form">
      <div class="ttl">Новая пометка</div>
      <input id="obs-label-input" placeholder="Что это? (человек, палатка, рюкзак…)">
      <textarea id="obs-note-input" placeholder="Заметка (необязательно)"></textarea>
      <!-- Статус ставится сразу, а не отдельным заходом в список пометок.
           Человек в момент разметки уже знает, насколько он уверен: заставлять
           его возвращаться к этому позже значит терять оценку -- именно так
           половина пометок и оставалась без статуса. -->
      <select id="obs-priority-input">${{priorityOptions()}}</select>
      <div class="row">
        <button class="btn-save" onclick="saveObservation()">Сохранить</button>
        <button class="btn-cancel" onclick="cancelDraw()">Отмена</button>
      </div>
      <span class="hint">Enter — сохранить, Esc — отменить</span>
    </div>`;
  form.hidden = false;

  // Ловушку кликов на время формы выключаем: пока она включена, клики по
  // полям формы до них не доходят -- прозрачный слой перехватывает всё.
  drawCatch.classList.remove('on');

  placeDrawForm();
  const input = document.getElementById('obs-label-input');
  input.focus();

  // Enter в однострочном поле -- сохранить. В заметке Enter оставляет
  // перенос строки: пометки бывают в несколько предложений, поэтому там
  // работает Ctrl+Enter.
  input.addEventListener('keydown', e => {{
    if (e.key === 'Enter') {{ e.preventDefault(); saveObservation(); }}
    if (e.key === 'Escape') {{ e.preventDefault(); cancelDraw(); }}
  }});
  document.getElementById('obs-note-input').addEventListener('keydown', e => {{
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{
      e.preventDefault(); saveObservation();
    }}
    if (e.key === 'Escape') {{ e.preventDefault(); cancelDraw(); }}
  }});
}}

function placeDrawForm() {{
  // Ставим форму рядом с нарисованной рамкой, но так, чтобы она не уехала
  // за край кадра и не накрыла саму рамку -- иначе человек не видит того,
  // что описывает.
  const form = document.getElementById('draw-form');
  if (form.hidden || !pendingBox) return;
  const wrap = videoWrap.getBoundingClientRect();
  const box = pendingBox.rectEl.getBoundingClientRect();
  const gap = 10;

  let left = box.right - wrap.left + gap;
  if (left + form.offsetWidth > wrap.width - gap) {{
    left = box.left - wrap.left - form.offsetWidth - gap;   // слева от рамки
  }}
  left = Math.max(gap, Math.min(left, wrap.width - form.offsetWidth - gap));

  let top = box.top - wrap.top;
  top = Math.max(gap, Math.min(top, wrap.height - form.offsetHeight - gap));

  form.style.left = left + 'px';
  form.style.top = top + 'px';
}}

function hideDrawForm() {{
  const form = document.getElementById('draw-form');
  form.hidden = true;
  form.innerHTML = '';
  // Ловушку возвращаем только если режим разметки всё ещё включён:
  // человек мог выключить его, пока форма была открыта.
  drawCatch.classList.toggle('on', drawMode);
}}

function cancelDraw() {{
  if (pendingBox) pendingBox.rectEl.remove();
  pendingBox = null;
  hideDrawForm();
}}

async function saveObservation() {{
  if (!pendingBox) return;
  const label = document.getElementById('obs-label-input').value.trim();
  const note = document.getElementById('obs-note-input').value.trim();
  const btn = document.querySelector('#draw-form .btn-save');
  if (btn) btn.disabled = true;
  try {{
    const res = await fetch(`/api/report/${{reportId}}/observations`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        timestamp_sec: pendingBox.timestamp_sec, bbox: pendingBox.bbox, label, note,
        priority: document.getElementById('obs-priority-input').value,
      }}),
    }});
    if (!res.ok) throw new Error('сервер ответил ' + res.status);
  }} catch (e) {{
    // НЕ глухой catch: пометка -- это находка, и человек обязан узнать,
    // что она не сохранилась, а не думать, что отметил.
    console.warn('пометка не сохранена', e);
    alert('Не удалось сохранить пометку. Рамка и текст на месте, попробуйте ещё раз.');
    if (btn) btn.disabled = false;
    return;
  }}
  pendingBox.rectEl.remove();
  pendingBox = null;
  hideDrawForm();
  await loadPriorities();
  loadObservations();
}}

// --- отрисовка боксов: ручные наблюдения + опционально то, что нашла модель ---
let aiDetections = [];
let showAiBoxes = false;
const AI_BOX_COLOR = {{ model: '#7a5cff', color: '#ffb300' }};  // фиолетовый/жёлтый -- как в отчёте сцен

async function loadAiDetections() {{
  const res = await fetch(`/api/report/${{reportId}}/ai_detections`);
  aiDetections = await res.json();
  document.getElementById('ai-count').textContent = aiDetections.length;
  renderVisibleObservations();  // без этого новые данные приходят, но экран не перерисовывается,
                                 // пока видео не начнёт/не продолжит играть (перерисовка иначе висит
                                 // только на событии timeupdate, которое не срабатывает на паузе)
}}

function renderVisibleObservations() {{
  overlay.querySelectorAll('rect.obs-box, text.obs-label, rect.ai-box, text.ai-label').forEach(el => el.remove());
  const t = video.currentTime;
  // собственные координаты слоя, а не экранные -- см. overlaySize()
  const rect = overlaySize();

  // ручные наблюдения -- bbox уже нормализован (0..1), рисуем как есть
  for (const obs of observations) {{
    if (Math.abs(obs.timestamp_sec - t) > 1.5) continue;
    const [x1, y1, x2, y2] = obs.bbox;
    const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r.setAttribute('class', 'obs-box');
    r.setAttribute('x', x1 * rect.width); r.setAttribute('y', y1 * rect.height);
    r.setAttribute('width', (x2 - x1) * rect.width); r.setAttribute('height', (y2 - y1) * rect.height);
    overlay.appendChild(r);
    if (obs.label) {{
      const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      txt.setAttribute('class', 'obs-label');
      txt.setAttribute('x', x1 * rect.width);
      txt.setAttribute('y', Math.max(14, y1 * rect.height - 6));
      txt.textContent = obs.label;
      overlay.appendChild(txt);
    }}
  }}

  // рамки модели -- bbox в detections.json в АБСОЛЮТНЫХ пикселях исходного
  // кадра, нужно поделить на реальное разрешение видео, чтобы перевести
  // в те же доли (0..1), что и у ручных наблюдений
  if (showAiBoxes && video.videoWidth) {{
    for (const det of aiDetections) {{
      if (Math.abs(det.timestamp_sec - t) > 0.5) continue;
      const [x1, y1, x2, y2] = det.bbox;
      const fx1 = x1 / video.videoWidth, fy1 = y1 / video.videoHeight;
      const fx2 = x2 / video.videoWidth, fy2 = y2 / video.videoHeight;
      const color = AI_BOX_COLOR[det.source] || AI_BOX_COLOR.model;
      const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      r.setAttribute('class', 'ai-box');
      r.setAttribute('x', fx1 * rect.width); r.setAttribute('y', fy1 * rect.height);
      r.setAttribute('width', (fx2 - fx1) * rect.width); r.setAttribute('height', (fy2 - fy1) * rect.height);
      r.setAttribute('style', `fill:none; stroke:${{color}}; stroke-width:2; stroke-dasharray:3,3; vector-effect:non-scaling-stroke;`);
      overlay.appendChild(r);
      const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      txt.setAttribute('class', 'ai-label');
      txt.setAttribute('x', fx1 * rect.width);
      txt.setAttribute('y', Math.max(14, fy1 * rect.height - 6));
      txt.setAttribute('style', `fill:${{color}}; font-size:calc(12px / var(--zoom, 1)); font-weight:bold; paint-order:stroke; stroke:#000; stroke-width:calc(3px / var(--zoom, 1));`);
      txt.textContent = `${{det.object_class}} ${{(det.confidence*100).toFixed(0)}}%`;
      overlay.appendChild(txt);
    }}
  }}
}}
video.addEventListener('timeupdate', renderVisibleObservations);

// --- копирование произвольного текста + "вся телеметрия кадра" дропдауном
// (тот же паттерн, что и в report.html/save_outputs() в sar_video_review.py --
// два отдельных HTML-документа, поэтому helpers продублированы, а не
// импортированы) ---
function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function copyText(text, btn) {{
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try {{ document.execCommand('copy'); }} catch (e) {{}}
  document.body.removeChild(ta);
  if (navigator.clipboard) {{ navigator.clipboard.writeText(text).catch(() => {{}}); }}
  if (btn) {{ const old = btn.textContent; btn.textContent = 'скопировано ✓'; setTimeout(() => btn.textContent = old, 1200); }}
}}

function renderRawTelemetryDropdown(rawText) {{
  if (!rawText) return '';
  return `
    <details class="raw-telemetry">
      <summary>📡 Вся телеметрия кадра (раскрыть — скопируется автоматически)</summary>
      <pre>${{escapeHtml(rawText)}}</pre>
      <button class="copybtn raw-copy-btn" type="button">📋 копировать</button>
    </details>`;
}}

// делегированные обработчики -- один раз на document, переживают
// innerHTML-перерисовку списков. 'toggle' у <details> не всплывает --
// обязательна фаза перехвата (true)
document.addEventListener('toggle', (e) => {{
  if (e.target.matches && e.target.matches('details.raw-telemetry') && e.target.open) {{
    const pre = e.target.querySelector('pre');
    if (pre) copyText(pre.textContent, null);
  }}
}}, true);
document.addEventListener('click', (e) => {{
  const btn = e.target.closest('.raw-copy-btn');
  if (btn) {{
    const pre = btn.closest('details').querySelector('pre');
    if (pre) copyText(pre.textContent, btn);
  }}
}});

// --- список наблюдений сбоку ---
function fmtTime(sec) {{
  sec = Math.round(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${{m}}:${{s.toString().padStart(2, '0')}}`;
}}

async function loadObservations() {{
  const res = await fetch(`/api/report/${{reportId}}/observations`);
  observations = await res.json();
  document.getElementById('obs-count').textContent = observations.length;
  const list = document.getElementById('obs-list');
  if (observations.length === 0) {{
    setCardsHtml(list, '<div class="empty">Пока нет отметок. Включите режим разметки и выделите область на видео.</div>');
  }} else {{
    setCardsHtml(list, observations.map(o => {{
      const gps = (o.lat !== null && o.lat !== undefined)
        ? `<div class="obs-gps">📍 дрон: ${{o.lat.toFixed(6)}}, ${{o.lon.toFixed(6)}}
           <a href="https://www.google.com/maps?q=${{o.lat}},${{o.lon}}" target="_blank" rel="noopener">🗺 карта</a></div>` : '';
      const estGps = (o.est_lat !== null && o.est_lat !== undefined)
        ? `<div class="obs-gps est">🎯 вероятные координаты объекта: ${{o.est_lat.toFixed(6)}}, ${{o.est_lon.toFixed(6)}}
           <a href="https://www.google.com/maps?q=${{o.est_lat}},${{o.est_lon}}" target="_blank" rel="noopener">🗺 карта</a></div>` : '';
      return `
      <div class="obs-item">
        <div class="obs-head">
          <span class="obs-time" onclick="jumpTo(${{o.timestamp_sec}})">▶ ${{fmtTime(o.timestamp_sec)}}</span>
          <span class="obs-author">${{o.viewer_name}}</span>
          <span class="obs-when">${{fmtStamp(o.created_at)}}</span>
          <button class="obs-link" onclick="copyFindingLink(event, ${{o.id}}, ${{o.timestamp_sec}})"
            title="Скопировать ссылку на находку">🔗</button>
          <a class="obs-frame" href="/finding/${{o.id}}/" target="_blank" rel="noopener"
            title="Открыть кадр находки">🖼</a>
          <button class="obs-del" onclick="deleteObservation(${{o.id}})"
            title="Удалить наблюдение целиком">🗑</button>
        </div>
        ${{o.label ? `<div class="obs-label">${{o.label}}</div>` : ''}}
        ${{o.note ? `<div class="obs-note">${{o.note}}</div>` : ''}}
        ${{gps}}
        ${{estGps}}
        ${{renderRawTelemetryDropdown(o.raw_telemetry)}}
        ${{renderPriorityControl('manual', o.id)}}
        ${{renderComments('manual', o.id)}}
      </div>`;
    }}).join(''));
  }}
  renderVisibleObservations();
}}

// --- список сцен, найденных моделью/цветовым детектором (справа, кликабельно) ---
const AI_SCENE_LABEL = {{ model: 'МОДЕЛЬ', color: 'ЦВЕТ' }};

async function loadAiScenes() {{
  let scenes;
  try {{
    const res = await fetch(`/api/report/${{reportId}}/ai_scenes`);
    scenes = await res.json();
  }} catch (e) {{ return; }}
  const list = document.getElementById('ai-scenes-list');
  document.getElementById('ai-scenes-count').textContent = scenes.length;
  if (!scenes.length) {{
    setCardsHtml(list, '<div class="empty">Модель пока ничего не нашла.</div>');
    return;
  }}
  setCardsHtml(list, scenes.map(s => {{
    const droneLine = (s.drone_lat !== null && s.drone_lat !== undefined)
      ? `<div class="obs-gps">📍 дрон: ${{s.drone_lat.toFixed(6)}}, ${{s.drone_lon.toFixed(6)}} `
        + `<a href="https://www.google.com/maps?q=${{s.drone_lat}},${{s.drone_lon}}" target="_blank" rel="noopener">🗺 карта</a></div>`
      : '';
    const estLine = (s.est_lat !== null && s.est_lat !== undefined)
      ? `<div class="obs-gps est">🎯 вероятные координаты объекта: ${{s.est_lat.toFixed(6)}}, ${{s.est_lon.toFixed(6)}} `
        + `<a href="https://www.google.com/maps?q=${{s.est_lat}},${{s.est_lon}}" target="_blank" rel="noopener">🗺 карта</a></div>`
      : '';
    return `
    <div class="obs-item">
      <div class="obs-head">
        <span class="obs-time" onclick="jumpTo(${{s.time_start_sec}})">▶ ${{fmtTime(s.time_start_sec)}}</span>
        <span class="ai-scene-badge ${{s.source}}">${{AI_SCENE_LABEL[s.source] || 'МОДЕЛЬ'}}</span>
      </div>
      <div class="obs-label">${{s.object_class}} · ${{(s.confidence * 100).toFixed(0)}}%</div>
      <div class="obs-note">${{s.count}} кадр(ов) · ${{fmtTime(s.time_start_sec)}}–${{fmtTime(s.time_end_sec)}}</div>
      ${{droneLine}}
      ${{estLine}}
      ${{renderRawTelemetryDropdown(s.raw_telemetry)}}
      ${{renderPriorityControl('ai_scene', s.ref_key)}}
      ${{renderComments('ai_scene', s.ref_key)}}
    </div>`;
  }}).join(''));
}}

// --- ссылка на находку -----------------------------------------------------
//
// Адрес абсолютный и приходит с сервера, а не собирается из location:
// платформа живёт за быстрым туннелем, и его имя меняется при каждом падении
// канала. Ссылка из location была бы верна только пока открыта эта вкладка.
// Внешний адрес спрашиваем у сервера, а не подставляем в страницу:
// туннель меняет имя при каждом падении канала, и вкладка, открытая до
// падения, продолжила бы копировать мёртвые ссылки. Один запрос на
// загрузку страницы, дальше держим в памяти.
let EXTERNAL_BASE = '';
let BOT_NAME = '';
fetch('/api/external_base')
  .then(r => r.json())
  .then(d => {{ EXTERNAL_BASE = d.base || ''; BOT_NAME = d.bot || ''; }})
  .catch(() => {{}});

function findingLink(obsId, seconds) {{
  // См. страницу операции: вечная ссылка -- через бота, потому что имя
  // туннеля перестаёт существовать при каждом его перезапуске.
  if (obsId && BOT_NAME) return `https://t.me/${{BOT_NAME}}?start=finding_${{obsId}}`;
  const base = EXTERNAL_BASE || location.origin;
  if (obsId) return `${{base}}/finding/${{obsId}}/`;
  const t = (seconds !== null && seconds !== undefined)
    ? `?t=${{Math.max(0, Math.floor(seconds))}}` : '';
  return `${{base}}/report/{report_id}/player/${{t}}`;
}}

async function copyFindingLink(e, obsId, seconds) {{
  if (e) {{ e.preventDefault(); e.stopPropagation(); }}
  const link = findingLink(obsId, seconds);
  try {{
    await navigator.clipboard.writeText(link);
    if (e && e.target) {{
      const el = e.target, was = el.textContent;
      el.textContent = '✓';
      setTimeout(() => {{ el.textContent = was; }}, 1400);
    }}
  }} catch (err) {{
    // Буфер обмена доступен только по https, а внутри сети платформа
    // ходит по http. Показываем ссылку, а не молчим.
    window.prompt('Скопируйте ссылку:', link);
  }}
}}

function jumpTo(sec) {{
  // Переходим и ОСТАНАВЛИВАЕМСЯ. Человек открывает находку, чтобы её
  // разглядеть; если видео продолжит играть, момент тут же уедет, и
  // придётся отматывать назад -- каждый раз.
  //
  // Пауза ставится до перемотки: иначе между seek и pause успевает
  // проиграться несколько кадров, и на экране оказывается не тот момент,
  // на который перешли.
  video.pause();
  video.currentTime = sec;

  // Рамки рисуются по timeupdate, а на паузе оно не приходит: без явной
  // перерисовки пометка не появится, пока видео не тронут.
  //
  // Перерисовываем ПОСЛЕ завершения перемотки. Сразу после присваивания
  // currentTime видео ещё может стоять на прежнем месте, и тогда
  // перерисовка стирает все рамки (она начинается с очистки) и не рисует
  // новую -- именно так рамка и пропадала при переходе к находке.
  //
  // Вызываем и сразу тоже: если перематывать было некуда (уже на этом
  // месте), события seeked не будет вовсе.
  video.addEventListener('seeked', renderVisibleObservations, {{ once: true }});
  renderVisibleObservations();
}}

// --- ранжирование детекций (точно человек/предположительно/предмет/отклонено) --
// см. detection_priorities в sar_common.py и обсуждение с пользователем.
// kind: 'ai_scene' | 'manual'. Только человек ставит эти статусы -- сервер
// сам сюда никогда не пишет.
const PRIORITY_LABELS = {{
  '': '— не размечено —',
  'confirmed_person': '✅ точно человек',
  'likely_person': '👤 предположительно человек',
  'confirmed_object': '🎒 предмет',
  'likely_object': '🎒 предположительно предмет',
  'anomaly': '❓ аномалия (непонятно, но подозрительно)',
  'rejected': '❌ отклонено',
}};
let priorityMap = {{}};

async function loadPriorities() {{
  try {{
    const res = await fetch(`/api/report/${{reportId}}/priorities`);
    const rows = await res.json();
    priorityMap = {{}};
    rows.forEach(r => {{ priorityMap[r.kind + '|' + r.ref_key] = r; }});
  }} catch (e) {{}}
}}

function renderPriorityControl(kind, refKey) {{
  const rec = priorityMap[kind + '|' + refKey];
  const current = rec ? rec.priority : '';
  const options = Object.keys(PRIORITY_LABELS).map(val =>
    `<option value="${{val}}" ${{val === current ? 'selected' : ''}}>${{PRIORITY_LABELS[val]}}</option>`).join('');
  const who = rec ? `<span class="priority-who">— ${{escapeHtml(rec.set_by)}}</span>` : '';
  return `
    <div class="priority-control">
      <select onchange="setPriority('${{kind}}', '${{refKey}}', this.value)">${{options}}</select>
      ${{who}}
    </div>`;
}}

// --- обсуждение находки ---
// Все комментарии отчёта грузятся ОДНИМ запросом и раскладываются по
// карточкам здесь: карточек бывают сотни, и запрос на каждую превратил бы
// открытие плеера в сотни обращений к серверу.
let commentsMap = {{}};

async function loadComments() {{
  try {{
    const res = await fetch(`/api/report/${{reportId}}/comments`);
    const rows = await res.json();
    commentsMap = {{}};
    rows.forEach(r => {{
      const key = r.kind + '|' + r.ref_key;
      (commentsMap[key] = commentsMap[key] || []).push(r);
    }});
  }} catch (e) {{}}
}}

// Когда запись СДЕЛАНА. Не путать с fmtTime -- та показывает место в видео.
//
// Год обязателен: без него «16.08 07:19» не отличить от прошлогоднего, а
// поиски идут годами и материал по ним хранится. Именно на этом и споткнулись.
//
// Никакого внешнего сервиса времени: toLocaleString -- встроенная функция
// браузера, работает офлайн, в любой стране, ничего никуда не передаёт и
// заблокировать её нельзя. Часы берутся с машины пользователя.
//
// Время в базе записано БЕЗ пояса -- это местное время операции. new Date()
// разбирает такую строку как местное, поэтому цифра остаётся ровно той, что
// записали: 07:06 в поле останется 07:06 и у зрителя из другого часового
// пояса. Пересчитывать её было бы хуже -- «во сколько нашли» имеет смысл
// именно по времени места работ.
function fmtStamp(iso) {{
  if (!iso) return '';
  try {{
    const d = new Date(iso);
    if (isNaN(d)) return String(iso).replace('T', ' ').slice(0, 16);
    return d.toLocaleString('ru-RU', {{ day:'2-digit', month:'2-digit',
                                        year:'numeric', hour:'2-digit',
                                        minute:'2-digit' }});
  }} catch (e) {{ return ''; }}
}}

function fmtCommentTime(iso) {{ return fmtStamp(iso); }}

function renderComments(kind, refKey) {{
  const list = commentsMap[kind + '|' + refKey] || [];
  const items = list.map(c => `
    <div class="cmt">
      <div class="cmt-head">
        <span class="cmt-author">${{escapeHtml(c.author)}}</span>
        <span class="cmt-time">${{fmtCommentTime(c.created_at)}}</span>
        ${{(c.author === VIEWER_NAME || IS_MODERATOR)
            ? `<button class="cmt-del" onclick="deleteComment(${{c.id}})" title="${{
                 c.author === VIEWER_NAME ? 'Удалить' : 'Удалить как модератор'}}">×</button>` : ''}}
      </div>
      <div class="cmt-text">${{escapeHtml(c.text)}}</div>
    </div>`).join('');
  // id завязан на пару (kind, ref_key) -- на странице одновременно живут
  // десятки карточек, и поле ввода каждой должно быть своим
  const inputId = `cmt-input-${{kind}}-${{refKey}}`.replace(/[^a-zA-Z0-9_-]/g, '_');
  // Недописанный текст переживает перерисовку списка -- см. commentDrafts.
  const draft = commentDrafts[inputId] || '';
  // Подпись говорит, что внутри, а не предлагает нажать: если разговор уже
  // есть, раздел и так раскрыт.
  const label = list.length ? `Комментарии · ${{list.length}}`
                            : 'Добавить комментарий';
  const open = list.length || draft || openDiscussions[inputId] ? 'open' : '';
  return `
    <details class="discussion" ${{open}} ontoggle="rememberDiscussion('${{inputId}}', this.open)">
      <summary>${{label}}</summary>
      <div class="cmt-list">${{items}}</div>
      ${{CAN_COMMENT ? `
      <div class="cmt-form">
        <textarea id="${{inputId}}" rows="2" placeholder="Ваш комментарий"
          oninput="onCommentInput(this)"
          onkeydown="onCommentKey(event, this)">${{escapeHtml(draft)}}</textarea>
        <div class="cmt-actions">
          <button ${{draft.trim() ? '' : 'disabled'}}
            onclick="addComment('${{kind}}', '${{refKey}}', '${{inputId}}', this)">Отправить</button>
          <span class="cmt-hint">Ctrl+Enter</span>
        </div>
      </div>` : `<div class="cmt-locked">${{COMMENT_LOCK_REASON}}</div>`}}
    </details>`;
}}

// --- недописанный текст не должен пропадать -------------------------------
//
// Список наблюдений и список сцен перерисовываются целиком через innerHTML
// каждые 15 секунд (см. loadObservations/loadAiScenes). Поле ввода живёт
// ВНУТРИ этих списков, поэтому набранный комментарий, фокус и позиция
// курсора исчезали ровно посреди фразы. Дописать длинную мысль было почти
// невозможно -- именно это и делало обсуждение неудобным.
//
// Защита двойная, и обе половины нужны:
//   * пока человек печатает, перерисовку откладываем (фокус и курсор
//     сохранить восстановлением значения нельзя);
//   * набранное всё равно помним, потому что можно отвлечься на видео --
//     фокус уйдёт, и тогда перерисовка законна, а текст терять всё равно
//     нельзя.
let commentDrafts = {{}};
let openDiscussions = {{}};
let pendingCardRerender = false;

function onCommentInput(ta) {{
  commentDrafts[ta.id] = ta.value;
  const btn = ta.parentNode.querySelector('button');
  if (btn) btn.disabled = !ta.value.trim();
}}

function onCommentKey(event, ta) {{
  // Ctrl+Enter -- привычная отправка; обычный Enter оставляет перенос
  // строки, потому что пометки бывают в несколько предложений.
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {{
    const btn = ta.parentNode.querySelector('button');
    if (btn && !btn.disabled) btn.click();
  }}
}}

function rememberDiscussion(inputId, isOpen) {{
  openDiscussions[inputId] = isOpen;
}}

function isTypingComment() {{
  const el = document.activeElement;
  return !!(el && el.closest && el.closest('.cmt-form'));
}}

// Ставится вместо прямого innerHTML в местах, где перерисовывается карточка
// с обсуждением внутри.
function setCardsHtml(el, html) {{
  if (isTypingComment()) {{ pendingCardRerender = true; return false; }}
  el.innerHTML = html;
  return true;
}}

// Как только человек ушёл из поля -- догоняем отложенную перерисовку, иначе
// список останется устаревшим до следующего тика.
document.addEventListener('focusout', () => {{
  setTimeout(() => {{
    if (pendingCardRerender && !isTypingComment()) {{
      pendingCardRerender = false;
      loadObservations();
      loadAiScenes();
    }}
  }}, 0);
}});

async function addComment(kind, refKey, inputId, btn) {{
  const ta = document.getElementById(inputId);
  const text = (ta.value || '').trim();
  if (!text) return;
  btn.disabled = true;
  try {{
    const res = await fetch(`/api/report/${{reportId}}/comments`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ kind, ref_key: refKey, text }}),
    }});
    if (!res.ok) throw new Error('сервер ответил ' + res.status);
    // Черновик стираем ТОЛЬКО после успешной отправки: если запрос не
    // прошёл, текст должен остаться в поле, а не пропасть.
    ta.value = '';
    delete commentDrafts[inputId];
    await loadComments();
    loadObservations();
    loadAiScenes();
  }} catch (e) {{
    // Не глухой catch: молча проглоченная ошибка здесь означает, что
    // человек считает сообщение отправленным, а его нет.
    console.warn('комментарий не отправлен', e);
    alert('Не удалось отправить комментарий. Текст сохранён, попробуйте ещё раз.');
    btn.disabled = false;
  }}
}}

async function deleteComment(id) {{
  if (!confirm('Удалить это сообщение?')) return;
  await fetch(`/api/report/${{reportId}}/comments/${{id}}`, {{ method: 'DELETE' }});
  await loadComments();
  loadObservations();
  loadAiScenes();
}}

async function setPriority(kind, refKey, priority) {{
  try {{
    await fetch(`/api/report/${{reportId}}/priorities`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ kind, ref_key: refKey, priority }}),
    }});
  }} catch (e) {{}}
  await loadPriorities();
  loadObservations();
  loadAiScenes();
}}

// глубокая ссылка из report.html (карточка сцены/полноэкранный кадр) --
// ?t=<секунды> в адресе плеера. Не автоплей -- сюда часто попадают, просто
// чтобы посмотреть контекст конкретного кадра, а не досмотреть видео.
// video.currentTime до loadedmetadata молча игнорируется браузером, поэтому
// откладываем, если метаданные ещё не подгрузились.
(function seekFromUrl() {{
  const t = parseFloat(new URLSearchParams(location.search).get('t'));
  if (isNaN(t)) return;
  const apply = () => {{ video.currentTime = t; }};
  if (video.readyState >= 1) apply();
  else video.addEventListener('loadedmetadata', apply, {{ once: true }});
}})();

async function deleteObservation(id) {{
  // Явно про наблюдение: кнопка стояла под формой комментария и её путали
  // с удалением сообщения, поэтому и в вопросе теперь сказано, что именно
  // исчезнет.
  if (!confirm('Удалить это наблюдение? Оно исчезнет у всех.')) return;
  await fetch(`/api/report/${{reportId}}/observations/${{id}}`, {{ method: 'DELETE' }});
  loadObservations();
}}

// --- полоса покрытия реального просмотра ---
async function loadCoverage() {{
  const res = await fetch(`/api/report/${{reportId}}/coverage`);
  const data = await res.json();
  const bar = document.getElementById('covbar');
  const buckets = data.buckets || [];
  bar.innerHTML = buckets.map(b => `<div class="covseg ${{b > 0 ? 'on' : ''}}"></div>`).join('');
  // без прочерков: пока никто не смотрел -- так и пишем, а не "— · зрителей: 0"
  const el = document.getElementById('cov-stat');
  if (!data.viewer_count) {{
    el.textContent = 'Это видео пока никто не просматривал';
  }} else {{
    const pct = (data.percent !== null && data.percent !== undefined)
      ? `просмотрено ${{data.percent}}%` : 'длительность видео пока неизвестна';
    el.textContent = `${{pct}} · зрителей: ${{data.viewer_count}}`;
  }}
}}

// --- отправка реально просмотренных диапазонов (не диапазонов сцен, а самого видео) ---
let lastReportedTime = 0;
video.addEventListener('loadedmetadata', () => {{ lastReportedTime = video.currentTime; }});
video.addEventListener('seeked', () => {{ lastReportedTime = video.currentTime; }});

setInterval(() => {{
  if (!video.paused && !video.seeking && video.currentTime > lastReportedTime) {{
    const start = lastReportedTime, end = video.currentTime;
    lastReportedTime = end;
    fetch(`/api/report/${{reportId}}/playback_watched`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ start_sec: start, end_sec: end }}),
    }}).catch(() => {{}});
  }}
}}, 5000);

// --- онлайн-индикатор (та же логика, что на остальных страницах) ---

// Одна галка прячет ВСЁ, что подсказала модель: и рамки поверх видео, и
// список сцен справа. Нужно, чтобы можно было пройти видео своими глазами,
// не глядя на подсказки -- иначе внимание невольно идёт туда, куда показала
// модель, и непомеченные ею места просматриваются хуже.
function applyAiVisibility() {{
  const block = document.getElementById('ai-scenes-block');
  if (block) block.style.display = showAiBoxes ? '' : 'none';
}}

document.getElementById('ai-toggle').addEventListener('change', e => {{
  showAiBoxes = e.target.checked;
  if (showAiBoxes && aiDetections.length === 0) loadAiDetections();
  applyAiVisibility();
  renderVisibleObservations();
}});
applyAiVisibility();

// приоритеты грузим ПЕРЕД первым рендером обоих списков -- иначе выпадающие
// списки на долю секунды отрисуются как "не размечено" и тут же перерисуются
async function initPlayerData() {{
  // приоритеты и комментарии -- ДО первой отрисовки карточек, иначе они
  // отрисуются пустыми и тут же перерисуются заново
  await Promise.all([loadPriorities(), loadComments()]);
  loadObservations();
  loadCoverage();
  loadAiDetections();
  loadAiScenes();
}}
initPlayerData();
setInterval(loadObservations, 15000);
setInterval(loadCoverage, 15000);
setInterval(loadAiScenes, 15000);
setInterval(loadPriorities, 15000);
// комментарии подтягиваются так же, как остальное -- чтобы сообщение
// коллеги появилось без перезагрузки страницы
setInterval(loadComments, 15000);

// пока видео ещё обрабатывается -- рамки модели в detections.json
// пополняются на лету (см. flush_partial_detections в sar_video_review.py),
// поэтому переопрашиваем чаще и убираем баннер, как только станет 'done'.
// Интервал настраивается в sar_config.json -> server.player_ai_poll_interval_sec
// (сервер сам зажимает его снизу до 5с, см. player_page() в sar_server.py).
const AI_POLL_INTERVAL_MS = {ai_poll_interval_ms};
if (isProcessing) {{
  const pollProcessing = setInterval(async () => {{
    loadAiDetections();
    loadAiScenes();
    try {{
      const res = await fetch(`/api/report/${{reportId}}/status`);
      const st = await res.json();
      if (st.status === 'done') {{
        isProcessing = false;
        clearInterval(pollProcessing);
        const banner = document.getElementById('processing-banner');
        if (banner) banner.remove();
        loadAiDetections();  // финальный полный набор детекций
        loadAiScenes();
      }}
    }} catch (e) {{}}
  }}, AI_POLL_INTERVAL_MS);
}}
</script>
</body></html>"""


def material_crumbs(conn, report, extra=""):
    """Крошки для страниц материала: путь назад в его операцию.

    Раньше плеер и отчёт вели «к списку файлов» -- в общую кучу всех
    материалов, мимо операции, из которой человек пришёл. Теперь виден путь
    и есть возврат на любой уровень.

    Материал может принадлежать двум операциям сразу: показываем ту, что
    добавлена раньше, остальные не теряются -- они видны в самой операции.
    """
    ops = sar_common.operations_of_material(conn, report["report_id"])
    rel = (report["rel_path"] or "").replace("\\", "/")
    name = rel.split("/")[-1]
    parts = ['<a href="/operations">Операции</a>']
    if ops:
        op = ops[-1]
        parts.append('<a href="/operation/%d/">%s</a>'
                     % (op["id"], html_escape(op["title"])))

        # ПАПКИ, В КОТОРЫХ ЛЕЖИТ ФАЙЛ. Без них крошки обрывались на
        # названии операции: человек видел файл, но не понимал, из какой он
        # папки и как вернуться именно туда -- а с подключённым облаком
        # вложенность стала заметной ("2026 08 14/Helicopter/Saykal").
        #
        # Путь считается ТЕМ ЖЕ способом, что и дерево
        # (sar_common.material_display_path): иначе крошки повели бы в
        # папку, которой в дереве нет.
        op_root = (op["folder"] or "").replace("\\", "/").strip("/")
        full = sar_common.material_display_path(
            dict(report), op_root, sar_common.cloud_display_roots(conn))
        inside = full[len(op_root) + 1:] if op_root and             full.startswith(op_root + "/") else full
        folders = inside.split("/")[:-1]
        acc = ""
        for f in folders:
            acc = acc + "/" + f if acc else f
            parts.append('<a href="/operation/%d/?path=%s">%s</a>'
                         % (op["id"], urllib.parse.quote(acc), html_escape(f)))
    else:
        parts.append('<a href="/">Не разобрано</a>')
    if extra:
        parts.append(extra)
    parts.append("<span>%s</span>" % html_escape(name))
    return " &rsaquo; ".join(parts)


NOT_READY_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>{name} — не готов</title>
<style>
body{{font-family:-apple-system,Arial,sans-serif;background:#12171c;color:#e8eeec;
  margin:0;padding:40px 22px;line-height:1.6}}
.wrap{{max-width:620px;margin:0 auto}}
a{{color:#5fb8c7}}
h1{{font-size:20px;margin:0 0 6px}}
.path{{color:#9aa8a5;font-size:13px;margin:0 0 26px;word-break:break-all}}
.card{{background:#1a2129;border:1px solid #2b353f;border-radius:10px;
  padding:20px 22px;margin:0 0 18px}}
p{{margin:0 0 14px}}
.note{{color:#9aa8a5;font-size:14px}}
button{{background:#1b5e20;color:#fff;border:1px solid #2e7d32;border-radius:7px;
  padding:10px 20px;font-size:15px;cursor:pointer}}
button:hover{{background:#227026}}
button:disabled{{background:#2a2f34;border-color:#39424a;color:#8b9499;cursor:default}}
#msg{{margin-left:14px;color:#9aa8a5;font-size:14px}}
.err{{color:#e5807a}}
</style></head><body><div class="wrap">
<p><a href="{back}">← к материалам операции</a></p>
<h1>{name}</h1>
<p class="path">☁ {folder}</p>
<div class="card">
  <p><b>Файл лежит в облаке и ещё не готов к просмотру.</b></p>
  {status}
  <p class="note">
    Чтобы его можно было смотреть, платформа скачает оригинал ({size}) и
    соберёт из него лёгкую копию. Сам оригинал после этого не нужен и
    освободит место — смотреть вы будете копию, как и весь остальной
    материал.
  </p>
  <p class="note">
    Само ничего не качается: {total} файлов из этого хранилища заняли бы
    десятки гигабайт трафика. Поэтому решение за вами, и за каждый файл
    отдельно.
  </p>
  <p style="margin-top:18px">
    <button id="go" onclick="prepare()" {btn_disabled}>{btn_label}</button>
    <span id="msg"></span>
  </p>
</div>
<p class="note">
  Подготовка идёт в фоне и занимает несколько минут: скачивание зависит от
  канала, сборка копии — от процессора. Обновите страницу позже.
</p>
<script>
async function prepare() {{
  const b = document.getElementById('go'), m = document.getElementById('msg');
  b.disabled = true;
  m.textContent = 'Отправляю…';
  const r = await fetch('/api/report/{report_id}/prepare', {{method: 'POST'}});
  const d = await r.json().catch(() => ({{}}));
  if (!r.ok || !d.ok) {{
    b.disabled = false;
    m.textContent = (d && d.error) || 'Не получилось';
    return;
  }}
  m.textContent = 'Принято. Файл встал в очередь на подготовку.';
  setTimeout(() => location.reload(), 3000);
}}

// Пока файл готовится, страницу обновляем сами: иначе человек смотрит на
// «в очереди» и не знает, сдвинулось ли что-нибудь.
if (document.getElementById('go').disabled) {{
  setTimeout(() => location.reload(), 15000);
}}
</script>
</div></body></html>"""


@app.route("/api/report/<report_id>/prepare", methods=["POST"])
def api_report_prepare(report_id):
    """Просьба подготовить облачный материал к просмотру.

    Отмечаем флагом, а не качаем прямо здесь: сервер по устройству проекта
    ничего не обрабатывает, он только читает. Скачает и соберёт копию
    воркер -- со всеми ограничителями расхода.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT cloud_file_id FROM reports WHERE report_id=?",
        (report_id,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "материал не найден"}), 404
    if not row["cloud_file_id"]:
        return jsonify({"ok": False,
                        "error": "этот материал не из облака"}), 400
    conn.execute("UPDATE reports SET proxy_requested=1 WHERE report_id=?",
                 (report_id,))
    conn.commit()
    return jsonify({"ok": True})


def cloud_prepare_state(conn, report):
    """Что сейчас происходит с подготовкой этого материала.

    Смотрим на ФАКТЫ, а не на предположения: стоит ли просьба, лежит ли
    недокачанный остаток, есть ли уже копия. Так состояние не разъедется с
    действительностью, даже если воркер перезапускали.

    Отдельно достаём ошибку подключения. Без неё страница говорит «в
    очереди» и молчит месяцами, когда на самом деле протух токен: человек
    ждёт, а платформа каждые 15 секунд получает отказ. Ровно это и вышло
    при первом живом включении.
    """
    rel = report.get("rel_path") or ""
    data_dir = _data_dir()
    state = {"requested": bool(report.get("proxy_requested")),
             "done_bytes": 0, "blocked": None}

    if os.path.exists(sar_common.proxy_video_path(data_dir, rel)):
        state["stage"] = "ready"
        return state

    try:
        import sar_staging
        part = sar_staging.Staging(sar_staging.staging_dir(data_dir),
                                    cap_bytes=1).path_for(rel) + ".part"
        if os.path.exists(part):
            state["done_bytes"] = os.path.getsize(part)
    except Exception:
        pass

    acc_id = report.get("cloud_account_id")
    for a in sar_common.cloud_accounts_public(conn):
        if a["id"] == acc_id and a.get("last_error"):
            state["blocked"] = a["last_error"]

    if state["done_bytes"]:
        state["stage"] = "downloading"
    elif state["requested"]:
        state["stage"] = "queued"
    else:
        state["stage"] = "idle"
    return state


def _not_ready_page(report):
    """Страница материала, который ещё не скачан из облака."""
    rel = (report.get("rel_path") or "").replace("\\", "/")
    folder, _, name = rel.rpartition("/")
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) n FROM reports WHERE cloud_file_id IS NOT NULL"
    ).fetchone()["n"]
    ops = sar_common.operations_of_material(conn, report["report_id"])
    back = "/operation/%d/" % ops[0]["id"] if ops else "/"
    size = report.get("cloud_size") or 0
    st = cloud_prepare_state(conn, report)

    if st["stage"] == "downloading":
        pct = (100.0 * st["done_bytes"] / size) if size else 0
        status = ('<p><b>Скачивается: %.0f%%</b> (%.0f из %.0f МБ)</p>'
                  % (pct, st["done_bytes"] / 1e6, size / 1e6))
    elif st["stage"] == "queued":
        status = ("<p><b>В очереди на подготовку.</b> Воркер возьмёт файл "
                  "в ближайшие секунды.</p>")
    else:
        status = ""

    if st["blocked"]:
        status += ('<p class="err">Но подготовка не идёт: %s</p>'
                   '<p class="note">Пока это не исправлено, файл так и будет '
                   'ждать. Проверьте подключение в <a href="/admin">настройках '
                   'платформы</a> — у Google токен живёт один час.</p>'
                   % html_escape(st["blocked"]))

    return NOT_READY_HTML.format(
        name=html_escape(name or rel),
        folder=html_escape(folder or "корень хранилища"),
        size=("%.1f ГБ" % (size / 1e9)) if size >= 1e9 else ("%.0f МБ" % (size / 1e6)),
        total=total, back=back, report_id=html_escape(report["report_id"]),
        status=status,
        btn_disabled="disabled" if st["stage"] in ("queued", "downloading") else "",
        btn_label=("Уже в очереди" if st["stage"] in ("queued", "downloading")
                   else "Подготовить к просмотру"))


@app.route("/report/<report_id>/player/")
def player_page(report_id):
    report = get_report_row(report_id)
    if report is None:
        return "Отчёт не найден", 404
    if report["kind"] != "video":
        return "Ручной плеер доступен только для видео", 400

    # СМОТРЯТ ЛЁГКУЮ КОПИЮ, а не оригинал -- значит и доступность плеера
    # определяет она. Материал из облака оригинала на диске не имеет вовсе,
    # и проверять его наличие здесь значило бы не пускать в плеер файл,
    # который прекрасно готов к просмотру.
    proxy = sar_common.proxy_video_path(_data_dir(), report["rel_path"])
    staged = sar_common.find_material_file(
        _path_roots()[0], _data_dir(), report["rel_path"])
    if not os.path.exists(proxy) and not staged:
        if report.get("cloud_file_id"):
            return _not_ready_page(report)
        return "Исходный видеофайл больше не найден на диске", 404

    # плеер доступен для видео в ЛЮБОМ статусе -- исходный файл на диске уже
    # есть с момента появления в очереди, ручная разметка от детектора не
    # зависит вообще. is_processing/баннер -- только про то, стоит ли ждать
    # рамок модели (и опрашивать за ними), не про доступность самого плеера.
    is_processing = report["status"] in ("queued", "processing")
    processing_banner = ""
    if report["status"] == "queued":
        processing_banner = (
            '<div class="processing-banner" id="processing-banner">'
            '<span class="spin">⚙️</span> Видео в очереди, обработка ещё не началась. '
            'Ручная разметка уже доступна — рамки модели появятся автоматически, как только начнётся анализ.</div>')
    elif report["status"] == "processing":
        pct = round(report["progress_pct"] or 0)
        processing_banner = (
            f'<div class="processing-banner" id="processing-banner">'
            f'<span class="spin">⚙️</span> Видео ещё обрабатывается ({pct}%). '
            f'Рамки модели будут появляться по мере анализа — обновляются автоматически.</div>')
    elif report["status"] == "error":
        processing_banner = (
            '<div class="processing-banner" id="processing-banner" style="background:#3a1414; '
            'border-color:#7a1f1f; color:#ff9999;">⚠ Автоматическая обработка завершилась с ошибкой '
            '— рамок модели не будет, но ручная разметка видео работает как обычно.</div>')

    # жёсткий минимум 5с здесь, на сервере — не полагаемся на то, что
    # значение в конфиге кто-то не выставит агрессивно низким по ошибке;
    # один клиент физически не может опрашивать сервер за рамками модели
    # чаще этого предела, что бы ни было написано в sar_config.json
    poll_interval_sec = max(5, SERVER_CFG.get("player_ai_poll_interval_sec", 5))
    ai_poll_interval_ms = int(poll_interval_sec * 1000)

    return PLAYER_PAGE_HTML.format(
        report_id=report_id, name=report["rel_path"],
        # в заголовке -- ИМЯ файла: полный путь уже стоит в крошках строкой
        # выше, и повторять его значит занимать место дважды
        short_name=(report["rel_path"] or "").replace("\\", "/").split("/")[-1],
        crumbs=material_crumbs(
            get_db(), report,
            extra='<a href="/report/%s/">сцены</a>' % report_id
            if report["status"] == "done" else ""),
        processing_banner=processing_banner,
        is_processing_js="true" if is_processing else "false",
        # json.dumps -- корректно экранирует кавычки/юникод в имени, которое
        # человек вводит сам при входе
        viewer_name_js=json.dumps(session.get("viewer_name", "аноним")),
        # Частота кадров нужна для покадровой перемотки. У необработанного
        # видео её ещё нет -- тогда 30 -- разумное приближение для съёмки с
        # дрона; ошибка в доли процента на один шаг незаметна.
        fps_js=json.dumps(report["fps"] or 30),
        can_comment_js="true" if can_comment() else "false",
        is_moderator_js="true" if is_moderator() else "false",
        comment_lock_js=json.dumps(
            "Координатор ограничил вам участие в обсуждениях."
            if current_role() == sar_common.ROLE_MUTED else
            "Чтобы писать в обсуждении, войдите по личной ссылке из бота "
            "(команда /help). Сейчас вы вошли по общему паролю — система не "
            "знает, кто вы, поэтому сообщения были бы без автора."),
        ai_poll_interval_ms=ai_poll_interval_ms)


@app.route("/report/<report_id>/")
def report_page(report_id):
    report = get_report_row(report_id)
    if report is None:
        return "Отчёт не найден", 404
    if report["status"] == "done":
        report_html_path = os.path.join(report["out_dir"], "report.html")
        if os.path.exists(report_html_path):
            return send_from_directory(report["out_dir"], "report.html")
        return "Отчёт помечен готовым, но файл report.html не найден", 500
    rel = (report["rel_path"] or "").replace("\\", "/")
    return PROCESSING_PAGE_HTML.format(
        name=rel.split("/")[-1], status=report["status"],
        crumbs=material_crumbs(get_db(), report),
        progress=round(report["progress_pct"] or 0), report_id=report_id)


@app.route("/report/<report_id>/<path:filename>")
def report_asset(report_id, filename):
    report = get_report_row(report_id)
    if report is None:
        return "Отчёт не найден", 404
    return send_from_directory(report["out_dir"], filename)


@app.route("/api/report/<report_id>/status")
def api_report_status(report_id):
    report = get_report_row(report_id)
    if report is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status": report["status"], "progress_pct": report["progress_pct"],
        "error": report["error"], "total_frames": report["total_frames"],
        "phase": report["phase"],
    })


@app.route("/api/report/<report_id>/log")
def api_report_log(report_id):
    since = int(request.args.get("since", 0))
    conn = get_db()
    rows = conn.execute(
        "SELECT id, line FROM logs WHERE report_id=? AND id>? ORDER BY id", (report_id, since)
    ).fetchall()
    last_id = rows[-1]["id"] if rows else since
    return jsonify({"lines": [dict(r) for r in rows], "last_id": last_id})


@app.route("/api/report/<report_id>/scene_viewed", methods=["POST"])
def api_scene_viewed(report_id):
    data = request.get_json(force=True, silent=True) or {}
    viewer_name = (data.get("viewer_name") or session.get("viewer_name") or "аноним")[:60]
    start_sec = data.get("time_start_sec")
    end_sec = data.get("time_end_sec")
    if start_sec is None or end_sec is None:
        return jsonify({"ok": False, "error": "missing time range"}), 400
    conn = get_db()
    conn.execute(
        "INSERT INTO watch_segments (report_id, viewer_name, start_sec, end_sec, ts) VALUES (?,?,?,?,?)",
        (report_id, viewer_name, float(start_sec), float(end_sec), datetime.now().isoformat()))
    conn.commit()
    return jsonify({"ok": True})


def get_online_count(conn):
    cutoff = time.time() - ONLINE_WINDOW_SEC
    row = conn.execute(
        "SELECT COUNT(DISTINCT viewer_name) AS c FROM presence WHERE last_seen > ?", (cutoff,)
    ).fetchone()
    return row["c"] if row else 0


@app.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    viewer_name = session.get("viewer_name")
    if not viewer_name:
        return jsonify({"error": "not authed"}), 401
    conn = get_db()
    now = time.time()
    conn.execute(
        "INSERT INTO presence (viewer_name, last_seen) VALUES (?,?) "
        "ON CONFLICT(viewer_name) DO UPDATE SET last_seen=excluded.last_seen",
        (viewer_name, now))
    # лёгкая уборка совсем протухших записей, чтобы таблица не росла бесконечно
    conn.execute("DELETE FROM presence WHERE last_seen < ?", (now - 86400,))
    conn.commit()
    return jsonify({"ok": True, "count": get_online_count(conn)})


@app.route("/api/online")
def api_online():
    conn = get_db()
    return jsonify({"count": get_online_count(conn)})


# ---------------------------------------------------------------------------
# АДМИНКА: ОГРАНИЧЕНИЯ РАСХОДА РЕСУРСОВ
#
# Зачем страница. Материал уезжает в облако, и каждое чтение файла
# становится скачиванием. Без ограничителей подключение папки означает
# попытку скачать всё разом; с ограничителями, зашитыми в код, их нельзя
# подстроить под конкретный канал, не правя файл и не перезапуская процессы.
#
# ПОЧЕМУ ЗНАЧЕНИЯ ЗАЖИМАЮТСЯ, А НЕ ОТВЕРГАЮТСЯ. Ввели "50 загрузок" --
# сохранится 4, и это видно прямо в форме. Отказ с ошибкой заставил бы
# гадать, что допустимо; тихое принятие 50 положило бы канал и квоту.
# Границы живут в sar_common.SETTINGS_SCHEMA, там же, где определены сами
# настройки -- чтобы форма и проверка не могли разойтись.
#
# ПОЧЕМУ НАСТРОЙКИ В БАЗЕ. Меняет их эта страница (сервер), а применяет
# воркер. Это разные процессы, общающиеся только через базу; запись в
# sar_config.json до воркера не доехала бы до перезапуска.
# ---------------------------------------------------------------------------

ADMIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>SAR Review — настройки</title>
<style>
body {{ font-family:-apple-system, Arial, sans-serif; background:#111; color:#eee;
        margin:0; padding:20px; }}
a {{ color:#6cb6ff; }}
h1 {{ font-size:18px; display:flex; justify-content:space-between;
      align-items:center; margin:0 0 4px; }}
.whoami {{ font-size:13px; color:#999; font-weight:normal; }}
.lead {{ color:#999; font-size:13px; max-width:720px; line-height:1.5;
         margin:10px 0 22px; }}
.card {{ background:#161616; border:1px solid #272727; border-radius:10px;
         padding:16px 18px; margin:0 0 14px; max-width:720px; }}
.card h2 {{ font-size:14px; margin:0 0 4px; }}
.hint {{ color:#8a8a8a; font-size:12.5px; line-height:1.5; margin:0 0 12px; }}
.row {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
input[type=number] {{ background:#0d0d0d; color:#eee; border:1px solid #333;
                      border-radius:6px; padding:7px 10px; width:130px;
                      font-size:14px; }}
input[type=checkbox] {{ width:17px; height:17px; }}
.range {{ color:#777; font-size:12px; }}
.actions {{ display:flex; align-items:center; gap:14px; margin-top:18px;
            max-width:720px; }}
button {{ background:#1b5e20; color:#fff; border:1px solid #2e7d32;
          border-radius:7px; padding:9px 18px; font-size:14px; cursor:pointer; }}
button:hover {{ background:#227026; }}
#saved {{ color:#7bc47f; font-size:13px; }}
.who {{ color:#777; font-size:12px; }}
.section {{ font-size:16px; margin:34px 0 6px; }}
select, input[type=text], input[type=password] {{ background:#0d0d0d; color:#eee;
  border:1px solid #333; border-radius:6px; padding:7px 10px; font-size:14px; }}
.acc {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
.acc .name {{ font-weight:600; }}
.acc .meta {{ color:#8a8a8a; font-size:12.5px; }}
.err {{ color:#e5807a; font-size:12.5px; margin-top:6px; }}
.warns {{ margin:8px 0 0; padding-left:20px; color:#e0b060; font-size:13px; }}
.warns li {{ margin:0 0 5px; }}
.fl {{ display:flex; align-items:center; gap:7px; font-size:13px;
  color:var(--soft); }}
.acc-edit {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  margin-top:12px; padding-top:12px; border-top:1px solid var(--line); }}
.acc-edit input {{ width:250px; }}
.ok {{ color:#7bc47f; font-size:12.5px; }}
.danger {{ background:#3a1c1c; border-color:#5c2b2b; }}
.danger:hover {{ background:#4a2222; }}
</style></head><body>
<h1>Настройки платформы <span class="whoami">{viewer_name}</span></h1>
<div class="lead">
  Ограничения расхода канала, места и квоты облака. Применяются воркером
  на ближайшем обходе папки — перезапускать ничего не нужно.
  <a href="/">← к операциям</a>
</div>
<div id="form"></div>
<div class="actions">
  <button onclick="save()">Сохранить</button>
  <span id="saved"></span>
</div>

<h2 class="section">Облачные хранилища</h2>
<div class="lead">
  Материал можно держать в облаке, а на этой машине оставлять только
  отчёты и лёгкие копии. Токен доступа получают сами — в консоли
  разработчика Google или на oauth.yandex.ru — и вставляют сюда.
  Перенаправление не используется: адрес платформы меняется при каждом
  перезапуске туннеля, и зарегистрированная ссылка возврата протухала бы
  вместе с ним.
</div>
<div id="clouds"></div>
<div class="card">
  <h2>Подключить хранилище</h2>
  <div class="hint">
    Токен показывается платформой только один раз — при вводе. Дальше он
    хранится в базе и наружу не отдаётся.
  </div>
  <div class="row">
    <label class="fl">Хранилище <select id="c_prov"></select></label>
    <label class="fl">Операция <select id="c_op"></select></label>
  </div>
  <div class="row" style="margin-top:10px">
    <input type="password" id="c_token" placeholder="токен доступа" style="width:340px">
  </div>
  <div class="row" style="margin-top:10px">
    <input type="text" id="c_root" placeholder="ссылка на папку с материалом"
           style="width:460px">
  </div>
  <div class="hint" style="margin-top:8px">
    Откройте нужную папку в облаке и скопируйте адрес из строки браузера.
    Оставите пустым — платформа возьмёт <b>весь диск целиком</b>, вместе с
    личными файлами.
  </div>
  <div class="row" style="margin-top:10px">
    <input type="text" id="c_label" placeholder="название подключения (необязательно)"
           style="width:340px">
  </div>
  <details style="margin-top:14px">
    <summary style="cursor:pointer;color:var(--soft);font-size:13.5px">
      Продление доступа — обязательно для Google
    </summary>
    <div class="hint" style="margin:10px 0">
      Токен Google живёт один час. Чтобы платформа продлевала его сама,
      заведите своё приложение в консоли Google, включите в OAuth Playground
      «Use your own OAuth credentials» и вставьте сюда три значения.
      Без них диск придётся подключать заново каждый час.
      <br>Яндекс.Диску это не нужно: там токен действует около года.
    </div>
    <div class="row">
      <input type="text" id="c_cid" placeholder="client_id" style="width:280px">
      <input type="password" id="c_csec" placeholder="client_secret" style="width:220px">
    </div>
    <div class="row" style="margin-top:10px">
      <input type="password" id="c_rt" placeholder="refresh token" style="width:400px">
    </div>
  </details>
  <div class="row" style="margin-top:14px">
    <button onclick="connectCloud()">Проверить и подключить</button>
    <span id="c_msg"></span>
  </div>
</div>
<script>
let SCHEMA = {{}}, VALUES = {{}};

function esc(t) {{
  return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function render() {{
  document.getElementById('form').innerHTML = Object.keys(SCHEMA).map(k => {{
    const s = SCHEMA[k], v = VALUES[k];
    let input;
    if (s.type === 'bool') {{
      input = `<input type="checkbox" id="f_${{k}}" ${{v ? 'checked' : ''}}>`;
    }} else if (s.type === 'choice') {{
      // Варианты приходят из реестра вместе со значением -- второго
      // списка во фронтенде быть не должно, он разойдётся с кодом.
      input = `<select id="f_${{k}}">` + s.options.map(o =>
        `<option value="${{o.value}}"${{o.value === v ? ' selected' : ''}}>`
        + `${{esc(o.label)}}</option>`).join('') + '</select>';
    }} else {{
      const step = s.type === 'float' ? '0.5' : '1';
      input = `<input type="number" id="f_${{k}}" value="${{v}}"
                 min="${{s.min}}" max="${{s.max}}" step="${{step}}">
               <span class="range">от ${{s.min}} до ${{s.max}}</span>`;
    }}
    const by = s.set_by ? `<div class="who">поставил: ${{esc(s.set_by)}}</div>` : '';
    return `<div class="card">
      <h2>${{esc(s.label)}}</h2>
      <div class="hint">${{esc(s.help)}}</div>
      <div class="row">${{input}}</div>${{by}}
    </div>`;
  }}).join('');
}}

async function load() {{
  const r = await fetch('/api/admin/settings');
  if (!r.ok) {{ document.getElementById('form').textContent = 'Нет доступа'; return; }}
  const d = await r.json();
  SCHEMA = d.schema; VALUES = d.values; render();
}}

async function save() {{
  const body = {{}};
  for (const k of Object.keys(SCHEMA)) {{
    const el = document.getElementById('f_' + k);
    body[k] = SCHEMA[k].type === 'bool' ? el.checked : el.value;
    // select и number оба отдают value -- отдельная ветка не нужна
  }}
  const r = await fetch('/api/admin/settings', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  const d = await r.json();
  if (!r.ok || !d.ok) {{
    document.getElementById('saved').textContent = 'Не сохранилось';
    return;
  }}
  // Показываем то, что РЕАЛЬНО сохранилось: значение могло быть зажато по
  // границам, и человек должен это увидеть, а не думать, что принято как есть.
  VALUES = d.values; render();
  const el = document.getElementById('saved');
  el.textContent = 'Сохранено';
  setTimeout(() => {{ el.textContent = ''; }}, 2500);
}}

// --- облачные хранилища ---------------------------------------------------

let CLOUD = {{providers: [], accounts: []}};

function renderClouds() {{
  const sel = document.getElementById('c_prov');
  if (!sel.options.length) {{
    sel.innerHTML = CLOUD.providers
      .map(p => `<option value="${{p.name}}">${{esc(p.label)}}</option>`).join('');
  }}
  const ops = document.getElementById('c_op');
  if (!ops.options.length) {{
    ops.innerHTML = '<option value="">— без операции —</option>'
      + (CLOUD.operations || [])
        .map(o => `<option value="${{o.id}}">${{esc(o.title)}}</option>`).join('');
  }}
  const box = document.getElementById('clouds');
  if (!CLOUD.accounts.length) {{
    box.innerHTML = '<div class="card"><div class="hint">'
      + 'Пока ничего не подключено — материал берётся из локальной папки.'
      + '</div></div>';
    return;
  }}
  box.innerHTML = CLOUD.accounts.map(a => {{
    const where = a.root_name || a.root_id || 'корень';
    const op = (CLOUD.operations || []).find(o => o.id === a.operation_id);
    const opTxt = op ? ` · операция: ${{esc(op.title)}}` : ' · без операции';
    const err = a.last_error
      ? `<div class="err">последняя ошибка: ${{esc(a.last_error)}}</div>` : '';
    const ok = a.last_ok_at ? '<span class="ok">проверено</span>' : '';
    // Главное, что надо знать про подключение: переживёт ли оно час.
    const renew = a.provider === 'google' && !a.can_refresh
      ? '<span class="err">доступ не продлевается — истечёт через час</span>'
      : '';
    return `<div class="card">
      <div class="acc">
        <span class="name">${{esc(a.label || a.provider)}}</span>
        <span class="meta">${{esc(a.provider)}} · папка: ${{esc(where)}}${{opTxt}}</span>
        ${{ok}}
        <button class="danger" onclick="dropCloud(${{a.id}})">Отключить</button>
      </div>${{renew ? '<div class="err">' + renew + '</div>' : ''}}${{err}}
      <div class="acc-edit">
        <label class="fl">Операция
          <select id="op_${{a.id}}">${{opOptions(a.operation_id)}}</select>
        </label>
        <input type="text" id="rt_${{a.id}}" value="${{esc(a.root_id || '')}}"
               placeholder="папка или ссылка на неё">
        <button onclick="saveCloud(${{a.id}})">Сохранить</button>
        <span id="am_${{a.id}}" class="ok"></span>
      </div>
    </div>`;
  }}).join('');
}}

function opOptions(selected) {{
  return '<option value="">— без операции —</option>'
    + (CLOUD.operations || []).map(o =>
        `<option value="${{o.id}}"${{o.id === selected ? ' selected' : ''}}>`
        + `${{esc(o.title)}}</option>`).join('');
}}

// Менять операцию и папку у УЖЕ подключённого диска. Без этого
// единственным способом было отключить и подключить заново -- а отключение
// убирает записи материала, то есть за смену операции платили пересканом.
async function saveCloud(id) {{
  const m = document.getElementById('am_' + id);
  m.textContent = 'Сохраняю…';
  const r = await fetch('/api/admin/cloud/' + id, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      operation_id: document.getElementById('op_' + id).value,
      root_id: document.getElementById('rt_' + id).value
    }})
  }});
  const d = await r.json();
  if (!r.ok || !d.ok) {{ m.textContent = (d && d.error) || 'Не вышло'; return; }}
  CLOUD.accounts = d.accounts;
  renderClouds();
  const m2 = document.getElementById('am_' + id);
  if (m2) {{
    m2.textContent = 'Сохранено. Материал переедет на ближайшем обходе.';
    setTimeout(() => {{ const e = document.getElementById('am_' + id);
                      if (e) e.textContent = ''; }}, 4000);
  }}
}}

async function loadClouds() {{
  const r = await fetch('/api/admin/cloud');
  if (!r.ok) return;
  CLOUD = await r.json();
  renderClouds();
}}

async function connectCloud() {{
  const msg = document.getElementById('c_msg');
  msg.textContent = 'Проверяю...';
  const r = await fetch('/api/admin/cloud', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      provider: document.getElementById('c_prov').value,
      label: document.getElementById('c_label').value,
      token: document.getElementById('c_token').value,
      root_id: document.getElementById('c_root').value,
      operation_id: document.getElementById('c_op').value,
      client_id: document.getElementById('c_cid').value,
      client_secret: document.getElementById('c_csec').value,
      refresh_token: document.getElementById('c_rt').value
    }})
  }});
  const d = await r.json();
  if (!r.ok || !d.ok) {{ msg.textContent = d.error || 'Не получилось'; return; }}
  // Токен из поля убираем сразу: он не должен оставаться в форме, в
  // истории браузера и на случайном скриншоте.
  ['c_token', 'c_csec', 'c_rt'].forEach(
    id => document.getElementById(id).value = '');
  const warns = d.warnings || [];
  msg.innerHTML = `Подключено, файлов: ${{d.found}}.`
    + (warns.length
       ? '<ul class="warns">' + warns.map(x => `<li>${{esc(x)}}</li>`).join('') + '</ul>'
       : '');
  CLOUD.accounts = d.accounts;
  renderClouds();
}}

async function dropCloud(id) {{
  const r = await fetch('/api/admin/cloud/' + id, {{method: 'DELETE'}});
  const d = await r.json();
  if (!d.ok) return;
  CLOUD.accounts = d.accounts;
  renderClouds();
  // Говорим, что именно убрали: молчаливое исчезновение сотни записей
  // выглядит как потеря данных.
  const c = d.cleanup || {{}};
  if (c.removed || c.kept) {{
    document.getElementById('c_msg').textContent =
      `Отключено. Убрано записей: ${{c.removed || 0}}` +
      (c.kept ? `, сохранено с пометками: ${{c.kept}}` : '');
  }}
}}

load();
loadClouds();
</script>
</body></html>"""


@app.route("/admin")
def admin_page():
    if not is_admin():
        return "Нужны права администратора", 403
    return ADMIN_PAGE_HTML.format(
        viewer_name=html_escape(session.get("viewer_name", "")))


# --- подключение облачных хранилищ ------------------------------------------
#
# ПОЧЕМУ НЕ КЛАССИЧЕСКИЙ OAUTH С ПЕРЕНАПРАВЛЕНИЕМ. Ему нужен постоянный
# адрес возврата, зарегистрированный у провайдера. У платформы такого
# адреса НЕТ: наружу она смотрит через туннель, имя которого меняется при
# каждом перезапуске (см. sar_tunnel.py). Зарегистрированный redirect_uri
# протухал бы вместе с туннелем, и подключение ломалось бы ровно тогда,
# когда его труднее всего чинить -- в поле.
#
# Поэтому токен вводится вручную: человек получает его сам (в консоли
# разработчика Google или на oauth.yandex.ru) и вставляет в поле. Это
# менее красиво, зато не зависит от адреса платформы вообще и работает
# одинаково на ноутбуке, на VPS и через любой туннель.
#
# ТОКЕН НАРУЖУ НЕ ВОЗВРАЩАЕТСЯ НИКОГДА -- ни в списке подключений, ни в
# ответе после сохранения. Для этого есть cloud_accounts_public().


@app.route("/api/admin/cloud", methods=["GET", "POST"])
def api_admin_cloud():
    if not is_admin():
        return jsonify({"ok": False, "error": "нужны права администратора"}), 403
    conn = get_db()

    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        provider = (data.get("provider") or "").strip()
        token = (data.get("token") or "").strip()
        if provider not in sar_cloud.PROVIDERS:
            return jsonify({"ok": False, "error": "неизвестное хранилище"}), 400
        if not token:
            return jsonify({"ok": False, "error": "не введён токен доступа"}), 400

        # Проверяем ДО сохранения: подключение, которое не работает, не
        # должно попадать в список исправных. Иначе человек уйдёт уверенный,
        # что диск подключён, а выяснится это при первой обработке.
        # Человек естественнее всего вставит ССЫЛКУ на папку -- её видно в
        # адресной строке. Понимаем и её, и голый идентификатор.
        #
        # И ещё: ссылку дважды вставляли в поле НАЗВАНИЯ -- оно первое
        # текстовое в форме, и рука идёт туда. Последствие тяжёлое: поле
        # папки остаётся пустым, платформа берёт весь диск и затягивает
        # чужие фотографии. Раз люди так делают, надо это понимать, а не
        # считать их ошибкой.
        label_in = (data.get("label") or "").strip()
        root_in = (data.get("root_id") or "").strip()
        if not root_in and label_in.startswith("http"):
            root_in, label_in = label_in, ""

        try:
            root = sar_cloud.folder_ref(provider, root_in)
        except sar_cloud.CloudError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        try:
            prov = sar_cloud.make_provider(provider, token)
            items = prov.list_folder(root or _default_root(provider))
        except sar_cloud.AuthExpired:
            return jsonify({"ok": False,
                            "error": "токен не принят хранилищем. Проверьте, "
                                     "что он не истёк и выдан с правом чтения "
                                     "файлов."}), 400
        except sar_cloud.CloudError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        # Ключи продления -- необязательны, но без них доступ к Google
        # умрёт через час, и подготовка материала (около трёх часов на
        # 150 файлов) не сможет завершиться в принципе.
        from datetime import timedelta
        try:
            op_id = int(data.get("operation_id") or 0)
        except (TypeError, ValueError):
            op_id = 0

        acc_id = sar_common.add_cloud_account(
            conn, provider=provider, token=token,
            label=label_in or None,
            root_id=root or _default_root(provider),
            root_name=(data.get("root_name") or "").strip() or None,
            who=session.get("viewer_name"),
            refresh_token=(data.get("refresh_token") or "").strip() or None,
            client_id=(data.get("client_id") or "").strip() or None,
            client_secret=(data.get("client_secret") or "").strip() or None,
            # Точного срока нам не сообщили, а у Google он всегда час.
            # Ставим его, чтобы продление включилось заранее, а не после
            # первого отказа.
            expires_at=(datetime.now() + timedelta(hours=1)).isoformat()
            if provider == "google" else None)
        if op_id:
            sar_common.update_cloud_account(conn, acc_id, operation_id=op_id)
        sar_common.update_cloud_account(
            conn, acc_id, last_ok_at=datetime.now().isoformat())

        # ПОДКЛЮЧЕНИЕ К КОРНЮ -- почти всегда ошибка. На боевом подключении
        # пустое поле папки означало «взять весь Диск», и в платформу
        # поисковой операции затянуло 288 личных фотографий. Молчать об
        # этом нельзя: человек уходит уверенный, что подключил нужную папку.
        # ПРЕДУПРЕЖДЕНИЙ МОЖЕТ БЫТЬ НЕСКОЛЬКО, и они независимы. Раньше
        # каждое затирало предыдущее: человек, подключивший весь диск без
        # операции и без продления, видел ровно одно из трёх -- и чинил
        # одно, оставаясь с двумя.
        warnings = []
        if not root:
            warnings.append(
                "Папка не указана, поэтому взят ВЕСЬ диск целиком. Обычно "
                "нужна одна папка с материалом операции: откройте её в "
                "облаке и вставьте адрес из строки браузера.")
        if not op_id:
            warnings.append(
                "Операция не выбрана, поэтому материал попадёт в «Не "
                "разобрано», а не в операцию.")
        if provider == "google" and not (data.get("client_id")
                                          and data.get("refresh_token")):
            warnings.append(
                "Доступ к Google истечёт через час, и подготовка материала "
                "остановится. Чтобы платформа продлевала его сама, нужны "
                "ключи вашего приложения и refresh token.")
        return jsonify({"ok": True, "id": acc_id, "found": len(items),
                        "warnings": warnings,
                        "accounts": sar_common.cloud_accounts_public(conn)})

    # Операции отдаём вместе со списком: папку в облаке надо к чему-то
    # привязать, а по её имени операцию не угадать.
    ops = [{"id": o["id"], "title": o["title"]}
           for o in sar_common.list_operations(conn)]
    return jsonify({"ok": True,
                    "providers": [{"name": p.name, "label": p.label}
                                  for p in sar_cloud.PROVIDERS.values()],
                    "operations": ops,
                    "accounts": sar_common.cloud_accounts_public(conn)})


def _default_root(provider):
    """Корень хранилища, если человек не указал папку."""
    return "disk:/" if provider == "yandex" else "root"


@app.route("/api/admin/cloud/<int:account_id>", methods=["DELETE", "POST"])
def api_admin_cloud_one(account_id):
    if not is_admin():
        return jsonify({"ok": False, "error": "нужны права администратора"}), 403
    conn = get_db()

    if request.method == "DELETE":
        res = sar_common.delete_cloud_account(conn, account_id)
        return jsonify({"ok": True, "cleanup": res,
                        "accounts": sar_common.cloud_accounts_public(conn)})

    data = request.get_json(force=True, silent=True) or {}
    rows = [a for a in sar_common.cloud_accounts(conn, enabled_only=False)
            if a["id"] == account_id]
    provider = rows[0]["provider"] if rows else "google"
    fields = {}
    for key in ("label", "root_id", "root_name", "enabled", "operation_id"):
        if key not in data:
            continue
        if key in ("enabled", "operation_id"):
            fields[key] = int(data[key] or 0) if key == "operation_id"                 else int(bool(data[key]))
        elif key == "root_id":
            try:
                fields[key] = sar_cloud.folder_ref(provider, data[key])
            except sar_cloud.CloudError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        else:
            fields[key] = data[key]
    was_op = rows[0].get("operation_id") if rows else None
    if fields:
        sar_common.update_cloud_account(conn, account_id, **fields)

    # СМЕНА ОПЕРАЦИИ ПЕРЕВОДИТ И УЖЕ НАЙДЕННЫЙ МАТЕРИАЛ. Иначе он остаётся
    # в прежней операции, а в новой появляются только файлы, найденные
    # после смены: одна папка оказывается разложена по двум операциям, и
    # понять это по интерфейсу невозможно.
    new_op = fields.get("operation_id")
    if "operation_id" in fields and new_op != was_op:
        ids = [r["report_id"] for r in conn.execute(
            "SELECT report_id FROM reports WHERE cloud_account_id=?",
            (account_id,)).fetchall()]
        for rid in ids:
            if was_op:
                sar_common.detach_material(conn, was_op, rid)
            if new_op:
                sar_common.attach_material(conn, new_op, rid)

    return jsonify({"ok": True,
                    "accounts": sar_common.cloud_accounts_public(conn)})


@app.route("/api/admin/cloud/<int:account_id>/browse")
def api_admin_cloud_browse(account_id):
    """Список папок хранилища -- чтобы выбрать, где лежит материал."""
    if not is_admin():
        return jsonify({"ok": False, "error": "нужны права администратора"}), 403
    conn = get_db()
    rows = [a for a in sar_common.cloud_accounts(conn, enabled_only=False)
            if a["id"] == account_id]
    if not rows:
        return jsonify({"ok": False, "error": "подключение не найдено"}), 404
    acc = rows[0]
    folder = request.args.get("folder") or acc.get("root_id")         or _default_root(acc["provider"])
    try:
        prov = sar_common.provider_for_account(conn, acc)
        items = prov.list_folder(folder)
    except sar_cloud.CloudError as e:
        # Причину записываем в подключение: иначе «почему не видно файлов»
        # выясняется только чтением журнала воркера.
        sar_common.update_cloud_account(conn, account_id, last_error=str(e))
        return jsonify({"ok": False, "error": str(e)}), 400
    sar_common.update_cloud_account(conn, account_id, last_error=None,
                                     last_ok_at=datetime.now().isoformat())
    return jsonify({
        "ok": True, "folder": folder,
        "items": [{"id": i.id, "name": i.name, "is_folder": i.is_folder,
                   "size": i.size} for i in items],
    })


@app.route("/api/admin/settings", methods=["GET", "POST"])
def api_admin_settings():
    if not is_admin():
        return jsonify({"ok": False, "error": "нужны права администратора"}), 403
    conn = get_db()

    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        who = session.get("viewer_name", "аноним")
        for key, value in data.items():
            if key in sar_common.SETTINGS_SCHEMA:
                sar_common.set_setting(conn, key, value, who=who)
            # Неизвестный ключ молча пропускаем: он может прийти от старой
            # вкладки, открытой до обновления. Ронять сохранение остальных
            # настроек из-за этого неправильно.

    # Схема отдаётся вместе со значениями: форма строится из неё, и второй
    # список полей во фронтенде заводить нельзя -- разойдётся с реестром.
    schema = {}
    rows = {r["key"]: r["set_by"] for r in
            conn.execute("SELECT key, set_by FROM settings").fetchall()}
    for key, spec in sar_common.SETTINGS_SCHEMA.items():
        schema[key] = dict(spec)
        schema[key]["set_by"] = rows.get(key)
    return jsonify({"ok": True, "schema": schema,
                    "values": sar_common.get_settings(conn)})


# ---------------------------------------------------------------------------
# ВЫГРУЗКА НАХОДОК КООРДИНАТОРУ
#
# Координатор на месте работает не в нашей платформе, а в своей карте или
# навигаторе. Пока координаты живут только внутри системы, они бесполезны
# ровно там, где нужны.
#
# ГЛАВНОЕ В ЭТОЙ ВЫГРУЗКЕ -- РАЗДЕЛЕНИЕ ДВУХ РАЗНЫХ ТОЧЕК:
#
#   * позиция ДРОНА в момент пометки -- то, что пишет телеметрия;
#   * вероятная точка ОБЪЕКТА на земле -- расчёт по наклону подвеса,
#     высоте и положению рамки в кадре (sar_common.estimate_ground_point).
#
# На реальном материале они расходятся на сотни метров: при высоте больше
# километра и наклоне камеры к горизонту объект оказывается в 653 метрах от
# точки под дроном. Подписать одно другим -- увести поиск в соседнее ущелье.
# Поэтому в KML это РАЗНЫЕ ПАПКИ с разными значками, а в GPX -- разный тип
# точки и пометка прямо в названии.
# ---------------------------------------------------------------------------

def _xml_escape(v):
    return (str("" if v is None else v)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _finding_export_point(f):
    """Точка, которая попадёт в выгрузку, или None.

    ЕДИНСТВЕННОЕ место, где решается "у этой находки есть координата".
    Список находок показывает счётчик рядом с кнопками выгрузки, и если
    считать его отдельным правилом, счётчик и файл разойдутся молча:
    кнопка скажет "12", в файле окажется 15. Ровно так уже расходился
    путь к папке резервных копий -- см. грабли в CLAUDE.md.

    Возвращает (широта, долгота, посчитан_ли_объект).
    """
    est_lat, est_lon = f.get("est_lat"), f.get("est_lon")
    if est_lat is not None and est_lon is not None:
        return est_lat, est_lon, True
    lat, lon = f.get("lat"), f.get("lon")
    if lat is not None and lon is not None:
        return lat, lon, False
    return None


def _findings_for_export(conn, op_id):
    """Пометки операции, у которых есть хоть какие-то координаты."""
    rows = conn.execute(
        "SELECT o.id, o.label, o.note, o.timestamp_sec, o.viewer_name, "
        "       o.lat, o.lon, o.est_lat, o.est_lon, o.est_distance_m, "
        "       o.created_at, r.rel_path, p.priority "
        "FROM manual_observations o "
        "JOIN operation_materials m ON m.report_id = o.report_id "
        "JOIN reports r ON r.report_id = o.report_id "
        "LEFT JOIN detection_priorities p "
        "  ON p.kind='manual' AND p.ref_key = CAST(o.id AS TEXT) "
        "WHERE m.operation_id=? AND (o.lat IS NOT NULL OR o.est_lat IS NOT NULL) "
        "ORDER BY o.id", (op_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        point = _finding_export_point(d)
        if point is None:
            # Половинчатая координата (одна из пары пустая) не годится ни
            # для карты, ни для навигатора. Отбор в SQL этого не ловит:
            # там проверяются РАЗНЫЕ пары полей. Пропускаем строку -- иначе
            # выгрузка целиком падает из-за одной кривой.
            continue
        d["plat"], d["plon"], d["estimated"] = point
        d["title"] = d["label"] or ("Находка №%d" % d["id"])
        d["status"] = sar_common.PRIORITY_LABELS.get(d["priority"], "")
        secs = d["timestamp_sec"]
        d["tc"] = ("%02d:%02d" % (int(secs) // 60, int(secs) % 60)
                   if secs is not None else "")
        d["file"] = os.path.basename(d["rel_path"] or "")
        d["link"] = finding_share_link(d["id"])
        out.append(d)
    return out


def _finding_description(f):
    bits = []
    if f["estimated"]:
        dist = (" (расчётная дальность %d м)" % int(f["est_distance_m"])
                if f["est_distance_m"] is not None else "")
        bits.append("ВЕРОЯТНАЯ ТОЧКА ОБЪЕКТА%s. Расчёт по телеметрии, "
                    "а не измерение -- проверяйте на месте." % dist)
        if f["lat"] is not None:
            bits.append("Дрон в этот момент: %.6f, %.6f" % (f["lat"], f["lon"]))
    else:
        bits.append("ПОЗИЦИЯ ДРОНА, не объекта. Объект находится в стороне: "
                    "расчёт точки на земле для этой пометки не выполнен.")
    if f["note"]:
        bits.append(f["note"])
    if f["status"]:
        bits.append("Статус проверки: %s" % f["status"])
    if f["tc"]:
        bits.append("Запись %s, таймкод %s" % (f["file"], f["tc"]))
    if f["viewer_name"]:
        bits.append("Отметил: %s" % f["viewer_name"])
    if f["link"]:
        bits.append(f["link"])
    return "\n".join(bits)


def _findings_kml(op_title, items):
    est = [f for f in items if f["estimated"]]
    drone = [f for f in items if not f["estimated"]]

    def placemarks(rows, style):
        out = []
        for f in rows:
            out.append(
                "    <Placemark>\n"
                "      <name>%s</name>\n"
                "      <description>%s</description>\n"
                "      <styleUrl>#%s</styleUrl>\n"
                "      <Point><coordinates>%.6f,%.6f,0</coordinates></Point>\n"
                "    </Placemark>" % (
                    _xml_escape(f["title"]), _xml_escape(_finding_description(f)),
                    style, f["plon"], f["plat"]))
        return "\n".join(out)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
        "  <name>%s — находки</name>\n"
        "  <description>Выгружено из платформы разбора аэровидеосъёмки. "
        "Точки двух разных видов, см. папки.</description>\n"
        '  <Style id="est"><IconStyle><color>ff2a2aff</color><scale>1.2</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/target.png</href>'
        "</Icon></IconStyle></Style>\n"
        '  <Style id="drone"><IconStyle><color>ff20a5da</color><scale>0.9</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href>'
        "</Icon></IconStyle></Style>\n"
        "  <Folder><name>Вероятные точки объектов (%d)</name>\n%s\n  </Folder>\n"
        "  <Folder><name>Позиции дрона — объект в стороне (%d)</name>\n%s\n  </Folder>\n"
        "</Document></kml>\n" % (
            _xml_escape(op_title), len(est), placemarks(est, "est"),
            len(drone), placemarks(drone, "drone")))


def _findings_gpx(op_title, items):
    pts = []
    for f in items:
        # В GPX нет папок, поэтому вид точки уходит в название и в <type>:
        # на экране навигатора видно только имя.
        name = f["title"] if f["estimated"] else f["title"] + " [дрон]"
        pts.append(
            '  <wpt lat="%.6f" lon="%.6f">\n'
            "    <name>%s</name>\n"
            "    <desc>%s</desc>\n"
            "    <type>%s</type>\n"
            "  </wpt>" % (
                f["plat"], f["plon"], _xml_escape(name),
                _xml_escape(_finding_description(f)),
                "объект" if f["estimated"] else "позиция дрона"))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="SAR Review" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        "  <metadata><name>%s — находки</name></metadata>\n%s\n</gpx>\n"
        % (_xml_escape(op_title), "\n".join(pts)))


# Разрыв, после которого просмотр считается НОВЫМ.
#
# «Количество просмотров» по отрезкам бессмысленно: один человек за один
# заход даёт их сотни (на боевых данных 2467 отрезков от одного человека).
# Считать надо заходы: подряд идущие отрезки одного человека по одному
# материалу с паузой меньше этой -- один просмотр.
SESSION_GAP_SEC = 30 * 60

ALIAS_LETTERS = "АБВГДЕЖЗИКЛМНПРСТУФХЦЧШЩЭЮЯ"


def _alias_for(i):
    """волонтёр А, Б, ... АА, АБ -- если людей больше, чем букв."""
    if i < len(ALIAS_LETTERS):
        return "волонтёр " + ALIAS_LETTERS[i]
    a, b = divmod(i - len(ALIAS_LETTERS), len(ALIAS_LETTERS))
    return "волонтёр " + ALIAS_LETTERS[a] + ALIAS_LETTERS[b]


def _report_aliases(conn, op_id):
    """Устойчивое обезличивание: один человек -- один псевдоним ВЕЗДЕ.

    Псевдонимы раздаются по убыванию вклада, поэтому «волонтёр А» -- это
    всегда тот, кто сделал больше всех. Структурный вывод («двое сделали
    больше половины») сохраняется, а имена нет.

    Собирается ОДИН словарь на весь отчёт и применяется при сборке данных,
    а не при отрисовке. Фильтр, который надо не забыть применить в каждом
    месте, однажды забудут -- тот же урок, что с токенами облачных
    подключений (`cloud_accounts_public`).
    """
    rows = conn.execute(
        "SELECT s.viewer_name AS who, SUM(s.end_sec - s.start_sec) AS secs "
        "FROM watch_segments s "
        "JOIN operation_materials m ON m.report_id = s.report_id "
        "WHERE m.operation_id=? GROUP BY s.viewer_name "
        "ORDER BY secs DESC", (op_id,)).fetchall()
    order = [r["who"] for r in rows]
    # Люди, которые ничего не смотрели, но ставили пометки или писали
    # комментарии, тоже должны получить псевдоним -- иначе их имя останется
    # в списке находок открытым текстом.
    for extra in conn.execute(
            "SELECT DISTINCT o.viewer_name AS who FROM manual_observations o "
            "JOIN operation_materials m ON m.report_id = o.report_id "
            "WHERE m.operation_id=?", (op_id,)):
        if extra["who"] not in order:
            order.append(extra["who"])
    for extra in conn.execute(
            "SELECT DISTINCT viewer_name AS who FROM map_marks "
            "WHERE operation_id=?", (op_id,)):
        if extra["who"] not in order:
            order.append(extra["who"])
    return {name: _alias_for(i) for i, name in enumerate(order) if name}


def _period_bounds(args):
    """Границы периода из запроса. Пустые -- вся операция.

    `to` включает весь указанный день: человек, выбравший «по 15 августа»,
    имеет в виду 15-е целиком, а не полночь на его начало. Сравнение идёт
    со строкой ISO, поэтому достаточно прибавить сутки и сравнивать строго.
    """
    frm = (args.get("from") or "").strip()[:10] or None
    to = (args.get("to") or "").strip()[:10] or None
    to_excl = None
    if to:
        try:
            d = datetime.strptime(to, "%Y-%m-%d") + timedelta(days=1)
            to_excl = d.strftime("%Y-%m-%d")
        except ValueError:
            to = None
    return frm, to, to_excl


def _period_sql(column, frm, to_excl):
    """Кусок WHERE и параметры для отбора по периоду."""
    sql, params = "", []
    if frm:
        sql += " AND %s >= ?" % column
        params.append(frm)
    if to_excl:
        sql += " AND %s < ?" % column
        params.append(to_excl)
    return sql, params


def _sessions(times):
    """Сколько РАЗ смотрели, а не сколько отрезков записано."""
    if not times:
        return 0
    times = sorted(times)
    n, prev = 1, times[0]
    for t in times[1:]:
        if (t - prev).total_seconds() > SESSION_GAP_SEC:
            n += 1
        prev = t
    return n


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _material_coverage(conn, op_id, frm, to_excl):
    """Покрытие по каждому материалу за период.

    Считается ПО ОТРЕЗКАМ, попавшим в период: «отчёт за 15 августа» --
    это работа, сделанная 15-го, а не материал, снятый 15-го. Материал,
    отснятый раньше, но разобранный в этот день, в отчёт попадает.
    """
    where, params = _period_sql("s.ts", frm, to_excl)
    rows = conn.execute(
        "SELECT s.report_id, s.viewer_name, s.start_sec, s.end_sec, s.ts "
        "FROM watch_segments s "
        "JOIN operation_materials m ON m.report_id = s.report_id "
        "WHERE m.operation_id=?" + where, [op_id] + params).fetchall()

    per = {}
    for r in rows:
        d = per.setdefault(r["report_id"],
                           {"intervals": [], "viewers": {}, "times": []})
        d["intervals"].append((r["start_sec"] or 0, r["end_sec"] or 0))
        t = _parse_ts(r["ts"])
        if t:
            d["viewers"].setdefault(r["viewer_name"], []).append(t)
            d["times"].append(t)
    return per


def _findings_per_material(conn, op_id, frm, to_excl):
    where, params = _period_sql("o.created_at", frm, to_excl)
    rows = conn.execute(
        "SELECT o.report_id, COUNT(*) n FROM manual_observations o "
        "JOIN operation_materials m ON m.report_id = o.report_id "
        "WHERE m.operation_id=?" + where + " GROUP BY o.report_id",
        [op_id] + params).fetchall()
    return {r["report_id"]: r["n"] for r in rows}


@app.route("/api/operations/<int:op_id>/report")
def api_operation_report(op_id):
    """Отчёт по операции: объём, покрытие, находки, люди, оговорки.

    ПЕРИОД ФИЛЬТРУЕТ РАБОТУ, а не дату съёмки. «Отчёт за 15 августа» --
    это что сделали 15-го: какие отрезки посмотрели, какие пометки
    поставили. Материал, снятый раньше, но разобранный в этот день,
    попадает; снятый в этот день, но никем не открытый, -- виден в
    таблице с нулевым покрытием.

    `anon=1` обезличивает: один человек -- один псевдоним ВЕЗДЕ, включая
    список находок. Псевдонимы по убыванию вклада, поэтому структурный
    вывод («двое сделали больше половины») сохраняется, а имена нет.
    """
    conn = get_db()
    op = conn.execute(
        "SELECT id, title, area FROM operations WHERE id=?", (op_id,)).fetchone()
    if op is None:
        return jsonify({"error": "операция не найдена"}), 404

    frm, to, to_excl = _period_bounds(request.args)
    anon = request.args.get("anon") in ("1", "true", "yes")
    alias = _report_aliases(conn, op_id) if anon else {}
    who = lambda name: alias.get(name, name) if anon else name

    mats = conn.execute(
        "SELECT r.report_id, r.rel_path, r.kind, r.duration_sec, r.status "
        "FROM reports r JOIN operation_materials m ON m.report_id = r.report_id "
        "WHERE m.operation_id=? ORDER BY r.rel_path", (op_id,)).fetchall()
    cov = _material_coverage(conn, op_id, frm, to_excl)
    finds = _findings_per_material(conn, op_id, frm, to_excl)

    video, photo = [], []
    for r in mats:
        c = cov.get(r["report_id"])
        dur = r["duration_sec"] or 0
        watched = 0.0
        if c:
            watched = sum(e - s for s, e in merge_intervals(c["intervals"]))
        # Заходы считаются ПО КАЖДОМУ ЧЕЛОВЕКУ отдельно и складываются.
        # По общей ленте времени двое, смотревшие одновременно, слипались
        # в один просмотр -- и выходило «просмотров 6, людей 8».
        views = (sum(_sessions(t) for t in c["viewers"].values()) if c else 0)
        row = {
            "report_id": r["report_id"],
            "name": os.path.basename(r["rel_path"] or ""),
            "folder": os.path.dirname(r["rel_path"] or ""),
            "views": views,
            "viewers": len(c["viewers"]) if c else 0,
            "findings": finds.get(r["report_id"], 0),
        }
        if r["kind"] == "video":
            row["duration_sec"] = dur
            # Без известной длительности процент посчитать не из чего, и
            # показывать ноль нельзя -- это читается как «не смотрели».
            row["coverage_pct"] = (round(min(100.0, watched / dur * 100), 1)
                                   if dur > 0 else None)
            row["watched_sec"] = round(watched, 1)
            video.append(row)
        else:
            photo.append(row)

    people = []
    agg = {}
    for c in cov.values():
        for name, times in c["viewers"].items():
            a = agg.setdefault(name, {"times": [], "materials": 0})
            a["times"] += times
            a["materials"] += 1
    seg_where, seg_params = _period_sql("s.ts", frm, to_excl)
    secs = {r["who"]: r["secs"] for r in conn.execute(
        "SELECT s.viewer_name AS who, SUM(s.end_sec - s.start_sec) AS secs "
        "FROM watch_segments s "
        "JOIN operation_materials m ON m.report_id = s.report_id "
        "WHERE m.operation_id=?" + seg_where + " GROUP BY s.viewer_name",
        [op_id] + seg_params)}
    mark_where, mark_params = _period_sql("o.created_at", frm, to_excl)
    marks_by = {r["who"]: r["n"] for r in conn.execute(
        "SELECT o.viewer_name AS who, COUNT(*) n FROM manual_observations o "
        "JOIN operation_materials m ON m.report_id = o.report_id "
        "WHERE m.operation_id=?" + mark_where + " GROUP BY o.viewer_name",
        [op_id] + mark_params)}
    for name, a in agg.items():
        people.append({
            "name": who(name),
            "seconds": round(secs.get(name, 0) or 0, 1),
            "materials": a["materials"],
            "marks": marks_by.get(name, 0),
        })
    for name, n in marks_by.items():
        if name not in agg:
            people.append({"name": who(name), "seconds": 0,
                           "materials": 0, "marks": n})
    people.sort(key=lambda p: (-p["seconds"], -p["marks"], p["name"]))

    findings = _findings_for_export(conn, op_id)
    by_status = {}
    fw, fp = _period_sql("o.created_at", frm, to_excl)
    total_marks = conn.execute(
        "SELECT COUNT(*) FROM manual_observations o "
        "JOIN operation_materials m ON m.report_id = o.report_id "
        "WHERE m.operation_id=?" + fw, [op_id] + fp).fetchone()[0]
    for r in conn.execute(
            "SELECT p.priority, COUNT(*) n FROM detection_priorities p "
            "GROUP BY p.priority"):
        by_status[sar_common.PRIORITY_LABELS.get(r["priority"], r["priority"])] = r["n"]

    tracks = conn.execute(
        "SELECT COUNT(*) FROM telemetry_tracks t "
        "JOIN operation_materials m ON m.report_id = t.report_id "
        "WHERE m.operation_id=? AND t.points <> '[]'", (op_id,)).fetchone()[0]

    watched_videos = sum(1 for v in video if v["views"] > 0)
    footage = sum(v["duration_sec"] or 0 for v in video)
    watched_total = sum(v["watched_sec"] for v in video)

    return jsonify({
        "operation": {"id": op["id"], "title": op["title"], "area": op["area"]},
        "period": {"from": frm, "to": to, "full": not (frm or to)},
        "anonymized": anon,
        "volume": {
            "materials": len(mats),
            "videos": len(video),
            "photos": len(photo),
            "footage_sec": round(footage, 1),
            "footage_known": sum(1 for v in video if v["duration_sec"]),
            "viewer_sec": round(sum(p["seconds"] for p in people), 1),
        },
        "coverage": {
            "videos_touched": watched_videos,
            "videos_untouched": len(video) - watched_videos,
            "watched_sec": round(watched_total, 1),
            # ПРОСМОТР ФОТОГРАФИЙ ПЛАТФОРМА НЕ ОТСЛЕЖИВАЕТ.
            #
            # Отрезки просмотра пишет только плеер (по timeupdate) и пинг
            # при открытии сцены. У снимка ни того, ни другого нет, и на
            # боевых данных это ноль отрезков на 92 фото.
            #
            # Показать «просмотрено 0 из 92» было бы ложью: это не «никто
            # не смотрел», а «мы не измеряем». Отдаём признак, а не число,
            # чтобы страница написала об этом словами.
            "photos_tracked": bool(sum(p["views"] for p in photo)),
            "photos_total": len(photo),
        },
        "second_pass": _second_pass_histogram(video + photo),
        "findings": {
            "total": total_marks,
            "by_status": by_status,
            "with_object_point": sum(1 for f in findings if f["estimated"]),
            "drone_only": sum(1 for f in findings if not f["estimated"]),
            "without_coords": total_marks - len(findings),
        },
        "geography": {"tracks": tracks, "videos": len(video)},
        "people": people,
        "materials": {"video": video, "photo": photo},
    })


def _second_pass_histogram(rows):
    """Сколько материалов видели один человек, двое, трое...

    Учёт второго прохода -- третий пункт в приоритетах платформы: важно не
    «сколько посмотрели», а «сколько посмотрели ДВАЖДЫ».
    """
    hist = {}
    for r in rows:
        hist[r["viewers"]] = hist.get(r["viewers"], 0) + 1
    return [{"viewers": k, "materials": hist[k]} for k in sorted(hist)]


@app.route("/api/operations/<int:op_id>/findings-map")
def api_findings_map(op_id):
    """Точки для карты внутри платформы.

    Берёт ТУ ЖЕ сборку, что и выгрузка в KML/GPX (_findings_for_export):
    разделение «вероятная точка объекта» и «позиция дрона» посчитано там
    один раз, и считать его второй раз здесь значило бы завести два
    расходящихся ответа на один вопрос.

    Отдаёт и то, сколько находок на карту НЕ ПОПАЛО. Карта, молча
    скрывающая половину пометок, хуже отсутствия карты: по ней делают
    вывод, что искать больше негде.
    """
    conn = get_db()
    op = conn.execute("SELECT id, title FROM operations WHERE id=?",
                      (op_id,)).fetchone()
    if op is None:
        return jsonify({"error": "операция не найдена"}), 404

    items = _findings_for_export(conn, op_id)
    total = conn.execute(
        "SELECT COUNT(*) FROM manual_observations o "
        "JOIN operation_materials m ON m.report_id = o.report_id "
        "WHERE m.operation_id=?", (op_id,)).fetchone()[0]

    points = [{
        "id": f["id"],
        "lat": f["plat"],
        "lon": f["plon"],
        "estimated": bool(f["estimated"]),
        "title": f["title"],
        "status": f["status"],
        "note": f["note"] or "",
        "file": f["file"],
        "tc": f["tc"],
        "viewer": f["viewer_name"] or "",
        # Пара «где был дрон» для тех точек, где посчитана точка объекта:
        # линия между ними показывает разнос нагляднее любой подписи. На
        # материале этой операции он доходит до 653 метров.
        "drone": ({"lat": f["lat"], "lon": f["lon"]}
                  if f["estimated"] and f["lat"] is not None else None),
        "distance_m": f["est_distance_m"],
    } for f in items]

    marks = [dict(m) for m in conn.execute(
        "SELECT id, lat, lon, label, note, viewer_name, created_at "
        "FROM map_marks WHERE operation_id=? ORDER BY id", (op_id,)).fetchall()]

    return jsonify({
        "operation": op["title"],
        "points": points,
        "marks": marks,
        "total": total,
        "without_coords": total - len(points),
        "estimated": sum(1 for p in points if p["estimated"]),
        "drone_only": sum(1 for p in points if not p["estimated"]),
    })


@app.route("/api/operations/<int:op_id>/tracks")
def api_operation_tracks(op_id):
    """Треки дрона по видео операции -- то, что реально облетели.

    Это географическая версия покрытия: по находкам видно, где смотрели
    внимательно, а по трекам -- куда вообще летали. Пустое место на карте
    при полном списке материалов значит «туда не летали», и увидеть это
    можно только так.

    ТОЛЬКО ЧИТАЕТ. Разбор SRT делает воркер (ensure_telemetry_tracks) и
    складывает готовый трек в telemetry_tracks. Раньше это считалось
    здесь, по запросу: 114 файлов на каждый холодный запрос (2,9 с), кеш
    в памяти процесса, умирающий при перезапуске, и обработка в слое,
    который по устройству проекта обрабатывать не должен.

    «Ещё не разобрано» и «разобрано, телеметрии нет» -- разные вещи, и
    считаются раздельно: первое пройдёт само, второе не изменится никогда.
    """
    conn = get_db()
    if conn.execute("SELECT 1 FROM operations WHERE id=?", (op_id,)).fetchone() is None:
        return jsonify({"error": "операция не найдена"}), 404

    rows = conn.execute(
        "SELECT r.report_id, r.rel_path, t.points, t.raw_points, t.last_sec "
        "FROM reports r "
        "JOIN operation_materials m ON m.report_id = r.report_id "
        "LEFT JOIN telemetry_tracks t ON t.report_id = r.report_id "
        "WHERE m.operation_id=? AND r.kind='video' ORDER BY r.rel_path",
        (op_id,)).fetchall()

    tracks, without, pending = [], 0, 0
    for row in rows:
        if row["points"] is None:
            pending += 1
            continue
        try:
            pts = json.loads(row["points"])
        except (TypeError, ValueError):
            pts = []
        if len(pts) < 2:
            without += 1
            continue
        tracks.append({
            "report_id": row["report_id"],
            "name": os.path.basename(row["rel_path"] or ""),
            "points": pts,
            "raw_points": row["raw_points"],
            "seconds": row["last_sec"],
        })

    return jsonify({
        "tracks": tracks,
        "videos": len(rows),
        "without_telemetry": without,
        "pending": pending,
    })


@app.route("/api/operations/<int:op_id>/map-marks", methods=["POST"])
def api_map_mark_add(op_id):
    """Точка, поставленная человеком прямо на карте.

    Координаты приходят от клика, а не из телеметрии -- поэтому это
    ЗНАНИЕ ЧЕЛОВЕКА, а не оценка платформы, и на карте она показывается
    третьим, отдельным значком. Смешать её с «вероятной точкой объекта»
    значило бы выдать чужое наблюдение за расчёт по телеметрии.
    """
    d = request.get_json(silent=True) or {}
    try:
        lat, lon = float(d.get("lat")), float(d.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "нужны координаты"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "координаты вне допустимого диапазона"}), 400

    conn = get_db()
    if conn.execute("SELECT 1 FROM operations WHERE id=?", (op_id,)).fetchone() is None:
        return jsonify({"error": "операция не найдена"}), 404
    cur = conn.execute(
        "INSERT INTO map_marks (operation_id, lat, lon, label, note, "
        "viewer_name, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
        (op_id, lat, lon, (d.get("label") or "").strip()[:120] or None,
         (d.get("note") or "").strip()[:2000] or None,
         session.get("viewer_name") or "—"))
    conn.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/operations/<int:op_id>/map-marks/<int:mark_id>",
           methods=["DELETE"])
def api_map_mark_delete(op_id, mark_id):
    """Убрать свою точку.

    Чужие точки может убирать только модератор: поставленная координатором
    отметка «сюда идёт группа» -- это указание, и стирать его посторонний
    не должен.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT viewer_name FROM map_marks WHERE id=? AND operation_id=?",
        (mark_id, op_id)).fetchone()
    if row is None:
        return jsonify({"error": "точка не найдена"}), 404
    if row["viewer_name"] != (session.get("viewer_name") or "—") \
            and not is_moderator():
        return jsonify({"error": "чужую точку может убрать только модератор"}), 403
    conn.execute("DELETE FROM map_marks WHERE id=?", (mark_id,))
    conn.commit()
    return jsonify({"ok": True})


@app.route("/api/operations/<int:op_id>/findings.<fmt>")
def api_findings_export(op_id, fmt):
    """Находки операции для карты или навигатора координатора."""
    if fmt not in ("kml", "gpx"):
        return "Неизвестный формат", 404
    conn = get_db()
    op = conn.execute("SELECT title FROM operations WHERE id=?", (op_id,)).fetchone()
    if op is None:
        return "Операция не найдена", 404
    items = _findings_for_export(conn, op_id)
    body = (_findings_kml(op["title"], items) if fmt == "kml"
            else _findings_gpx(op["title"], items))
    safe = re.sub(r"[^\w\-. ]+", "_", op["title"] or "operation").strip() or "operation"
    resp = make_response(body)
    resp.headers["Content-Type"] = (
        "application/vnd.google-earth.kml+xml; charset=utf-8" if fmt == "kml"
        else "application/gpx+xml; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        "attachment; filename*=UTF-8''%s"
        % urllib.parse.quote("%s — находки.%s" % (safe, fmt)))
    return resp


# ---------------------------------------------------------------------------
# СТРАНИЦА КАДРА НАХОДКИ
#
# Окно предпросмотра хорошо для беглого взгляда, но у него потолок: оно
# маленькое, живёт по наведению курсора и исчезает, стоит его увести. Когда
# находку надо РАЗГЛЯДЫВАТЬ -- нужен отдельный экран, который не закроется
# сам, который можно открыть в новой вкладке и на который можно дать ссылку
# другому человеку.
#
# Рамка здесь рисуется поверх кадра в SVG и выключается галочкой. Это не
# украшение: обводка притягивает взгляд, и посмотреть на участок "своими
# глазами", без подсказки, иначе невозможно.
# ---------------------------------------------------------------------------

def html_escape(value):
    """Экранирование текста для вставки в HTML-шаблон.

    В sar_server.py такой функции не было: страницы собираются через
    .format(), а подписи находок пишут люди -- кавычка или угловая скобка
    в подписи ломала бы разметку страницы.
    """
    return (str("" if value is None else value)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_EXTERNAL_BASE_CACHE = {"url": None, "bot": "", "at": 0.0}
_EXTERNAL_BASE_TTL = 30.0


@app.route("/api/external_base")
def api_external_base():
    """Внешний адрес платформы для постоянных ссылок.

    Отдельным запросом, а не в шаблоне страницы: адрес быстрого туннеля
    меняется при каждом падении канала, и страница, открытая до падения,
    копировала бы мёртвые ссылки до перезагрузки.
    """
    return jsonify({"base": external_base(), "bot": bot_username()})


def external_base():
    """Внешний адрес платформы для постоянных ссылок, либо "".

    Живёт в telegram_bot.service_url -- туда его пишет sar_tunnel.py при
    каждом подъёме туннеля. Читаем файл заново (с коротким кэшем), а не
    один раз при старте: быстрый туннель меняет адрес при каждом падении,
    а сервер при этом не перезапускается. Прочитанное на старте значение
    протухало бы, и кнопка "копировать ссылку" выдавала бы мёртвый адрес --
    ровно то, ради чего эта кнопка и делалась.
    """
    now = time.time()
    if _EXTERNAL_BASE_CACHE["url"] is not None and \
            now - _EXTERNAL_BASE_CACHE["at"] < _EXTERNAL_BASE_TTL:
        return _EXTERNAL_BASE_CACHE["url"]
    url, bot = "", ""
    try:
        with open(os.path.join(SCRIPT_DIR, "sar_config.json"), encoding="utf-8") as f:
            tb = json.load(f).get("telegram_bot") or {}
        url = (tb.get("service_url") or "").strip()
        bot = (tb.get("bot_username") or "").strip().lstrip("@")
    except Exception:                                   # noqa: BLE001
        # Конфига нет или он битый -- ссылка просто будет относительной.
        # Ронять из-за этого страницу находки нельзя.
        url, bot = "", ""
    url = url.rstrip("/")
    _EXTERNAL_BASE_CACHE.update({"url": url, "bot": bot, "at": now})
    return url


def bot_username():
    """Имя бота для вечных ссылок. Заполняет сам бот при старте."""
    external_base()          # заодно обновит кэш, если он протух
    return _EXTERNAL_BASE_CACHE.get("bot") or ""


def finding_share_link(obs_id):
    """ВЕЧНАЯ ссылка на находку.

    Прямой адрес платформы живёт только до следующего перезапуска туннеля:
    имя быстрого туннеля случайное, старое исчезает из DNS, и перенаправить
    с него невозможно -- домена больше нет, запрос до нас не доходит.

    Адрес t.me не меняется никогда. Поэтому делимся ссылкой на бота: он
    знает текущий адрес платформы (его туда пишет sar_tunnel.py) и выдаст
    рабочую персональную ссылку прямо на эту находку.

    Если имени бота ещё нет -- отдаём прямую ссылку: она хотя бы работает
    сейчас, и это лучше, чем ничего.
    """
    bot = bot_username()
    if bot:
        return f"https://t.me/{bot}?start=finding_{int(obs_id)}"
    base = external_base()
    return f"{base}/finding/{int(obs_id)}/" if base else ""


FINDING_FRAME_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Находка — {short}</title>
<style>
body {{ font-family:-apple-system,Arial,sans-serif; background:#111; color:#eee;
        margin:0; padding:18px; }}
a {{ color:#8ecbff; }}
.crumbs {{ font-size:13px; color:#888; margin:0 0 10px; }}
.crumbs a {{ color:#6bb; text-decoration:none; }}
.crumbs a:hover {{ text-decoration:underline; }}
h1 {{ font-size:17px; margin:6px 0 4px; }}
.meta {{ font-size:13px; color:#9aa4ad; margin:0 0 12px; }}
.meta b {{ color:#d7dee3; font-weight:600; }}
.bar {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:0 0 12px;
        font-size:13px; }}
.btn {{ display:inline-flex; align-items:center; gap:7px; border:1px solid #3a4650;
        background:#1b2229; color:#dfe7ec; border-radius:7px; padding:7px 12px;
        cursor:pointer; font-size:13px; text-decoration:none; }}
.btn:hover {{ background:#242e37; }}
.btn.main {{ border-color:#2b6b74; background:#12363b; color:#c9f0f5; }}
.btn.main:hover {{ background:#17454b; }}
.chk {{ display:inline-flex; align-items:center; gap:6px; color:#c2cbd2;
        user-select:none; cursor:pointer; }}
.view {{ position:relative; width:100%; height:74vh; max-height:78vh; overflow:hidden; background:#000;
         border:1px solid #2a333b; border-radius:8px; touch-action:none;
         cursor:zoom-in; }}
.zoom {{ position:absolute; top:0; left:0; transform-origin:0 0; will-change:transform; }}
.zoom img {{ display:block; width:100%; user-select:none; -webkit-user-drag:none; }}
.box {{ position:absolute; inset:0; width:100%; height:100%; pointer-events:none;
        overflow:visible; }}
.box rect {{ fill:none; stroke:#ffd24a; stroke-width:1.5;
             vector-effect:non-scaling-stroke; }}
.box rect.under {{ stroke:rgba(0,0,0,.75); stroke-width:3; }}
.hint {{ font-size:12px; color:#78838c; margin:9px 0 0; }}
.cols {{ display:grid; grid-template-columns:1fr 330px; gap:18px; align-items:start; }}
@media (max-width:900px) {{ .cols {{ grid-template-columns:1fr; }} }}
.side {{ display:flex; flex-direction:column; gap:16px; }}
.fld label {{ display:block; font-size:11px; letter-spacing:.08em;
              text-transform:uppercase; color:#78838c; margin:0 0 6px; }}
.fld select {{ width:100%; background:#1b2229; color:#dfe7ec; font-size:13px;
               border:1px solid #3a4650; border-radius:7px; padding:7px 9px; }}
.by {{ font-size:11.5px; color:#78838c; margin-top:5px; }}
.note {{ background:#161d23; border:1px solid #26313a; border-radius:8px;
         padding:10px 13px; font-size:13.5px; color:#d3dbe1;
         white-space:pre-wrap; word-wrap:break-word; }}
.gps {{ font-size:12.5px; color:#9aa4ad; line-height:1.7; }}
.gps b {{ color:#d7dee3; font-weight:600; }}
.gps a {{ color:#8ecbff; }}
.gps .est {{ color:#c9f0f5; }}
.cmts {{ display:flex; flex-direction:column; gap:9px; margin-bottom:9px; }}
.cmt {{ background:#161d23; border:1px solid #26313a; border-radius:8px;
        padding:8px 11px; }}
.cmt-head {{ display:flex; gap:8px; align-items:baseline; font-size:11.5px;
             color:#78838c; margin-bottom:4px; }}
.cmt-author {{ color:#a9d6e5; font-weight:600; }}
.cmt-del {{ margin-left:auto; background:none; border:none; color:#6b757e;
            cursor:pointer; font-size:14px; line-height:1; }}
.cmt-del:hover {{ color:#e0785f; }}
.cmt-text {{ font-size:13.5px; color:#dfe7ec; white-space:pre-wrap;
             word-wrap:break-word; }}
.cmt-empty {{ font-size:12.5px; color:#6b757e; }}
.cmt-form textarea {{ width:100%; background:#161d23; color:#dfe7ec;
  border:1px solid #3a4650; border-radius:7px; padding:8px 10px;
  font:13px/1.45 inherit; resize:vertical; }}
.cmt-actions {{ display:flex; align-items:center; gap:9px; margin-top:6px; }}
.cmt-actions button {{ border:1px solid #2b6b74; background:#12363b;
  color:#c9f0f5; border-radius:7px; padding:6px 13px; font-size:13px;
  cursor:pointer; }}
.cmt-actions button:disabled {{ opacity:.45; cursor:default; }}
.cmt-hint {{ font-size:11.5px; color:#6b757e; }}
.cmt-locked {{ font-size:12.5px; color:#a68a5b; }}
.toast {{ position:fixed; left:50%; bottom:26px; transform:translateX(-50%);
          background:#12363b; border:1px solid #2b6b74; color:#c9f0f5;
          padding:9px 16px; border-radius:8px; font-size:13px; opacity:0;
          pointer-events:none; transition:opacity .18s; }}
.toast.on {{ opacity:1; }}
/* Кадр режет ВОРКЕР, и между созданием пометки и готовым превью проходит
   до полуминуты (замерено на боевой находке: 24 с). Всё это время страница
   показывала битую картинку -- то есть «сломалось», хотя на деле «ещё не
   готово». В списке находок этот случай обрабатывался, а здесь нет. */
/* ЭТА СТРОКА НЕСУЩАЯ, не убирать.
   Атрибут hidden прячет элемент правилом БРАУЗЕРА [hidden]{{display:none}},
   а любой авторский display его перебивает -- авторские стили сильнее
   браузерных независимо от специфичности. Без неё display:flex ниже
   означал, что блок виден ВСЕГДА: непрозрачный фон закрывал готовый кадр,
   и над нормальным фото бесконечно крутился спиннер. */
.shot-wait[hidden] {{ display:none; }}
.shot-wait {{ position:absolute; inset:0; margin:0; display:flex;
              flex-direction:column; gap:10px;
              align-items:center; justify-content:center;
              font-size:13px; color:#7c8790; background:#15181b; }}
/* Тот же спиннер, что и в списках (static/spinner.gif) -- отдаётся своей
   статикой, потому что платформа обязана работать в поле без интернета. */
.shot-wait i {{ width:96px; height:96px; display:block; flex-shrink:0;
                background:url(/static/spinner.gif) center/contain no-repeat; }}
@media (prefers-reduced-motion:reduce) {{ .shot-wait i {{ display:none; }} }}
</style></head>
<body>
<p class="crumbs">{crumbs}</p>
<h1>{label}</h1>
<p class="meta">{meta}</p>

<div class="bar">
  {player_btn}
  <button class="btn" onclick="copyLink()">🔗 Копировать ссылку</button>
  <label class="chk"><input type="checkbox" id="showbox" checked> показывать рамку</label>
</div>

<div class="cols">
  <div class="left">
    <div class="view" id="view">
      <div class="zoom" id="zoom">
        <!-- onerror НЕ зовёт функцию: скрипт объявлен ниже по странице, и
             картинка успевает отвалиться раньше, чем он разобран. Вызов
             падал с «shotMissing is not defined», обработчик не выполнялся,
             и оставалась ровно битая картинка. Здесь только пометка, а
             разбирает её скрипт, когда бы тот ни загрузился. -->
        <img id="shot" src="{img_src}" alt="Кадр находки" onerror="this.dataset.failed='1'">
        <svg class="box" id="box" viewBox="0 0 1 1" preserveAspectRatio="none">
          <rect class="under"></rect><rect></rect>
        </svg>
      </div>
      <!-- Блок ожидания -- СОСЕД .zoom, а не его ребёнок. У .zoom нет
           собственных размеров: он подстраивается под картинку, и пока та
           не загрузилась, схлопывается почти в ноль. Внутри него inset:0
           давал коробочку в угол, куда кот не помещался. Здесь же контейнер
           во всю высоту просмотра. Заодно блок не уезжает вместе с
           масштабированием и панорамированием -- их transform висит на
           .zoom. -->
      <p class="shot-wait" id="shotwait" hidden><i></i><span>Кадр готовится…</span></p>
    </div>
    <p class="hint">Клик — приблизить, Shift+клик — отдалить, колесо — масштаб,
    перетаскивание — сдвиг кадра. Кадр показан целиком, без обрезки.</p>
  </div>

  <aside class="side">
    {note_block}
    <div class="fld">
      <label for="prio">Статус проверки</label>
      <select id="prio" onchange="setPriority(this.value)"></select>
      <div class="by" id="prio-by"></div>
    </div>
    {coords_block}
    <div class="fld">
      <label>Обсуждение <span id="cmt-count"></span></label>
      <div id="cmts" class="cmts"></div>
      {comment_form}
    </div>
  </aside>
</div>
<div class="toast" id="toast"></div>

<script>
const BBOX = {bbox_json};
const PERMALINK = {permalink_json};
const REPORT_ID = {report_id_json};
const OBS_ID = {obs_id};
const VIEWER_NAME = {viewer_name_json};
const IS_MODERATOR = {is_moderator_json};
const CAN_COMMENT = {can_comment_json};
const PRIORITY_LABELS = {priority_labels_json};
const view = document.getElementById('view');
const zoom = document.getElementById('zoom');
const box = document.getElementById('box');
let scale = 1, ox = 0, oy = 0, drag = null, press = null, pinch = 0;

if (BBOX && BBOX.length === 4) {{
  const x1 = Math.min(BBOX[0], BBOX[2]), x2 = Math.max(BBOX[0], BBOX[2]);
  const y1 = Math.min(BBOX[1], BBOX[3]), y2 = Math.max(BBOX[1], BBOX[3]);
  box.querySelectorAll('rect').forEach(r => {{
    r.setAttribute('x', x1); r.setAttribute('y', y1);
    r.setAttribute('width', Math.max(0, x2 - x1));
    r.setAttribute('height', Math.max(0, y2 - y1));
  }});
}} else {{
  box.style.display = 'none';
  document.getElementById('showbox').disabled = true;
}}

document.getElementById('showbox').addEventListener('change', e => {{
  box.style.display = e.target.checked ? '' : 'none';
}});

function apply() {{
  const w = view.clientWidth, h = view.clientHeight;
  ox = Math.min(0, Math.max(ox, w - w * scale));
  oy = Math.min(0, Math.max(oy, h - h * scale));
  zoom.style.transform = `translate(${{ox}}px, ${{oy}}px) scale(${{scale}})`;
  view.style.cursor = scale > 1 ? (drag ? 'grabbing' : 'grab') : 'zoom-in';
}}

function zoomAt(factor, at) {{
  const before = scale;
  scale = Math.min(12, Math.max(1, scale * factor));
  if (scale === before) return;
  const r = view.getBoundingClientRect();
  const cx = (at.clientX - r.left - ox) / before;
  const cy = (at.clientY - r.top - oy) / before;
  ox = at.clientX - r.left - cx * scale;
  oy = at.clientY - r.top - cy * scale;
  apply();
}}

view.addEventListener('wheel', e => {{
  e.preventDefault();
  zoomAt(e.deltaY < 0 ? 1.25 : 1 / 1.25, e);
}}, {{ passive: false }});

view.addEventListener('mousedown', e => {{
  if (e.button !== 0) return;
  e.preventDefault();
  press = {{ x: e.clientX, y: e.clientY, moved: 0 }};
  if (scale > 1) {{ drag = {{ x: e.clientX, y: e.clientY }}; apply(); }}
}});
document.addEventListener('mousemove', e => {{
  if (press) press.moved = Math.max(press.moved,
    Math.hypot(e.clientX - press.x, e.clientY - press.y));
  if (!drag) return;
  ox += e.clientX - drag.x; oy += e.clientY - drag.y;
  drag = {{ x: e.clientX, y: e.clientY }};
  apply();
}});
document.addEventListener('mouseup', e => {{
  const p = press; press = null;
  if (drag) {{ drag = null; apply(); }}
  if (!p || p.moved > 5) return;
  if (!e.target.closest('#view')) return;
  zoomAt(e.shiftKey ? 1 / 1.6 : 1.6, e);
}});

view.addEventListener('touchstart', e => {{
  if (e.touches.length === 1 && scale > 1)
    drag = {{ x: e.touches[0].clientX, y: e.touches[0].clientY }};
}}, {{ passive: true }});
view.addEventListener('touchmove', e => {{
  if (e.touches.length === 1 && drag) {{
    e.preventDefault();
    ox += e.touches[0].clientX - drag.x; oy += e.touches[0].clientY - drag.y;
    drag = {{ x: e.touches[0].clientX, y: e.touches[0].clientY }};
    apply(); return;
  }}
  if (e.touches.length !== 2) return;
  e.preventDefault();
  const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX,
                       e.touches[0].clientY - e.touches[1].clientY);
  if (pinch) zoomAt(d / pinch, {{
    clientX: (e.touches[0].clientX + e.touches[1].clientX) / 2,
    clientY: (e.touches[0].clientY + e.touches[1].clientY) / 2 }});
  pinch = d;
}}, {{ passive: false }});
view.addEventListener('touchend', () => {{ pinch = 0; drag = null; }});

function toast(text) {{
  const t = document.getElementById('toast');
  t.textContent = text; t.classList.add('on');
  setTimeout(() => t.classList.remove('on'), 2200);
}}

// Превью ещё не вырезано воркером. Это НЕ ошибка -- ждём и пробуем снова,
// иначе человек видит битую картинку и уходит, считая находку испорченной.
// Потолок нужен: если кадр не получился вовсе (исходника нет даже в облаке),
// бесконечный опрос молча грузил бы сервер до закрытия вкладки.
var shotTries = 0, shotTimer = null;
function shotMissing() {{
  var img = document.getElementById('shot'),
      wait = document.getElementById('shotwait');
  if (!img || !wait) return;
  img.style.visibility = 'hidden';
  wait.hidden = false;
  if (shotTries >= 12) {{           // ~1 минута
    // Спиннер УБИРАЕМ: крутилка над тем, что уже не придёт, обещает
    // несбыточное. Меняем только подпись, не всю разметку.
    var icon = wait.querySelector('i'), msg = wait.querySelector('span');
    if (icon) icon.style.display = 'none';
    if (msg) msg.textContent = 'Кадр не удалось подготовить';
    return;
  }}
  shotTries++;
  // ПОВТОР ПО ТАЙМЕРУ, А НЕ ПО СОБЫТИЮ 'error'.
  //
  // Раньше следующая попытка назначалась только из обработчика ошибки.
  // Если запрос не падал, а ЗАВИСАЛ -- сервер перезапустили, сеть моргнула,
  // туннель переподключился -- ошибки не происходило, и цепочка вставала
  // навсегда. Снаружи это выглядело как бесконечное «Кадр готовится…»
  // над кадром, который давно готов.
  //
  // Таймер идёт независимо и сам проверяет, появилась ли картинка.
  clearTimeout(shotTimer);
  shotTimer = setTimeout(function () {{
    if (img.naturalWidth > 0) {{      // успели загрузить -- ждать больше нечего
      img.style.visibility = '';
      wait.hidden = true;
      return;
    }}
    var base = img.src.split('&retry=')[0];
    img.src = base + '&retry=' + shotTries;
    shotMissing();                    // следующая попытка не ждёт ошибки
  }}, 5000);
}}
document.addEventListener('DOMContentLoaded', function () {{
  var img = document.getElementById('shot'),
      wait = document.getElementById('shotwait');
  if (!img || !wait) return;
  img.addEventListener('load', function () {{
    // Успех после ожидания: вернуть кадр и убрать сообщение.
    img.style.visibility = '';
    wait.hidden = true;
  }});
  img.addEventListener('error', shotMissing);
  // Картинка могла отвалиться ДО того, как скрипт разобран -- тогда
  // события 'error' мы уже не услышим. Два признака этого: пометка от
  // inline-обработчика и загруженная «пустышка» нулевой ширины.
  if (img.dataset.failed === '1' ||
      (img.complete && img.naturalWidth === 0)) {{
    shotMissing();
  }}
}});

async function copyLink() {{
  const link = PERMALINK || location.href;
  try {{
    await navigator.clipboard.writeText(link);
    toast('Ссылка скопирована');
  }} catch (err) {{
    // Буфер обмена недоступен без защищённого соединения -- платформа
    // ходит по http внутри сети. Показываем ссылку, чтобы человек мог
    // скопировать руками, вместо молчаливого "ничего не произошло".
    window.prompt('Скопируйте ссылку:', link);
  }}
}}

// Высоту окна подгоняем под пропорции кадра: при фиксированной высоте
// под кадром 16:9 оставалась широкая чёрная полоса. Пересчитываем и при
// изменении размера окна -- иначе после поворота телефона полоса
// возвращается.
function fitView() {{
  const img = document.getElementById('shot');
  if (!img.naturalWidth) return;
  const h = view.clientWidth * img.naturalHeight / img.naturalWidth;
  view.style.height = Math.min(h, window.innerHeight * 0.78) + 'px';
  apply();
}}
document.getElementById('shot').addEventListener('load', fitView);
window.addEventListener('resize', fitView);
if (document.getElementById('shot').complete) fitView();

// --- статус проверки -------------------------------------------------------
//
// Тот же эндпоинт, что и в плеере, и та же пара (kind, ref_key). Поэтому
// статус, поставленный здесь, виден в плеере и в таблице находок сразу --
// синхронизировать отдельно нечего.
function esc(t) {{
  return String(t === null || t === undefined ? '' : t)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function fmtWhen(iso) {{
  const d = new Date(iso);
  if (isNaN(d)) return String(iso || '').replace('T', ' ').slice(0, 16);
  return d.toLocaleString('ru-RU', {{ day:'2-digit', month:'2-digit',
    year:'numeric', hour:'2-digit', minute:'2-digit' }});
}}

async function loadPriority() {{
  const sel = document.getElementById('prio');
  sel.innerHTML = '<option value="">— не размечено —</option>' +
    Object.keys(PRIORITY_LABELS).map(
      v => `<option value="${{v}}">${{esc(PRIORITY_LABELS[v])}}</option>`).join('');
  try {{
    const r = await fetch(`/api/report/${{encodeURIComponent(REPORT_ID)}}/priorities`);
    const rows = await r.json();
    const mine = rows.find(x => x.kind === 'manual' && String(x.ref_key) === String(OBS_ID));
    sel.value = mine ? mine.priority : '';
    document.getElementById('prio-by').textContent =
      mine ? `поставил ${{mine.set_by}} · ${{fmtWhen(mine.set_at)}}` : '';
  }} catch (err) {{
    document.getElementById('prio-by').textContent = 'не удалось загрузить статус';
  }}
}}

async function setPriority(value) {{
  const by = document.getElementById('prio-by');
  try {{
    const r = await fetch(`/api/report/${{encodeURIComponent(REPORT_ID)}}/priorities`, {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ kind: 'manual', ref_key: String(OBS_ID), priority: value }}),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'отказ');
    by.textContent = d.priority ? `поставил ${{d.set_by}} · ${{fmtWhen(d.set_at)}}` : '';
    toast('Статус сохранён');
  }} catch (err) {{
    by.textContent = 'не сохранилось';
  }}
}}

// --- обсуждение ------------------------------------------------------------

async function loadComments() {{
  const box = document.getElementById('cmts');
  let list = [];
  try {{
    const r = await fetch(`/api/report/${{encodeURIComponent(REPORT_ID)}}/comments`);
    const all = await r.json();
    list = all.filter(c => c.kind === 'manual' && String(c.ref_key) === String(OBS_ID));
  }} catch (err) {{
    box.innerHTML = '<div class="cmt-empty">не удалось загрузить обсуждение</div>';
    return;
  }}
  document.getElementById('cmt-count').textContent = list.length ? `· ${{list.length}}` : '';
  box.innerHTML = list.length ? list.map(c => `
    <div class="cmt">
      <div class="cmt-head">
        <span class="cmt-author">${{esc(c.author)}}</span>
        <span>${{fmtWhen(c.created_at)}}</span>
        ${{(c.author === VIEWER_NAME || IS_MODERATOR)
          ? `<button class="cmt-del" title="Удалить"
               onclick="deleteComment(${{c.id}})">×</button>` : ''}}
      </div>
      <div class="cmt-text">${{esc(c.text)}}</div>
    </div>`).join('') : '<div class="cmt-empty">Пока никто не высказался</div>';
}}

async function addComment() {{
  const ta = document.getElementById('cmt-text');
  const text = (ta.value || '').trim();
  if (!text) return;
  const btn = document.getElementById('cmt-send');
  btn.disabled = true;
  try {{
    const r = await fetch(`/api/report/${{encodeURIComponent(REPORT_ID)}}/comments`, {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ kind: 'manual', ref_key: String(OBS_ID), text }}),
    }});
    const d = await r.json();
    if (!d.ok) {{ toast(d.message || 'не отправилось'); return; }}
    ta.value = '';
    await loadComments();
  }} catch (err) {{
    toast('не отправилось');
  }} finally {{
    btn.disabled = false;
  }}
}}

async function deleteComment(id) {{
  if (!confirm('Удалить это сообщение?')) return;
  try {{
    await fetch(`/api/report/${{encodeURIComponent(REPORT_ID)}}/comments/${{id}}`,
                {{ method: 'DELETE' }});
    await loadComments();
  }} catch (err) {{ toast('не удалилось'); }}
}}

function onCommentKey(e) {{
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{ e.preventDefault(); addComment(); }}
}}

if (CAN_COMMENT) {{
  const ta = document.getElementById('cmt-text');
  ta.addEventListener('input', () => {{
    document.getElementById('cmt-send').disabled = !ta.value.trim();
  }});
}}

loadPriority();
loadComments();
// Обсуждение общее с плеером: пока страница открыта, чужие сообщения
// должны появляться сами, иначе разговор идёт вслепую.
setInterval(loadComments, 15000);

apply();
</script>
</body></html>
"""


FINDING_GONE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Находка не найдена</title>
<style>
body {{ font-family:-apple-system,Arial,sans-serif; background:#111; color:#eee;
        margin:0; padding:60px 20px; display:flex; justify-content:center; }}
.box {{ max-width:520px; }}
h1 {{ font-size:20px; margin:0 0 12px; }}
p {{ color:#9aa4ad; line-height:1.6; margin:0 0 12px; font-size:14px; }}
a {{ color:#8ecbff; }}
.btn {{ display:inline-block; margin-top:8px; border:1px solid #2b6b74;
        background:#12363b; color:#c9f0f5; border-radius:7px;
        padding:8px 14px; text-decoration:none; font-size:13px; }}
</style></head>
<body><div class="box">
<h1>Находки №{obs_id} больше нет</h1>
<p>Скорее всего её удалили после проверки — например, признали ложной.
Сама платформа работает, дело только в этой записи.</p>
<p>Если ссылку прислали недавно и она должна работать — спросите у того,
кто её прислал: возможно, находку убрали по ошибке.</p>
<a class="btn" href="/operations">Ко всем операциям</a>
</div></body></html>
"""


@app.route("/finding/<int:obs_id>/")
def finding_frame(obs_id):
    """Кадр находки на отдельной странице."""
    conn = get_db()
    row = conn.execute(
        "SELECT o.*, r.rel_path, r.kind FROM manual_observations o "
        "JOIN reports r ON r.report_id = o.report_id "
        "WHERE o.id=?", (obs_id,)).fetchone()
    if row is None:
        # Сюда попадают по ссылке на удалённую находку. Голый текст "не
        # найдено" оставлял человека в тупике: непонятно, ошибся ли он,
        # сломалась ли платформа и куда идти дальше.
        return FINDING_GONE_HTML.format(obs_id=int(obs_id)), 404

    op = conn.execute(
        "SELECT op.id, op.title FROM operations op "
        "JOIN operation_materials m ON m.operation_id = op.id "
        "WHERE m.report_id=?", (row["report_id"],)).fetchone()

    crumbs = ('<a href="/operations">Операции</a> › '
              + (f'<a href="/operation/{op["id"]}/">{html_escape(op["title"])}</a> › '
                 if op else '')
              + html_escape(os.path.basename(row["rel_path"] or "")))

    seconds = row["timestamp_sec"]
    tc = ""
    if seconds is not None:
        tc = "%02d:%02d" % (int(seconds) // 60, int(seconds) % 60)

    player_btn = ""
    if row["kind"] == "video":
        href = f'/report/{row["report_id"]}/player/'
        if seconds is not None:
            href += f"?t={max(0, int(seconds))}"
        player_btn = (f'<a class="btn main" href="{href}">▶ Открыть в плеере'
                      + (f' на {tc}' if tc else '') + '</a>')

    bits = []
    if row["viewer_name"]:
        bits.append(f'отметил <b>{html_escape(row["viewer_name"])}</b>')
    if tc:
        bits.append(f'таймкод <b>{tc}</b>')
    if row["lat"] is not None and row["lon"] is not None:
        bits.append(f'координаты <b>{row["lat"]:.5f}, {row["lon"]:.5f}</b>')

    permalink = finding_share_link(obs_id)

    note_block = ""
    if row["note"]:
        note_block = ('<div class="fld"><label>Описание</label>'
                      f'<div class="note">{html_escape(row["note"])}</div></div>')

    gps = []
    if row["lat"] is not None and row["lon"] is not None:
        gps.append(
            f'📍 позиция дрона: <b>{row["lat"]:.6f}, {row["lon"]:.6f}</b> '
            f'<a href="https://www.google.com/maps?q={row["lat"]},{row["lon"]}" '
            f'target="_blank" rel="noopener">карта</a>')
    if row["est_lat"] is not None and row["est_lon"] is not None:
        dist = (f' (~{int(row["est_distance_m"])} м)'
                if row["est_distance_m"] is not None else "")
        gps.append(
            f'<span class="est">🎯 вероятная точка объекта: '
            f'<b>{row["est_lat"]:.6f}, {row["est_lon"]:.6f}</b>{dist} '
            f'<a href="https://www.google.com/maps?q={row["est_lat"]},{row["est_lon"]}" '
            f'target="_blank" rel="noopener">карта</a></span>')
    coords_block = ""
    if gps:
        coords_block = ('<div class="fld"><label>Координаты</label>'
                        '<div class="gps">' + "<br>".join(gps) + "</div></div>")

    if can_comment():
        comment_form = (
            '<div class="cmt-form">'
            '<textarea id="cmt-text" rows="3" placeholder="Ваш комментарий" '
            'onkeydown="onCommentKey(event)"></textarea>'
            '<div class="cmt-actions">'
            '<button id="cmt-send" disabled onclick="addComment()">Отправить</button>'
            '<span class="cmt-hint">Ctrl+Enter</span></div></div>')
    elif current_role() == sar_common.ROLE_MUTED:
        comment_form = ('<div class="cmt-locked">Координатор ограничил вам '
                        'участие в обсуждениях.</div>')
    else:
        comment_form = ('<div class="cmt-locked">Чтобы писать в обсуждении, '
                        'войдите по персональной ссылке из бота (команда /help).</div>')

    return FINDING_FRAME_HTML.format(
        short=html_escape(os.path.basename(row["rel_path"] or "")),
        label=html_escape(row["label"] or "Находка без подписи"),
        meta=" · ".join(bits) or "—",
        crumbs=crumbs,
        player_btn=player_btn,
        img_src=f"/api/finding/{obs_id}/preview?full=1",
        bbox_json=json.dumps(_finding_bbox(dict(row))),
        permalink_json=json.dumps(permalink),
        note_block=note_block,
        coords_block=coords_block,
        comment_form=comment_form,
        report_id_json=json.dumps(row["report_id"]),
        obs_id=int(obs_id),
        viewer_name_json=json.dumps(session.get("viewer_name", "")),
        is_moderator_json=json.dumps(bool(is_moderator())),
        can_comment_json=json.dumps(bool(can_comment())),
        priority_labels_json=json.dumps(sar_common.PRIORITY_LABELS,
                                        ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# ПУЛЬС ПРИСУТСТВИЯ ВО ВСЕ СТРАНИЦЫ
#
# Делается здесь, в конце модуля, а не рядом с самими шаблонами: так блок
# гарантированно выполняется после того, как определены ВСЕ шаблоны, в
# каком бы порядке их ни переставили дальше. Маршруты читают эти имена в
# момент запроса, а не при импорте, поэтому подмена в конце модуля на них
# действует.
#
# Список страниц явный, а не "все шаблоны подряд": на страницу входа и на
# открытый без пароля /guide пульс ставить нельзя -- там человек ещё не
# опознан, и присутствие означало бы "онлайн" неизвестно кого.
# ---------------------------------------------------------------------------

PAGES_WITH_PRESENCE = (
    "TREE_PAGE_HTML", "PROCESSING_PAGE_HTML", "PLAYER_PAGE_HTML",
    "OPERATIONS_PAGE_HTML", "OPERATION_CARD_HTML", "PHOTO_VIEWER_HTML",
    "FINDING_FRAME_HTML",
)

for _page in PAGES_WITH_PRESENCE:
    _tpl = globals()[_page]
    assert "</body>" in _tpl, f"{_page}: некуда вставить пульс присутствия"
    globals()[_page] = _tpl.replace(
        "</body>", HEARTBEAT_JS_FORMAT + "\n</body>", 1)
del _page, _tpl


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------

def main():
    global SERVER_CFG, DATA_DIR, DB_PATH, REPORTS_DIR, _TELEMETRY_INDEX

    SERVER_CFG, config_path = sar_common.load_server_config(SCRIPT_DIR)
    SERVER_CFG["watch_dir"], DATA_DIR, DB_PATH, REPORTS_DIR = sar_common.resolve_paths(
        SERVER_CFG["watch_dir"], SERVER_CFG.get("data_dir"))

    if SERVER_CFG["shared_password"] == "change_me":
        print("=" * 70)
        print("ВНИМАНИЕ: пароль по умолчанию 'change_me' не изменён!")
        print(f"Смените \"server.shared_password\" в {config_path} перед тем,")
        print("как открывать сервис наружу.")
        print("=" * 70)

    # идемпотентно — безопасно вызывать независимо от того, успел ли уже
    # sar_worker.py создать/смигрировать БД к этому моменту или ещё нет
    sar_common.init_db(DB_PATH)
    app.secret_key = init_secret_key()

    # индекс telemetry/ строится один раз при старте (не на каждый запрос
    # плеера) -- используется в get_telemetry_for_report() как резервный
    # источник GPS, если рядом с видео нет videoname.srt
    detection_cfg_path = os.path.join(SCRIPT_DIR, "sar_config.json")
    detection_cfg = load_detection_config(detection_cfg_path if os.path.exists(detection_cfg_path) else None)
    telemetry_dir = sar_common.resolve_telemetry_dir(
        SERVER_CFG["watch_dir"], detection_cfg.get("telemetry_dir", sar_common.DEFAULT_TELEMETRY_DIR_NAME))
    _TELEMETRY_INDEX = sar_common.build_telemetry_index(telemetry_dir)

    print(f"Папка проекта: {SERVER_CFG['watch_dir']}")
    print(f"Данные: {DATA_DIR}")
    print(f"Телеметрия: {telemetry_dir} ({len(_TELEMETRY_INDEX['by_stem'])} SRT-файл(ов) проиндексировано)")
    print(f"Сервер запущен: http://{SERVER_CFG['host']}:{SERVER_CFG['port']}")
    print("ВАЖНО: обработка видео/фото теперь отдельным процессом — "
          "убедитесь, что параллельно запущен `python sar_worker.py`, "
          "иначе новые файлы не будут ставиться в очередь и обрабатываться.")

    try:
        from waitress import serve
        # ПОТОКОВ НУЖНО МНОГО, и вот почему: отдача видео (/report/<id>/video)
        # держит поток занятым ВСЁ ВРЕМЯ просмотра, а не доли секунды как
        # обычный запрос. При threads=8 шесть человек, одновременно смотрящих
        # видео, занимали 6 потоков из 8 -- на опросы списка файлов, плеера и
        # heartbeat оставалось два, и интерфейс у всех начинал заметно
        # подвисать (поймано на реальной работе команды из 6 человек).
        # Потоки тут почти бесплатны: они ждут сеть и диск, а не считают.
        threads = SERVER_CFG.get("server_threads", 32)
        print(f"Запускаю через waitress (production WSGI, потоков: {threads}) на "
              f"http://{SERVER_CFG['host']}:{SERVER_CFG['port']}")
        serve(app, host=SERVER_CFG["host"], port=SERVER_CFG["port"], threads=threads)
    except ImportError:
        print("ВНИМАНИЕ: waitress не установлен (pip install waitress) -- "
              "запускаю через встроенный dev-сервер Flask. Он НЕ предназначен "
              "для продакшена (нестабилен под нагрузкой/долгой работой). "
              "Установите waitress и перезапустите перед боевым использованием.")
        app.run(host=SERVER_CFG["host"], port=SERVER_CFG["port"], threaded=True)


if __name__ == "__main__":
    main()
