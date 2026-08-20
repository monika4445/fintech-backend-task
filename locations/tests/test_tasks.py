import datetime
import json
import uuid
from decimal import Decimal
from unittest import mock

import fakeredis
from django.test import SimpleTestCase

from locations import redis_client
from locations.encoders import ExtendedJSONEncoder, dumps, loads, restore_decimals
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
        from django.core.serializers.json import DjangoJSONEncoder

        moment = datetime.datetime(2026, 8, 20, 12, 30, 45, 123456)
        ours = json.loads(json.dumps({"at": moment}, cls=ExtendedJSONEncoder))["at"]
        theirs = json.loads(json.dumps({"at": moment}, cls=DjangoJSONEncoder))["at"]
        self.assertEqual(ours, "2026-08-20T12:30:45.123456")
        self.assertEqual(theirs, "2026-08-20T12:30:45.123")

    def test_nested_structures(self):
        payload = dumps({"points": [{"lat": Decimal("1.5")}, {"lat": Decimal("2.5")}]})
        self.assertEqual(json.loads(payload)["points"][1]["lat"], "2.5")

    def test_unknown_type_still_raises(self):
        class Weird:
            pass

        with self.assertRaises(TypeError):
            dumps({"x": Weird()})

    def test_bytes_raise_a_useful_error(self):
        with self.assertRaises(TypeError) as ctx:
            dumps({"blob": b"\xff\xfe"})
        self.assertIn("base64", str(ctx.exception))


class RestoreDecimalsTests(SimpleTestCase):
    """Обратное преобразование по списку полей, а не по форме строки."""

    def test_round_trip_keeps_precision(self):
        original = {"lat": Decimal("0.1"), "lon": Decimal("55.755826")}
        restored = restore_decimals(loads(dumps(original)), fields=("lat", "lon"))
        self.assertEqual(restored, original)
        self.assertNotEqual(Decimal(float(Decimal("0.1"))), Decimal("0.1"))

    def test_does_not_touch_fields_it_was_not_asked_about(self):
        """Ранняя версия угадывала по форме строки и портила данные.

        '0042' превращалось в Decimal('42'), '1e5' — в Decimal('1E+5'),
        а 'NaN' — в значение, не равное самому себе.
        """
        data = {"card_last4": "0042", "zip": "01234", "ref": "1e5", "amount": "10.50"}
        restored = restore_decimals(data, fields=("amount",))
        self.assertEqual(restored["card_last4"], "0042")
        self.assertEqual(restored["zip"], "01234")
        self.assertEqual(restored["ref"], "1e5")
        self.assertEqual(restored["amount"], Decimal("10.50"))

    def test_rejects_nan_and_infinity(self):
        for poison in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(poison=poison):
                with self.assertRaises(ValueError):
                    restore_decimals({"amount": poison}, fields=("amount",))

    def test_rejects_non_numeric_field(self):
        with self.assertRaises(ValueError):
            restore_decimals({"amount": "около десяти"}, fields=("amount",))

    def test_missing_field_is_not_an_error(self):
        self.assertEqual(restore_decimals({"a": 1}, fields=("b",)), {"a": 1})


class CeleryArgumentSerializationTests(SimpleTestCase):
    """Где именно возникает TypeError из условия задачи."""

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
        self.assertIsInstance(restored["lat"], Decimal)
        self.assertIsInstance(restored["at"], datetime.datetime)
        self.assertIsInstance(restored["device"], uuid.UUID)
        self.assertEqual(restored, payload)

    def test_task_body_receives_real_decimal(self):
        from kombu.serialization import dumps as kombu_dumps
        from kombu.serialization import loads as kombu_loads

        content_type, encoding, data = kombu_dumps(
            {"lat": Decimal("55.7558")}, serializer="json"
        )
        delivered = kombu_loads(data, content_type, encoding)
        with self.assertRaises(TypeError):
            json.dumps(delivered)

    def test_celery_app_keeps_stock_serializer(self):
        from config.celery import app

        self.assertEqual(app.conf.task_serializer, "json")
        self.assertEqual(list(app.conf.accept_content), ["json"])

    def test_settings_are_not_overridden_in_code(self):
        """Раньше config/celery.py присваивал app.conf.* поверх settings."""
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[2]
            / "config"
            / "celery.py"
        ).read_text()
        self.assertNotIn("app.conf.task_serializer =", source)
        self.assertNotIn("app.conf.accept_content =", source)
