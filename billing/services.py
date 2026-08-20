"""Задача 1: атомарная обработка транзакций пользователя.

Ограничение задания: только save() и ORM, bulk_update запрещён. Это ограничение
задаёт потолок пропускной способности, см. LOCK_DURATION_WARNING_ROWS ниже.
"""

import logging
from collections.abc import Collection

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import transaction

from billing.models import Profile, Transaction, TransactionStatus

logger = logging.getLogger(__name__)

# Запрет bulk_update означает один UPDATE на строку. При RTT до БД в 1 мс это
# примерно 1 секунда удержания SELECT FOR UPDATE на каждую тысячу транзакций,
# в течение которых любой конкурирующий процесс по этому пользователю стоит.
# Порог не меняет поведение, он делает потолок видимым в логах до того, как о
# нём сообщат из мониторинга.
LOCK_DURATION_WARNING_ROWS = 1_000

# Размер порции серверного курсора. Обычный обход queryset вычисляет весь набор
# в память до начала цикла: измерено 733 байта на строку, то есть 699 MiB на
# миллион транзакций одного пользователя. С iterator() тот же обход занимает
# 14 MiB, потому что строки приезжают порциями. Разница не в скорости, а в том,
# что без неё крупный клиент вообще не обрабатывается: воркер получает OOM-kill
# посреди открытой транзакции, откатывается и упирается в тот же предел снова.
FETCH_CHUNK_SIZE = 2_000


def _resolve_user_id(user: AbstractBaseUser | int) -> int:
    """Достаёт первичный ключ, не полагаясь на наличие атрибута `pk`.

    Ранняя версия делала `getattr(user, "pk", user)`. Атрибут `pk` есть у любой
    модели Django, поэтому переданный по ошибке `Profile` молча сходил за
    пользователя: функция брала id профиля как id пользователя и обрабатывала
    транзакции постороннего лица, не подняв ни одной ошибки. Соседство
    пользователя и профиля в этой же операции делает такую опечатку особенно
    вероятной.
    """
    if isinstance(user, get_user_model()):
        if user.pk is None:
            raise ValueError(
                "process_user_transactions получил несохранённого пользователя: "
                "у объекта нет первичного ключа"
            )
        return user.pk
    if isinstance(user, int) and not isinstance(user, bool):
        return user
    raise TypeError(
        "process_user_transactions ожидает экземпляр User или его первичный "
        f"ключ, получено: {type(user).__name__}"
    )


def _validated_statuses(statuses: Collection[str] | None) -> Collection[str] | None:
    """Отвергает одиночную строку там, где ожидается коллекция строк.

    `statuses="pending"` — частая опечатка, и она тихая: Django развернёт строку
    посимвольно в `status__in=["p", "e", "n", ...]`, совпадений не найдёт и
    обработает ноль транзакций, а флаг в профиле всё равно будет выставлен.
    """
    if statuses is None:
        return None
    if isinstance(statuses, (str, bytes)):
        raise TypeError(
            "statuses ожидает коллекцию строк, а не одну строку: "
            f"передайте ({statuses!r},) вместо {statuses!r}"
        )
    return statuses


@transaction.atomic
def process_user_transactions(
    user: AbstractBaseUser | int,
    *,
    statuses: Collection[str] | None = None,
) -> int:
    """Переводит транзакции пользователя в статус processed.

    Всё выполняется в одной транзакции БД: либо применяются и новые статусы, и
    флаг в профиле, либо не применяется ничего.

    По умолчанию обрабатываются ВСЕ транзакции пользователя, как того требует
    условие. Схема задаёт `status` как varchar(20) и перечня допустимых значений
    не фиксирует, поэтому сузить обработку собственным набором статусов означало
    бы на чужих данных не обработать ничего и при этом отметить профиль как
    обработанный.

    Args:
        user: экземпляр User или его первичный ключ. Условие описывает функцию
            как берущую пользователя, поэтому объект принимается; id принимается
            тоже, потому что вызывающему часто нечего передать кроме него, а
            заставлять его ради этого делать лишний запрос незачем.
        statuses: необязательное сужение набора. Осмысленное значение —
            `PROCESSABLE_STATUSES`: оно запрещает переходы вида
            refunded -> processed, то есть повторный учёт уже возвращённых
            денег. Решение принимает вызывающий, потому что перечень статусов —
            это знание о предметной области, а не о механике обработки.

    Возвращает количество фактически обновлённых транзакций.

    Raises:
        Profile.DoesNotExist: профиля нет. Схема гарантирует UNIQUE на user_id,
            но не гарантирует наличие строки, а работать без профиля эта
            операция не может: флаг ставить некуда. Падаем громко, а не
            создаём профиль на лету, потому что молчаливое создание скрыло бы
            поломку в регистрации пользователя.
    """
    user_id = _resolve_user_id(user)
    statuses = _validated_statuses(statuses)

    # Два уровня блокировки здесь удерживаются сознательно, и решение записано,
    # чтобы его не пришлось выводить заново. Мьютексом служит строка профиля: она
    # берётся первой и сериализует всех вызывающих этой функции. Построчный
    # FOR UPDATE ниже нужен не для этого, а на случай других писателей по
    # таблице транзакций, о которых эта функция знать не может. Цена известна:
    # PostgreSQL проставляет каждой заблокированной версии строки xmax, то есть
    # грязнит страницу, и на крупных партиях это заметная работа сверх самих
    # UPDATE. Если писатель когда-нибудь окажется ровно один, построчную
    # блокировку можно снять — но только после проверки, а не по догадке.
    #
    # Блокируется только строка профиля, и это проверяется тестом по
    # фактическому SQL, а не утверждается комментарием.
    #
    # select_related("user") здесь был бы активно вредным, хотя выглядит как
    # оптимизация. В PostgreSQL `SELECT ... FOR UPDATE` без `OF` блокирует
    # строки ВСЕХ таблиц джойна, поэтому join с auth_user означал бы блокировку
    # пользователя на всё время цикла — то есть ровно то, чего мы избегаем.
    # Функции поле profile.user не нужно ни разу, так что join не нужен вовсе.
    profile = Profile.objects.select_for_update().get(user_id=user_id)

    queryset = Transaction.objects.select_for_update().filter(user_id=user_id)
    if statuses is not None:
        queryset = queryset.filter(status__in=statuses)

    transactions = (
        queryset
        # order_by обязателен, а не косметика. Без него порядок строк выбирает
        # планировщик, два воркера на пересекающихся наборах берут блокировки в
        # разном порядке и получают deadlock. Общий порядок по pk делает
        # захват согласованным между всеми процессами.
        .order_by("pk")
    )

    updated = 0
    for tx in transactions.iterator(chunk_size=FETCH_CHUNK_SIZE):
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

    if updated == 0:
        # Условие требует выставить флаг, поэтому он выставляется и здесь. Но
        # «профиль отмечен как обработанный, а обработано ноль строк» — это
        # состояние, о котором надо узнать из логов, а не по расследованию
        # расхождения в отчётах. Штатно означает, что транзакций просто нет.
        logger.warning(
            "process_user_transactions: user_id=%s, обновлено 0 транзакций, "
            "флаг в профиле всё равно выставляется",
            user_id,
        )

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
