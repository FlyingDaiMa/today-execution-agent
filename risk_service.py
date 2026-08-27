import json
import re
import time
import traceback
from datetime import datetime, timedelta

import database
from planner import generate_execution_plan, parse_datetime
from pool_service import prepare_tasks_for_planning


RISK_WINDOW_HOURS = 24
RISK_REMINDER_SECONDS = 60
SAFE_PUSH_START_HOUR = 8
SAFE_PUSH_END_HOUR = 22


def _format_duration(minutes):
    minutes = max(0, int(minutes or 0))
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}小时{remainder}分钟"
    if hours:
        return f"{hours}小时"
    return f"{remainder}分钟"


def _task_remaining_minutes(task):
    value = task.get("remaining_minutes")
    if value is None:
        value = task.get("estimated_minutes")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def calculate_task_progress(task, subtask_progress=None):
    if subtask_progress and int(subtask_progress.get("total") or 0) > 0:
        return max(0, min(100, int(subtask_progress.get("percent") or 0)))

    try:
        estimated = int(task.get("estimated_minutes") or 0)
        remaining = _task_remaining_minutes(task)
    except (TypeError, ValueError):
        return 0

    if estimated <= 0:
        return 0

    completed = max(0, min(estimated, estimated - remaining))
    return max(0, min(100, round(completed * 100 / estimated)))


def _target_progress(hours_left):
    """A transparent milestone proxy until daily plans are persisted."""
    if hours_left <= 0:
        return 100
    if hours_left <= 6:
        return 80
    if hours_left <= 12:
        return 60
    if hours_left <= RISK_WINDOW_HOURS:
        return 40
    return 0


def assess_task_risk(task, now=None, subtask_progress=None):
    if now is None:
        now = datetime.now()

    if task.get("status") != "pending":
        return None

    deadline = parse_datetime(task.get("deadline"))
    if deadline is None:
        return None

    remaining_minutes = _task_remaining_minutes(task)
    if remaining_minutes <= 0:
        return None

    hours_left = (deadline - now).total_seconds() / 3600
    actual_progress = calculate_task_progress(task, subtask_progress)
    target_progress = _target_progress(hours_left)

    if hours_left <= 0:
        risk_type = "overdue"
        severity = 1000 + min(500, abs(hours_left))
        reason = (
            f"任务已逾期{abs(hours_left):.1f}小时，"
            f"仍有{_format_duration(remaining_minutes)}未完成"
        )
    elif remaining_minutes > hours_left * 60:
        risk_type = "insufficient_time"
        severity = 800 + (remaining_minutes - hours_left * 60) / 10
        reason = (
            f"距离截止仅{hours_left:.1f}小时，"
            f"剩余工作量约{_format_duration(remaining_minutes)}，"
            "已超过剩余自然时间"
        )
    elif hours_left <= RISK_WINDOW_HOURS and actual_progress < target_progress:
        risk_type = "progress_behind"
        severity = 500 + (target_progress - actual_progress)
        reason = (
            f"距离截止仅{hours_left:.1f}小时，"
            f"当前进度{actual_progress}%低于建议进度{target_progress}%"
        )
    else:
        return None

    return {
        "task_id": int(task.get("id")),
        "title": task.get("title") or "未命名任务",
        "deadline": deadline.strftime("%Y-%m-%d %H:%M"),
        "hours_left": round(hours_left, 1),
        "remaining_minutes": remaining_minutes,
        "actual_progress": actual_progress,
        "target_progress": target_progress,
        "risk_type": risk_type,
        "risk_reason": reason,
        "severity": severity,
    }


def find_user_risks(user_open_id, tasks, now=None):
    if now is None:
        now = datetime.now()

    risks = []
    for task in tasks:
        if task.get("status") != "pending":
            continue
        subtask_progress = database.get_task_subtask_progress(
            user_open_id,
            task.get("id"),
        )
        risk = assess_task_risk(
            task,
            now=now,
            subtask_progress=subtask_progress,
        )
        if risk is not None:
            risks.append(risk)

    return sorted(
        risks,
        key=lambda item: (
            -float(item.get("severity") or 0),
            float(item.get("hours_left") or 0),
            int(item.get("task_id") or 0),
        ),
    )


