import re
from datetime import datetime, timedelta

import database
from tone_service import get_encouragement


_SET_PATTERNS = {
    "morning": re.compile(
        r"^设置晨间(?:计划|推送) ([0-2]\d:[0-5]\d)$"
    ),
    "evening": re.compile(
        r"^设置晚间(?:总结|推送) ([0-2]\d:[0-5]\d)$"
    ),
}

_DISABLE_COMMANDS = {
    "关闭晨间计划": "morning",
    "关闭晨间推送": "morning",
    "关闭晚间总结": "evening",
    "关闭晚间推送": "evening",
}


def parse_push_command(text: str):
    if not isinstance(text, str):
        return None

    command = text.strip()

    if command == "查看推送设置":
        return {"action": "show"}

    if command in _DISABLE_COMMANDS:
        return {
            "action": "disable",
            "push_type": _DISABLE_COMMANDS[command],
        }

    for push_type, pattern in _SET_PATTERNS.items():
        match = pattern.fullmatch(command)
        if match is None:
            continue

        push_time = match.group(1)

        try:
            datetime.strptime(push_time, "%H:%M")
        except ValueError:
            return None

        return {
            "action": "set",
            "push_type": push_type,
            "time": push_time,
        }

    return None


def format_push_settings(preference):
    preference = preference or {}

    morning_enabled = bool(preference.get("morning_push_enabled", 0))
    morning_time = preference.get("morning_push_time") or "08:00"
    evening_enabled = bool(preference.get("evening_push_enabled", 0))
    evening_time = preference.get("evening_push_time") or "22:00"

    morning_text = (
        f"已开启（{morning_time}）" if morning_enabled else "已关闭"
    )
    evening_text = (
        f"已开启（{evening_time}）" if evening_enabled else "已关闭"
    )

    return (
        "当前推送设置：\n"
        f"- 晨间计划：{morning_text}\n"
        f"- 晚间总结：{evening_text}"
    )


def handle_push_command(user_open_id: str, text: str):
    parsed = parse_push_command(text)

    if parsed is None:
        return None

    if parsed["action"] == "show":
        return format_push_settings(database.get_user_preference(user_open_id))

    push_type = parsed["push_type"]
    label = "晨间计划" if push_type == "morning" else "晚间总结"

    if parsed["action"] == "disable":
        database.update_push_preference(
            user_open_id,
            push_type,
            enabled=False,
        )
        return f"{label}已关闭。"

    push_time = parsed["time"]
    database.update_push_preference(
        user_open_id,
        push_type,
        enabled=True,
        push_time=push_time,
    )
    return f"{label}已设置为每天 {push_time} 推送。"


def is_push_due(
    now: datetime,
    target_hhmm: str,
    grace_minutes: int = 5,
) -> bool:
    if grace_minutes <= 0:
        raise ValueError("grace_minutes 必须大于 0")

    try:
        target_time = datetime.strptime(target_hhmm, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("目标时间必须使用 HH:MM 格式") from exc

    target = now.replace(
        hour=target_time.hour,
        minute=target_time.minute,
        second=0,
        microsecond=0,
    )
    window_end = target + timedelta(minutes=grace_minutes)
    return target <= now < window_end


def _calculate_plan_completion(tasks, plan_snapshot):
    if not plan_snapshot or not isinstance(plan_snapshot.get("plan"), list):
        return None

    baseline_plan = plan_snapshot.get("baseline_plan")
    if not isinstance(baseline_plan, list):
        baseline_plan = plan_snapshot["plan"]

    targets = {}
    baselines = {}
    titles = {}
    for item in baseline_plan:
        task_id = item.get("task_id")
        if task_id is None:
            continue
        key = str(task_id)
        targets[key] = targets.get(key, 0) + int(
            item.get("planned_minutes") or 0
        )
        baselines[key] = max(
            baselines.get(key, 0),
            int(item.get("estimated_minutes") or 0),
        )
        titles[key] = item.get("title") or "未命名任务"

    if not targets:
        return {"task_count": 0, "percent": 100, "unfinished": []}

    tasks_by_id = {str(task.get("id")): task for task in tasks}
    achieved = 0
    total = sum(targets.values())
    unfinished = []

    for key, target in targets.items():
        task = tasks_by_id.get(key, {})
        if task.get("status") == "completed":
            contribution = target
        else:
            current = task.get("remaining_minutes")
            if current is None:
                current = task.get("estimated_minutes")
            progress = max(0, baselines.get(key, target) - int(current or 0))
            contribution = min(target, progress)
        achieved += contribution
        if contribution < target:
            unfinished.append(titles[key])

    percent = round(achieved * 100 / total) if total else 100
    return {
        "task_count": len(targets),
        "percent": percent,
        "unfinished": unfinished,
    }


def build_evening_summary(
    tasks,
    summary_date=None,
    plan_snapshot=None,
    assistant_tone=None,
):
    active_tasks = [
        task for task in tasks if task.get("status") != "cancelled"
    ]

    if not active_tasks:
        return (
            "今日总结：今天没有需要统计的任务。\n"
            "好好休息，明天继续稳稳向前。"
        )

    completed_tasks = []

    for task in active_tasks:
        if task.get("status") != "completed":
            continue

        if summary_date is None:
            completed_tasks.append(task)
            continue

        updated_at = str(task.get("updated_at") or "")

        if updated_at[:10] == summary_date:
            completed_tasks.append(task)

    pending_tasks = [
        task for task in active_tasks if task.get("status") == "pending"
    ]

    lines = [
        "今日完成情况：",
        f"- 已完成：{len(completed_tasks)} 项",
        f"- 待继续：{len(pending_tasks)} 项",
    ]

    plan_completion = _calculate_plan_completion(tasks, plan_snapshot)
    if plan_completion is not None:
        lines.extend(
            [
                f"- 今日计划任务：{plan_completion['task_count']} 项",
                f"- 今日计划完成率：{plan_completion['percent']}%",
            ]
        )
        if plan_completion["unfinished"]:
            lines.append(
                "- 今日计划未完成："
                + "、".join(plan_completion["unfinished"])
            )

    if completed_tasks:
        lines.append(
            "- 完成任务："
            + "、".join(
                task.get("title") or "未命名任务"
                for task in completed_tasks
            )
        )

    if pending_tasks:
        lines.append(
            "- 待继续任务："
            + "、".join(
                task.get("title") or "未命名任务"
                for task in pending_tasks
            )
        )
        lines.append("需要继续推进时，回复「安排我的今天」即可重新安排。")

    if not pending_tasks and completed_tasks:
        lines.append(get_encouragement(assistant_tone, has_progress=True))
    elif completed_tasks:
        lines.append(get_encouragement(assistant_tone, has_progress=True))
    elif not pending_tasks:
        lines.append("今天没有新完成的任务，当前也没有待办。")
    else:
        lines.append(get_encouragement(assistant_tone, has_progress=False))

    return "\n".join(lines)
