import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import database
import push_runner


class PushRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()
        database.save_user_preference("u1", "balanced")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _create_task(self, title, status="pending", updated_at=None):
        task_id = database.create_task(
            "u1",
            {
                "title": title,
                "deadline": "2026-08-24 18:00",
                "estimated_minutes": 60,
                "priority": "high",
            },
        )

        if status != "pending" or updated_at is not None:
            connection = database.get_connection()
            try:
                connection.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (
                        status,
                        updated_at or "2026-08-24 12:00:00",
                        task_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

        return task_id

    def test_morning_success_records_and_duplicate_is_skipped(self):
        self._create_task("完成主动推送")
        database.update_push_preference(
            "u1",
            "morning",
            enabled=True,
            push_time="08:00",
        )
        send_func = Mock(return_value=True)
        now = datetime(2026, 8, 24, 8, 2)

        first = push_runner.run_due_pushes(send_func, now=now)
        second = push_runner.run_due_pushes(send_func, now=now)

        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(send_func.call_count, 1)
        self.assertIn("晨间计划", send_func.call_args.args[1])
        self.assertIn("完成主动推送", send_func.call_args.args[1])
        self.assertTrue(
            database.has_push_been_delivered(
                "u1",
                "morning",
                "2026-08-24",
            )
        )

    def test_failed_send_is_not_recorded_and_can_retry(self):
        database.update_push_preference(
            "u1",
            "morning",
            enabled=True,
            push_time="08:00",
        )
        send_func = Mock(side_effect=[False, True])
        now = datetime(2026, 8, 24, 8, 1)

        failed = push_runner.run_due_pushes(send_func, now=now)
        retried = push_runner.run_due_pushes(send_func, now=now)

        self.assertEqual(failed["failed"], 1)
        self.assertEqual(retried["sent"], 1)
        self.assertEqual(send_func.call_count, 2)

    def test_one_user_exception_does_not_block_another_user(self):
        database.save_user_preference("u2", "balanced")

        for user_open_id in ("u1", "u2"):
            database.update_push_preference(
                user_open_id,
                "morning",
                enabled=True,
                push_time="08:00",
            )

        send_func = Mock(
            side_effect=[RuntimeError("temporary failure"), True]
        )
        result = push_runner.run_due_pushes(
            send_func,
            now=datetime(2026, 8, 24, 8, 1),
        )

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertFalse(
            database.has_push_been_delivered(
                "u1", "morning", "2026-08-24"
            )
        )
        self.assertTrue(
            database.has_push_been_delivered(
                "u2", "morning", "2026-08-24"
            )
        )

    def test_evening_summary_counts_only_completed_today(self):
        self._create_task(
            "今天完成",
            status="completed",
            updated_at="2026-08-24 17:00:00",
        )
        self._create_task(
            "以前完成",
            status="completed",
            updated_at="2026-08-23 17:00:00",
        )
        self._create_task("还要继续")
        database.update_push_preference(
            "u1",
            "evening",
            enabled=True,
            push_time="22:00",
        )
        send_func = Mock(return_value=True)

        result = push_runner.run_due_pushes(
            send_func,
            now=datetime(2026, 8, 24, 22, 3),
        )

        text = send_func.call_args.args[1]
        self.assertEqual(result["sent"], 1)
        self.assertIn("已完成：1 项", text)
        self.assertIn("待继续：1 项", text)
        self.assertIn("今天完成", text)
        self.assertNotIn("以前完成", text)

    def test_old_database_schema_is_migrated_with_pushes_disabled(self):
        old_db_path = Path(self.temp_dir.name) / "old.db"
        connection = sqlite3.connect(old_db_path)
        try:
            connection.execute(
                """
                CREATE TABLE user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_open_id TEXT NOT NULL UNIQUE,
                    priority_strategy TEXT,
                    onboarding_completed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO user_preferences (
                    user_open_id,
                    priority_strategy,
                    onboarding_completed,
                    created_at,
                    updated_at
                ) VALUES ('old-user', 'balanced', 1, 'now', 'now')
                """
            )
            connection.commit()
        finally:
            connection.close()

        database.DB_PATH = old_db_path
        database.init_db()
        preference = database.get_user_preference("old-user")

        self.assertEqual(preference["morning_push_enabled"], 0)
        self.assertEqual(preference["evening_push_enabled"], 0)
        self.assertEqual(preference["morning_push_time"], "08:00")
        self.assertEqual(preference["evening_push_time"], "22:00")


if __name__ == "__main__":
    unittest.main()
