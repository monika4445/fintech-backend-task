"""Контрактные тесты на конфигурацию.

Все находки финального ревью, которые пришлось чинить, были не в прикладном
коде, а в конфигурации: секреты в образе, дефолтный SECRET_KEY, тихий откат на
SQLite, вытеснение сообщений брокера, запуск от root. Ни один из них не мог быть
пойман обычным тестом, поэтому здесь они зафиксированы явно.
"""

import importlib.util
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent

VALID_ENV = {
    "DEBUG": "0",
    "SECRET_KEY": "x" * 50,
    "DOMAIN": "example.com",
    "POSTGRES_DB": "app",
    "POSTGRES_USER": "app",
    "POSTGRES_PASSWORD": "secret",
    "POSTGRES_HOST": "db",
    "CELERY_BROKER_URL": "redis://redis-broker:6379/0",
    "REDIS_URL": "redis://redis-cache:6379/0",
}


def load_settings(**overrides):
    """Исполняет config/settings.py заново с подменённым окружением."""
    env = {**VALID_ENV, **overrides}
    env = {k: v for k, v in env.items() if v is not None}
    saved = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(
            "probe_settings", BASE_DIR / "config" / "settings.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


class FailFastOnMissingConfigTests(SimpleTestCase):
    def test_missing_secret_key_refuses_to_start(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            load_settings(SECRET_KEY=None)
        self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_missing_domain_refuses_to_start(self):
        with self.assertRaises(ImproperlyConfigured):
            load_settings(DOMAIN=None)

    def test_missing_postgres_host_refuses_to_start(self):
        """Тихий откат на SQLite означал бы потерю данных при рестарте."""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            load_settings(POSTGRES_HOST=None)
        self.assertIn("POSTGRES_HOST", str(ctx.exception))

    def test_missing_broker_url_refuses_to_start(self):
        with self.assertRaises(ImproperlyConfigured):
            load_settings(CELERY_BROKER_URL=None)

    def test_postgresql_is_the_only_supported_backend(self):
        """Раньше здесь был флаг DJANGO_ALLOW_SQLITE, и он не работал.

        Миграции падали на AddIndexConcurrently с TypeError: CONCURRENTLY есть
        только у постгресового schema editor. Неработающая аварийная дверь хуже
        её отсутствия, потому что на неё рассчитывают.
        """
        settings = load_settings()
        self.assertEqual(
            settings.DATABASES["default"]["ENGINE"],
            "django.db.backends.postgresql",
        )
        still_postgres = load_settings(DJANGO_ALLOW_SQLITE="1")
        self.assertEqual(
            still_postgres.DATABASES["default"]["ENGINE"],
            "django.db.backends.postgresql",
            "флаг больше не должен уводить на неподдерживаемый бэкенд",
        )

    def test_debug_mode_does_not_require_secret_key(self):
        """Локальная разработка не должна требовать генерации ключа."""
        settings = load_settings(DEBUG="1", SECRET_KEY=None, DOMAIN=None)
        self.assertTrue(settings.SECRET_KEY)


class ProductionHardeningTests(SimpleTestCase):
    def test_allowed_hosts_has_no_empty_or_bogus_entries(self):
        settings = load_settings()
        self.assertEqual(settings.ALLOWED_HOSTS, ["example.com", "www.example.com"])
        # Раньше пустой DOMAIN давал ALLOWED_HOSTS == ["www.", ...]:
        # фильтр отбрасывал "", но "www." оставался истинным.
        self.assertNotIn("www.", settings.ALLOWED_HOSTS)

    def test_localhost_is_not_trusted_in_production(self):
        settings = load_settings()
        self.assertNotIn("localhost", settings.ALLOWED_HOSTS)
        self.assertNotIn("127.0.0.1", settings.ALLOWED_HOSTS)

    def test_cookies_are_secure_in_production(self):
        """SECURE_PROXY_SSL_HEADER сам по себе кук не защищает."""
        settings = load_settings()
        self.assertTrue(settings.SESSION_COOKIE_SECURE)
        self.assertTrue(settings.CSRF_COOKIE_SECURE)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)

    def test_ssl_redirect_is_owned_by_nginx_only(self):
        settings = load_settings()
        self.assertFalse(settings.SECURE_SSL_REDIRECT)

    def test_connections_are_pooled_and_not_also_persistent(self):
        """Django запрещает сочетать пул с персистентными соединениями.

        "Pooling doesn't support persistent connections" — поэтому при
        включённом пуле CONN_MAX_AGE обязан быть нулём, а не наоборот.
        """
        pooled = load_settings()["default"] if False else load_settings().DATABASES["default"]
        self.assertIn("pool", pooled["OPTIONS"])
        self.assertEqual(pooled["CONN_MAX_AGE"], 0)
        self.assertGreater(pooled["OPTIONS"]["pool"]["max_size"], 0)

    def test_pgbouncer_mode_falls_back_to_persistent_connections(self):
        """С внешним пулером свой пул лишний, а персистентные соединения дёшевы."""
        direct = load_settings(DB_POOL="0").DATABASES["default"]
        self.assertNotIn("pool", direct.get("OPTIONS", {}))
        self.assertGreater(direct["CONN_MAX_AGE"], 0)

    def test_timezone_is_explicit(self):
        settings = load_settings()
        self.assertNotEqual(settings.TIME_ZONE, "America/Chicago")

    def test_errors_reach_stdout_with_debug_off(self):
        """Дефолт Django закрывает console фильтром require_debug_true.

        При DEBUG=0 необработанное исключение во вью не попадает никуда.
        """
        settings = load_settings()
        logging_config = settings.LOGGING
        console = logging_config["handlers"]["console"]
        self.assertNotIn("filters", console)
        self.assertIn("console", logging_config["loggers"]["django.request"]["handlers"])
        self.assertIn("console", logging_config["root"]["handlers"])


class BuildAndDeploymentContractTests(SimpleTestCase):
    """Находки, которые живут в файлах сборки, а не в Python."""

    def test_dockerignore_keeps_secrets_out_of_the_image(self):
        content = (BASE_DIR / ".dockerignore").read_text()
        for entry in (".env", ".venv/", ".git/"):
            self.assertIn(entry, content, f"{entry} должен быть в .dockerignore")

    def test_container_does_not_run_as_root(self):
        content = (BASE_DIR / "Dockerfile").read_text()
        self.assertIn("USER app", content)

    def test_gunicorn_emits_access_logs(self):
        content = (BASE_DIR / "Dockerfile").read_text()
        self.assertIn("--access-logfile", content)

    def test_gunicorn_is_not_limited_to_one_request_per_worker(self):
        """Синхронная модель обслуживала ровно `workers` запросов одновременно."""
        content = (BASE_DIR / "Dockerfile").read_text()
        self.assertIn("gthread", content)
        self.assertIn("--threads", content)

    def test_worker_recycling_is_not_pathologically_frequent(self):
        """django.setup() стоит ~150 мс; порог 1000 съедал ~2% ёмкости."""
        content = (BASE_DIR / "Dockerfile").read_text()
        self.assertIn("--max-requests", content)
        self.assertIn("10000", content)

    def test_image_does_not_carry_curl_just_for_healthchecks(self):
        content = (BASE_DIR / "Dockerfile").read_text()
        self.assertNotIn("install -y --no-install-recommends libpq5 curl", content)

    def test_copy_does_not_duplicate_the_app_layer(self):
        """Отдельный chown -R после COPY удваивает вклад приложения в образ."""
        content = (BASE_DIR / "Dockerfile").read_text()
        self.assertIn("COPY --chown=app:app", content)
        self.assertNotIn("chown -R app:app /app", content)

    def test_broker_redis_does_not_evict_messages(self):
        content = (BASE_DIR / "docker-compose.yml").read_text()
        broker = content.split("\n  redis-broker:", 1)[1].split("\n  redis-cache:", 1)[0]
        self.assertIn("noeviction", broker)
        self.assertIn("--appendonly yes", broker)
        self.assertNotIn("allkeys-lru", broker)

    def test_certbot_is_not_hidden_behind_a_profile(self):
        """С profiles: ["tls"] задокументированный up -d не продлевал ничего."""
        content = (BASE_DIR / "docker-compose.yml").read_text()
        certbot = content.split("\n  certbot:", 1)[1].split("\n  test:", 1)[0]
        self.assertNotIn("profiles", certbot)

    def test_migrations_do_not_run_in_every_app_container(self):
        content = (BASE_DIR / "docker-compose.yml").read_text()
        app_block = content.split("\n  app:", 1)[1].split("\n  worker:", 1)[0]
        self.assertNotIn("manage.py migrate", app_block)
        self.assertIn("service_completed_successfully", app_block)

    def test_worker_healthcheck_does_not_boot_an_interpreter_or_use_the_broker(self):
        """`celery inspect ping` стоил 0.5 с и рассылал broadcast всем воркерам."""
        content = (BASE_DIR / "docker-compose.yml").read_text()
        worker = content.split("\n  worker:", 1)[1].split("\n  nginx:", 1)[0]
        # Только исполняемая строка: в комментарии рядом «inspect ping»
        # упомянут намеренно, как то, что заменили.
        probe = next(
            line for line in worker.splitlines() if line.strip().startswith("test:")
        )
        self.assertNotIn("inspect ping", probe)
        self.assertIn("celery.heartbeat", probe)

    def test_services_have_resource_limits(self):
        content = (BASE_DIR / "docker-compose.yml").read_text()
        for service in ("app", "worker", "db", "nginx"):
            block = content.split(f"\n  {service}:", 1)[1][:600]
            self.assertIn("mem_limit", block, f"у {service} нет лимита памяти")

    def test_nginx_compresses_responses(self):
        shared = (
            BASE_DIR / "docker" / "nginx" / "templates" / "05-shared.conf.template"
        ).read_text()
        self.assertIn("gzip              on;", shared)
        self.assertIn("application/json", shared)

    def test_nginx_rate_limits_the_api(self):
        shared = (
            BASE_DIR / "docker" / "nginx" / "templates" / "05-shared.conf.template"
        ).read_text()
        ssl = (
            BASE_DIR / "docker" / "nginx" / "templates" / "20-ssl.conf.template"
        ).read_text()
        self.assertIn("limit_req_zone", shared)
        self.assertIn("limit_req zone=api", ssl)

    def test_static_is_hashed_before_being_cached_forever(self):
        """Вечный кэш на нехешированных именах отдаёт старую статику после деплоя."""
        settings = load_settings()
        self.assertIn(
            "ManifestStaticFilesStorage",
            settings.STORAGES["staticfiles"]["BACKEND"],
        )
        ssl = (
            BASE_DIR / "docker" / "nginx" / "templates" / "20-ssl.conf.template"
        ).read_text()
        self.assertIn("immutable", ssl)

    def test_migrations_do_not_block_writes(self):
        """CREATE INDEX держит SHARE, DROP INDEX — ACCESS EXCLUSIVE."""
        migration = next(
            (BASE_DIR / "billing" / "migrations").glob("0002_*.py")
        ).read_text()
        self.assertIn("atomic = False", migration)
        self.assertIn("AddIndexConcurrently", migration)
        self.assertIn("RemoveIndexConcurrently", migration)

    def test_celery_task_logs_are_not_two_lines_per_task(self):
        settings = load_settings()
        self.assertEqual(
            settings.LOGGING["loggers"]["celery.app.trace"]["level"], "WARNING"
        )

    def test_task_arguments_are_not_logged(self):
        """celery.worker.strategy печатает полные аргументы задачи на INFO.

        Здесь это координаты пользователя, то есть персональные данные в
        системе сбора логов.
        """
        settings = load_settings()
        self.assertEqual(
            settings.LOGGING["loggers"]["celery.worker.strategy"]["level"], "WARNING"
        )

    def test_logs_are_structured(self):
        settings = load_settings()
        handler = settings.LOGGING["handlers"]["console"]
        self.assertEqual(handler["formatter"], "json")

    def test_celery_does_not_replace_the_logging_config(self):
        """worker_hijack_root_logger по умолчанию True.

        При нём весь блок LOGGING внутри воркера игнорируется, и половина
        системы пишет текстом, пока вторая пишет JSON.
        """
        settings = load_settings()
        self.assertFalse(settings.CELERY_WORKER_HIJACK_ROOT_LOGGER)

    def test_nginx_degraded_mode_uses_separate_templates_not_sed(self):
        script = (
            BASE_DIR / "docker" / "nginx" / "entrypoint.d" / "40-select-tls-mode.sh"
        ).read_text()
        self.assertNotIn("sed -i", script)
        templates = BASE_DIR / "docker" / "nginx" / "templates"
        self.assertTrue((templates / "10-http-direct.conf.template").exists())
        self.assertTrue((templates / "10-http-redirect.conf.template").exists())
