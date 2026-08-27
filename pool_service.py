from scheduler import sort_tasks


POOL_COMMANDS = {
    "查看待安排池",
    "待安排池",
    "查看可选任务",
    "我的待安排任务",
}


def is_pool_task(task):
    return not bool(task.get("deadline"))


def prepare_tasks_for_planning(
    tasks,
    strategy="balanced",
    category_order=None,
):
    """Deadline tasks always precede optional pool tasks."""
    active = [task for task in tasks if task.get("status") == "pending"]
    deadline_tasks = [task for task in active if not is_pool_task(task)]
    pool_tasks = [task for task in active if is_pool_task(task)]

    ordered_deadline = sort_tasks(deadline_tasks, strategy, category_order)
    ordered_pool = sort_tasks(pool_tasks, strategy, category_order)

    for task in ordered_deadline:
        task["is_optional"] = False
    for task in ordered_pool:
        task["is_optional"] = True

    return ordered_deadline + ordered_pool, ordered_deadline, ordered_pool


def is_pool_command(text):
    return str(text or "").strip().lower() in POOL_COMMANDS


def _format_priority(priority):
    return {
        "high": "高",
        "medium": "中",
        "low": "低",
        "unknown": "未说明",
    }.get(str(priority or "unknown").lower(), "未说明")


def format_pool_tasks(tasks):
    pool_tasks = [
        task
        for task in tasks
        if task.get("status") == "pending" and is_pool_task(task)
    ]

    if not pool_tasks:
        return (
            "🗂️ 待安排池目前为空。\n\n"
            "创建任务时明确说“没有截止日期”，任务就会进入这里。"
        )

    lines = [f"🗂️ 待安排池（共 {len(pool_tasks)} 个）", ""]
    for index, task in enumerate(pool_tasks, start=1):
        minutes = task.get("remaining_minutes")
        if minutes is None:
            minutes = task.get("estimated_minutes")
        try:
            minutes = int(minutes or 0)
        except (TypeError, ValueError):
            minutes = 0

        lines.extend(
            [
                f"{index}. #{task.get('id')} {task.get('title') or '未命名任务'}",
                f"   剩余：{minutes}分钟",
                f"   重要程度：{_format_priority(task.get('priority'))}",
                "",
            ]
        )

    lines.extend(
        [
            "这些任务不会挤占有截止日期的任务或固定安排。",
            "只有当天仍有足够空闲时，才会作为可选任务进入计划。",
        ]
    )
    return "\n".join(lines)
