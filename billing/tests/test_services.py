from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from billing.models import Profile, Transaction, TransactionStatus
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

    def test_illegal_source_statuses_are_not_touched(self):
        """refunded -> processed это повторный учёт возвращённых денег."""
        user = make_user()
        refunded = make_tx(user, TransactionStatus.REFUNDED)
        failed = make_tx(user, TransactionStatus.FAILED)

        updated = process_user_transactions(user.pk)

        self.assertEqual(updated, 0)
        refunded.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual(refunded.status, TransactionStatus.REFUNDED)
        self.assertEqual(failed.status, TransactionStatus.FAILED)

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

    def test_is_idempotent(self):
        user = make_user()
        make_tx(user, TransactionStatus.PENDING)

        self.assertEqual(process_user_transactions(user.pk), 1)
        self.assertEqual(process_user_transactions(user.pk), 0)
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
        import threading

        user = make_user()
        make_tx(user, TransactionStatus.PENDING)
        results = []
        errors = []

        def worker():
            try:
                results.append(process_user_transactions(user.pk))
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
