import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def required_env(name: str) -> str:
    """Обязательная переменная окружения без тихого дефолта.

    Падение на старте — правильное поведение. Дефолт у секрета или у адреса
    базы означает, что забытая переменная не ломает деплой, а тихо меняет
    поведение системы: приложение поднимается с публично известным ключом или
    начинает писать данные не туда, а /healthz при этом отвечает 200.
    """
    value = os.environ.get(name, "")
    if not value:
        raise ImproperlyConfigured(
            f"Переменная окружения {name} обязательна и не задана."
        )
    return value


DEBUG = os.environ.get("DEBUG", "0") == "1"

# В отладке допускается сгенерированный на лету ключ, в остальных случаях —
# только явный. Дефолтная строка в коде означала бы подделку сессионных кук и
# токенов сброса пароля любым, кто читал репозиторий.
if DEBUG:
    SECRET_KEY = os.environ.get("SECRET_KEY") or "django-insecure-debug-only"
else:
    SECRET_KEY = required_env("SECRET_KEY")

# Единственный источник правды по домену: его читает и nginx, и Django.
# Расхождение между ними даёт 400 Bad Request, который выглядит как баг
# приложения, а не как ошибка конфигурации.
DOMAIN = os.environ.get("DOMAIN", "")
if not DOMAIN and not DEBUG:
    raise ImproperlyConfigured("Переменная окружения DOMAIN обязательна и не задана.")

ALLOWED_HOSTS = [DOMAIN, f"www.{DOMAIN}"] if DOMAIN else []
if DEBUG:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1"]

# Схема нужна и обязана совпадать с той, что реально отдаёт nginx.
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h not in
                        ("localhost", "127.0.0.1")]

# ---------------------------------------------------------------------------
# HTTPS за обратным прокси
# ---------------------------------------------------------------------------
# Читается напрямую в HttpRequest.is_secure() (django/http/request.py), никакого
# middleware для этого не нужно. Без него Django за прокси видит http и
# is_secure() всегда False.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# А вот эти флаги ни из чего не выводятся: куки помечаются Secure только если
# сказать об этом явно. Без них SECURE_PROXY_SSL_HEADER на защищённость кук
# не влияет вообще.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# SECURE_SSL_REDIRECT намеренно выключен. Редирект на https принадлежит ровно
# одному слою, и этот слой — nginx. Продублированный здесь, он ломает режим
# первого запуска: пока сертификата нет, nginx поднимается HTTP-only и
# проксирует напрямую, а Django всё равно отдаёт 301 на https, которого ещё
# никто не слушает.
SECURE_SSL_REDIRECT = False

# Это реализует SecurityMiddleware, и только ради этого он в списке ниже.
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
# HSTS отдаёт nginx (add_header в 20-ssl.conf), дублировать не нужно.
SECURE_HSTS_SECONDS = 0

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # Нужен для collectstatic, который вызывается на старте контейнера app.
    "django.contrib.staticfiles",
    # Нужен для CONCURRENTLY-операций в миграциях billing.0002.
    "django.contrib.postgres",
    "billing",
    "locations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
