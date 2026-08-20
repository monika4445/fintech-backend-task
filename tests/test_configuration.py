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

    def test_sqlite_requires_explicit_opt_in(self):
        settings = load_settings()
        self.assertEqual(
            settings.DATABASES["default"]["ENGINE"],
            "django.db.backends.postgresql",
        )
        opted_in = load_settings(DJANGO_ALLOW_SQLITE="1")
        self.assertEqual(
            opted_in.DATABASES["default"]["ENGINE"], "django.db.backends.sqlite3"
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

    def test_database_connections_are_reused(self):
        settings = load_settings()
        self.assertGreater(settings.DATABASES["default"]["CONN_MAX_AGE"], 0)

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

    def test_nginx_degraded_mode_uses_separate_templates_not_sed(self):
        script = (
            BASE_DIR / "docker" / "nginx" / "entrypoint.d" / "40-select-tls-mode.sh"
        ).read_text()
        self.assertNotIn("sed -i", script)
        templates = BASE_DIR / "docker" / "nginx" / "templates"
        self.assertTrue((templates / "10-http-direct.conf.template").exists())
        self.assertTrue((templates / "10-http-redirect.conf.template").exists())
