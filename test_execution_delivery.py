import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import database
import main
import push_runner
from news_service import build_news_section, fetch_news, handle_news_command
from push_service import build_evening_summary
from reminder_service import run_due_reminders
from tone_service import get_encouragement, handle_tone_command


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class ExecutionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()
        database.save_user_preference("u1", "balanced")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _create_task(self, title="完成报告", minutes=60):
        return database.create_task(
            "u1",
            {
                "title": title,
                "deadline": "2026-08-25 20:00",
                "estimated_minutes": minutes,
                "priority": "high",
                "category": "study",
            },
        )

    def _save_plan(self, task_id, end="2026-08-25 10:00", minutes=60):
        database.save_daily_plan_snapshot(
            "u1",
            "2026-08-25",
            [
                {
                    "task_id": task_id,
                    "title": "完成报告",
                    "start_time": "2026-08-25 09:00",
                    "end_time": end,
                    "planned_minutes": minutes,
                    "estimated_minutes": minutes,
                }
            ],
        )

    def test_snapshot_drives_evening_completion_rate(self):
        task_id = self._create_task()
        self._save_plan(task_id)
        database.update_task_remaining_minutes("u1", task_id, 30)

        summary = build_evening_summary(
            database.get_user_tasks("u1"),
            summary_date="2026-08-25",
            plan_snapshot=database.get_daily_plan_snapshot("u1", "2026-08-25"),
        )

        self.assertIn("今日计划任务：1 项", summary)
        self.assertIn("今日计划完成率：50%", summary)
        self.assertIn("今日计划未完成：完成报告", summary)

    def test_replan_keeps_original_evening_completion_baseline(self):
        task_id = self._create_task(minutes=100)
        self._save_plan(task_id, minutes=100)
        database.update_task_remaining_minutes("u1", task_id, 60)
        database.save_daily_plan_snapshot(
            "u1",
            "2026-08-25",
            [
                {
                    "task_id": task_id,
                    "title": "完成报告",
                    "end_time": "2026-08-25 11:00",
                    "planned_minutes": 60,
                    "estimated_minutes": 60,
                    "original_estimated_minutes": 100,
                }
            ],
        )

        snapshot = database.get_daily_plan_snapshot("u1", "2026-08-25")
        summary = build_evening_summary(
            database.get_user_tasks("u1"),
            summary_date="2026-08-25",
            plan_snapshot=snapshot,
        )

        self.assertEqual(snapshot["plan"][0]["planned_minutes"], 60)
        self.assertEqual(snapshot["baseline_plan"][0]["planned_minutes"], 100)
        self.assertIn("今日计划完成率：40%", summary)

    def test_percent_progress_updates_remaining_work(self):
        task_id = self._create_task(minutes=100)
        reply = main.process_task_progress("u1", f"#{task_id}进度60%")
        task = database.get_task_by_id("u1", task_id)

        self.assertEqual(task["remaining_minutes"], 40)
        self.assertIn("整体进度：60%", reply)
        self.assertIn("进度已记录", reply)

    def test_completed_task_counts_as_full_plan_progress(self):
        task_id = self._create_task()
        self._save_plan(task_id)
        database.update_task_status("u1", task_id, "completed")
        database.save_daily_plan_snapshot("u1", "2026-08-25", [])
        summary = build_evening_summary(
            database.get_user_tasks("u1"),
            summary_date=datetime.now().strftime("%Y-%m-%d"),
            plan_snapshot=database.get_daily_plan_snapshot("u1", "2026-08-25"),
        )
        self.assertIn("今日计划完成率：100%", summary)

    def test_task_end_reminder_is_idempotent(self):
        task_id = self._create_task()
        self._save_plan(task_id)
        send = Mock(return_value=True)
        now = datetime(2026, 8, 25, 10, 2)

        first = run_due_reminders(send, now=now)
        second = run_due_reminders(send, now=now)

        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(send.call_count, 1)
        self.assertIn("计划时段已结束", send.call_args.args[1])

    def test_course_reminder_is_idempotent(self):
        database.create_blocked_time(
            "u1",
            "课程：产品设计",
            "2026-08-25 10:00",
            "2026-08-25 11:00",
            source="course:test",
        )
        send = Mock(return_value=True)
        now = datetime(2026, 8, 25, 9, 52)

        self.assertEqual(run_due_reminders(send, now=now)["sent"], 1)
        self.assertEqual(run_due_reminders(send, now=now)["sent"], 0)
        self.assertIn("课程即将开始", send.call_args.args[1])

    def test_reminders_respect_safe_hours(self):
        send = Mock(return_value=True)
        result = run_due_reminders(send, now=datetime(2026, 8, 25, 23, 0))
        self.assertEqual(result["sent"], 0)
        send.assert_not_called()

    def test_news_commands_persist_keywords_and_can_disable(self):
        reply = handle_news_command("u1", "设置兴趣关键词 AI产品，飞书")
        preference = database.get_user_preference("u1")
        self.assertIn("已保存", reply)
        self.assertEqual(preference["interest_keywords"], ["AI产品", "飞书"])
        self.assertEqual(preference["news_enabled"], 1)

        handle_news_command("u1", "关闭资讯彩蛋")
        self.assertEqual(database.get_user_preference("u1")["news_enabled"], 0)

    def test_assistant_tone_is_persisted_and_changes_encouragement(self):
        reply = handle_tone_command("u1", "设置助手语气 活泼夸夸")
        self.assertIn("活泼夸夸", reply)
        self.assertEqual(
            database.get_user_preference("u1")["assistant_tone"],
            "playful",
        )
        self.assertIn("继续冲", get_encouragement("playful", has_progress=True))

    def test_rss_news_requires_valid_source_link(self):
        payload = b"""<?xml version='1.0' encoding='utf-8'?>
        <rss><channel>
          <item><title>Valid story</title><link>https://example.com/a</link>
          <description>Short summary</description><source>Example</source></item>
          <item><title>Invalid story</title><link>javascript:bad</link></item>
        </channel></rss>"""

        items = fetch_news(
            "AI",
            opener=lambda *_args, **_kwargs: _FakeResponse(payload),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "Example")

    def test_news_failure_does_not_block_morning_plan(self):
        database.update_news_preference("u1", keywords=["AI"], enabled=True)
        preference = database.get_user_preference("u1")
        self.assertEqual(
            build_news_section(
                preference,
                fetcher=Mock(side_effect=RuntimeError("offline")),
            ),
            "",
        )

        self._create_task()
        with patch.object(push_runner, "build_news_section", return_value=""):
            text = push_runner.build_morning_plan(
                "u1",
                now=datetime(2026, 8, 25, 9, 0),
            )
        self.assertIn("晨间计划", text)
        self.assertIn("完成报告", text)


if __name__ == "__main__":
    unittest.main()
