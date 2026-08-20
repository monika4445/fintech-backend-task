"""Замена частичного индекса на обычный по (user_id, id).

Частичный индекс покрывал только необработанные строки и обслуживал ровно один
запрос. Каскадное удаление пользователя ищет по user_id без фильтра по статусу,
и на нём план деградировал до Seq Scan по всей таблице (замерено на 200k строк:
100 000 строк отфильтровано впустую). Обычный индекс с user_id в префиксе
обслуживает оба пути.

CONCURRENTLY по той же причине, что и в 0002: обычный CREATE INDEX держит на
таблице SHARE и останавливает записи, DROP INDEX берёт ACCESS EXCLUSIVE и
останавливает всё.
"""

from django.contrib.postgres.operations import (
    AddIndexConcurrently,
    RemoveIndexConcurrently,
)
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("billing", "0002_remove_transaction_tx_user_status_idx_and_more"),
    ]

    operations = [
        # Сначала новый, потом снятие старого: обратный порядок оставил бы
        # выборку без индекса на время построения.
        AddIndexConcurrently(
            model_name="transaction",
            index=models.Index(fields=["user", "id"], name="tx_user_id_idx"),
        ),
        RemoveIndexConcurrently(
            model_name="transaction",
            name="tx_user_unprocessed_idx",
        ),
    ]
