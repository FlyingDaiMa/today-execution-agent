import traceback
from datetime import datetime, timedelta

import database
from planner import parse_datetime


def _is_due(now, target, grace_minutes=5):
    return target <= now < target + timedelta(minutes=grace_minutes)


def _build_due_course_reminders(user_open_id, now):
    date_text = now.strftime("%Y-%m-%d")
    reminders = []
    for block in database.get_user_blocked_times(user_open_id, date_text):
        if not str(block.get("source") or "").startswith("course:"):
            continue
        start = parse_datetime(block.get("start_time"))
        if start is None or not _is_due(now, start - timedelta(minutes=10)):
            continue
        key = f"course:{block.get('start_time')}:{block.get('title')}"
        text = (
            "⏰ 课程即将开始\n\n"
            f"{block.get('title') or '课程'}\n"
            f"时间：{str(block.get('start_time'))[-5:]}–"
            f"{str(block.get('end_time'))[-5:]}\n\n"
            "已为你保留课程时间，不会安排普通任务。"
        )
        reminders.append((key, text))
    return reminders


def _build_due_task_reminders(user_open_id, now):
    snapshot = database.get_daily_plan_snapshot(
        user_open_id,
        now.strftime("%Y-%m-%d"),
    )
    if not snapshot:
        return []

    final_items = {}
    for item in snapshot.get("plan", []):
        task_id = item.get("task_id")
        end = parse_datetime(item.get("end_time"))
        if task_id is None or end is None:
            continue
        key = str(task_id)
        if key not in final_items or end > final_items[key][0]:
            final_items[key] = (end, item)

    reminders = []
    for task_id, (end, item) in final_items.items():
        if not _is_due(now, end):
            continue
        task = database.get_task_by_id(user_open_id, int(task_id))
        if not task or task.get("status") != "pending":
            continue
        reminder_key = f"task-end:{item.get('end_time')}:{task_id}"
        text = (
            "⏱️ 计划时段已结束\n\n"
            f"#{task_id} {task.get('title') or '未命名任务'}\n"
            f"原计划结束：{str(item.get('end_time'))[-5:]}\n\n"
            "如果已经完成，可以回复“#编号完成了”；"
            "如果还没完成，请告诉我还需要多少分钟，我会重新安排。"
        )
        reminders.append((reminder_key, text))
    return reminders


def run_due_reminders(send_func, now=None):
    if now is None:
        now = datetime.now()
    result = {"sent": 0, "skipped": 0, "failed": 0}

    if now.hour < 8 or now.hour >= 22:
        return result

    for user_open_id in database.list_onboarded_users():
        try:
            reminders = _build_due_course_reminders(user_open_id, now)
            reminders.extend(_build_due_task_reminders(user_open_id, now))
            for reminder_key, text in reminders:
                if database.has_reminder_been_delivered(
                    user_open_id,
                    reminder_key,
                ):
                    result["skipped"] += 1
                    continue
                if not send_func(user_open_id, text):
                    result["failed"] += 1
                    continue
                if database.record_reminder_delivery(
                    user_open_id,
                    reminder_key,
                    now.isoformat(timespec="seconds"),
                ):
                    result["sent"] += 1
                else:
                    result["skipped"] += 1
        except Exception as exc:
            result["failed"] += 1
            print(
                f"处理执行提醒失败：user={user_open_id}, error={exc!r}",
                flush=True,
            )
            traceback.print_exc()
    return result
