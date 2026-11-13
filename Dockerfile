# Образ платформы для поставки через cd (github.com/Dmitry-Fedotov-Dev/cd,
# тип проекта dockerfile). Подробности и команды сервера: deploy/cd/README.md.
#
# Контракт cd, под который это собрано: пользователь 10001, корень только
# для чтения, писать можно в /tmp (tmpfs 64 МБ) и в папку данных /app/data,
# слушать $PORT (3000). Секреты -- в env-файле проекта на сервере, не здесь.
FROM python:3.12-slim

# ffmpeg -- лёгкие копии видео и превью; tini -- корректная доставка сигналов
# и сбор зомби (у нас дерево процессов: воркер запускает ffmpeg и детектор).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# opencv-python тянет libGL ради окон, которых в контейнере нет;
# headless -- тот же модуль cv2 без графики.
COPY requirements.txt ./
RUN sed 's/^opencv-python\b.*/opencv-python-headless/' requirements.txt > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt \
 && rm /tmp/req.txt

COPY . .

# Все процессы ищут sar_config.json и sar_backups/ рядом со своим кодом.
# Код здесь только для чтения, поэтому -- ссылки в папку данных (см.
# sar_container.py). Сама папка данных монтируется cd поверх /app/data.
RUN ln -sfn /app/data/sar_config.json /app/sar_config.json \
 && ln -sfn /app/data/sar_backups /app/sar_backups \
 && mkdir -p /app/data && chown 10001:10001 /app/data

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp PORT=3000
USER 10001:10001
EXPOSE 3000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "sar_container.py"]
