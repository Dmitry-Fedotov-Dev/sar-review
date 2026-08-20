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
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime

from flask import (Flask, request, session, redirect, url_for, jsonify,
                    send_from_directory, send_file, make_response, g, Response)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    _, _, db_path, _ = sar_common.resolve_paths(watch)
    data_dir = os.path.dirname(db_path)
    facts = sar_health.collect(
        conn, watch,
        backup_dir=os.path.join(os.path.dirname(watch.rstrip(os.sep)), "sar_backups"),
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
    const thumb = `<a class="thumb-wrap" href="${{mediaHref}}" title="Открыть">
           <img class="thumb-img" src="/api/thumbnail/${{encodeURIComponent(it.name)}}" loading="lazy"
                onerror="this.style.display='none'; this.nextElementSibling.style.display='';">
           <span class="kind-icon" style="display:none">${{icon}}</span>
         </a>`;
    let right = `<span class="badge ${{it.status}}">${{badgeLabel(it.status)}}</span>`;
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

async function heartbeat() {{
  try {{
    const res = await fetch('/api/heartbeat', {{ method: 'POST' }});
    const data = await res.json();
    if (data.count !== undefined) document.getElementById('online-count').textContent = data.count;
  }} catch (e) {{}}
}}
heartbeat();
setInterval(heartbeat, 15000);
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
<style>
:root {{
  --bg:#12171c; --card:#1a2129; --card2:#212a33; --line:#2b353f;
  --ink:#e8eeec; --soft:#9aa8a5; --dim:#6f7d7a;
  --accent:#5fb8c7; --warm:#e07a3f;
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

.crumbs{{font-size:13px;color:var(--soft);margin-bottom:10px;
  word-break:break-word}}
.crumbs a{{color:var(--accent);text-decoration:none}}
.crumbs .sep{{color:var(--dim);margin:0 5px}}

.row{{display:flex;align-items:center;gap:11px;padding:9px 11px;
  border-bottom:1px solid var(--line);text-decoration:none;color:inherit}}
.row:hover{{background:var(--card)}}
.row .ic{{width:19px;text-align:center;flex-shrink:0;opacity:.85}}
.row .nm{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-size:14px}}
.row .meta{{font-size:12px;color:var(--soft);flex-shrink:0}}
.bar{{width:74px;height:6px;background:var(--card2);border-radius:3px;
  overflow:hidden;flex-shrink:0}}
.bar i{{display:block;height:100%;background:var(--accent)}}

.empty{{color:var(--soft);padding:34px 14px;text-align:center;
  border:1px dashed var(--line);border-radius:9px}}
.note{{font-size:12px;color:var(--dim);margin:10px 2px}}
.find{{display:block;padding:11px;border-bottom:1px solid var(--line);
  text-decoration:none;color:inherit}}
.find:hover{{background:var(--card)}}
.find .lbl{{font-size:14px}}
.find .sub{{font-size:12px;color:var(--soft);margin-top:2px}}
.find .when{{color:var(--dim);font-size:11.5px}}
.tag{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:4px;
  background:var(--card2);color:var(--soft);margin-right:6px}}
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
    <button class="tab" data-t="live">Эфиры</button>
    <button class="tab" data-t="find">Находки</button>
    <button class="tab" data-t="rep">Отчёт</button>
  </div>
  <div id="body"></div>
</div>

<script>
const OP = Number(location.pathname.split('/').filter(Boolean)[1]);
let path = new URLSearchParams(location.search).get('path') || '';
let tab = 'mat';

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
  render();
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
  return `<a class="row" href="${{href}}">
    <span class="ic">${{f.kind === 'video' ? '▭' : '🖼'}}</span>
    <span class="nm">${{esc(f.name)}}</span>
    ${{bar}}
    <span class="meta">✍ ${{f.manual_count || 0}}</span></a>`;
}}

