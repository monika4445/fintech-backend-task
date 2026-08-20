"""Задача 1: атомарная обработка транзакций пользователя.

Ограничение задания: только save() и ORM, bulk_update запрещён. Это ограничение
задаёт потолок пропускной способности, см. LOCK_DURATION_WARNING_ROWS ниже.
"""

import logging

from django.db import transaction

from billing.models import PROCESSABLE_STATUSES, Profile, Transaction, TransactionStatus

logger = logging.getLogger(__name__)

# Запрет bulk_update означает один UPDATE на строку. При RTT до БД в 1 мс это
# примерно 1 секунда удержания SELECT FOR UPDATE на каждую тысячу транзакций,
# в течение которых любой конкурирующий процесс по этому пользователю стоит.
# Порог не меняет поведение, он делает потолок видимым в логах до того, как о
# нём сообщат из мониторинга.
LOCK_DURATION_WARNING_ROWS = 1_000


@transaction.atomic
def process_user_transactions(user_id: int) -> int:
    """Переводит обрабатываемые транзакции пользователя в processed.

    Всё выполняется в одной транзакции БД: либо применяются и новые статусы, и
    флаг в профиле, либо не применяется ничего.

    Возвращает количество фактически обновлённых транзакций.

    Raises:
        Profile.DoesNotExist: профиля нет. Схема гарантирует UNIQUE на user_id,
            но не гарантирует наличие строки, а работать без профиля эта
            операция не может: флаг ставить некуда. Падаем громко, а не
            создаём профиль на лету, потому что молчаливое создание скрыло бы
            поломку в регистрации пользователя.
    """
    # Блокируем только профиль. Строку User эта операция не меняет, и лишний
    # FOR UPDATE на ней добавил бы ребро в граф ожиданий с любым кодом, который
    # трогает пользователя (смена email, обновление last_login при логине).
    # select_related убирает отдельный запрос за пользователем.
    profile = (
        Profile.objects.select_for_update()
        .select_related("user")
        .get(user_id=user_id)
    )

    transactions = (
        Transaction.objects.select_for_update()
        .filter(user_id=user_id, status__in=PROCESSABLE_STATUSES)
        # order_by обязателен, а не косметика. Без него порядок строк выбирает
        # планировщик, два воркера на пересекающихся наборах берут блокировки в
        # разном порядке и получают deadlock. Общий порядок по pk делает
        # захват согласованным между всеми процессами.
        .order_by("pk")
    )

    updated = 0
    for tx in transactions:
        tx.status = TransactionStatus.PROCESSED
        # updated_at перечислен явно. Django фильтрует список полей по
        # update_fields ДО вызова pre_save (django/db/models/base.py), поэтому
        # auto_now=True без явного упоминания молча не сработает.
        tx.save(update_fields=["status", "updated_at"])
        updated += 1

    if updated >= LOCK_DURATION_WARNING_ROWS:
        logger.warning(
            "process_user_transactions: %d строк по одному UPDATE, user_id=%s. "
            "Блокировка держится всё это время; при росте объёма операцию нужно "
            "резать на батчи с осознанным отказом от глобальной атомарности.",
            updated,
            user_id,
        )

    # Идемпотентность: повторный вызов не находит обрабатываемых транзакций,
    # возвращает 0 и не трогает уже выставленный флаг лишним UPDATE.
    if not profile.has_processed_transactions:
        profile.has_processed_transactions = True
        profile.save(update_fields=["has_processed_transactions"])

    # Побочный эффект только после коммита. Внутри atomic он опубликовал бы
    # факт, которого может не случиться, и хуже того — воркер успел бы забрать
    # задачу раньше коммита и не увидел бы данных.
    transaction.on_commit(lambda: _notify_processed(user_id, updated))

    return updated


def _notify_processed(user_id: int, updated: int) -> None:
    logger.info("processed %d transactions for user_id=%s", updated, user_id)