# Иначе действует дефолт Django America/Chicago, и любое приведение к местному
# времени в отчётах уезжает на девять часов.
TIME_ZONE = os.environ.get("TIME_ZONE", "Europe/Moscow")

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
# SQLite допустим только по явному разрешению. Тихий откат на него при забытом
# POSTGRES_HOST означал бы, что финансовые транзакции пишутся в файл внутри
# контейнера и исчезают при рестарте, без единой ошибки в логе. Отдельно на
# SQLite Django молча выбрасывает FOR UPDATE из запроса
# (django/db/models/sql/compiler.py), то есть вместе с данными теряется и
# защита от гонок.
if os.environ.get("DJANGO_ALLOW_SQLITE") == "1":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    # Пул соединений на стороне приложения. Без него каждый воркер держит
    # собственное постоянное соединение, и их число равно (воркеры x реплики).
    # Соединение в PostgreSQL это процесс ОС весом порядка десяти мегабайт, так
    # что при полусотне реплик упор идёт в max_connections раньше, чем в CPU
    # базы. Пул разрывает эту связь: число физических соединений перестаёт быть
    # функцией числа реплик.
    #
    # Django запрещает сочетать пул с персистентными соединениями
    # ("Pooling doesn't support persistent connections"), поэтому CONN_MAX_AGE
    # здесь обязан быть нулём — это не оплошность, а требование бэкенда.
    DB_POOL = os.environ.get("DB_POOL", "1") == "1"
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": required_env("POSTGRES_DB"),
            "USER": required_env("POSTGRES_USER"),
            "PASSWORD": required_env("POSTGRES_PASSWORD"),
            "HOST": required_env("POSTGRES_HOST"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
    if DB_POOL:
        DATABASES["default"]["CONN_MAX_AGE"] = 0
        DATABASES["default"]["OPTIONS"] = {
            "pool": {
                "min_size": int(os.environ.get("DB_POOL_MIN", "2")),
                "max_size": int(os.environ.get("DB_POOL_MAX", "10")),
                "timeout": 10,
            }
        }
    else:
        # Режим для PgBouncer в transaction mode: свой пул там лишний, а
        # персистентные соединения до пулера дешевы.
        DATABASES["default"]["CONN_MAX_AGE"] = int(
            os.environ.get("DB_CONN_MAX_AGE", "60")
        )
        DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# ---------------------------------------------------------------------------
# Redis: брокер и кэш это РАЗНЫЕ инстансы
# ---------------------------------------------------------------------------
# Общий инстанс с политикой allkeys-lru вытесняет ключи очереди Celery под
# нагрузкой, и сообщения пропадают молча: продюсер уже получил подтверждение.
# Брокер живёт с noeviction и persistence, кэш — с вытеснением.
CELERY_BROKER_URL = required_env("CELERY_BROKER_URL")
REDIS_URL = required_env("REDIS_URL")

CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "")
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
# Подтверждение после выполнения, а не до: падение воркера возвращает задачу в
# очередь вместо тихой потери.
# Бэкенд результатов не сконфигурирован, и результаты никому не нужны:
# говорим это явно, чтобы Celery не пытался их сохранять при появлении бэкенда.
# Celery по умолчанию подменяет обработчики корневого логгера своими, и весь
# блок LOGGING ниже внутри воркера просто игнорируется. Проверено: воркер писал
# текстом «[... : INFO/ForkPoolWorker-4] ...» вместо JSON, то есть половина
# системы логировала в другом формате, чем вторая.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
# Django по умолчанию (django/utils/log.py:DEFAULT_LOGGING) закрывает console
# фильтром require_debug_true, а mail_admins — require_debug_false с пустым
# ADMINS. То есть при DEBUG=0 необработанное исключение во вью не попадает
# никуда: ни в stdout, ни в почту. Конфиг ниже это закрывает.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FORMAT = os.environ.get("LOG_FORMAT", "json")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "config.log_format.JsonFormatter"},
        "verbose": {
            "format": "%(asctime)s %(levelname)s %(name)s %(process)d %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if LOG_FORMAT == "json" else "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": os.environ.get("SQL_LOG_LEVEL", "WARNING"),
            "propagate": False,
        },
        # По две строки INFO на каждую задачу (получена, выполнена). При
        # миллионе задач в сутки это два миллиона строк, которые ничего не
        # сообщают, но оплачиваются при приёме и хранении. Ошибки задач
        # логируются на ERROR и сюда не попадают.
        #
        # У celery.worker.strategy причина строже, чем объём: строка «Task ...
        # received» печатает ПОЛНЫЙ список аргументов задачи. Здесь это
        # координаты пользователя, то есть персональные данные, которые молча
        # утекают в систему сбора логов и живут там по её сроку хранения.
        # Проверено на живом воркере: в записи оказалось
        # "args": "(11, {'lat': Decimal('1'), 'ts': 5000})".
        "celery.app.trace": {
            "handlers": ["console"],
            "level": os.environ.get("CELERY_TRACE_LOG_LEVEL", "WARNING"),
            "propagate": False,
        },
        "celery.worker.strategy": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "static"

# Имя файла содержит хеш содержимого, поэтому новый деплой меняет URL. Без этого
# заголовок кэширования на стороне nginx означает, что часть пользователей до
# месяца работает со старой статикой на новом бэкенде — класс инцидентов,
# который не воспроизводится у себя и «чинится» просьбой нажать Ctrl+F5.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
    },
}
