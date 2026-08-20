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
PROCESSABLE_STATUSES = frozenset(
    {TransactionStatus.PENDING, TransactionStatus.AUTHORIZED}
)


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
            # Выборка в process_user_transactions идёт ровно по этой паре.
            # Без индекса это seq scan по всей таблице транзакций под
            # SELECT FOR UPDATE, то есть блокировка растёт вместе с историей.
            models.Index(fields=["user", "status"], name="tx_user_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Transaction(id={self.pk}, status={self.status})"
