"""Переход на частичный индекс и снятие избыточного индекса ForeignKey.

Операции взяты в CONCURRENTLY-варианте, а сгенерированные Django обычные
заменены вручную. Причина не стилистическая: `CREATE INDEX` держит на таблице
SHARE-блокировку и останавливает все записи на время построения, а `DROP INDEX`
берёт ACCESS EXCLUSIVE и останавливает вообще всё. На таблице транзакций
работающего сервиса это минуты простоя записи, то есть плановая авария внутри
обычного деплоя.

CONCURRENTLY не выполняется внутри транзакции, поэтому atomic = False. Плата за
это — миграция не откатывается автоматически: при обрыве остаётся INVALID-индекс,
который надо удалить руками и запустить снова. Это осознанный размен простоя на
ручное восстановление в редком случае.
"""

import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.operations import (
    AddIndexConcurrently,
    RemoveIndexConcurrently,
)
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("billing", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Сначала строим новый индекс, потом убираем старые. Обратный порядок
        # оставил бы выборку без индекса на время построения, а это seq scan по
        # всей истории под SELECT FOR UPDATE.
        AddIndexConcurrently(
            model_name="transaction",
            index=models.Index(
                condition=models.Q(("status__in", ["authorized", "pending"])),
                fields=["user", "id"],
                name="tx_user_unprocessed_idx",
            ),
        ),
        RemoveIndexConcurrently(
            model_name="transaction",
            name="tx_user_status_idx",
        ),
        # Снимает автоматический индекс ForeignKey на (user_id): он был строгим
        # префиксом снятого выше составного и остаётся избыточным при частичном.
        migrations.AlterField(
            model_name="transaction",
            name="user",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="transactions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
