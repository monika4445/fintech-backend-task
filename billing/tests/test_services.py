from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from billing.models import (
    PROCESSABLE_STATUSES,
    Profile,
    Transaction,
    TransactionStatus,
)
from billing.services import process_user_transactions


def make_user(username="alice"):
    user = User.objects.create(username=username, email=f"{username}@example.com")
    Profile.objects.create(user=user, has_processed_transactions=False)
    return user


def make_tx(user, status, amount="10.00"):
    return Transaction.objects.create(user=user, status=status, amount=Decimal(amount))


class ProcessUserTransactionsTests(TestCase):
    def test_updates_statuses_and_profile_flag(self):
        user = make_user()
        pending = make_tx(user, TransactionStatus.PENDING)
        authorized = make_tx(user, TransactionStatus.AUTHORIZED)

        updated = process_user_transactions(user.pk)

        self.assertEqual(updated, 2)
        pending.refresh_from_db()
        authorized.refresh_from_db()
        self.assertEqual(pending.status, TransactionStatus.PROCESSED)
        self.assertEqual(authorized.status, TransactionStatus.PROCESSED)
        self.assertTrue(Profile.objects.get(user=user).has_processed_transactions)

    def test_updated_at_moves_despite_update_fields(self):
        """auto_now не срабатывает, если поля нет в update_fields."""
        user = make_user()
        tx = make_tx(user, TransactionStatus.PENDING)
        before = Transaction.objects.get(pk=tx.pk).updated_at

        process_user_transactions(user.pk)

        after = Transaction.objects.get(pk=tx.pk).updated_at
        self.assertGreater(after, before)

    def test_updates_every_transaction_by_default(self):
        """Условие требует обновить статус ВСЕХ транзакций пользователя.

        Сужение набора собственным перечнем статусов означало бы, что на данных
        с любым другим значением status не обработается ничего.
        """
        user = make_user()
        rows = [
            make_tx(user, TransactionStatus.PENDING),
            make_tx(user, TransactionStatus.AUTHORIZED),
            make_tx(user, TransactionStatus.FAILED),
            make_tx(user, TransactionStatus.REFUNDED),
        ]

        updated = process_user_transactions(user.pk)

        self.assertEqual(updated, 4)
        for tx in rows:
            tx.refresh_from_db()
            self.assertEqual(tx.status, TransactionStatus.PROCESSED)

    def test_updates_a_status_the_project_does_not_know_about(self):
        """status в схеме это varchar(20) без перечня значений."""
        user = make_user()
        tx = Transaction.objects.create(user=user, status="hold", amount="1.00")

        self.assertEqual(process_user_transactions(user.pk), 1)
        tx.refresh_from_db()
        self.assertEqual(tx.status, TransactionStatus.PROCESSED)

    def test_unknown_status_is_not_rejected_by_validation(self):
        """choices на поле сделали бы `hold` невалидным для форм и сериализаторов.

        Это противоречило бы решению обрабатывать все транзакции: схема задаёт
        status как varchar(20) и перечня значений не фиксирует.
        """
        user = make_user()
        tx = Transaction(user=user, status="hold", amount="1.00")
        tx.full_clean()  # не должно бросать ValidationError
        tx.save()
        self.assertEqual(Transaction.objects.get(pk=tx.pk).status, "hold")

    def test_accepts_a_user_instance_as_well_as_an_id(self):
        """Условие описывает функцию как берущую пользователя User."""
        user = make_user()
        make_tx(user, TransactionStatus.PENDING)

        self.assertEqual(process_user_transactions(user), 1)
        self.assertTrue(Profile.objects.get(user=user).has_processed_transactions)

    def test_unsaved_user_fails_loudly(self):
        from django.contrib.auth.models import User as UserModel

        with self.assertRaises(ValueError):
            process_user_transactions(UserModel(username="ghost"))

    def test_caller_can_narrow_the_set_explicitly(self):
        """refunded -> processed это повторный учёт возвращённых денег.

        Запрет доступен вызывающему параметром, но решение принимает он.
        """
        user = make_user()
        pending = make_tx(user, TransactionStatus.PENDING)
        refunded = make_tx(user, TransactionStatus.REFUNDED)

        updated = process_user_transactions(user.pk, statuses=PROCESSABLE_STATUSES)

        self.assertEqual(updated, 1)
        pending.refresh_from_db()
        refunded.refresh_from_db()
        self.assertEqual(pending.status, TransactionStatus.PROCESSED)
        self.assertEqual(refunded.status, TransactionStatus.REFUNDED)

    def test_other_users_transactions_are_untouched(self):
        user = make_user("alice")
        other = make_user("bob")
        mine = make_tx(user, TransactionStatus.PENDING)
        theirs = make_tx(other, TransactionStatus.PENDING)

        process_user_transactions(user.pk)

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.status, TransactionStatus.PROCESSED)
        self.assertEqual(theirs.status, TransactionStatus.PENDING)
        self.assertFalse(Profile.objects.get(user=other).has_processed_transactions)

    def test_repeated_run_reaches_the_same_state(self):
        """Повторный вызов не портит результат: конечное состояние то же."""
        user = make_user()
        tx = make_tx(user, TransactionStatus.PENDING)

        self.assertEqual(process_user_transactions(user.pk), 1)
        self.assertEqual(process_user_transactions(user.pk), 1)

        tx.refresh_from_db()
        self.assertEqual(tx.status, TransactionStatus.PROCESSED)
        self.assertTrue(Profile.objects.get(user=user).has_processed_transactions)

    def test_zero_rows_is_logged_rather_than_silent(self):
        """Флаг выставляется по условию, но «ноль обработано» должно быть видно."""
        user = make_user()

        with self.assertLogs("billing.services", level="WARNING") as logs:
            self.assertEqual(process_user_transactions(user.pk), 0)

        self.assertIn("обновлено 0 транзакций", "".join(logs.output))
        self.assertTrue(Profile.objects.get(user=user).has_processed_transactions)

    def test_missing_profile_raises_instead_of_silently_creating(self):
        user = User.objects.create(username="ghost", email="ghost@example.com")
        make_tx(user, TransactionStatus.PENDING)

        with self.assertRaises(Profile.DoesNotExist):
            process_user_transactions(user.pk)

    def test_notification_fires_only_after_commit(self):
        user = make_user()
        make_tx(user, TransactionStatus.PENDING)

        with mock.patch("billing.services._notify_processed") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                process_user_transactions(user.pk)
                notify.assert_not_called()
            notify.assert_called_once_with(user.pk, 1)


