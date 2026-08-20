FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --uid 10001 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Каталоги, в которые пишет процесс, должны принадлежать непривилегированному
# пользователю. Запуск от root означает, что любое RCE в Django или в
# зависимости сразу получает root внутри контейнера.
RUN mkdir -p /app/static /app/media && chown -R app:app /app
USER app

EXPOSE 8000

# --access-logfile - обязателен: без него у приложения нет вообще никаких
# access-логов, и авария выглядит как тишина.
# --max-requests с jitter перезапускает воркер до того, как утечка памяти
# в зависимости успеет стать инцидентом.
# Домена в образе нет ни в каком виде: он приходит только из окружения, поэтому
# смена DOMAIN не требует пересборки.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "30", \
     "--graceful-timeout", "30", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "100", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]


# Отдельная стадия для тестов. Прод-образ не должен нести fakeredis и прочий
# тестовый инструментарий: это лишние пакеты в рантайме и лишняя поверхность.
FROM base AS test

USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
USER app

CMD ["python", "manage.py", "test", "-v", "2", "--buffer"]
