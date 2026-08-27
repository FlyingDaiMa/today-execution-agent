import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import llm_service
import main
from subtask_service import parse_subtask_command


class SubtaskFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(
            self.temp_dir.name
        ) / "test.db"
        database.init_db()

        main.confirmation_task_breakdowns.clear()
        main.confirmation_progress_completions.clear()
        main.pending_task_selections.clear()

        self.task_id = database.create_task(
            "u1",
            {
                "title": "修改答辩PPT",
                "deadline": "2026-08-26 18:00",
                "estimated_minutes": 120,
                "priority": "high",
            },
        )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        main.confirmation_task_breakdowns.clear()
        main.confirmation_progress_completions.clear()
        main.pending_task_selections.clear()
        self.temp_dir.cleanup()

    def test_parse_subtask_commands(self):
        self.assertEqual(
            parse_subtask_command("帮我拆分 #7"),
            {
                "action": "split",
                "task_id": 7,
                "original_text": "帮我拆分 #7",
            },
        )
        self.assertEqual(
            parse_subtask_command("查看 #7 子任务"),
            {
                "action": "view",
                "task_id": 7,
            },
        )
        self.assertEqual(
            parse_subtask_command("#7-2完成了"),
            {
                "action": "complete",
                "task_id": 7,
                "position": 2,
            },
        )
        self.assertEqual(
            parse_subtask_command("恢复 #7-2"),
            {
                "action": "reopen",
                "task_id": 7,
                "position": 2,
            },
        )

    def test_database_replace_progress_and_user_isolation(self):
        first = database.replace_task_subtasks(
            "u1",
            self.task_id,
            ["梳理结构", "修改内容", "检查排版"],
        )
        self.assertEqual(len(first), 3)
        self.assertEqual(
            database.get_task_subtask_progress(
                "u1",
                self.task_id,
            )["percent"],
            0,
        )

        self.assertTrue(
            database.update_task_subtask_status(
                "u1",
                self.task_id,
                1,
                "completed",
            )
        )
        self.assertEqual(
            database.get_task_subtask_progress(
                "u1",
                self.task_id,
            ),
            {
                "total": 3,
                "completed": 1,
                "percent": 33,
            },
        )
        self.assertFalse(
            database.update_task_subtask_status(
                "u2",
                self.task_id,
                1,
                "completed",
            )
        )

        second = database.replace_task_subtasks(
            "u1",
            self.task_id,
            ["重新梳理", "最终提交"],
        )
        self.assertEqual(
            [item["title"] for item in second],
            ["重新梳理", "最终提交"],
        )
        self.assertEqual(
            database.get_task_subtask_progress(
                "u1",
                self.task_id,
            )["percent"],
            0,
        )

    def test_breakdown_output_is_whitelisted_and_cleaned(self):
        result = llm_service.validate_task_breakdown_result(
            {
                "subtasks": [
                    {"title": "1. 梳理结构", "ignored": "x"},
                    "- 修改内容",
                    "3、检查排版",
                ],
                "unexpected": "discarded",
            }
        )

        self.assertEqual(
            result,
            {
                "subtasks": [
                    "梳理结构",
                    "修改内容",
                    "检查排版",
                ],
                "fallback_used": False,
            },
        )

    def test_split_can_match_task_name_fragment(self):
        suggestion = {
            "subtasks": ["步骤一", "步骤二"],
            "fallback_used": False,
        }

        with patch.object(
            main,
            "generate_task_breakdown",
            return_value=suggestion,
        ):
            reply = main.process_task_breakdown_request(
                "u1",
                {
                    "action": "split",
                    "task_id": None,
                    "original_text": "帮我把PPT拆小",
                },
            )

        self.assertIn("修改答辩PPT", reply)

    def test_suggestion_requires_confirmation_before_write(self):
        suggestion = {
            "subtasks": [
                "梳理PPT结构",
                "修改核心内容",
                "统一视觉与排版",
            ],
            "fallback_used": False,
        }

        with patch.object(
            main,
            "generate_task_breakdown",
            return_value=suggestion,
        ):
            reply = main.process_task_breakdown_request(
                "u1",
                {
                    "action": "split",
                    "task_id": self.task_id,
                    "original_text": f"拆分 #{self.task_id}",
                },
            )

        self.assertIn("AI 任务拆分建议", reply)
        self.assertEqual(
            database.get_task_subtasks(
                "u1",
                self.task_id,
            ),
            [],
        )

        saved_reply = (
            main.process_task_breakdown_confirmation(
                "u1",
                "确认拆分",
            )
        )
        self.assertIn("子任务已保存", saved_reply)
        self.assertIn("整体进度：0% (0/3)", saved_reply)
        self.assertEqual(
            len(
                database.get_task_subtasks(
                    "u1",
                    self.task_id,
                )
            ),
            3,
        )

    def test_subtask_completion_progress_and_parent_confirmation(self):
        database.replace_task_subtasks(
            "u1",
            self.task_id,
            ["步骤一", "步骤二"],
        )

        first = main.process_subtask_status_command(
            "u1",
            {
                "action": "complete",
                "task_id": self.task_id,
                "position": 1,
            },
        )
        self.assertIn("50% (1/2)", first)
        self.assertEqual(
            database.get_task_by_id(
                "u1",
                self.task_id,
            )["status"],
            "pending",
        )

        final = main.process_subtask_status_command(
            "u1",
            {
                "action": "complete",
                "task_id": self.task_id,
                "position": 2,
            },
        )
        self.assertIn("所有子任务都已完成", final)
        self.assertIn(
            "u1",
            main.confirmation_progress_completions,
        )
        self.assertEqual(
            database.get_task_by_id(
                "u1",
                self.task_id,
            )["status"],
            "pending",
        )

        confirmation = (
            main.process_progress_completion_confirmation(
                "u1",
                "确认",
            )
        )
        self.assertIn("任务已完成", confirmation)
        self.assertEqual(
            database.get_task_by_id(
                "u1",
                self.task_id,
            )["status"],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
