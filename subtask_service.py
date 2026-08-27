import re


SUBTASK_REFERENCE_PATTERN = re.compile(
    r"#\s*(\d+)\s*[-.．]\s*(\d+)"
)


def parse_subtask_command(
    user_text: str,
):
    """
    解析不需要调用模型的高确定性子任务命令。

    支持：
    - 帮我拆分 #7
    - 查看 #7 子任务
    - #7-2 完成了
    - 恢复 #7-2
    """

    text = str(user_text or "").strip()
    compact = text.replace(" ", "")

    subtask_match = SUBTASK_REFERENCE_PATTERN.search(
        text
    )

    if subtask_match:
        task_id = int(
            subtask_match.group(1)
        )
        position = int(
            subtask_match.group(2)
        )

        reopen_keywords = {
            "恢复",
            "撤销完成",
            "改为未完成",
            "重新打开",
            "还没完成",
        }
        complete_keywords = {
            "完成",
            "做完",
            "勾选",
            "标记完成",
            "搞定",
        }

        if any(
            keyword in compact
            for keyword in reopen_keywords
        ):
            return {
                "action": "reopen",
                "task_id": task_id,
                "position": position,
            }

        if any(
            keyword in compact
            for keyword in complete_keywords
        ):
            return {
                "action": "complete",
                "task_id": task_id,
                "position": position,
            }

    task_id = None
    task_match = re.search(
        r"(?:#|任务)\s*(\d+)",
        text,
    )

    if task_match:
        task_id = int(
            task_match.group(1)
        )

    subtask_words = {
        "子任务",
        "执行步骤",
        "步骤列表",
    }
    view_words = {
        "查看",
        "显示",
        "进度",
        "列出",
    }

    if (
        any(word in compact for word in subtask_words)
        and any(word in compact for word in view_words)
    ):
        return {
            "action": "view",
            "task_id": task_id,
        }

    split_words = {
        "拆分",
        "拆小",
        "分解",
        "拆成步骤",
        "生成执行步骤",
        "重新拆分",
    }

    if any(
        word in compact
        for word in split_words
    ):
        return {
            "action": "split",
            "task_id": task_id,
            "original_text": text,
        }

    return None


def calculate_subtask_progress(
    subtasks: list,
) -> dict:
    """按已完成子任务数量计算整体进度。"""

    total = len(subtasks)
    completed = sum(
        1
        for item in subtasks
        if item.get("status") == "completed"
    )

    return {
        "total": total,
        "completed": completed,
        "percent": (
            round(completed * 100 / total)
            if total
            else 0
        ),
    }
