import os
import tempfile
import unittest
from datetime import datetime, timedelta

import config
import storage
import subscribers
import web_server


class StripeSubscriberStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = os.path.join(self.tmp.name, "test.db")
        storage.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _future(self):
        return (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")

    def _past(self):
        return (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")

    def test_active_subscription_grants_paid_access(self):
        sub = subscribers.add_subscriber("vip@example.com", tier="free")
        updated = subscribers.sync_stripe_subscription(
            sub.id,
            customer_id="cus_123",
            subscription_id="sub_123",
            status="active",
            current_period_end=self._future(),
        )

        self.assertIsNotNone(updated)
        self.assertTrue(subscribers.is_paid(updated))
        self.assertEqual(updated.stripe_customer_id, "cus_123")
        self.assertEqual(updated.stripe_subscription_id, "sub_123")

    def test_past_due_subscription_keeps_access_until_period_end(self):
        sub = subscribers.add_subscriber("grace@example.com", tier="free")
        updated = subscribers.sync_stripe_subscription(
            sub.id,
            customer_id="cus_grace",
            subscription_id="sub_grace",
            status="past_due",
            current_period_end=self._future(),
        )

        self.assertTrue(subscribers.is_paid(updated))

    def test_cancel_at_period_end_keeps_access_and_records_notice_state(self):
        sub = subscribers.add_subscriber("canceling@example.com", tier="free")
        period_end = self._future()
        updated = subscribers.sync_stripe_subscription(
            sub.id,
            customer_id="cus_canceling",
            subscription_id="sub_canceling",
            status="active",
            current_period_end=period_end,
            cancel_at_period_end=True,
            cancel_at=period_end,
        )

        self.assertTrue(subscribers.is_paid(updated))
        self.assertTrue(updated.stripe_cancel_at_period_end)
        self.assertEqual(updated.stripe_cancel_at, period_end)

    def test_cancel_at_timestamp_without_period_end_flag_records_notice_state(self):
        sub = subscribers.add_subscriber("cancel-at@example.com", tier="free")
        period_end_dt = datetime.now() + timedelta(days=30)
        period_end_ts = int(period_end_dt.timestamp())

        web_server._sync_stripe_subscription_object(
            {
                "id": "sub_cancel_at",
                "customer": "cus_cancel_at",
                "status": "active",
                "metadata": {"subscriber_id": str(sub.id)},
                "cancel_at_period_end": False,
                "cancel_at": period_end_ts,
                "items": {"data": [{"current_period_end": period_end_ts}]},
            }
        )
        updated = subscribers.get_by_id(sub.id)

        self.assertTrue(subscribers.is_paid(updated))
        self.assertTrue(updated.stripe_cancel_at_period_end)
        self.assertEqual(updated.stripe_cancel_at, web_server._stripe_ts(period_end_ts))

    def test_ended_subscription_removes_paid_access(self):
        sub = subscribers.add_subscriber("ended@example.com", tier="paid")
        updated = subscribers.sync_stripe_subscription(
            sub.id,
            customer_id="cus_ended",
            subscription_id="sub_ended",
            status="canceled",
            current_period_end=self._past(),
        )

        self.assertFalse(subscribers.is_paid(updated))
        self.assertEqual(updated.tier, "free")

    def test_stripe_event_idempotency_marker(self):
        self.assertFalse(subscribers.stripe_event_processed("evt_1"))
        subscribers.record_stripe_event("evt_1", "invoice.paid")
        subscribers.record_stripe_event("evt_1", "invoice.paid")
        self.assertTrue(subscribers.stripe_event_processed("evt_1"))


if __name__ == "__main__":
    unittest.main()