class RollbackTests(TransactionTestCase):
    """Откат проверяется только с реальным коммитом, поэтому TransactionTestCase.

    TestCase заворачивает каждый тест в свою транзакцию, и внутренний atomic
    становится savepoint — то есть проверялся бы не тот механизм.
    """

    def test_everything_rolls_back_when_profile_save_fails(self):
        user = make_user()
        tx = make_tx(user, TransactionStatus.PENDING)

        with mock.patch.object(
            Profile, "save", side_effect=RuntimeError("boom")
        ), self.assertRaises(RuntimeError):
            process_user_transactions(user.pk)

        tx.refresh_from_db()
        self.assertEqual(tx.status, TransactionStatus.PENDING)
        self.assertFalse(Profile.objects.get(user=user).has_processed_transactions)

    def test_rollback_leaves_no_partial_batch(self):
        """Падение на середине набора не должно оставить половину обработанной."""
        user = make_user()
        transactions = [make_tx(user, TransactionStatus.PENDING) for _ in range(5)]

        original_save = Transaction.save
        calls = {"n": 0}

        def failing_save(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom on third row")
            return original_save(self, *args, **kwargs)

        with mock.patch.object(
            Transaction, "save", failing_save
        ), self.assertRaises(RuntimeError):
            process_user_transactions(user.pk)

        for tx in transactions:
            tx.refresh_from_db()
            self.assertEqual(tx.status, TransactionStatus.PENDING)


@skipUnlessDBFeature("has_select_for_update")
class RowLockingTests(TransactionTestCase):
    """Проверяет, что блокировка действительно берётся.

    На SQLite пропускается: has_select_for_update = False. Это и есть причина,
    по которой боевая тестовая сьюта обязана ходить в Postgres, иначе
    единственный механизм защиты от гонки не покрыт вообще ничем.
    """

    def test_concurrent_run_cannot_double_process(self):
        """Блокировка сериализует конкурентные вызовы.

        Проверяется с явным сужением набора статусов: при обработке всех
        транзакций подряд повторный проход просто перезаписывает те же строки
        тем же значением, и по количеству обновлённых строк отличить
        сериализацию от её отсутствия невозможно.
        """
        import threading

        user = make_user()
        make_tx(user, TransactionStatus.PENDING)
        results = []
        errors = []

        def worker():
            try:
                results.append(
                    process_user_transactions(
                        user.pk, statuses=PROCESSABLE_STATUSES
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        # Ровно один поток видит транзакцию необработанной, остальные ждут
        # блокировку и после её снятия не находят ничего.
        self.assertEqual(sorted(results), [0, 0, 0, 1])


@skipUnlessDBFeature("has_select_for_update")
class LockScopeTests(TestCase):
    """Что именно блокируется, проверяется по SQL, а не по комментарию.

    Первая версия этой функции делала
        Profile.objects.select_for_update().select_related("user")
    и была снабжена комментарием «блокируется только профиль». Комментарий был
    неверен: в PostgreSQL `SELECT ... FOR UPDATE` без `OF` блокирует строки ВСЕХ
    таблиц джойна, так что auth_user блокировался на всё время цикла обновлений.
    Ни один поведенческий тест этого не видел.
    """

    def test_profile_lock_does_not_join_or_lock_auth_user(self):
        from django.test.utils import CaptureQueriesContext

        user = make_user()
        make_tx(user, TransactionStatus.PENDING)

        with CaptureQueriesContext(connection) as captured:
            process_user_transactions(user.pk)

        locking = [q["sql"] for q in captured.captured_queries if "FOR UPDATE" in q["sql"]]
        self.assertTrue(locking, "ожидались блокирующие запросы")

        profile_locks = [sql for sql in locking if '"profile"' in sql]
        self.assertTrue(profile_locks, "профиль должен блокироваться")
        for sql in profile_locks:
            self.assertNotIn(
                "auth_user",
                sql,
                "join с auth_user означает FOR UPDATE и по строке пользователя",
            )

    def test_transactions_are_locked_in_primary_key_order(self):
        """Без общего порядка два воркера получают deadlock."""
        from django.test.utils import CaptureQueriesContext

        user = make_user()
        for _ in range(3):
            make_tx(user, TransactionStatus.PENDING)

        with CaptureQueriesContext(connection) as captured:
            process_user_transactions(user.pk)

        tx_locks = [
            q["sql"]
            for q in captured.captured_queries
            if "FOR UPDATE" in q["sql"] and '"transaction"' in q["sql"]
        ]
        self.assertTrue(tx_locks)
        self.assertIn("ORDER BY", tx_locks[0])
