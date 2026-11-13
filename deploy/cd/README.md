# Поставка SAR Review через cd

[cd](https://github.com/Dmitry-Fedotov-Dev/cd) деплоит репозиторий на свой
сервер из GitHub Actions без ключей и паролей в репозитории: задание
предъявляет OIDC-токен GitHub, сервер (`cdd`) сам собирает образ по
[`Dockerfile`](../../Dockerfile), запускает его под фиксированными лимитами,
проверяет здоровье и откатывает, если новая версия не поднялась.

```
push в master ──► тесты (pytest) ──► deploy на каждый сервер из CD_TARGETS
                                     └─ cdd: docker build → новый контейнер → /login 200? → иначе откат
```

## Что внутри контейнера

Один контейнер на экземпляр. Внутри [`sar_container.py`](../../sar_container.py):

- собирает `sar_config.json` в папке данных из шаблона и env-файла проекта;
- запускает `sar_worker.py`, `sar_server.py` (порт 3000) и, если есть токен,
  `sar_telegram_bot.py`. Упал воркер или веб — контейнер выходит с ошибкой,
  docker поднимает его заново. Бот перезапускается отдельно и платформу не роняет;
- раз в 6 часов делает резервную копию базы (`sar_backup.py`), первую — через
  минуту после старта.

Всё постоянное — в одной папке данных проекта cd
(`/var/lib/cdd/projects/<проект>/data` на сервере, `/app/data` в контейнере):

```
data/
  sar_config.json    конфиг (пути и порт перезаписываются при старте, остальное ваше)
  material/          watch_dir — видео и фото кладутся сюда (и telemetry/ для SRT)
  sar_data/          база, отчёты, прокси, превью, secret.key
  sar_backups/       резервные копии базы
  tmp/               временные файлы (чистится при старте)
```

## На сервере — один раз

cd уже установлен (`install.sh`, см. README cd). Для каждого экземпляра:

```bash
# 1. env-файл с паролем и токеном бота (шаблон: deploy/cd/sar.env.example)
sudo install -m 600 /dev/null /etc/cdd/env/sar.env
sudo nano /etc/cdd/env/sar.env

# 2. разрешить репозиторию деплоить проект "sar"
sudo cdctl project add sar \
     --repo Dmitry-Fedotov-Dev/sar-review --branch master --environment production \
     --type dockerfile --health /login --data /app/data \
     --env-file /etc/cdd/env/sar.env \
     --cpus 2 --memory 2g --pids 512 \
     --domain sar.example.com
```

Почему такие флаги:

| Флаг | Зачем |
|---|---|
| `--type dockerfile` | у платформы свой образ (Python, ffmpeg) — не node и не static |
| `--health /login` | НЕ `/healthz`: он отвечает 503 при «копий нет ни одной» и при «на диске < 3 ГБ». Первый же деплой на чистый сервер откатился бы (копия появляется через минуту после старта), а при заполненном диске откатывался бы любой деплой — включая тот, что чинит. `/healthz` остаётся для мониторинга |
| `--data /app/data` | без неё база и отчёты пропадали бы при каждом деплое |
| `--memory 2g` | умолчание cd 256 МБ — не хватит ни ffmpeg, ни OpenCV |
| `--pids 512` | в лимит входят потоки: у waitress их 32, плюс воркер, ffmpeg, бот. 128 по умолчанию мало |

## В репозитории — один раз

Settings → Secrets and variables → Actions → **Variables**:

- один сервер: `CD_SERVER` = `https://deploy.example.com`, `CD_PROJECT` = `sar`
  (без `CD_PROJECT` — `sar-review`);
- несколько серверов — `CD_TARGETS`:

  ```json
  [{"server": "https://deploy.bishkek.example", "project": "sar"},
   {"server": "https://deploy.osh.example",     "project": "sar-alai", "environment": "alai"}]
  ```

  `environment` необязателен (по умолчанию `production`) и должен совпадать
  с `--environment` в `cdctl project add` на том сервере.

Дальше каждый push в `master` прогоняет тесты и деплоит на все цели
параллельно; отказ одного сервера не отменяет остальные. Если не задано
ни `CD_TARGETS`, ни `CD_SERVER`, прогон **падает** с подсказкой — зелёный
прогон без единого деплоя читался бы как «выкатили».

Вручную (Actions → Deploy → Run workflow): `deploy`, `status`, `logs`,
`rollback`; поле «только этот проект» — для одной цели.

## Под другим именем на том же сервере — клон

Отдельная операция, отдельный адрес, свой пароль и бот, тот же код:

```bash
sudo install -m 600 /dev/null /etc/cdd/env/sar-alai.env && sudo nano /etc/cdd/env/sar-alai.env
sudo cdctl project clone sar-alai --from sar --env-file /etc/cdd/env/sar-alai.env --domain alai.example.com
```

Клон стартует сразу на текущем образе `sar` и получает каждый следующий
релиз сам — в `CD_TARGETS` его добавлять **не нужно**. У клона своя папка
данных: база, отчёты, пользователи, `secret.key` не пересекаются.

## Материал

- **Облако (предпочтительно):** подключить Google Диск / Яндекс.Диск в
  `/admin` — локально остаются только отчёты и лёгкие копии.
- **Файлы на сервер:** в `/var/lib/cdd/projects/<проект>/data/material/`,
  владелец 10001 — иначе процесс в контейнере их не прочитает:
  ```bash
  sudo rsync -a --chown=10001:10001 ./video/ /var/lib/cdd/projects/sar/data/material/
  ```

## Перенос существующей установки (Windows → сервер)

1. `cdctl project add …` и первый деплой (создаст папку данных).
2. `cdctl stop sar`.
3. Скопировать в `data/`: `sar_config.json` (пути и порт перепишутся сами),
   в `data/sar_data/` — `sar_data.db`, `secret.key`, `reports/`, `proxies/`,
   `thumbnails/`. Затем `sudo chown -R 10001:10001 /var/lib/cdd/projects/sar/data`.
4. `cdctl start sar`.

Материал находится по `rel_path`, поэтому записи подхватываются, если
структура папок внутри `material/` та же, что была в `watch_dir`.

**Не проверено:** отчёты, собранные на Windows, хранят пути кадров с `\`.
Браузер такие ссылки обычно понимает, но на перенесённой базе это стоит
проверить глазами до операции.

## Чего тут нет

- **HTTPS и домен** делает cd (`--domain`, Caddy на сервере). `Caddyfile`,
  `sar_https.py`, `sar_tunnel.py`, `sar_ddns.py` в контейнере не запускаются.
  Поэтому `/healthz` всегда показывает `warn` «сторож туннеля не отмечался» —
  это правда, сторожа нет; критичным этот пункт не бывает.
- **Детектор.** Весов модели в образе нет (они не в репозитории), поэтому
  новая установка ставит `auto_process=false`. Ручной разбор, плеер, облако,
  отчёты работают полностью.
- **Копия базы вне сервера.** Резервные копии лежат на том же диске, что и
  база. От ошибки в данных защищают, от смерти диска — нет: забирайте
  `data/sar_backups/` на другую машину.
