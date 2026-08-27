import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import database
import risk_service


class RiskServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()
        database.save_user_preference("u1", "balanced")
        self.now = datetime(2026, 8, 25, 10, 0)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _create_task(
        self,
        title="完成风险任务",
        deadline=None,
        estimated_minutes=120,
        remaining_minutes=None,
        priority="high",
        user_open_id="u1",
    ):
        if deadline is None:
            deadline = self.now + timedelta(hours=8)
        task_id = database.create_task(
            user_open_id,
            {
                "title": title,
                "deadline": deadline.strftime("%Y-%m-%d %H:%M"),
                "estimated_minutes": estimated_minutes,
                "priority": priority,
            },
        )
        if remaining_minutes is not None:
            connection = database.get_connection()
            try:
                connection.execute(
                    "UPDATE tasks SET remaining_minutes = ? WHERE id = ?",
                    (remaining_minutes, task_id),
                )
                connection.commit()
            finally:
                connection.close()
        return task_id

    def test_due_soon_and_behind_is_risk_with_explanation(self):
        task_id = self._create_task()
        task = database.get_task_by_id("u1", task_id)

        risk = risk_service.assess_task_risk(task, now=self.now)

        self.assertEqual(risk["risk_type"], "progress_behind")
        self.assertEqual(risk["actual_progress"], 0)
        self.assertEqual(risk["target_progress"], 60)
        self.assertIn("距离截止", risk["risk_reason"])
        self.assertIn("当前进度0%", risk_service.format_risk_alert(risk))

    def test_safe_task_and_completed_task_do_not_trigger(self):
        task_id = self._create_task(
            deadline=self.now + timedelta(days=3),
            estimated_minutes=100,
            remaining_minutes=20,
        )
        task = database.get_task_by_id("u1", task_id)
        self.assertIsNone(risk_service.assess_task_risk(task, now=self.now))

        database.update_task_status("u1", task_id, "completed")
        task = database.get_task_by_id("u1", task_id)
        self.assertIsNone(risk_service.assess_task_risk(task, now=self.now))

    def test_proactive_alert_is_daily_idempotent(self):
        self._create_task()
        send_func = Mock(return_value=True)

        first = risk_service.run_risk_checks(send_func, now=self.now)
        second = risk_service.run_risk_checks(send_func, now=self.now)

        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(send_func.call_count, 1)
        self.assertIn("触发原因", send_func.call_args.args[1])
        self.assertIsNotNone(database.get_active_risk_alert("u1"))

    def test_failed_alert_is_deleted_and_can_retry(self):
        self._create_task()
        send_func = Mock(side_effect=[False, True])

        failed = risk_service.run_risk_checks(send_func, now=self.now)
        retried = risk_service.run_risk_checks(send_func, now=self.now)

        self.assertEqual(failed["failed"], 1)
        self.assertEqual(retried["sent"], 1)
        self.assertEqual(send_func.call_count, 2)

    def test_status_creates_proposal_and_confirmation_is_persisted(self):
        task_id = self._create_task()
        send_func = Mock(return_value=True)
        risk_service.run_risk_checks(send_func, now=self.now)

        proposal_text = risk_service.handle_active_risk_input(
            "u1",
            "现在10:01，今天只能投入1小时，14:00到15:00有课",
            now=self.now + timedelta(minutes=1),
        )
        task_before_confirmation = database.get_task_by_id("u1", task_id)
        active = database.get_active_risk_alert("u1")
        proposal = json.loads(active["proposal_json"])

        self.assertIn("补救计划建议", proposal_text)
        self.assertEqual(active["status"], "awaiting_confirmation")
        self.assertEqual(task_before_confirmation["remaining_minutes"], 120)
        self.assertTrue(
            any(block.get("temporary") for block in proposal["protected_blocks"])
        )
        self.assertLessEqual(
            sum(item["planned_minutes"] for item in proposal["schedule"]),
            60,
        )

        confirmed_text = risk_service.handle_active_risk_input(
            "u1",
            "确认调整",
            now=self.now + timedelta(minutes=2),
        )

        self.assertIn("补救计划已确认并保存", confirmed_text)
        self.assertIsNone(database.get_active_risk_alert("u1"))
        self.assertIsNotNone(database.get_latest_confirmed_risk_plan("u1"))
        self.assertEqual(
            database.get_task_by_id("u1", task_id)["remaining_minutes"],
            120,
        )
        saved_text = risk_service.handle_risk_command("u1", "查看补救计划")
        self.assertIn("已确认的补救计划", saved_text)
        self.assertNotIn("确认调整", saved_text)

    def test_dismiss_keeps_original_task_unchanged(self):
        task_id = self._create_task()
        risk_service.run_risk_checks(Mock(return_value=True), now=self.now)

        text = risk_service.handle_active_risk_input(
            "u1",
            "暂不调整",
            now=self.now + timedelta(seconds=10),
        )

        self.assertIn("原任务", text)
        self.assertIsNone(database.get_active_risk_alert("u1"))
        task = database.get_task_by_id("u1", task_id)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["remaining_minutes"], 120)

    def test_one_minute_reminder_sends_once_and_keeps_original(self):
        task_id = self._create_task()
        send_func = Mock(return_value=True)
        risk_service.run_risk_checks(send_func, now=self.now)

        reminded = risk_service.run_risk_checks(
            send_func,
            now=self.now + timedelta(seconds=61),
        )
        repeated = risk_service.run_risk_checks(
            send_func,
            now=self.now + timedelta(seconds=122),
        )

        self.assertEqual(reminded["reminded"], 1)
        self.assertEqual(repeated["reminded"], 0)
        self.assertEqual(send_func.call_count, 2)
        self.assertIn("原任务和原计划保持不变", send_func.call_args.args[1])
        task = database.get_task_by_id("u1", task_id)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["remaining_minutes"], 120)


if __name__ == "__main__":
    unittest.main()
