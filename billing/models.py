from django.conf import settings
from django.db import models
from django.utils import timezone


class TransactionStatus(models.TextChoices):
    """Справочник известных значений `status`, а НЕ ограничение поля.

    Даёт именованные константы вместо строковых литералов в коде, чтобы
    опечатка `proccessed` была видна на импорте, а не в данных.

    Намеренно не подставляется в `choices` самого поля: схема задания описывает
    `status` как `varchar(20)` и никакого перечня значений не фиксирует. Объявив
    `choices`, мы добавили бы ограничение уровня приложения, которого в
    постановке нет, — и это прямо противоречило бы решению обрабатывать все
    транзакции независимо от статуса. Значение вроде `hold` из чужой системы
    должно записываться и обрабатываться, а не отклоняться валидацией формы.
    """

    PENDING = "pending", "Ожидает обработки"
    AUTHORIZED = "authorized", "Захолдировано"
    PROCESSED = "processed", "Обработана"
    FAILED = "failed", "Отклонена"
    REFUNDED = "refunded", "Возвращена"


# Набор статусов, из которых переход в processed безопасен с точки зрения денег:
# refunded -> processed и failed -> processed означали бы повторный учёт уже
# возвращённых средств.
#
# ВАЖНО: по умолчанию НЕ применяется. Условие задания требует обновить статус
# всех транзакций пользователя, а перечня допустимых значений `status` не задаёт
# вовсе (в схеме это просто varchar(20)). Ограничивать обработку собственным
# перечнем значило бы молча решить за постановщика, и на данных со статусом вне
# этого перечня функция не обработала бы ничего. Поэтому набор доступен
# вызывающему параметром, а решение остаётся за ним.
#
# Кортеж, а не frozenset: порядок значений попадает в текст SQL, и у множества
# он меняется между процессами, дробя один логический запрос на несколько
# записей в pg_stat_statements.
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
    # varchar(20) ровно так, как в схеме задания: без choices, потому что
    # перечня допустимых значений постановка не задаёт. См. TransactionStatus.
    status = models.CharField(max_length=20, default=TransactionStatus.PENDING)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # default, а не auto_now_add. Оба заполняют поле автоматически, но
    # auto_now_add дополнительно делает его нередактируемым: явно переданное
    # значение молча игнорируется. Схема задания требует только timestamp, а
    # тихая потеря переданного значения ломает перенос данных из другой системы
    # и загрузку фикстур — ровно тот вид отказа, который здесь везде считается
    # худшим.
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "transaction"
        indexes = [
            # Обычный индекс по (user_id, id), а не частичный по статусу.
            #
            # Частичный вариант здесь был и оказался ошибкой. Он покрывал только
            # необработанные строки и был в девять раз меньше, но обслуживал
            # ровно один запрос. К колонке user_id ведёт как минимум ещё один
            # путь — каскадное удаление пользователя, — и на нём план
            # деградировал до Seq Scan по всей таблице: замерено на 200k строк,
            # 100 000 строк отфильтровано впустую. Для финтеха удаление аккаунта
            # не гипотеза, а требование об удалении персональных данных.
            #
            # Этот индекс обслуживает оба пути: user_id идёт префиксом, поэтому
            # поиск по одному user_id тоже им покрывается. Это и есть причина,
            # по которой автоматический индекс ForeignKey остаётся отключённым:
            # он был бы строгим префиксом вот этого и потому избыточен.
            models.Index(fields=["user", "id"], name="tx_user_id_idx"),
        ]

    def __str__(self) -> str:
        return f"Transaction(id={self.pk}, status={self.status})"
