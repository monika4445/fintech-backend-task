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

    def test_non_string_dict_keys_are_rejected(self):
        """Граница формата, а не недоработка энкодера.

        json.dumps вызывает default() только для значений; ключи он обрабатывает
        отдельной веткой. В JSON ключ объекта всегда строка, поэтому словарь с
        ключом UUID не является JSON-совместимым, и отказ здесь правильный.
        Тест фиксирует границу, чтобы она была осознанной, а не случайной.
        """
        for key in (Decimal("1.5"), uuid.uuid4(), datetime.date(2026, 1, 1)):
            with self.subTest(key=type(key).__name__):
                with self.assertRaises(TypeError) as ctx:
                    dumps({key: "x"})
                self.assertIn("keys must be", str(ctx.exception))

        # Строковые и целочисленные ключи формат допускает.
        self.assertEqual(json.loads(dumps({"a": 1, 2: "b"})), {"a": 1, "2": "b"})

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


class SendLocationUpdateTests(SimpleTestCase):
    """Тесты самой функции из условия задачи 2.

    Эти тесты однажды уже были удалены заодно с защитой от переупорядочивания,
    и репозиторий какое-то время не проверял свой основной ответ на вторую
    задачу вообще: ни формат ключа, ни TTL, ни то, что Decimal вообще
    записывается. Соседние тесты покрывали энкодер и сериализацию Celery, то
    есть механику вокруг, но не сам таск.
    """

    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        patcher = mock.patch.object(redis_client, "get_redis", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_task(self, user_id, payload):
        return send_location_update.apply(args=(user_id, payload)).get()

    def test_key_format_matches_the_specification(self):
        """Условие задаёт ключ буквально: f"user:{user_id}:last_loc"."""
        self.run_task(7, {"lat": Decimal("55.7558")})
        self.assertEqual(self.redis.keys("*"), ["user:7:last_loc"])

    def test_value_is_json_dumps_of_the_payload(self):
        """И значение задано буквально: json.dumps(location_data)."""
        self.run_task(7, {"lat": Decimal("55.7558"), "lon": Decimal("37.6173")})
        raw = self.redis.get("user:7:last_loc")
        self.assertIsInstance(raw, str)
        self.assertEqual(json.loads(raw), {"lat": "55.7558", "lon": "37.6173"})

    def test_writes_decimal_datetime_and_uuid(self):
        """Три типа, названные в условии, плюс вложенность."""
        self.run_task(
            42,
            {
                "lat": Decimal("55.755826"),
                "at": datetime.datetime(2026, 8, 20, 12, 30, 45, 123456),
                "device": uuid.UUID("12345678-1234-5678-1234-567812345678"),
                "points": [{"amount": Decimal("0.10")}],
            },
        )
        stored = json.loads(self.redis.get("user:42:last_loc"))
        self.assertEqual(stored["lat"], "55.755826")
        self.assertEqual(stored["at"], "2026-08-20T12:30:45.123456")
        self.assertEqual(stored["device"], "12345678-1234-5678-1234-567812345678")
        self.assertEqual(stored["points"][0]["amount"], "0.10")

    def test_plain_json_dumps_would_have_failed_on_the_same_payload(self):
        """Фиксируем, что тест проверяет именно исправленный баг."""
        payload = {"lat": Decimal("55.7558")}
        with self.assertRaises(TypeError):
            json.dumps(payload)
        self.run_task(1, payload)
        self.assertIsNotNone(self.redis.get("user:1:last_loc"))

    def test_sets_a_ttl(self):
        """Ключ на пользователя без TTL живёт вечно; см. README."""
        self.run_task(42, {"lat": Decimal("1")})
        ttl = self.redis.ttl("user:42:last_loc")
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, LAST_LOCATION_TTL_SECONDS)

    def test_writes_only_the_documented_key(self):
        """Защита от переупорядочивания снята вместе со вторым ключом версии."""
        self.run_task(42, {"lat": Decimal("1"), "ts": 100})
        self.assertEqual(self.redis.keys("*"), ["user:42:last_loc"])

    def test_arbitrary_keys_carry_no_special_meaning(self):
        """Условие обещает работу с любым JSON-совместимым словарём.

        Ключ вроде `ts` в чужих данных может означать что угодно, и таск не
        должен приписывать ему смысл: последняя запись побеждает.
        """
        self.run_task(8, {"ts": 5, "lat": Decimal("1")})
        self.run_task(8, {"ts": 1, "lat": Decimal("2")})
        self.assertEqual(json.loads(self.redis.get("user:8:last_loc"))["lat"], "2")

    def test_each_user_gets_their_own_key(self):
        self.run_task(1, {"lat": Decimal("1")})
        self.run_task(2, {"lat": Decimal("2")})
        self.assertEqual(json.loads(self.redis.get("user:1:last_loc"))["lat"], "1")
        self.assertEqual(json.loads(self.redis.get("user:2:last_loc"))["lat"], "2")


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
