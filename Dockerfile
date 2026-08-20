FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl намеренно не ставится: он весил бы около десяти мегабайт ради одного
# healthcheck, который делается штатным urllib из уже установленного Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --uid 10001 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --chown прямо в COPY. Отдельный `chown -R` после копирования создаёт в
# оверлейной ФС полную копию всех файлов новым слоем, то есть удваивает вклад
# приложения в размер образа.
COPY --chown=app:app . .

RUN mkdir -p /app/static /app/media && chown app:app /app/static /app/media
USER app

EXPOSE 8000

# gthread вместо синхронной модели по умолчанию. Приложение ждёт Postgres и
# Redis, а не считает, поэтому синхронные воркеры обслуживали ровно три запроса
# одновременно и очередь росла при загрузке CPU в единицы процентов.
# Число потоков согласовано с DB_POOL_MAX: соединения Django потоко-локальны,
# и без пула такая конфигурация умножила бы нагрузку на базу, а не сняла её.
#
# --max-requests поднят с 1000 до 10000. Измерено: django.setup() стоит 153 мс,
# и при сотне запросов в секунду на воркер прежний порог перезапускал его раз в
# десять секунд, теряя около двух процентов ёмкости ради защиты от утечки,
# которой здесь нет.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--worker-class", "gthread", \
     "--workers", "3", \
     "--threads", "8", \
     "--timeout", "30", \
     "--graceful-timeout", "30", \
     "--max-requests", "10000", \
     "--max-requests-jitter", "1000", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]


FROM base AS test

USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
USER app

CMD ["python", "manage.py", "test", "-v", "2", "--buffer"]
