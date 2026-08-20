"""Тесты на план запроса.

Частичный индекс связан с PROCESSABLE_STATUSES неявно: его предикат обязан
покрывать ровно те статусы, которые выбирает process_user_transactions. Связь
эту компилятор не проверяет. Добавить статус в множество и забыть про индекс —
значит тихо вернуться к сканированию всей истории под SELECT FOR UPDATE, причём
все остальные тесты останутся зелёными, потому что поведение не изменится,
изменится только время.
"""

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, skipUnlessDBFeature

from billing.models import PROCESSABLE_STATUSES, Profile, Transaction, TransactionStatus

INDEX_NAME = "tx_user_unprocessed_idx"


@skipUnlessDBFeature("has_select_for_update")
class PartialIndexContractTests(TestCase):
    def test_index_predicate_matches_processable_statuses(self):
        """Предикат индекса и множество статусов обязаны совпадать."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_expr(indpred, indrelid) FROM pg_index "
                "WHERE indexrelid = %s::regclass",
                [INDEX_NAME],
            )
            row = cursor.fetchone()

        self.assertIsNotNone(row, f"индекс {INDEX_NAME} отсутствует")
        predicate = row[0]
        self.assertIsNotNone(predicate, f"{INDEX_NAME} перестал быть частичным")

        for status in PROCESSABLE_STATUSES:
            self.assertIn(
                str(status.value),
                predicate,
                f"статус {status.value} есть в PROCESSABLE_STATUSES, "
                f"но не покрыт предикатом индекса: {predicate}",
            )
        terminal = set(TransactionStatus.values) - {s.value for s in PROCESSABLE_STATUSES}
        for status in terminal:
            self.assertNotIn(
                f"'{status}'",
                predicate,
                f"терминальный статус {status} попал в частичный индекс, "
                "он раздувает индекс без пользы",
            )

    def test_planner_uses_the_partial_index(self):
        user = User.objects.create(username="planner", email="p@e.com")
        Profile.objects.create(user=user)
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
                for i in range(20_000)
            ],
            batch_size=5000,
        )
        with connection.cursor() as cursor:
            cursor.execute('ANALYZE "transaction"')

        queryset = Transaction.objects.filter(
            user_id=user.pk, status__in=PROCESSABLE_STATUSES
        ).order_by("pk")
        sql, params = queryset.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (COSTS OFF) {sql}", params)
            plan = "\n".join(line for (line,) in cursor.fetchall())

        self.assertIn(
            INDEX_NAME,
            plan,
            "планировщик не использует частичный индекс; предикат разошёлся "
            f"с PROCESSABLE_STATUSES?\n{plan}",
        )
        self.assertNotIn("Seq Scan", plan, plan)