def format_risk_alert(risk):
    hours_left = float(risk.get("hours_left") or 0)
    if hours_left < 0:
        time_text = f"已逾期{abs(hours_left):.1f}小时"
    else:
        time_text = f"还剩{hours_left:.1f}小时"

    return "\n".join(
        [
            "⚠️ 任务延期风险提醒",
            "",
            f"任务编号：#{risk.get('task_id')}",
            f"任务：{risk.get('title')}",
            f"截止时间：{risk.get('deadline')}（{time_text}）",
            f"当前进度：{risk.get('actual_progress')}%",
            f"建议当前进度：{risk.get('target_progress')}%",
            f"剩余工作量：{_format_duration(risk.get('remaining_minutes'))}",
            "",
            f"触发原因：{risk.get('risk_reason')}",
            "",
            "如果仍要继续，请告诉我你现在的时间、状态和临时安排，",
            "例如：现在11:40，今天只能再投入1小时，14:00到15:00有课。",
            "我会先生成补救建议，确认前不会修改原任务或固定安排。",
            "暂时不调整可回复「暂不调整」。",
        ]
    )


def _parse_user_context(user_context, now):
    context = str(user_context or "")
    planning_now = now

    current_match = re.search(
        r"现在(?:是)?\s*(\d{1,2})[:：](\d{2})",
        context,
    )
    if current_match:
        hour = int(current_match.group(1))
        minute = int(current_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate >= now:
                planning_now = candidate

    capacity_limit = None
    capacity_match = re.search(
        r"(?:只剩|只有|只能|还能)[^\d]{0,8}(\d+(?:\.\d+)?)\s*(小时|分钟)",
        context,
    )
    if capacity_match:
        value = float(capacity_match.group(1))
        capacity_limit = round(value * 60) if capacity_match.group(2) == "小时" else round(value)
        capacity_limit = max(15, min(12 * 60, capacity_limit))

    temporary_blocks = []
    for match in re.finditer(
        r"(\d{1,2})[:：](\d{2})\s*(?:到|至|[-—~～])\s*(\d{1,2})[:：](\d{2})",
        context,
    ):
        start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
        if not (
            0 <= start_hour <= 23
            and 0 <= end_hour <= 23
            and 0 <= start_minute <= 59
            and 0 <= end_minute <= 59
        ):
            continue
        start = planning_now.replace(
            hour=start_hour,
            minute=start_minute,
            second=0,
            microsecond=0,
        )
        end = planning_now.replace(
            hour=end_hour,
            minute=end_minute,
            second=0,
            microsecond=0,
        )
        if end > start:
            temporary_blocks.append(
                {
                    "title": "用户临时安排（仅用于本次补救）",
                    "start": start.strftime("%Y-%m-%d %H:%M"),
                    "end": end.strftime("%Y-%m-%d %H:%M"),
                    "temporary": True,
                }
            )

    focus_match = re.search(r"(?:先做|优先)[^#\d]{0,6}#?(\d+)", context)
    focus_task_id = int(focus_match.group(1)) if focus_match else None

    return {
        "planning_now": planning_now,
        "capacity_limit": capacity_limit,
        "temporary_blocks": temporary_blocks,
        "focus_task_id": focus_task_id,
    }


def _trim_plan_to_capacity(plan, capacity_limit):
    if capacity_limit is None:
        return plan

    result = []
    remaining_capacity = int(capacity_limit)
    for item in plan:
        if remaining_capacity <= 0:
            break
        planned = int(item.get("planned_minutes") or 0)
        if planned <= 0:
            continue
        used = min(planned, remaining_capacity)
        trimmed = dict(item)
        trimmed["planned_minutes"] = used
        start = parse_datetime(trimmed.get("start_time"))
        if start is not None:
            trimmed["end_time"] = (start + timedelta(minutes=used)).strftime(
                "%Y-%m-%d %H:%M"
            )
        result.append(trimmed)
        remaining_capacity -= used
    return result


def build_rescue_plan(user_open_id, risk, user_context, now=None):
    if now is None:
        now = datetime.now()

    context = _parse_user_context(user_context, now)
    planning_now = context["planning_now"]
    date_text = planning_now.strftime("%Y-%m-%d")

    tasks = [
        task
        for task in database.get_user_tasks(user_open_id)
        if task.get("status") == "pending"
    ]
    preference = database.get_user_preference(user_open_id) or {}
    strategy = preference.get("priority_strategy") or "balanced"
    category_order = preference.get("category_order")

    blocked_windows = [
        {
            "title": row.get("title") or "忙碌",
            "start": row.get("start_time"),
            "end": row.get("end_time"),
            "temporary": False,
        }
        for row in database.get_user_blocked_times(user_open_id, date_text)
    ]
    blocked_windows.extend(context["temporary_blocks"])

    sorted_tasks, _, _ = prepare_tasks_for_planning(
        tasks,
        strategy,
        category_order,
    )
    focus_task_id = context["focus_task_id"] or int(risk.get("task_id"))
    sorted_tasks.sort(
        key=lambda task: (
            bool(task.get("is_optional")),
            0 if int(task.get("id")) == focus_task_id else 1,
        )
    )

    plan = generate_execution_plan(
        sorted_tasks,
        now=planning_now,
        blocked_windows=blocked_windows,
    )
    plan = _trim_plan_to_capacity(plan, context["capacity_limit"])

    planned_by_task = {}
    for item in plan:
        task_id = int(item.get("task_id"))
        planned_by_task[task_id] = planned_by_task.get(task_id, 0) + int(
            item.get("planned_minutes") or 0
        )

    delayed = []
    uncovered = []
    risk_task_id = int(risk.get("task_id"))
    for task in tasks:
        task_id = int(task.get("id"))
        remaining = _task_remaining_minutes(task)
        planned = planned_by_task.get(task_id, 0)
        if planned >= remaining:
            continue
        item = {
            "task_id": task_id,
            "title": task.get("title") or "未命名任务",
            "unplanned_minutes": remaining - planned,
        }
        if task_id == risk_task_id:
            uncovered.append(item)
        else:
            delayed.append(item)

    return {
        "risk_task_id": risk_task_id,
        "risk_title": risk.get("title") or "未命名任务",
        "generated_at": now.isoformat(timespec="seconds"),
        "planning_start": planning_now.strftime("%Y-%m-%d %H:%M"),
        "user_context": user_context,
        "strategy": strategy,
        "capacity_limit": context["capacity_limit"],
        "before": {
            "progress": int(risk.get("actual_progress") or 0),
            "target_progress": int(risk.get("target_progress") or 0),
            "remaining_minutes": int(risk.get("remaining_minutes") or 0),
            "deadline": risk.get("deadline"),
            "reason": risk.get("risk_reason"),
        },
        "protected_blocks": blocked_windows,
        "schedule": plan,
        "delayed_tasks": delayed,
        "uncovered_risk_work": uncovered,
    }


def format_rescue_plan(proposal):
    before = proposal.get("before") or {}
    lines = [
        "🛟 补救计划建议",
        "",
        f"风险任务：#{proposal.get('risk_task_id')} {proposal.get('risk_title')}",
        f"你提供的状态：{proposal.get('user_context')}",
        "",
        "调整前：",
        f"- 当前进度 {before.get('progress')}%，建议进度 {before.get('target_progress')}%",
        f"- 截止 {before.get('deadline')}，仍需{_format_duration(before.get('remaining_minutes'))}",
        "",
        "推荐调整后：",
    ]

    schedule = proposal.get("schedule") or []
    if schedule:
        for index, item in enumerate(schedule, start=1):
            lines.append(
                f"{index}. {str(item.get('start_time') or '')[-5:]}–"
                f"{str(item.get('end_time') or '')[-5:]} "
                f"#{item.get('task_id')} {item.get('title')} "
                f"（{_format_duration(item.get('planned_minutes'))}）"
            )
    else:
        lines.append("- 当前状态下没有可用执行时间，请补充可用时间。")

    protected = proposal.get("protected_blocks") or []
    if protected:
        lines.extend(["", "保持不动的固定/临时安排："])
        for block in protected:
            lines.append(
                f"- {str(block.get('start') or '')[-5:]}–"
                f"{str(block.get('end') or '')[-5:]} {block.get('title')}"
            )

    delayed = proposal.get("delayed_tasks") or []
    if delayed:
        lines.extend(["", "建议暂缓的较低优先级任务："])
        for item in delayed:
            lines.append(
                f"- #{item.get('task_id')} {item.get('title')}："
                f"暂缓{_format_duration(item.get('unplanned_minutes'))}"
            )

    uncovered = proposal.get("uncovered_risk_work") or []
    if uncovered:
        lines.extend(["", "仍需你决定的风险："])
        for item in uncovered:
            lines.append(
                f"- #{item.get('task_id')} 仍有"
                f"{_format_duration(item.get('unplanned_minutes'))}无法排入今天"
            )

    lines.extend(
        [
            "",
            "调整原因：优先保护临近截止的风险任务，固定安排不移动；",
            "时间不足时只建议暂缓其他任务，不会擅自修改它们。",
            "",
            "回复「确认调整」保存这份补救计划；",
            "回复「暂不调整」保留原计划；",
            "也可以直接告诉我你希望怎样修改方案。",
        ]
    )
    return "\n".join(lines)


def format_confirmed_plan(proposal):
    return "\n".join(
        [
            "✅ 补救计划已确认并保存",
            "",
            f"风险任务：#{proposal.get('risk_task_id')} {proposal.get('risk_title')}",
            f"计划开始：{proposal.get('planning_start')}",
            f"执行时段：{len(proposal.get('schedule') or [])} 段",
            "",
            "任务状态、截止时间和固定安排都没有被擅自修改。",
            "之后可发送「查看补救计划」重新查看这份方案。",
        ]
    )


def format_saved_plan(proposal):
    text = format_rescue_plan(proposal)
    confirmation_marker = "\n回复「确认调整」保存这份补救计划；"
    if confirmation_marker in text:
        text = text.split(confirmation_marker, 1)[0]
    return text.replace(
        "🛟 补救计划建议",
        "🛟 已确认的补救计划",
        1,
    )


def handle_risk_command(user_open_id, text):
    normalized = str(text or "").strip().lower()
    if normalized not in {"查看补救计划", "我的补救计划", "查看最新补救计划"}:
        return None

    row = database.get_latest_confirmed_risk_plan(user_open_id)
    if row is None:
        return "当前还没有已确认的补救计划。"

    try:
        proposal = json.loads(row.get("proposal_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return "最新补救计划记录无法读取，请重新生成。"

    return format_saved_plan(proposal)


def handle_active_risk_input(user_open_id, text, now=None):
    alert = database.get_active_risk_alert(user_open_id)
    if alert is None:
        return None
    if now is None:
        now = datetime.now()

    normalized = str(text or "").strip().lower()
    dismiss_words = {
        "暂不调整",
        "先不调整",
        "不调整",
        "取消",
        "算了",
        "保持原计划",
    }
    confirm_words = {
        "确认",
        "确认调整",
        "确认补救计划",
        "按这个执行",
        "可以",
        "好的",
        "ok",
        "yes",
    }
    changed_at = now.isoformat(timespec="seconds")

    if normalized in dismiss_words:
        database.close_risk_alert(
            alert.get("id"),
            user_open_id,
            "dismissed",
            changed_at,
        )
        return (
            "好的，本次暂不调整。\n\n"
            "原任务、截止时间和固定安排都保持不变。"
        )

    task = database.get_task_by_id(user_open_id, alert.get("task_id"))
    if task is None or task.get("status") != "pending":
        database.close_risk_alert(
            alert.get("id"),
            user_open_id,
            "resolved",
            changed_at,
        )
        return "这个任务的风险状态已经解除，本次补救流程已结束。"

    if alert.get("status") == "awaiting_confirmation" and normalized in confirm_words:
        try:
            proposal = json.loads(alert.get("proposal_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return "补救方案记录无法读取，请重新告诉我你的当前状态。"
        database.close_risk_alert(
            alert.get("id"),
            user_open_id,
            "confirmed",
            changed_at,
        )
        return format_confirmed_plan(proposal)

    try:
        risk = json.loads(alert.get("snapshot_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        risk = {}

    if alert.get("status") == "awaiting_confirmation":
        old_context = str(alert.get("user_context") or "").strip()
        user_context = f"{old_context}；补充要求：{text}" if old_context else str(text)
    else:
        user_context = str(text)

    proposal = build_rescue_plan(
        user_open_id,
        risk,
        user_context,
        now=now,
    )
    saved = database.save_risk_rescue_proposal(
        alert.get("id"),
        user_open_id,
        user_context,
        json.dumps(proposal, ensure_ascii=False),
        changed_at,
    )
    if not saved:
        return "风险状态已经发生变化，请发送「查看我的任务」确认最新情况。"
    return format_rescue_plan(proposal)


def _format_risk_reminder(alert):
    if alert.get("status") == "awaiting_confirmation":
        action = "回复「确认调整」保存方案，或回复「暂不调整」保留原计划。"
    else:
        action = "告诉我当前时间和状态以生成补救建议，或回复「暂不调整」。"
    return "\n".join(
        [
            "⏱️ 风险处理等待确认",
            "",
            "一分钟内没有收到你的确认，原任务和原计划保持不变。",
            action,
        ]
    )


def run_risk_checks(send_func, now=None, can_alert_func=None):
    if now is None:
        now = datetime.now()

    result = {"sent": 0, "reminded": 0, "skipped": 0, "failed": 0}
    now_text = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(seconds=RISK_REMINDER_SECONDS)).isoformat(
        timespec="seconds"
    )

    for alert in database.list_due_risk_reminders(cutoff):
        try:
            if send_func(alert.get("user_open_id"), _format_risk_reminder(alert)):
                database.mark_risk_alert_reminded(alert.get("id"), now_text)
                result["reminded"] += 1
            else:
                result["failed"] += 1
        except Exception as exc:
            result["failed"] += 1
            print(f"发送风险确认提醒失败：{exc!r}", flush=True)
            traceback.print_exc()

    if not (SAFE_PUSH_START_HOUR <= now.hour < SAFE_PUSH_END_HOUR):
        return result

    for user_open_id in database.list_onboarded_users():
        try:
            if database.get_active_risk_alert(user_open_id) is not None:
                result["skipped"] += 1
                continue
            if can_alert_func is not None and not can_alert_func(user_open_id):
                result["skipped"] += 1
                continue

            risks = find_user_risks(
                user_open_id,
                database.get_user_tasks(user_open_id),
                now=now,
            )
            if not risks:
                result["skipped"] += 1
                continue

            risk = risks[0]
            alert_id = database.create_risk_alert(
                user_open_id,
                risk.get("task_id"),
                now.strftime("%Y-%m-%d"),
                risk.get("risk_type"),
                risk.get("risk_reason"),
                json.dumps(risk, ensure_ascii=False),
                now_text,
            )
            if alert_id is None:
                result["skipped"] += 1
                continue

            if send_func(user_open_id, format_risk_alert(risk)):
                result["sent"] += 1
            else:
                database.delete_risk_alert(alert_id, user_open_id)
                result["failed"] += 1
        except Exception as exc:
            result["failed"] += 1
            print(
                f"处理主动风险提醒失败：user={user_open_id}, error={exc!r}",
                flush=True,
            )
            traceback.print_exc()

    return result


def run_risk_loop(send_func, interval_seconds=30, stop_event=None, can_alert_func=None):
    if interval_seconds <= 0:
        raise ValueError("interval_seconds 必须大于 0")

    while stop_event is None or not stop_event.is_set():
        try:
            run_risk_checks(
                send_func,
                can_alert_func=can_alert_func,
            )
        except Exception as exc:
            print(f"风险提醒轮询失败：{exc!r}", flush=True)
            traceback.print_exc()

        if stop_event is None:
            time.sleep(interval_seconds)
        elif stop_event.wait(interval_seconds):
            break
