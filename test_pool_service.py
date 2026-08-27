import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import database
import llm_service
import push_runner
from planner import generate_execution_plan, parse_datetime
from pool_service import (
    format_pool_tasks,
    is_pool_command,
    prepare_tasks_for_planning,
)


class PoolServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()
        database.save_user_preference("u1", "balanced")
        database.save_user_preference("u2", "balanced")

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _create_task(
        self,
        title,
        deadline,
        minutes=60,
        priority="medium",
        user_open_id="u1",
    ):
        return database.create_task(
            user_open_id,
            {
                "title": title,
                "deadline": deadline,
                "estimated_minutes": minutes,
                "priority": priority,
            },
        )

    def test_explicit_no_deadline_does_not_remain_missing(self):
        result = llm_service.apply_task_scheduling_intent(
            {
                "intent": "create_task",
                "title": "整理照片",
                "deadline": None,
                "estimated_minutes": 30,
                "priority": "low",
                "missing_fields": ["deadline"],
            },
            "整理照片，没有截止日期，预计30分钟，重要程度低",
        )

        self.assertIsNone(result["deadline"])
        self.assertNotIn("deadline", result["missing_fields"])
        self.assertEqual(result["scheduling_bucket"], "pool")

    def test_implicit_missing_deadline_still_requires_follow_up(self):
        result = llm_service.apply_task_scheduling_intent(
            {
                "intent": "create_task",
                "title": "整理照片",
                "deadline": None,
                "estimated_minutes": 30,
                "priority": "low",
                "missing_fields": ["deadline"],
            },
            "整理照片，预计30分钟，重要程度低",
        )

        self.assertIn("deadline", result["missing_fields"])
        self.assertEqual(result["scheduling_bucket"], "needs_deadline")

    def test_update_can_move_existing_task_into_pool(self):
        result = llm_service.validate_task_update_result(
            llm_service.apply_task_update_scheduling_intent(
                {
                    "intent": "update_task",
                    "task_id": 7,
                    "task_title": None,
                    "updates": {},
                },
                "把 #7 改成没有截止日期",
            )
        )

        self.assertEqual(result["intent"], "update_task")
        self.assertIn("deadline", result["updates"])
        self.assertIsNone(result["updates"]["deadline"])

    def test_null_deadline_is_persisted_and_pool_view_is_user_isolated(self):
        task_id = self._create_task(
            "整理照片",
            None,
            minutes=30,
            priority="low",
        )
        self._create_task("另一个用户的任务", None, user_open_id="u2")

        stored = database.get_task_by_id("u1", task_id)
        text = format_pool_tasks(database.get_user_tasks("u1"))

        self.assertIsNone(stored["deadline"])
        self.assertIn("整理照片", text)
        self.assertIn("重要程度：低", text)
        self.assertNotIn("另一个用户的任务", text)
        self.assertTrue(is_pool_command("查看待安排池"))

    def test_deadline_tasks_always_sort_before_pool_tasks(self):
        ordered, deadline_tasks, pool_tasks = prepare_tasks_for_planning(
            [
                {
                    "id": 1,
                    "title": "池内高优先任务",
                    "deadline": None,
                    "estimated_minutes": 30,
                    "priority": "high",
                    "status": "pending",
                },
                {
                    "id": 2,
                    "title": "有截止日期的低优先任务",
                    "deadline": "2026-08-26 20:00",
                    "estimated_minutes": 30,
                    "priority": "low",
                    "status": "pending",
                },
            ]
        )

        self.assertEqual([task["id"] for task in ordered], [2, 1])
        self.assertEqual([task["id"] for task in deadline_tasks], [2])
        self.assertEqual([task["id"] for task in pool_tasks], [1])
        self.assertFalse(ordered[0]["is_optional"])
        self.assertTrue(ordered[1]["is_optional"])

    def test_pool_task_stays_out_when_remaining_capacity_is_insufficient(self):
        ordered, _, _ = prepare_tasks_for_planning(
            [
                {
                    "id": 1,
                    "title": "今天截止",
                    "deadline": "2026-08-25 22:00",
                    "estimated_minutes": 60,
                    "priority": "high",
                    "status": "pending",
                },
                {
                    "id": 2,
                    "title": "池任务",
                    "deadline": None,
                    "estimated_minutes": 60,
                    "priority": "low",
                    "status": "pending",
                },
            ]
        )

        plan = generate_execution_plan(
            ordered,
            now=datetime(2026, 8, 25, 20, 30),
            blocked_windows=[],
        )

        self.assertEqual({item["task_id"] for item in plan}, {1})

    def test_pool_task_enters_as_optional_when_capacity_is_sufficient(self):
        ordered, _, _ = prepare_tasks_for_planning(
            [
                {
                    "id": 1,
                    "title": "今天截止",
                    "deadline": "2026-08-25 22:00",
                    "estimated_minutes": 60,
                    "priority": "high",
                    "status": "pending",
                },
                {
                    "id": 2,
                    "title": "池任务",
                    "deadline": None,
                    "estimated_minutes": 30,
                    "priority": "low",
                    "status": "pending",
                },
            ]
        )

        plan = generate_execution_plan(
            ordered,
            now=datetime(2026, 8, 25, 19, 30),
            blocked_windows=[],
        )
        pool_items = [item for item in plan if item["task_id"] == 2]

        self.assertEqual(len(pool_items), 1)
        self.assertTrue(pool_items[0]["is_optional"])

    def test_pool_task_does_not_overlap_fixed_course(self):
        ordered, _, _ = prepare_tasks_for_planning(
            [
                {
                    "id": 1,
                    "title": "截止型任务",
                    "deadline": "2026-08-25 22:00",
                    "estimated_minutes": 60,
                    "priority": "high",
                    "status": "pending",
                },
                {
                    "id": 2,
                    "title": "池任务",
                    "deadline": None,
                    "estimated_minutes": 60,
                    "priority": "low",
                    "status": "pending",
                },
            ]
        )
        course_start = datetime(2026, 8, 25, 10, 0)
        course_end = datetime(2026, 8, 25, 11, 0)

        plan = generate_execution_plan(
            ordered,
            now=datetime(2026, 8, 25, 9, 0),
            blocked_windows=[
                {
                    "title": "课程",
                    "start": "2026-08-25 10:00",
                    "end": "2026-08-25 11:00",
                }
            ],
        )

        for item in plan:
            start = parse_datetime(item["start_time"])
            end = parse_datetime(item["end_time"])
            self.assertTrue(end <= course_start or start >= course_end)

    def test_morning_plan_marks_pool_task_as_optional(self):
        self._create_task(
            "今天截止",
            "2026-08-25 18:00",
            minutes=60,
            priority="high",
        )
        self._create_task("整理照片", None, minutes=30, priority="low")

        text = push_runner.build_morning_plan(
            "u1",
            now=datetime(2026, 8, 25, 9, 0),
        )

        self.assertIn("今天截止", text)
        self.assertIn("整理照片", text)
        self.assertIn("[待安排池可选]", text)


if __name__ == "__main__":
    unittest.main()