function render() {{
  const b = document.getElementById('body');
  if (!data) return;

  if (tab === 'mat') {{
    let html = crumbs();
    const rows = data.folders.map(f =>
      `<a class="row" href="#" onclick="go('${{esc(path ? path + '/' + f.name : f.name)}}');return false">
        <span class="ic">📁</span><span class="nm">${{esc(f.name)}}</span>
        <span class="meta">${{f.files ? f.files + ' файл(ов)' : '—'}}</span></a>`
    ).concat(data.files.map(fileRow));

    if (data.outside && data.outside.length) {{
      rows.push(`<div class="note">Вне папки операции — добавлены вручную:</div>`);
      data.outside.forEach(f => rows.push(fileRow(f)));
    }}
    html += rows.length ? rows.join('') : `<div class="empty">Папка пуста</div>`;
    b.innerHTML = html;

  }} else if (tab === 'find') {{
    if (!findings) {{ b.innerHTML = '<div class="empty">Загружаю…</div>'; loadFindings(); return; }}
    b.innerHTML = findings.length
      ? `<div class="note">Только размеченное человеком: ручные пометки и
           статусы триажа. Сцены модели сюда не попадают — их тысячи, и
           настоящие находки в них потерялись бы.</div>` +
        findings.map(f => {{
          // Находка ведёт ТУДА, ГДЕ ОНА НАЙДЕНА: в плеер на её таймкод.
          // Список без переходов бесполезен -- человек видит «верёвка,
          // 66 с» и не может посмотреть, что там на самом деле.
          const href = `/report/${{f.report_id}}/player/` +
            (f.seconds != null ? `?t=${{Math.max(0, Math.floor(f.seconds))}}` : '');
          const tc = f.seconds != null
            ? String(Math.floor(f.seconds / 60)).padStart(2,'0') + ':' +
              String(Math.floor(f.seconds % 60)).padStart(2,'0')
            : '';
          return `<a class="find" href="${{href}}">
            <div class="lbl"><span class="tag">${{f.kind === 'manual' ? '✍ пометка' : '🏷 триаж'}}</span>${{esc(f.label) || '—'}}</div>
            <div class="sub">${{esc(f.file)}}${{tc ? ' · ' + tc : ''}}${{f.viewer ? ' · ' + esc(f.viewer) : ''}}${{f.lat ? ' · 📍' : ''}}</div>
            <div class="sub when">записано ${{fmtStamp(f.created_at)}}</div>
          </a>`;
        }}).join('')
      : `<div class="empty">Находок пока нет</div>`;

  }} else if (tab === 'live') {{
    b.innerHTML = `<div class="empty">Эфиры появятся, когда включим трансляции.<br>
      Записи эфиров будут попадать сюда же, в материалы операции.</div>`;
  }} else {{
    b.innerHTML = `<div class="empty">Отчёт заказчику — в работе.<br>
      Соберётся из сводки выше и находок, размеченных людьми.</div>`;
  }}
}}

async function loadFindings() {{
  const r = await fetch(`/api/operations/${{OP}}/findings`);
  findings = (await r.json()).findings || [];
  render();
}}

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
            "label": f.get("label") or f.get("priority") or "",
            "viewer": f.get("viewer_name") or f.get("author") or "",
            "seconds": f.get("timestamp_sec"),
            "lat": f.get("lat"), "lon": f.get("lon"),
            "created_at": f.get("created_at") or f.get("updated_at"),
        })
    return jsonify({"findings": out})


