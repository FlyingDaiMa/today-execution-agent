import time
import traceback
from datetime import datetime

import database
from planner import generate_execution_plan, parse_datetime
from push_service import build_evening_summary, is_push_due
from pool_service import prepare_tasks_for_planning
from reminder_service import run_due_reminders
from news_service import build_news_section


PUSH_TYPES = ("morning", "evening")


def _get_blocked_windows(user_open_id, date_text):
    rows = database.get_user_blocked_times(
        user_open_id,
        date_text,
    )

    return [
        {
            "title": row.get("title") or "忙碌",
            "start": row.get("start_time"),
            "end": row.get("end_time"),
        }
        for row in rows
    ]


def _get_deadline_risk(task, now):
    deadline = parse_datetime(task.get("deadline"))

    if deadline is None:
        return ""

    if deadline < now:
        return " ⚠️ 已逾期"

    if deadline.date() == now.date():
        return " ⚠️ 今天截止"

    return ""


def build_morning_plan(user_open_id, now=None):
    if now is None:
        now = datetime.now()

    date_text = now.strftime("%Y-%m-%d")
    tasks = database.get_user_tasks(user_open_id)
    pending_tasks = [
        task
        for task in tasks
        if task.get("status") == "pending"
    ]
    preference = database.get_user_preference(user_open_id) or {}

    lines = [f"🌅 晨间计划｜{date_text}", ""]

    if not pending_tasks:
        database.save_daily_plan_snapshot(user_open_id, date_text, [])
        lines.extend(
            [
                "今天暂无待办任务。",
                "需要时直接告诉我你想完成什么。",
            ]
        )
        news_section = build_news_section(preference)
        if news_section:
            lines.extend(["", news_section])
        return "\n".join(lines)

    strategy = preference.get("priority_strategy") or "balanced"
    category_order = preference.get("category_order")
    sorted_tasks, deadline_tasks, pool_tasks = prepare_tasks_for_planning(
        pending_tasks,
        strategy,
        category_order,
    )
    blocked_windows = _get_blocked_windows(user_open_id, date_text)
    plan = generate_execution_plan(
        sorted_tasks,
        now=now,
        blocked_windows=blocked_windows,
    )
    database.save_daily_plan_snapshot(user_open_id, date_text, plan)

    tasks_by_id = {
        task.get("id"): task
        for task in pending_tasks
    }

    if not plan:
        lines.append("今天暂时没有可生成的时间段安排，待办如下：")

        for task in deadline_tasks:
            title = task.get("title") or "未命名任务"
            lines.append(
                f"- {title}{_get_deadline_risk(task, now)}"
            )

        if pool_tasks:
            lines.extend(["", "🗂️ 待安排池可选任务："])
            for task in pool_tasks:
                lines.append(
                    f"- #{task.get('id')} {task.get('title') or '未命名任务'}"
                    "（今天空间不足，暂不占用）"
                )

        news_section = build_news_section(preference)
        if news_section:
            lines.extend(["", news_section])

        return "\n".join(lines)

    for index, item in enumerate(plan, start=1):
        start_time = str(item.get("start_time") or "")[-5:]
        end_time = str(item.get("end_time") or "")[-5:]
        title = item.get("title") or "未命名任务"
        segment = int(item.get("segment") or 1)
        segment_text = f"（第{segment}段）" if segment > 1 else ""
        source_task = tasks_by_id.get(item.get("task_id"), item)
        risk_text = _get_deadline_risk(source_task, now)
        optional_text = " [待安排池可选]" if item.get("is_optional") else ""
        lines.append(
            f"{index}. {start_time}–{end_time} "
            f"{title}{segment_text}{risk_text}{optional_text}"
        )

    if pool_tasks:
        scheduled_pool_ids = {
            str(item.get("task_id"))
            for item in plan
            if item.get("is_optional") and item.get("task_id") is not None
        }
        unscheduled_pool = [
            task
            for task in pool_tasks
            if str(task.get("id")) not in scheduled_pool_ids
        ]
        lines.extend(["", "🗂️ 待安排池："])
        if scheduled_pool_ids:
            lines.append("- 今天仍有足够空闲，已列为可选执行项。")
        for task in unscheduled_pool:
            lines.append(
                f"- #{task.get('id')} {task.get('title') or '未命名任务'}："
                "继续留在池中，不挤占截止型任务。"
            )

    lines.extend(
        [
            "",
            "任务间已预留 15 分钟缓冲。",
        ]
    )

    news_section = build_news_section(preference)
    if news_section:
        lines.extend(["", news_section])
    return "\n".join(lines)


def build_push_text(user_open_id, push_type, now=None):
    if now is None:
        now = datetime.now()

    if push_type == "morning":
        return build_morning_plan(user_open_id, now=now)

    if push_type == "evening":
        tasks = database.get_user_tasks(user_open_id)
        snapshot = database.get_daily_plan_snapshot(
            user_open_id,
            now.strftime("%Y-%m-%d"),
        )
        return build_evening_summary(
            tasks,
            summary_date=now.strftime("%Y-%m-%d"),
            plan_snapshot=snapshot,
            assistant_tone=(
                database.get_user_preference(user_open_id) or {}
            ).get("assistant_tone"),
        )

    raise ValueError("push_type 必须是 morning 或 evening")


def run_due_pushes(send_func, now=None):
    if now is None:
        now = datetime.now()

    delivery_date = now.strftime("%Y-%m-%d")
    result = {
        "sent": 0,
        "skipped": 0,
        "failed": 0,
    }

    for push_type in PUSH_TYPES:
        try:
            users = database.list_push_enabled_users(push_type)
        except Exception as exc:
            result["failed"] += 1
            print(
                f"读取{push_type}推送用户失败：{exc!r}",
                flush=True,
            )
            traceback.print_exc()
            continue

        for preference in users:
            user_open_id = preference.get("user_open_id")
            push_time = preference.get("push_time")

            try:
                if not is_push_due(now, push_time):
                    result["skipped"] += 1
                    continue

                if database.has_push_been_delivered(
                    user_open_id,
                    push_type,
                    delivery_date,
                ):
                    result["skipped"] += 1
                    continue

                text = build_push_text(
                    user_open_id,
                    push_type,
                    now=now,
                )

                if not send_func(user_open_id, text):
                    result["failed"] += 1
                    continue

                if database.record_push_delivery(
                    user_open_id,
                    push_type,
                    delivery_date,
                    delivered_at=now.isoformat(timespec="seconds"),
                ):
                    result["sent"] += 1
                else:
                    result["skipped"] += 1

            except Exception as exc:
                result["failed"] += 1
                print(
                    "处理主动推送失败："
                    f"type={push_type}, "
                    f"user={user_open_id}, "
                    f"error={exc!r}",
                    flush=True,
                )
                traceback.print_exc()

    return result


def run_push_loop(
    send_func,
    interval_seconds=30,
    stop_event=None,
):
    if interval_seconds <= 0:
        raise ValueError("interval_seconds 必须大于 0")

    while stop_event is None or not stop_event.is_set():
        try:
            run_due_pushes(send_func)
            run_due_reminders(send_func)
        except Exception as exc:
            print(
                f"主动推送轮询失败：{exc!r}",
                flush=True,
            )
            traceback.print_exc()

        if stop_event is None:
            time.sleep(interval_seconds)
        elif stop_event.wait(interval_seconds):
            break
