"""Сверка фактических колонок в базе со схемой из задания.

Этот тест появился последним и по конкретному поводу. Схема требует
`User.id — bigint`, но штатная `django.contrib.auth.User` создаёт `integer`:
приложение `auth` переопределяет `DEFAULT_AUTO_FIELD` своим `AutoField`, и
настройка проекта на него не влияет. Вместе с ключом `integer` получали и обе
колонки внешних ключей, потому что колонка FK повторяет тип цели.

Расхождение прожило через несколько проходов ревью, потому что все они читали
код, а не смотрели на то, что реально создано в базе. Модели выглядели
безупречно: `DEFAULT_AUTO_FIELD = BigAutoField` стоял на месте.
"""

from django.db import connection
from django.test import TestCase, skipUnlessDBFeature

# Схема из условия. Ключ — таблица и колонка, значение — ожидаемый тип
# PostgreSQL и точность там, где условие её задаёт.
EXPECTED = {
    ("user", "id"): ("bigint", None),
    ("user", "username"): ("character varying", None),
    ("user", "email"): ("character varying", None),
    ("profile", "id"): ("bigint", None),
    ("profile", "user_id"): ("bigint", None),
    ("profile", "has_processed_transactions"): ("boolean", None),
    ("transaction", "id"): ("bigint", None),
    ("transaction", "user_id"): ("bigint", None),
    ("transaction", "status"): ("character varying", 20),
    ("transaction", "amount"): ("numeric", (12, 2)),
    ("transaction", "created_at"): ("timestamp with time zone", None),
    ("transaction", "updated_at"): ("timestamp with time zone", None),
}


@skipUnlessDBFeature("has_select_for_update")
class SchemaMatchesTheAssignmentTests(TestCase):
    """PostgreSQL-специфично: на SQLite типов в этом смысле просто нет."""

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name, column_name, data_type,
                       character_maximum_length, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name IN ('user', 'profile', 'transaction')
                """
            )
            cls.columns = {
                (t, c): (dtype, length, precision, scale)
                for t, c, dtype, length, precision, scale in cursor.fetchall()
            }

    def test_every_column_from_the_assignment_exists(self):
        for key in EXPECTED:
            with self.subTest(column="%s.%s" % key):
                self.assertIn(key, self.columns, f"колонка {key} отсутствует")

    def test_column_types_match(self):
        for key, (expected_type, _) in EXPECTED.items():
            with self.subTest(column="%s.%s" % key):
                actual = self.columns.get(key)
                self.assertIsNotNone(actual, f"колонка {key} отсутствует")
                self.assertEqual(
                    actual[0],
                    expected_type,
                    f"{key[0]}.{key[1]}: схема требует {expected_type}, "
                    f"в базе {actual[0]}",
                )

    def test_declared_precision_matches(self):
        """varchar(20) и decimal(12,2) заданы в условии явно."""
        status = self.columns[("transaction", "status")]
        self.assertEqual(status[1], 20, f"status: ожидалось varchar(20), было {status[1]}")

        amount = self.columns[("transaction", "amount")]
        self.assertEqual(
            (amount[2], amount[3]),
            (12, 2),
            f"amount: ожидалось decimal(12,2), было ({amount[2]},{amount[3]})",
        )

    def test_profile_user_is_unique(self):
        """Схема помечает Profile.user_id как UNIQUE."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM pg_index i
                JOIN pg_class t ON t.oid = i.indrelid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
                WHERE t.relname = 'profile' AND a.attname = 'user_id' AND i.indisunique
                """
            )
            self.assertGreater(cursor.fetchone()[0], 0, "profile.user_id не UNIQUE")