@app.route("/api/tree")
def api_tree():
    conn = get_db()
    watch_dir = os.path.abspath(SERVER_CFG["watch_dir"])
    # Папки операций обходятся рекурсивно (заказчик раскладывает материал
    # так же, как у себя в облаке), корень -- плоско, там «Не разобрано».
    found = sar_common.scan_all_materials(watch_dir)

    # Фильтр по операции. Отбор идёт по СВЯЗЯМ в базе, а не по префиксу пути:
    # материал может быть добавлен в операцию, физически лежа где угодно, и
    # может принадлежать двум операциям сразу. Путь на диске -- удобство
    # раскладки, а не источник истины о принадлежности.
    op_param = (request.args.get("op") or "").strip()
    only = None
    op_title = None
    if op_param == "unsorted":
        only = {r["report_id"] for r in sar_common.unsorted_materials(conn)}
        op_title = "Не разобрано"
    elif op_param.isdigit():
        op = sar_common.get_operation(conn, int(op_param))
        if op is None:
            return jsonify({"items": [], "error": "операции нет"}), 404
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

    return jsonify({"items": items, "operation": op_title, "op": op_param or None})


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
    if found.get(filename) not in ("video", "photo"):
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
</style></head>
<body>
<div class="header-row">
  <p><a href="/">&larr; к списку файлов</a></p>
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

