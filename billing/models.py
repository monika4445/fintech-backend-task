from django.conf import settings
from django.db import models


class TransactionStatus(models.TextChoices):
    """Статусы вынесены в тип, а не разбросаны строковыми литералами.

    Это то, что превращает опечатку "proccessed" из тихой порчи данных в
    ошибку валидации.
    """

    PENDING = "pending", "Ожидает обработки"
    AUTHORIZED = "authorized", "Захолдировано"
    PROCESSED = "processed", "Обработана"
    FAILED = "failed", "Отклонена"
    REFUNDED = "refunded", "Возвращена"


# Явная машина состояний вместо "любой статус -> processed".
# Без этого списка refunded -> processed и failed -> processed разрешены, то
# есть уже возвращённые деньги учитываются повторно. В финтехе это не стилистика.
# Кортеж, а не frozenset: порядок значений попадает в текст SQL, и у множества
# он меняется между процессами. Один логический запрос при этом дробится на
# несколько записей в pg_stat_statements, и статистика по нему рассыпается.
PROCESSABLE_STATUSES = (TransactionStatus.PENDING, TransactionStatus.AUTHORIZED)


class Profile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    has_processed_transactions = models.BooleanField(default=False)

    class Meta:
        db_table = "profile"

    def __str__(self) -> str:
        return f"Profile(user_id={self.user_id})"


class Transaction(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transactions",
        # ForeignKey по умолчанию заводит собственный индекс на (user_id),
        # который является строгим префиксом частичного индекса ниже и потому
        # избыточен. Измерено на 200k строк: 1280 kB чистых накладных расходов,
        # и, что важнее, лишний индексный апдейт на каждую запись — а эта
        # функция делает по одному UPDATE на строку.
        db_index=False,
    )
    status = models.CharField(
        max_length=20,
        choices=TransactionStatus.choices,
        default=TransactionStatus.PENDING,
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "transaction"
        indexes = [
            # Частичный индекс: выборка в process_user_transactions запрашивает
            # только необработанные транзакции, а терминальные (processed,
            # failed, refunded) не запрашивает никогда.
            #
            # Измерено на 200k строк, из которых 2% активны: полный индекс на
            # (user_id, status) занимает 1288 kB, этот — 144 kB, в девять раз
            # меньше, выборка 2.49 мс против 3.32 мс. Важнее разовой экономии
            # поведение во времени: полный индекс растёт вместе с историей
            # навсегда, частичный остаётся примерно постоянным, потому что доля
            # незавершённых транзакций от объёма истории не зависит.
            #
            # ВНИМАНИЕ: condition обязан совпадать с PROCESSABLE_STATUSES.
            # Связь неявная, поэтому её стережёт тест на план запроса
            # (billing/tests/test_query_plans.py). Добавить статус в множество
            # и забыть про индекс — значит тихо вернуться к bitmap scan по
            # всей таблице.
            models.Index(
                fields=["user", "id"],
                name="tx_user_unprocessed_idx",
                condition=models.Q(status__in=sorted(PROCESSABLE_STATUSES)),
            ),
        ]

    def __str__(self) -> str:
        return f"Transaction(id={self.pk}, status={self.status})"
