FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Домена в образе нет ни в каком виде: он приходит только из окружения,
# поэтому смена DOMAIN не требует пересборки.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]


# Отдельная стадия для тестов. Прод-образ не должен нести fakeredis и прочий
# тестовый инструментарий: это лишние пакеты в рантайме и лишняя поверхность.
FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

CMD ["python", "manage.py", "test", "-v", "2"]