async function heartbeat() {{
  try {{
    const res = await fetch('/api/heartbeat', {{ method: 'POST' }});
    const data = await res.json();
    if (data.count !== undefined) document.getElementById('online-count').textContent = data.count;
  }} catch (e) {{}}
}}
heartbeat();
setInterval(heartbeat, 15000);
</script>
</body></html>"""


def get_report_row(report_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
    return dict(row) if row else None


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
        srt_path = os.path.splitext(report["abs_path"])[0] + ".srt"
        if not os.path.exists(srt_path):
            match, reason = sar_common.find_telemetry_for_video(report["abs_path"], _TELEMETRY_INDEX)
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
    if not os.path.exists(report["abs_path"]):
        return "Исходный видеофайл больше не найден на диске", 404
    # conditional=True -> Flask/Werkzeug сам обрабатывает Range-заголовки,
    # это и даёт перемотку в <video> без ручной реализации потоковой отдачи
    return send_file(report["abs_path"], conditional=True)


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
</style></head>
<body>
<p><a href="/">&larr; к списку файлов</a>{report_link}</p>
<h1>Снимок — {name}</h1>
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
    if not os.path.exists(report["abs_path"]):
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

    return PHOTO_VIEWER_HTML.format(report_id=report_id, name=report["rel_path"],
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

    # out_dir может быть пустым у записи, созданной до начала обработки или
    # повреждённой. Раньше это роняло ВЕСЬ список файлов с TypeError: одна
    # плохая строка делала страницу недоступной целиком. Считаем, что сцен
    # просто нет -- список должен пережить любую отдельную запись.
    out_dir = report["out_dir"]
    if not out_dir:
        return []
    det_path = os.path.join(out_dir, "detections.json")
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

    conn.execute(
        "INSERT INTO manual_observations (report_id, viewer_name, timestamp_sec, bbox, label, "
        "note, lat, lon, est_lat, est_lon, est_distance_m, raw_telemetry, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (report_id, viewer_name, float(timestamp_sec), json.dumps(bbox), label, note, lat, lon,
         est_lat, est_lon, est_distance_m, raw_telemetry, datetime.now().isoformat()))
    conn.commit()
    return jsonify({"ok": True})


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

.video-wrap {{ position:relative; width:100%; background:#000; border-radius:6px; overflow:hidden; }}
.video-wrap video {{ width:100%; display:block; }}
/* pointer-events:none по умолчанию -- иначе оверлей перехватывает клики по
   нативным элементам управления видео (play/пауза/перемотка/громкость),
   и ими становится невозможно пользоваться. Включаем перехват кликов
   ТОЛЬКО когда реально идёт рисование рамки. */
#overlay {{ position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none; }}
#overlay.draw-mode {{ cursor:crosshair; pointer-events:auto; }}
#overlay rect.obs-box {{ fill:none; stroke:#ff3b3b; stroke-width:2; }}
#overlay rect.temp-box {{ fill:rgba(255,59,59,0.15); stroke:#ff3b3b; stroke-width:2; stroke-dasharray:5,4; }}
#overlay text.obs-label {{ fill:#ff3b3b; font-size:14px; font-weight:bold; paint-order:stroke; stroke:#000; stroke-width:3px; }}

.toolbar {{ display:flex; align-items:center; gap:10px; margin:10px 0; flex-wrap:wrap; }}
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

.draw-form {{ background:#1b1b1b; border:1px solid #3355aa; border-radius:8px; padding:12px; margin:10px 0; }}
.draw-form input, .draw-form textarea {{ width:100%; box-sizing:border-box; background:#0d0d0d; color:#eee;
    border:1px solid #333; border-radius:5px; padding:8px; margin-bottom:8px; font-family:inherit; font-size:13px; }}
.draw-form textarea {{ resize:vertical; min-height:50px; }}
.draw-form .row {{ display:flex; gap:8px; }}
.draw-form button {{ flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; }}
.btn-save {{ background:#2f9e44; color:#fff; }}
.btn-cancel {{ background:#444; color:#eee; }}

.obs-item {{ background:#1b1b1b; border:1px solid #2a2a2a; border-radius:8px; padding:10px 12px; margin-bottom:8px; }}
.obs-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; }}
.obs-time {{ color:#8ecbff; font-weight:bold; cursor:pointer; font-size:14px; }}
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
.obs-actions {{ margin-top:6px; display:flex; gap:8px; }}
.obs-actions button {{ font-size:11px; padding:3px 8px; border-radius:5px; border:1px solid #444;
                        background:#252525; color:#ccc; cursor:pointer; }}
.obs-actions button:hover {{ background:#333; }}
.priority-control {{ margin-top:8px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.priority-control select {{ font-size:12px; padding:4px 6px; border-radius:5px; border:1px solid #444;
                             background:#0d0d0d; color:#eee; }}
.priority-who {{ font-size:11px; color:#888; }}
/* обсуждение находки */
.discussion {{ margin-top:8px; border-top:1px solid #2a2a2a; padding-top:6px; }}
.discussion summary {{ cursor:pointer; color:#8ecbff; font-size:12px; user-select:none; }}
.discussion summary:hover {{ color:#b3d9ff; }}
.cmt-list {{ margin:8px 0 6px; display:flex; flex-direction:column; gap:6px; }}
.cmt {{ background:#141414; border:1px solid #2a2a2a; border-radius:6px; padding:6px 8px; }}
.cmt-head {{ display:flex; align-items:center; gap:8px; margin-bottom:2px; }}
.cmt-author {{ font-size:11px; font-weight:bold; color:#9fe8b5; }}
.cmt-time {{ font-size:10px; color:#777; }}
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
.cmt-del {{ margin-left:auto; background:none; border:none; color:#777; cursor:pointer;
            font-size:15px; line-height:1; padding:0 2px; }}
.cmt-del:hover {{ color:#ff6666; }}
.cmt-text {{ font-size:12.5px; color:#ddd; white-space:pre-wrap; word-break:break-word; }}
.cmt-form {{ display:flex; gap:6px; align-items:flex-start; }}
.cmt-form textarea {{ flex:1; background:#0d0d0d; color:#eee; border:1px solid #333;
                       border-radius:5px; padding:6px; font-family:inherit; font-size:12.5px;
                       resize:vertical; min-height:34px; }}
.cmt-form textarea:focus {{ outline:none; border-color:#3355aa; }}
.cmt-form button {{ font-size:12px; padding:6px 10px; border-radius:5px; border:1px solid #3355aa;
                     background:#252525; color:#8ecbff; cursor:pointer; white-space:nowrap; }}
.cmt-form button:hover {{ background:#3355aa; color:#fff; }}
.cmt-form button:disabled {{ opacity:0.5; cursor:default; }}
.cmt-locked {{ font-size:11.5px; color:#888; background:#141414; border:1px dashed #333;
                border-radius:6px; padding:7px 9px; line-height:1.5; }}
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
    <div class="video-wrap">
      <video id="video" controls src="/report/{report_id}/video"></video>
      <svg id="overlay"></svg>
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

    <div id="draw-form-slot"></div>

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
const drawToggle = document.getElementById('draw-toggle');
let drawMode = false;
let observations = [];
let drawing = null; // {{startX, startY, rectEl}}
let pendingBox = null; // нормализованный bbox, ждущий сохранения формы

// --- режим разметки: включение/выключение ---
drawToggle.addEventListener('click', () => {{
  drawMode = !drawMode;
  overlay.classList.toggle('draw-mode', drawMode);
  drawToggle.textContent = '🖊 Режим разметки: ' + (drawMode ? 'вкл' : 'выкл');
  drawToggle.classList.toggle('active', drawMode);
}});

function overlayPoint(evt) {{
  const rect = overlay.getBoundingClientRect();
  return {{
    x: Math.max(0, Math.min(rect.width, evt.clientX - rect.left)),
    y: Math.max(0, Math.min(rect.height, evt.clientY - rect.top)),
    w: rect.width, h: rect.height,
  }};
}}

overlay.addEventListener('mousedown', e => {{
  if (!drawMode) return;
  video.pause();  // фиксируем таймкод на момент начала разметки, не даём ему уехать
  const p = overlayPoint(e);
  const rectEl = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rectEl.setAttribute('class', 'temp-box');
  overlay.appendChild(rectEl);
  drawing = {{ startX: p.x, startY: p.y, rectEl, w: p.w, h: p.h, atTime: video.currentTime }};
}});

overlay.addEventListener('mousemove', e => {{
  if (!drawing) return;
  const p = overlayPoint(e);
  const x = Math.min(drawing.startX, p.x), y = Math.min(drawing.startY, p.y);
  const w = Math.abs(p.x - drawing.startX), h = Math.abs(p.y - drawing.startY);
  drawing.rectEl.setAttribute('x', x); drawing.rectEl.setAttribute('y', y);
  drawing.rectEl.setAttribute('width', w); drawing.rectEl.setAttribute('height', h);
  drawing.lastX = p.x; drawing.lastY = p.y;
}});

overlay.addEventListener('mouseup', () => {{
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

function showDrawForm() {{
  const slot = document.getElementById('draw-form-slot');
  slot.innerHTML = `
    <div class="draw-form">
      <input id="obs-label-input" placeholder="Что это? (человек, палатка, рюкзак...)" autofocus>
      <textarea id="obs-note-input" placeholder="Заметка (необязательно)"></textarea>
      <div class="row">
        <button class="btn-save" onclick="saveObservation()">Сохранить</button>
        <button class="btn-cancel" onclick="cancelDraw()">Отмена</button>
      </div>
    </div>`;
  document.getElementById('obs-label-input').focus();
}}

function cancelDraw() {{
  if (pendingBox) pendingBox.rectEl.remove();
  pendingBox = null;
  document.getElementById('draw-form-slot').innerHTML = '';
}}

async function saveObservation() {{
  if (!pendingBox) return;
  const label = document.getElementById('obs-label-input').value.trim();
  const note = document.getElementById('obs-note-input').value.trim();
  try {{
    await fetch(`/api/report/${{reportId}}/observations`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        timestamp_sec: pendingBox.timestamp_sec, bbox: pendingBox.bbox, label, note,
      }}),
    }});
  }} catch (e) {{}}
  pendingBox.rectEl.remove();
  pendingBox = null;
  document.getElementById('draw-form-slot').innerHTML = '';
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
  const rect = overlay.getBoundingClientRect();

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
      r.setAttribute('style', `fill:none; stroke:${{color}}; stroke-width:2; stroke-dasharray:3,3;`);
      overlay.appendChild(r);
      const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      txt.setAttribute('class', 'ai-label');
      txt.setAttribute('x', fx1 * rect.width);
      txt.setAttribute('y', Math.max(14, fy1 * rect.height - 6));
      txt.setAttribute('style', `fill:${{color}}; font-size:12px; font-weight:bold; paint-order:stroke; stroke:#000; stroke-width:3px;`);
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
    list.innerHTML = '<div class="empty">Пока нет отметок. Включите режим разметки и выделите область на видео.</div>';
  }} else {{
    list.innerHTML = observations.map(o => {{
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
        </div>
        ${{o.label ? `<div class="obs-label">${{o.label}}</div>` : ''}}
        ${{o.note ? `<div class="obs-note">${{o.note}}</div>` : ''}}
        ${{gps}}
        ${{estGps}}
        ${{renderRawTelemetryDropdown(o.raw_telemetry)}}
        ${{renderPriorityControl('manual', o.id)}}
        ${{renderComments('manual', o.id)}}
        <div class="obs-actions">
          <button onclick="deleteObservation(${{o.id}})">🗑 удалить</button>
        </div>
      </div>`;
    }}).join('');
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
    list.innerHTML = '<div class="empty">Модель пока ничего не нашла.</div>';
    return;
  }}
  list.innerHTML = scenes.map(s => {{
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
  }}).join('');
}}

function jumpTo(sec) {{
  video.currentTime = sec;
  video.play();
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
  return `
    <details class="discussion" ${{list.length ? 'open' : ''}}>
      <summary>💬 обсуждение${{list.length ? ` (${{list.length}})` : ''}}</summary>
      <div class="cmt-list">${{items}}</div>
      ${{CAN_COMMENT ? `
      <div class="cmt-form">
        <textarea id="${{inputId}}" rows="2" placeholder="Написать сообщение…"></textarea>
        <button onclick="addComment('${{kind}}', '${{refKey}}', '${{inputId}}', this)">Отправить</button>
      </div>` : `<div class="cmt-locked">${{COMMENT_LOCK_REASON}}</div>`}}
    </details>`;
}}

async function addComment(kind, refKey, inputId, btn) {{
  const ta = document.getElementById(inputId);
  const text = (ta.value || '').trim();
  if (!text) return;
  btn.disabled = true;
  try {{
    await fetch(`/api/report/${{reportId}}/comments`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ kind, ref_key: refKey, text }}),
    }});
    ta.value = '';
    await loadComments();
    loadObservations();
    loadAiScenes();
  }} catch (e) {{}}
  btn.disabled = false;
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
  if (!confirm('Удалить эту отметку?')) return;
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
async function heartbeat() {{
  try {{
    const res = await fetch('/api/heartbeat', {{ method: 'POST' }});
    const data = await res.json();
    if (data.count !== undefined) document.getElementById('online-count').textContent = data.count;
  }} catch (e) {{}}
}}
heartbeat();
setInterval(heartbeat, 15000);

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
    name = (report["rel_path"] or "").replace("\\", "/").split("/")[-1]
    parts = ['<a href="/operations">Операции</a>']
    if ops:
        op = ops[-1]
        parts.append('<a href="/operation/%d/">%s</a>' % (op["id"], op["title"]))
    else:
        parts.append('<a href="/">Не разобрано</a>')
    if extra:
        parts.append(extra)
    parts.append("<span>%s</span>" % name)
    return " &rsaquo; ".join(parts)


@app.route("/report/<report_id>/player/")
def player_page(report_id):
    report = get_report_row(report_id)
    if report is None:
        return "Отчёт не найден", 404
    if report["kind"] != "video":
        return "Ручной плеер доступен только для видео", 400
    if not os.path.exists(report["abs_path"]):
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
    return PROCESSING_PAGE_HTML.format(
        name=report["rel_path"], status=report["status"],
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
# ENTRYPOINT
# ---------------------------------------------------------------------------

def main():
    global SERVER_CFG, DATA_DIR, DB_PATH, REPORTS_DIR, _TELEMETRY_INDEX

    SERVER_CFG, config_path = sar_common.load_server_config(SCRIPT_DIR)
    SERVER_CFG["watch_dir"], DATA_DIR, DB_PATH, REPORTS_DIR = sar_common.resolve_paths(SERVER_CFG["watch_dir"])

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
