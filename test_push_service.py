import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import database
import push_service


class PushServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_init_twice_and_default_push_preferences(self):
        database.init_db()
        database.update_push_preference("u1", "morning", push_time="08:30")
        pref = database.get_user_preference("u1")
        self.assertEqual(pref["morning_push_enabled"], 0)
        self.assertEqual(pref["morning_push_time"], "08:30")
        self.assertEqual(pref["evening_push_enabled"], 0)
        self.assertEqual(pref["evening_push_time"], "22:00")

    def test_parse_push_commands(self):
        self.assertEqual(
            push_service.parse_push_command("设置晨间计划 08:30"),
            {"action": "set", "push_type": "morning", "time": "08:30"},
        )
        self.assertEqual(
            push_service.parse_push_command("关闭晚间总结"),
            {"action": "disable", "push_type": "evening"},
        )
        self.assertEqual(
            push_service.parse_push_command("设置晚间总结 21:30"),
            {"action": "set", "push_type": "evening", "time": "21:30"},
        )
        self.assertEqual(
            push_service.parse_push_command("关闭晨间计划"),
            {"action": "disable", "push_type": "morning"},
        )
        self.assertEqual(
            push_service.parse_push_command("设置晨间推送 08:30"),
            {"action": "set", "push_type": "morning", "time": "08:30"},
        )
        self.assertEqual(
            push_service.parse_push_command("关闭晨间推送"),
            {"action": "disable", "push_type": "morning"},
        )
        self.assertEqual(
            push_service.parse_push_command("查看推送设置"),
            {"action": "show"},
        )
        self.assertIsNone(push_service.parse_push_command("设置晨间计划 8:30"))
        self.assertIsNone(push_service.parse_push_command("设置晨间计划 24:00"))

    def test_setting_closing_query_and_enabled_users(self):
        database.save_user_preference("u1", "balanced")
        response = push_service.handle_push_command(
            "u1", "设置晨间计划 07:45"
        )
        self.assertIn("07:45", response)
        self.assertEqual(
            database.list_push_enabled_users("morning")[0]["user_open_id"],
            "u1",
        )
        response = push_service.handle_push_command("u1", "关闭晨间计划")
        self.assertIn("已关闭", response)
        self.assertEqual(database.list_push_enabled_users("morning"), [])
        settings = push_service.handle_push_command("u1", "查看推送设置")
        self.assertIn("晨间计划：已关闭", settings)
        self.assertIn("晚间总结：已关闭", settings)

    def test_delivery_log_is_idempotent(self):
        self.assertTrue(
            database.record_push_delivery("u1", "morning", "2026-08-24")
        )
        self.assertFalse(
            database.record_push_delivery("u1", "morning", "2026-08-24")
        )
        self.assertTrue(
            database.has_push_been_delivered(
                "u1", "morning", "2026-08-24"
            )
        )

    def test_due_window_boundaries(self):
        self.assertTrue(
            push_service.is_push_due(
                datetime(2026, 8, 24, 8, 0, 0), "08:00"
            )
        )
        self.assertTrue(
            push_service.is_push_due(
                datetime(2026, 8, 24, 8, 4, 59), "08:00"
            )
        )
        self.assertFalse(
            push_service.is_push_due(
                datetime(2026, 8, 24, 8, 5, 0), "08:00"
            )
        )
        self.assertFalse(
            push_service.is_push_due(
                datetime(2026, 8, 24, 7, 59, 59), "08:00"
            )
        )

    def test_evening_summary(self):
        summary = push_service.build_evening_summary(
            [
                {"title": "修改 PPT", "status": "completed"},
                {"title": "复习数据结构", "status": "pending"},
                {"title": "已取消会议", "status": "cancelled"},
            ]
        )
        self.assertIn("已完成：1 项", summary)
        self.assertIn("待继续：1 项", summary)
        self.assertIn("修改 PPT", summary)
        self.assertIn("复习数据结构", summary)
        self.assertNotIn("已取消会议", summary)

        empty_summary = push_service.build_evening_summary([])
        self.assertIn("没有需要统计的任务", empty_summary)


if __name__ == "__main__":
    unittest.main()
