import tempfile
import unittest
from pathlib import Path

import database
import llm_service
from preference_service import (
    format_category_order,
    parse_category_preference_command,
)
from scheduler import sort_tasks


class CategoryPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_complete_order_command_is_parsed(self):
        command = parse_category_preference_command(
            "设置事务优先级 家庭 > 健康 > 学习 > 工作与兼职 > 个人生活 > 其他"
        )
        self.assertEqual(command["action"], "set")
        self.assertEqual(
            command["order"],
            ["family", "health", "study", "work", "personal", "other"],
        )

    def test_duplicate_or_incomplete_order_is_rejected(self):
        command = parse_category_preference_command(
            "设置事务优先级 家庭 > 家庭 > 学习"
        )
        self.assertEqual(command["action"], "set")
        self.assertIn("error", command)

    def test_order_is_persisted_without_overwriting_strategy(self):
        database.save_user_preference("u1", "balanced")
        order = ["family", "health", "study", "work", "personal", "other"]
        database.update_category_order("u1", order)
        preference = database.get_user_preference("u1")

        self.assertEqual(preference["priority_strategy"], "balanced")
        self.assertEqual(preference["category_order"], order)
        self.assertTrue(preference["onboarding_completed"])
        self.assertEqual(format_category_order(order).split(" ＞ ")[0], "家庭")

    def test_onboarding_is_complete_only_after_category_order(self):
        database.save_onboarding_strategy("new-user", "deadline")
        self.assertFalse(database.has_completed_onboarding("new-user"))

        database.update_category_order(
            "new-user",
            ["health", "family", "study", "work", "personal", "other"],
            complete_onboarding=True,
        )
        self.assertTrue(database.has_completed_onboarding("new-user"))

    def test_category_breaks_tie_using_user_order(self):
        tasks = [
            {
                "id": 1,
                "title": "工作任务",
                "deadline": "2026-08-30 20:00",
                "estimated_minutes": 60,
                "priority": "medium",
                "category": "work",
            },
            {
                "id": 2,
                "title": "家庭任务",
                "deadline": "2026-08-30 20:00",
                "estimated_minutes": 60,
                "priority": "medium",
                "category": "family",
            },
        ]
        order = ["family", "health", "study", "work", "personal", "other"]

        sorted_tasks = sort_tasks(tasks, "balanced", order)

        self.assertEqual(sorted_tasks[0]["id"], 2)
        self.assertGreater(
            sorted_tasks[0]["category_preference_score"],
            sorted_tasks[1]["category_preference_score"],
        )

    def test_near_deadline_still_beats_higher_category(self):
        tasks = [
            {
                "id": 1,
                "title": "马上截止的工作任务",
                "deadline": "2000-01-01 10:00",
                "estimated_minutes": 60,
                "priority": "medium",
                "category": "work",
            },
            {
                "id": 2,
                "title": "很久以后的家庭任务",
                "deadline": "2099-01-01 10:00",
                "estimated_minutes": 60,
                "priority": "medium",
                "category": "family",
            },
        ]
        order = ["family", "health", "study", "work", "personal", "other"]

        self.assertEqual(sort_tasks(tasks, "balanced", order)[0]["id"], 1)

    def test_task_category_is_validated_persisted_and_updatable(self):
        result = llm_service.validate_task_result(
            {
                "intent": "create_task",
                "title": "复习考试",
                "deadline": "2026-08-30 20:00",
                "estimated_minutes": 60,
                "priority": "high",
                "category": "study",
                "missing_fields": [],
            }
        )
        task_id = database.create_task("u1", result)
        self.assertEqual(database.get_task_by_id("u1", task_id)["category"], "study")

        database.update_task(task_id, "u1", {"category": "family"})
        self.assertEqual(database.get_task_by_id("u1", task_id)["category"], "family")

    def test_invalid_model_category_falls_back_to_other(self):
        result = llm_service.validate_task_result(
            {
                "intent": "create_task",
                "category": "invented",
                "priority": "medium",
                "missing_fields": [],
            }
        )
        self.assertEqual(result["category"], "other")


if __name__ == "__main__":
    unittest.main()
