import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-development-key")
DEBUG = os.environ.get("DEBUG", "0") == "1"

# Единственный источник правды по домену: его читает и nginx, и Django.
# Расхождение между ними даёт 400 Bad Request, который выглядит как баг приложения.
DOMAIN = os.environ.get("DOMAIN", "")
ALLOWED_HOSTS = [h for h in (DOMAIN, f"www.{DOMAIN}", "localhost", "127.0.0.1") if h]

# Django за прокси видит http, а не https. Без этой пары request.is_secure()
# возвращает False, и secure-флаги на session/csrf-куках не выставляются.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# SECURE_SSL_REDIRECT намеренно НЕ включается. Редирект на https принадлежит
# ровно одному слою, и этот слой — nginx.
# Продублировав его здесь, мы ломаем режим первого запуска: пока сертификата
# нет, nginx поднимается HTTP-only и проксирует напрямую, а Django всё равно
# отдаёт 301 на https, которого ещё никто не слушает. Проверено вживую:
# смена DOMAIN на домен без сертификата давала 301 на мёртвый адрес.
SECURE_SSL_REDIRECT = False

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # Нужен для collectstatic, который вызывается на старте контейнера app.
    "django.contrib.staticfiles",
    "billing",
    "locations",
]

# SecurityMiddleware обязателен, иначе SECURE_SSL_REDIRECT и
# SECURE_PROXY_SSL_HEADER ниже — просто переменные, которые никто не читает.
# Настройка, которая ничего не делает, хуже отсутствующей: она даёт ложную
# уверенность при код-ревью.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "app"),
            "USER": os.environ.get("POSTGRES_USER", "app"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "app"),
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL)

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "static"
