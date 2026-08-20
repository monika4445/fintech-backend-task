"""Тесты на план запроса.

Индекс по (user_id, id) обслуживает два разных пути: пакетную выборку в
process_user_transactions и поиск транзакций при каскадном удалении
пользователя. Второй путь легко упустить — именно так сюда однажды попал
частичный индекс, который ускорял первый запрос и превращал второй в Seq Scan
по всей таблице.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, skipUnlessDBFeature

from billing.models import Profile, Transaction, TransactionStatus

INDEX_NAME = "tx_user_id_idx"

# Выборочность важна: если пользователю принадлежит половина таблицы,
# планировщик справедливо предпочтёт Seq Scan, и тест проверял бы не индекс,
# а объём данных. Здесь у проверяемого пользователя доля порядка процента,
# как и бывает в реальной таблице транзакций.
VICTIM_ROWS = 500
BACKGROUND_ROWS = 40_000


def seed(user, rows):
    Transaction.objects.bulk_create(
        [
            Transaction(
                user=user,
                status=(
                    TransactionStatus.PENDING
                    if i % 50 == 0
                    else TransactionStatus.PROCESSED
                ),
                amount="10.00",
            )
            for i in range(rows)
        ],
        batch_size=5000,
    )
    with connection.cursor() as cursor:
        cursor.execute('ANALYZE "transaction"')


def explain(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (COSTS OFF) {sql}", params)
        return "\n".join(line for (line,) in cursor.fetchall())


@skipUnlessDBFeature("has_select_for_update")
class IndexCoverageTests(TestCase):
    def test_index_is_not_partial(self):
        """Частичный индекс покрывал бы только один из двух путей."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_expr(indpred, indrelid) FROM pg_index "
                "WHERE indexrelid = %s::regclass",
                [INDEX_NAME],
            )
            row = cursor.fetchone()

        self.assertIsNotNone(row, f"индекс {INDEX_NAME} отсутствует")
        self.assertIsNone(
            row[0],
            "индекс стал частичным: поиск по одному user_id (каскадное "
            "удаление) им больше не покрывается",
        )

    def setUp(self):
        self.user = get_user_model().objects.create(username="planner", email="p@e.com")
        Profile.objects.create(user=self.user)
        background = get_user_model().objects.create(username="crowd", email="c@e.com")
        Profile.objects.create(user=background)
        seed(background, BACKGROUND_ROWS)
        seed(self.user, VICTIM_ROWS)

    def test_batch_query_uses_the_index(self):
        user = self.user

        queryset = Transaction.objects.filter(user_id=user.pk).order_by("pk")
        sql, params = queryset.query.sql_with_params()
        plan = explain(sql, params)

        self.assertIn(INDEX_NAME, plan, plan)
        self.assertNotIn("Seq Scan", plan, plan)

    def test_cascade_delete_lookup_uses_the_index(self):
        """Регрессия, которую внёс частичный индекс.

        on_delete=CASCADE ищет транзакции по user_id без фильтра по статусу.
        Удаление аккаунта — не гипотетический сценарий, а требование об
        удалении персональных данных.
        """
        user = self.user

        plan = explain('SELECT id FROM "transaction" WHERE user_id = %s', [user.pk])

        self.assertNotIn(
            "Seq Scan",
            plan,
            "поиск транзакций пользователя идёт последовательным сканированием, "
            f"удаление аккаунта прочитает всю таблицу:\n{plan}",
        )
        self.assertIn(INDEX_NAME, plan, plan)
