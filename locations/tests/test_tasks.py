import datetime
import json
import uuid
from decimal import Decimal
from unittest import mock

import fakeredis
from django.test import SimpleTestCase

from locations import redis_client
from locations.encoders import ExtendedJSONEncoder, dumps, loads
from locations.tasks import LAST_LOCATION_TTL_SECONDS, send_location_update


class EncoderTests(SimpleTestCase):
    def test_plain_json_dumps_fails_on_decimal(self):
        """Фиксируем исходный баг, чтобы фикс не откатили молча."""
        with self.assertRaises(TypeError) as ctx:
            json.dumps({"lat": Decimal("55.7558")})
        self.assertIn("Decimal", str(ctx.exception))

    def test_handles_decimal_datetime_uuid(self):
        payload = dumps(
            {
                "lat": Decimal("55.755826"),
                "lon": Decimal("37.617300"),
                "at": datetime.datetime(2026, 8, 20, 12, 30, 45, 123456),
                "device": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            }
        )
        decoded = json.loads(payload)
        self.assertEqual(decoded["lat"], "55.755826")
        self.assertEqual(decoded["at"], "2026-08-20T12:30:45.123456")
        self.assertEqual(decoded["device"], "12345678-1234-5678-1234-567812345678")

    def test_microseconds_survive_unlike_djangojsonencoder(self):
        """DjangoJSONEncoder режет микросекунды до миллисекунд, наш кодек нет."""
        from django.core.serializers.json import DjangoJSONEncoder

        moment = datetime.datetime(2026, 8, 20, 12, 30, 45, 123456)
        ours = json.loads(json.dumps({"at": moment}, cls=ExtendedJSONEncoder))["at"]
        theirs = json.loads(json.dumps({"at": moment}, cls=DjangoJSONEncoder))["at"]
        self.assertEqual(ours, "2026-08-20T12:30:45.123456")
        self.assertEqual(theirs, "2026-08-20T12:30:45.123")
        self.assertNotEqual(ours, theirs)

    def test_decimal_round_trip_keeps_precision(self):
        original = Decimal("0.1")
        restored = loads(dumps({"v": original}), restore_decimals=True)["v"]
        self.assertEqual(restored, original)
        self.assertNotEqual(Decimal(float(original)), original)

    def test_nested_structures(self):
        payload = dumps({"points": [{"lat": Decimal("1.5")}, {"lat": Decimal("2.5")}]})
        self.assertEqual(json.loads(payload)["points"][1]["lat"], "2.5")

    def test_unknown_type_still_raises(self):
        """Энкодер не должен глотать то, чего не понимает."""

        class Weird:
            pass

        with self.assertRaises(TypeError):
            dumps({"x": Weird()})


class SendLocationUpdateTests(SimpleTestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        patcher = mock.patch.object(
            redis_client, "get_redis", return_value=self.redis
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writes_decimal_payload_without_error(self):
        send_location_update.apply(
            args=(42, {"lat": Decimal("55.7558"), "lon": Decimal("37.6173")})
        ).get()

        stored = json.loads(self.redis.get("user:42:last_loc"))
        self.assertEqual(stored, {"lat": "55.7558", "lon": "37.6173"})

    def test_sets_ttl(self):
        send_location_update.apply(args=(42, {"lat": Decimal("1")})).get()
        ttl = self.redis.ttl("user:42:last_loc")
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, LAST_LOCATION_TTL_SECONDS)

    def test_key_format_matches_specification(self):
        send_location_update.apply(args=(7, {"lat": 1})).get()
        self.assertIn("user:7:last_loc", self.redis.keys("*"))


class CeleryArgumentSerializationTests(SimpleTestCase):
    """Где именно возникает TypeError из условия задачи.

    Одинаковый текст ошибки возможен в двух местах: в веб-процессе, когда kombu
    сериализует аргументы на .delay(), и в воркере, когда json.dumps работает в
    теле таска. Это два разных бага, и фикс у них разный. Тесты ниже
    фиксируют, что на актуальном kombu первый случай уже закрыт, значит
    диагноз однозначен: виновато тело таска.
    """

    def test_kombu_json_already_handles_decimal_datetime_uuid(self):
        from kombu.serialization import dumps as kombu_dumps
        from kombu.serialization import loads as kombu_loads

        payload = {
            "lat": Decimal("55.755826"),
            "at": datetime.datetime(2026, 8, 20, 12, 30, 45, 123456),
            "device": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        }
        content_type, encoding, data = kombu_dumps(payload, serializer="json")
        restored = kombu_loads(data, content_type, encoding)

        self.assertEqual(content_type, "application/json")
        # Типы восстанавливаются, а не приезжают строками.
        self.assertIsInstance(restored["lat"], Decimal)
        self.assertIsInstance(restored["at"], datetime.datetime)
        self.assertIsInstance(restored["device"], uuid.UUID)
        self.assertEqual(restored, payload)

    def test_task_body_receives_real_decimal(self):
        """Именно поэтому json.dumps в теле таска и падает.

        kombu честно довозит Decimal до воркера, так что аргумент внутри таска
        это настоящий Decimal, а не строка.
        """
        from kombu.serialization import dumps as kombu_dumps
        from kombu.serialization import loads as kombu_loads

        content_type, encoding, data = kombu_dumps(
            {"lat": Decimal("55.7558")}, serializer="json"
        )
        delivered = kombu_loads(data, content_type, encoding)
        with self.assertRaises(TypeError):
            json.dumps(delivered)

    def test_celery_app_keeps_stock_serializer(self):
        """Свой content type не регистрируется намеренно.

        Он не даёт выгоды и вносит ContentDisallowed при раскатке.
        """
        from config.celery import app

        self.assertEqual(app.conf.task_serializer, "json")
        self.assertEqual(list(app.conf.accept_content), ["json"])
