import json
import os
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime

import lark_oapi as lark
from dotenv import load_dotenv
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
)

from llm_service import (
    recognize_task,
    complete_task,
    recognize_blocked_times,
    recognize_task_completion,
    recognize_task_update,
    recognize_task_progress,
    recognize_intent,
    generate_task_breakdown,
)

from database import (
    init_db,
    create_task,
    get_user_tasks,
    get_task_by_id,
    update_task_status,
    update_task,
    update_task_remaining_minutes,
    get_user_preference,
    save_user_preference,
    save_onboarding_strategy,
    update_category_order,
    has_completed_onboarding,
    get_user_blocked_times,
    create_blocked_time,
    replace_blocked_times_for_source,
    replace_task_subtasks,
    get_task_subtasks,
    update_task_subtask_status,
    get_task_subtask_progress,
    save_daily_plan_snapshot,
)

from scheduler import sort_tasks
from planner import generate_execution_plan
from push_runner import run_push_loop
from push_service import handle_push_command
from course_service import (
    CourseImportError,
    build_course_occurrences,
    format_course_rules,
    format_import_confirmation,
    parse_course_workbook,
    parse_course_calendar,
    parse_first_week_monday,
    format_ics_import_confirmation,
)
from subtask_service import (
    parse_subtask_command,
)
from risk_service import (
    handle_active_risk_input,
    handle_risk_command,
    run_risk_loop,
)
from pool_service import (
    format_pool_tasks,
    is_pool_command,
    prepare_tasks_for_planning,
)
from preference_service import (
    format_category_order,
    format_category_setup_prompt,
    get_category_label,
    parse_category_order_text,
    parse_category_preference_command,
)
from news_service import handle_news_command
from tone_service import get_encouragement, handle_tone_command


load_dotenv()

APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")

if not APP_ID or not APP_SECRET:
    raise RuntimeError(
        "未读取到飞书应用凭证，请检查 .env 文件。"
    )


api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.INFO)
    .build()
)


# =========================
# Conversation State
# =========================

pending_tasks = {}
confirmation_tasks = {}
confirmation_blocked_times = {}
confirmation_task_updates = {}
confirmation_task_status_changes = {}
confirmation_progress_completions = {}
confirmation_task_breakdowns = {}
ambiguous_intents = {}
pending_course_imports = {}

# 当用户通过任务名称操作，但匹配到多个任务时，
# 保存“上一轮要做什么 + 候选任务”，等待下一轮选择编号。
pending_task_selections = {}


def can_receive_proactive_risk(user_open_id: str) -> bool:
    """Avoid interrupting an unfinished multi-turn operation."""
    conversation_states = (
        pending_tasks,
        confirmation_tasks,
        confirmation_blocked_times,
        confirmation_task_updates,
        confirmation_task_status_changes,
        confirmation_progress_completions,
        confirmation_task_breakdowns,
        ambiguous_intents,
        pending_course_imports,
        pending_task_selections,
    )
    return not any(user_open_id in state for state in conversation_states)


# =========================
# Message Idempotency
# =========================

processed_messages = OrderedDict()

MAX_PROCESSED_MESSAGES = 1000
MESSAGE_TTL_SECONDS = 3600


def is_duplicate_message(
    message_id: str
) -> bool:

    if not message_id:
        return False

    now = time.time()

    expired_ids = [
        msg_id
        for msg_id, timestamp
        in processed_messages.items()
        if now - timestamp
        > MESSAGE_TTL_SECONDS
    ]

    for msg_id in expired_ids:
        processed_messages.pop(
            msg_id,
            None,
        )

    if message_id in processed_messages:

        print(
            f"检测到重复消息，已忽略：{message_id}",
            flush=True,
        )

        return True

    processed_messages[
        message_id
    ] = now

    while (
        len(processed_messages)
        > MAX_PROCESSED_MESSAGES
    ):

        processed_messages.popitem(
            last=False
        )

    return False


# =========================
# 飞书消息发送
# =========================

def send_text(
    receive_id: str,
    text: str,
) -> bool:

    try:

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(
                    json.dumps(
                        {"text": text},
                        ensure_ascii=False,
                    )
                )
                .build()
            )
            .build()
        )

        response = (
            api_client
            .im
            .v1
            .message
            .create(request)
        )

        if response.success():

            print(
                "消息已发送。",
                flush=True,
            )

            return True

        print(
            "发送消息失败："
            f"code={response.code}, "
            f"msg={response.msg}, "
            f"log_id={response.get_log_id()}",
            flush=True,
        )
        return False

    except Exception as exc:

        print(
            f"发送消息时发生错误：{exc!r}",
            flush=True,
        )
        traceback.print_exc()
        return False


# =========================
# Onboarding
# =========================

def format_onboarding_welcome() -> str:

    lines = [
        "👋 欢迎使用「今日执行 Agent」",
        "",
        "在正式开始之前，我想先了解你希望我怎样帮你安排任务。",
        "",
        "当多个任务同时需要处理时，",
        "你更希望我优先考虑什么？",
        "",
        "A｜截止时间优先",
        "越接近截止日期的任务越优先",
        "",
        "B｜重要程度优先",
        "优先保证重要任务得到充足时间",
        "",
        "C｜快速完成优先",
        "优先完成耗时较短、可以快速清掉的任务",
        "",
        "D｜平衡安排",
        "综合考虑截止时间、重要程度和预计耗时",
        "",
        "请直接回复：A、B、C 或 D",
        "",
        "这个偏好以后会作为 Agent 自动排程的重要依据。",
    ]

    return "\n".join(lines)


def normalize_onboarding_choice(
    user_text: str
):

    text = (
        user_text
        .strip()
        .lower()
    )

    strategy_map = {
        "a": "deadline",
        "a.": "deadline",
        "a、": "deadline",
        "截止时间": "deadline",
        "截止时间优先": "deadline",
        "deadline": "deadline",

        "b": "importance",
        "b.": "importance",
        "b、": "importance",
        "重要程度": "importance",
        "重要程度优先": "importance",
        "重要性优先": "importance",
        "importance": "importance",

        "c": "quick_win",
        "c.": "quick_win",
        "c、": "quick_win",
        "快速完成": "quick_win",
        "快速完成优先": "quick_win",
        "quick_win": "quick_win",

        "d": "balanced",
        "d.": "balanced",
        "d、": "balanced",
        "平衡": "balanced",
        "平衡安排": "balanced",
        "综合考虑": "balanced",
        "balanced": "balanced",
    }

    return strategy_map.get(text)


def get_strategy_display(
    strategy: str
) -> dict:

    strategy_map = {
        "deadline": {
            "name": "截止时间优先",
            "description": "排程时会更关注任务的截止时间和紧迫程度。",
        },

        "importance": {
            "name": "重要程度优先",
            "description": "排程时会优先保证重要任务获得执行时间。",
        },

        "quick_win": {
            "name": "快速完成优先",
            "description": "排程时会适当优先处理耗时较短、能够快速完成的任务。",
        },

        "balanced": {
            "name": "平衡安排",
            "description": "排程时会综合考虑截止时间、重要程度和预计耗时。",
        },
    }

    return strategy_map.get(
        strategy,
        {
            "name": strategy,
            "description": "",
        },
    )


def process_onboarding(
    sender_open_id: str,
    user_text: str,
) -> str:

    preference = get_user_preference(sender_open_id) or {}

    if (
        preference.get("priority_strategy")
        and not preference.get("onboarding_completed")
    ):
        try:
            category_order = parse_category_order_text(user_text)
        except ValueError:
            return format_category_setup_prompt()

        update_category_order(
            sender_open_id,
            category_order,
            complete_onboarding=True,
        )
        strategy_info = get_strategy_display(
            preference.get("priority_strategy")
        )
        return "\n".join(
            [
                "✅ 初始设置完成",
                "",
                f"任务排程策略：{strategy_info['name']}",
                f"事务优先级：{format_category_order(category_order)}",
                "",
                "固定安排和临近截止风险仍会优先保护。",
                "现在可以直接告诉我任务。",
            ]
        )

    strategy = normalize_onboarding_choice(user_text)

    if strategy is None:

        print(
            "用户尚未完成 Onboarding，"
            "本轮未识别到有效策略选择。",
            flush=True,
        )

        return format_onboarding_welcome()

    save_onboarding_strategy(
        sender_open_id,
        strategy,
    )

    strategy_info = (
        get_strategy_display(
            strategy
        )
    )

    print(
        "用户完成 Onboarding："
        f"user={sender_open_id}, "
        f"strategy={strategy}",
        flush=True,
    )

    lines = [
        "✅ 已记录任务排程策略",
        "",
        "你的任务安排偏好：",
        f"「{strategy_info['name']}」",
        "",
        strategy_info["description"],
        "",
        "还差最后一步：设置事务类别优先级。",
        "",
        format_category_setup_prompt(),
    ]

    return "\n".join(lines)


def handle_category_preference_command(sender_open_id: str, user_text: str):
    command = parse_category_preference_command(user_text)
    if command is None:
        return None

    preference = get_user_preference(sender_open_id) or {}
    if command["action"] == "view":
        order = preference.get("category_order")
        if not order:
            return (
                "你还没有设置事务类别优先级。\n\n"
                "可发送：\n"
                "设置事务优先级 家庭 > 健康 > 学习 > 工作与兼职 > 个人生活 > 其他"
            )
        return "你的事务优先级（从高到低）：\n\n" + format_category_order(order)

    if command.get("error"):
        return "事务优先级格式无法使用。\n\n" + format_category_setup_prompt()

    update_category_order(sender_open_id, command["order"])
    return (
        "✅ 事务优先级已更新\n\n"
        + format_category_order(command["order"])
        + "\n\n固定安排和临近截止风险仍会优先保护。"
    )


# =========================
# 展示辅助
# =========================

def get_priority_text(
    priority: str
) -> str:

    priority_map = {
        "high": "高",
        "medium": "中",
        "low": "低",
        "unknown": "未说明",
    }

    return priority_map.get(
        priority,
        "未说明",
    )


def get_category_text(category: str) -> str:
    return get_category_label(category)


def get_duration_text(
    minutes
) -> str:
    """
    将分钟转换成更自然的中文时长。

    例如：
    30 -> 30分钟
    60 -> 1小时
    75 -> 1小时15分钟
    105 -> 1小时45分钟
    150 -> 2小时30分钟
    """

    if minutes is None:
        return "未说明"

    try:
        minutes = int(minutes)

    except (
        TypeError,
        ValueError,
    ):
        return "未说明"

    if minutes < 0:
        return "未说明"

    if minutes < 60:
        return f"{minutes}分钟"

    hours = minutes // 60
    remaining_minutes = minutes % 60

    if remaining_minutes == 0:
        return f"{hours}小时"

    return (
        f"{hours}小时"
        f"{remaining_minutes}分钟"
    )


def get_deadline_text(task: dict) -> str:
    deadline = task.get("deadline")
    if deadline:
        return str(deadline)
    if task.get("scheduling_bucket") == "needs_deadline":
        return "未说明"
    return "无明确截止日期（待安排池）"


def get_missing_names(
    missing_fields: list
) -> list:

    field_name_map = {
        "deadline": "截止时间",
        "estimated_minutes": "预计耗时",
        "priority": "重要程度",
        "title": "任务内容",
    }

    return [
        field_name_map.get(
            field,
            field,
        )
        for field
        in missing_fields
    ]


# =========================
# 回复格式
# =========================

def format_pending_reply(
    task: dict
) -> str:

    title = (
        task.get("title")
        or "未识别"
    )

    deadline = get_deadline_text(task)

    duration = (
        get_duration_text(
            task.get(
                "estimated_minutes"
            )
        )
    )

    priority = (
        get_priority_text(
            task.get(
                "priority",
                "unknown",
            )
        )
    )

    missing_fields = (
        task.get(
            "missing_fields",
            [],
        )
    )

    missing_names = (
        get_missing_names(
            missing_fields
        )
    )

    lines = [
        "📌 我先记下这个任务",
        "",
        f"任务：{title}",
        f"截止时间：{deadline}",
        f"预计耗时：{duration}",
        f"重要程度：{priority}",
        f"事务类别：{get_category_text(task.get('category'))}",
        "",
        "还需要你补充：",
        "、".join(
            missing_names
        ),
        "",
        "你可以直接在下一条消息里一起告诉我。",
        "例如：周一晚上8点，预计1个半小时，比较重要。",
    ]

    return "\n".join(lines)


def format_confirmation_reply(
    task: dict
) -> str:

    title = (
        task.get("title")
        or "未识别"
    )

    deadline = get_deadline_text(task)

    duration = (
        get_duration_text(
            task.get(
                "estimated_minutes"
            )
        )
    )

    priority = (
        get_priority_text(
            task.get(
                "priority",
                "unknown",
            )
        )
    )

    lines = [
        "📋 请确认这个任务",
        "",
        f"任务：{title}",
        f"截止时间：{deadline}",
        f"预计耗时：{duration}",
        f"重要程度：{priority}",
        f"事务类别：{get_category_text(task.get('category'))}",
        "",
        "如果信息正确，请回复：确认",
        "如果不想创建，请回复：取消",
        "",
        "如果需要修改，也可以直接告诉我。",
        "例如：改成明天晚上9点。",
    ]

    return "\n".join(lines)


def format_created_reply(
    task: dict,
    task_id: int,
) -> str:

    title = (
        task.get("title")
        or "未识别"
    )

    deadline = get_deadline_text(task)

    duration = (
        get_duration_text(
            task.get(
                "estimated_minutes"
            )
        )
    )

    priority = (
        get_priority_text(
            task.get(
                "priority",
                "unknown",
            )
        )
    )

    lines = [
        "✅ 任务创建成功",
        "",
        f"任务编号：#{task_id}",
        f"任务：{title}",
        f"截止时间：{deadline}",
        f"预计耗时：{duration}",
        f"重要程度：{priority}",
        f"事务类别：{get_category_text(task.get('category'))}",
        "",
        "这个任务已经保存到数据库。",
        "即使程序关闭或电脑重启，任务也不会丢失。",
    ]

    if not task.get("deadline"):
        lines.extend(
            [
                "",
                "它已进入待安排池，只会在截止型任务安排完且仍有足够空闲时作为可选任务出现。",
            ]
        )

    lines.extend(
        [
            "",
            "你可以发送「查看我的任务」查看已保存任务。",
        ]
    )

    return "\n".join(lines)


# =========================
# 查看任务
# =========================

def format_user_tasks(
    tasks: list
) -> str:

    if not tasks:

        return (
            "📭 你目前还没有保存的任务。\n\n"
            "直接告诉我一个任务，我可以帮你创建。"
        )

    lines = [
        f"📋 我的任务（共 {len(tasks)} 个）",
        "",
    ]

    for index, task in enumerate(
        tasks,
        start=1,
    ):

        title = (
            task.get("title")
            or "未命名任务"
        )

        deadline = get_deadline_text(task)

        duration = (
            get_duration_text(
                task.get(
                    "estimated_minutes"
                )
            )
        )

        priority = (
            get_priority_text(
                task.get(
                    "priority",
                    "unknown",
                )
            )
        )

        status = (
            task.get(
                "status",
                "pending",
            )
        )

        status_map = {
            "pending": "待完成",
            "completed": "已完成",
            "cancelled": "已取消",
        }

        status_text = (
            status_map.get(
                status,
                status,
            )
        )

        task_id = (
            task.get("id")
        )

        task_remaining = task.get(
            "remaining_minutes"
        )

        task_lines = [
            f"{index}. {title}",
            f"   编号：#{task_id}",
            f"   截止：{deadline}",
            f"   耗时：{duration}",
        ]

        if not task.get("deadline"):
            task_lines.append(
                "   归属：待安排池（可选任务）"
            )

        if (
            status == "pending"
            and task_remaining is not None
            and task_remaining
            != task.get("estimated_minutes")
        ):
            task_lines.append(
                "   当前剩余："
                f"{get_duration_text(task_remaining)}"
            )

        subtask_progress = task.get(
            "subtask_progress"
        )

        if (
            subtask_progress
            and subtask_progress.get("total", 0) > 0
        ):
            task_lines.append(
                "   子任务进度："
                f"{subtask_progress.get('percent')}% "
                f"({subtask_progress.get('completed')}/"
                f"{subtask_progress.get('total')})"
            )

        task_lines.extend(
            [
                f"   重要程度：{priority}",
                f"   事务类别：{get_category_text(task.get('category'))}",
                f"   状态：{status_text}",
                "",
            ]
        )

        lines.extend(
            task_lines
        )

    return "\n".join(
        lines
    ).rstrip()


def process_view_tasks(
    sender_open_id: str
) -> str:

    print(
        "开始查询用户任务。",
        flush=True,
    )

    tasks = (
        get_user_tasks(
            sender_open_id
        )
    )

    for task in tasks:
        progress = get_task_subtask_progress(
            sender_open_id,
            task.get("id"),
        )

        if progress.get("total", 0) > 0:
            task["subtask_progress"] = progress

    print(
        f"查询完成，共找到 {len(tasks)} 个任务。",
        flush=True,
    )

    return format_user_tasks(
        tasks
    )


def process_view_pool(sender_open_id: str) -> str:
    return format_pool_tasks(
        get_user_tasks(sender_open_id)
    )


# =========================
# 智能排序
# =========================

def format_scheduled_tasks(
    tasks: list,
    strategy: str,
) -> str:

    if not tasks:

        return (
            "📭 你目前还没有可以安排的任务。\n\n"
            "先告诉我一些任务，我再帮你安排执行顺序。"
        )

    strategy_info = (
        get_strategy_display(
            strategy
        )
    )

    lines = [
        "🧭 建议执行顺序",
        "",
        f"当前排程策略：{strategy_info['name']}",
        "",
    ]

    for index, task in enumerate(
        tasks,
        start=1,
    ):

        title = (
            task.get("title")
            or "未命名任务"
        )

        deadline = get_deadline_text(task)

        duration = (
            get_duration_text(
                task.get(
                    "estimated_minutes"
                )
            )
        )

        priority = (
            get_priority_text(
                task.get(
                    "priority",
                    "unknown",
                )
            )
        )

        score = (
            task.get(
                "score",
                0,
            )
        )

        optional_text = (
            " [待安排池可选]"
            if task.get("is_optional")
            else ""
        )

        lines.extend(
            [
                f"{index}. {title}{optional_text}",
                f"   截止：{deadline}",
                f"   预计耗时：{duration}",
                f"   重要程度：{priority}",
                f"   事务类别：{get_category_text(task.get('category'))}",
                f"   综合得分：{score}",
                "",
            ]
        )

    lines.extend(
        [
            "这个顺序由你的个人偏好、",
            "截止时间、重要程度和预计耗时共同计算。",
            "无明确截止日期的任务固定排在截止型任务之后。",
            "",
            "当前阶段先给出执行优先顺序，",
            "下一步会继续生成具体的时间段安排。",
        ]
    )

    return "\n".join(lines)


def process_schedule_tasks(
    sender_open_id: str
) -> str:

    print(
        "开始生成用户任务排序。",
        flush=True,
    )

    tasks = (
        get_user_tasks(
            sender_open_id
        )
    )

    pending_only = [
        task
        for task in tasks
        if task.get("status") == "pending"
    ]

    preference = (
        get_user_preference(
            sender_open_id
        )
    )

    strategy = "balanced"

    if preference:
        strategy = (
            preference.get(
                "priority_strategy"
            )
            or "balanced"
        )

    print(
        "当前用户排程策略："
        f"{strategy}",
        flush=True,
    )

    category_order = (
        preference.get("category_order")
        if preference
        else None
    )

    sorted_tasks, _, _ = prepare_tasks_for_planning(
        pending_only,
        strategy,
        category_order,
    )

    print(
        "排序结果："
        + json.dumps(
            sorted_tasks,
            ensure_ascii=False,
        ),
        flush=True,
    )

    return (
        format_scheduled_tasks(
            sorted_tasks,
            strategy,
        )
    )


# =========================
# 今日执行计划
# =========================

def format_execution_plan(
    plan: list,
    strategy: str,
    blocked_windows: list | None = None,
    pool_tasks: list | None = None,
) -> str:

    if blocked_windows is None:
        blocked_windows = []
    if pool_tasks is None:
        pool_tasks = []

    if not plan:
        lines = [
            "📭 今天暂时没有可以安排的任务。",
        ]

        if blocked_windows:
            lines.extend(
                [
                    "",
                    "今天已记录的忙碌时间：",
                ]
            )

            for block in blocked_windows:
                start_time = (
                    block.get("start", "")[-5:]
                )
                end_time = (
                    block.get("end", "")[-5:]
                )
                title = (
                    block.get("title")
                    or "忙碌"
                )

                lines.append(
                    f"- {start_time}–{end_time} {title}"
                )

        if pool_tasks:
            lines.extend(
                [
                    "",
                    "🗂️ 待安排池中的可选任务今天暂未加入：",
                ]
            )
            for task in pool_tasks:
                lines.append(
                    f"- #{task.get('id')} {task.get('title') or '未命名任务'}"
                )

        lines.extend(
            [
                "",
                "你可以先创建任务，然后发送「安排我的今天」。",
            ]
        )

        return "\n".join(lines)

    strategy_info = get_strategy_display(
        strategy
    )

    lines = [
        "📅 今日执行计划",
        "",
        f"当前排程策略：{strategy_info['name']}",
    ]

    if blocked_windows:
        lines.extend(
            [
                "",
                "🚫 已自动避开忙碌时间：",
            ]
        )

        for block in blocked_windows:
            start_time = (
                block.get("start", "")[-5:]
            )
            end_time = (
                block.get("end", "")[-5:]
            )
            title = (
                block.get("title")
                or "忙碌"
            )

            lines.append(
                f"- {start_time}–{end_time} {title}"
            )

    lines.append("")

    planned_by_task = {}
    estimated_by_task = {}
    title_by_task = {}

    for item in plan:
        task_key = (
            item.get("task_id")
            if item.get("task_id") is not None
            else item.get("title")
        )

        planned_by_task[task_key] = (
            planned_by_task.get(task_key, 0)
            + int(item.get("planned_minutes") or 0)
        )

        estimated_by_task[task_key] = (
            int(item.get("estimated_minutes") or 0)
        )

        title_by_task[task_key] = (
            item.get("title")
            or "未命名任务"
        )

    for index, item in enumerate(
        plan,
        start=1,
    ):

        title = (
            item.get("title")
            or "未命名任务"
        )

        segment = int(
            item.get("segment")
            or 1
        )

        segment_text = (
            f"（第{segment}段）"
            if segment > 1
            else ""
        )

        start_time = (
            item.get("start_time", "")[-5:]
        )

        end_time = (
            item.get("end_time", "")[-5:]
        )

        overdue_text = (
            " ⚠️ 已逾期"
            if item.get("overdue")
            else ""
        )

        optional_text = (
            " [待安排池可选]"
            if item.get("is_optional")
            else ""
        )

        planned_minutes = int(
            item.get("planned_minutes")
            or 0
        )

        lines.extend(
            [
                f"{index}. {title}{segment_text}{overdue_text}{optional_text}",
                f"   时间：{start_time}–{end_time}",
                f"   计划执行：{get_duration_text(planned_minutes)}",
                "",
            ]
        )

    unfinished = []

    for task_key, estimated_minutes in (
        estimated_by_task.items()
    ):
        if estimated_minutes <= 0:
            continue

        planned_minutes = (
            planned_by_task.get(task_key, 0)
        )

        if planned_minutes < estimated_minutes:
            unfinished.append(
                (
                    title_by_task.get(
                        task_key,
                        "未命名任务",
                    ),
                    estimated_minutes
                    - planned_minutes,
                )
            )

    if unfinished:
        lines.extend(
            [
                "⚠️ 今天剩余时间不足：",
            ]
        )

        for title, remaining_minutes in unfinished:
            lines.append(
                f"- {title} 还剩约"
                f"{get_duration_text(remaining_minutes)}"
            )

        lines.append("")

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

    if pool_tasks:
        lines.append("🗂️ 待安排池说明：")
        if scheduled_pool_ids:
            lines.append("- 今天有足够空闲，标为“待安排池可选”的任务可以选择执行。")
        for task in unscheduled_pool:
            lines.append(
                f"- #{task.get('id')} {task.get('title') or '未命名任务'}："
                "今天空间不足，继续留在待安排池。"
            )
        lines.append("")

    lines.extend(
        [
            "任务之间已预留 15 分钟缓冲时间。",
            "当前 MVP 默认将今天的可执行时间规划到 22:00。",
        ]
    )

    return "\n".join(lines)

def process_today_plan(
    sender_open_id: str
) -> str:

    print(
        "开始生成今日执行计划。",
        flush=True,
    )

    # =========================
    # 1. 查询待办任务
    # =========================

    tasks = get_user_tasks(
        sender_open_id
    )

    pending_only = [
        task
        for task in tasks
        if task.get("status") == "pending"
    ]

    # =========================
    # 2. 查询用户排程偏好
    # =========================

    preference = get_user_preference(
        sender_open_id
    )

    strategy = "balanced"

    if preference:
        strategy = (
            preference.get(
                "priority_strategy"
            )
            or "balanced"
        )

    print(
        "今日计划使用排程策略："
        f"{strategy}",
        flush=True,
    )

    # =========================
    # 3. Scheduler 任务排序
    # =========================

    category_order = (
        preference.get("category_order")
        if preference
        else None
    )

    sorted_tasks, _, pool_tasks = prepare_tasks_for_planning(
        pending_only,
        strategy,
        category_order,
    )

    # =========================
    # 4. 查询今天的 Busy Time
    # =========================

    today_text = (
        datetime.now()
        .strftime("%Y-%m-%d")
    )

    blocked_time_rows = (
        get_user_blocked_times(
            sender_open_id,
            today_text,
        )
    )

    print(
        f"查询到今天忙碌时间："
        f"{len(blocked_time_rows)} 条",
        flush=True,
    )

    blocked_windows = []

    for item in blocked_time_rows:
        blocked_windows.append(
            {
                "title": (
                    item.get("title")
                    or "忙碌"
                ),
                "start": item.get(
                    "start_time"
                ),
                "end": item.get(
                    "end_time"
                ),
            }
        )

    print(
        "今日忙碌时间："
        + json.dumps(
            blocked_windows,
            ensure_ascii=False,
        ),
        flush=True,
    )

    # =========================
    # 5. Planner V2
    # =========================

    plan = generate_execution_plan(
        sorted_tasks,
        blocked_windows=blocked_windows,
    )

    print(
        "今日执行计划："
        + json.dumps(
            plan,
            ensure_ascii=False,
        ),
        flush=True,
    )

    save_daily_plan_snapshot(
        sender_open_id,
        today_text,
        plan,
    )

    return format_execution_plan(
        plan,
        strategy,
        blocked_windows=blocked_windows,
        pool_tasks=pool_tasks,
    )


# =========================
# Busy Time
# =========================

def looks_like_blocked_time(
    user_text: str
) -> bool:
    """
    先用轻量关键词判断是否值得调用 Busy Time LLM。

    这样普通任务不会每次都额外调用一次模型，
    可以降低延迟和 API 消耗。
    """

    text = user_text.strip()

    keywords = {
        "有课",
        "上课",
        "课程",
        "开会",
        "会议",
        "吃饭",
        "午饭",
        "晚饭",
        "早餐",
        "通勤",
        "面试",
        "约会",
        "没空",
        "没时间",
        "不能安排",
        "被占用",
        "占用",
    }

    return any(
        keyword in text
        for keyword in keywords
    )


def format_blocked_time_confirmation(
    blocked_times: list
) -> str:
    """
    生成 Busy Time 确认消息。
    """

    lines = [
        "🕒 请确认这些忙碌时间",
        "",
    ]

    for index, item in enumerate(
        blocked_times,
        start=1,
    ):
        title = (
            item.get("title")
            or "忙碌"
        )

        start_time = (
            item.get("start_time")
            or "未说明"
        )

        end_time = (
            item.get("end_time")
            or "未说明"
        )

        lines.extend(
            [
                f"{index}. {title}",
                f"   开始：{start_time}",
                f"   结束：{end_time}",
                "",
            ]
        )

    lines.extend(
        [
            "如果信息正确，请回复：确认",
            "如果不想保存，请回复：取消",
            "",
            "如果需要修改，可以直接重新描述忙碌时间。",
            "例如：改成今天下午4点到6点上课。",
        ]
    )

    return "\n".join(lines)


def process_new_blocked_time(
    sender_open_id: str,
    user_text: str,
) -> str | None:
    """
    尝试将当前消息识别为 Busy Time。

    如果不是 Busy Time，返回 None，
    交给普通任务流程继续处理。
    """

    if not looks_like_blocked_time(
        user_text
    ):
        return None

    print(
        "开始识别 Busy Time。",
        flush=True,
    )

    result = recognize_blocked_times(
        user_text
    )

    print(
        "Busy Time 识别结果："
        + json.dumps(
            result,
            ensure_ascii=False,
        ),
        flush=True,
    )

    if (
        result.get("intent")
        != "create_blocked_times"
    ):
        return None

    blocked_times = result.get(
        "blocked_times",
        [],
    )

    if not blocked_times:
        return None

    confirmation_blocked_times[
        sender_open_id
    ] = blocked_times

    print(
        "Busy Time 进入 awaiting_confirmation 状态。",
        flush=True,
    )

    return format_blocked_time_confirmation(
        blocked_times
    )


def process_blocked_time_confirmation(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    处理 Busy Time 的确认、取消或重新修改。
    """

    blocked_times = (
        confirmation_blocked_times[
            sender_open_id
        ]
    )

    normalized_text = (
        user_text
        .strip()
        .lower()
    )

    confirm_words = {
        "确认",
        "确认保存",
        "确定",
        "好的",
        "可以",
        "没问题",
        "ok",
        "yes",
    }

    if normalized_text in confirm_words:
        created_ids = []

        for item in blocked_times:
            blocked_id = create_blocked_time(
                sender_open_id,
                item.get("title") or "忙碌",
                item.get("start_time"),
                item.get("end_time"),
                source="manual",
            )

            created_ids.append(
                blocked_id
            )

        confirmation_blocked_times.pop(
            sender_open_id,
            None,
        )

        print(
            "Busy Time 已保存："
            f"ids={created_ids}",
            flush=True,
        )

        lines = [
            "✅ 忙碌时间已保存",
            "",
        ]

        for index, item in enumerate(
            blocked_times,
            start=1,
        ):
            lines.extend(
                [
                    f"{index}. {item.get('title') or '忙碌'}",
                    f"   {item.get('start_time')} → {item.get('end_time')}",
                    "",
                ]
            )

        lines.extend(
            [
                "之后发送「安排我的今天」，",
                "我会自动避开这些时间。",
            ]
        )

        # Busy Time 可能是在一个尚未结束的任务流程中插入的。
        # 不丢弃原任务状态，但明确提醒用户，避免下一次“确认”造成困惑。
        if sender_open_id in confirmation_tasks:
            task = confirmation_tasks[sender_open_id]
            lines.extend(
                [
                    "",
                    "📌 你还有一个未确认任务：",
                    f"{task.get('title') or '未命名任务'}",
                    "如需继续创建它，请回复「确认」；",
                    "不想创建请回复「取消」。",
                ]
            )

        elif sender_open_id in pending_tasks:
            task = pending_tasks[sender_open_id]
            lines.extend(
                [
                    "",
                    "📌 你之前还有一个待补充任务：",
                    f"{task.get('title') or '未命名任务'}",
                    "后续可以继续补充它的信息。",
                ]
            )

        return "\n".join(lines)

    cancel_words = {
        "取消",
        "取消保存",
        "不要了",
        "算了",
        "no",
    }

    if normalized_text in cancel_words:
        confirmation_blocked_times.pop(
            sender_open_id,
            None,
        )

        print(
            "用户取消 Busy Time 保存。",
            flush=True,
        )

        return (
            "🗑️ 已取消这次忙碌时间保存。\n\n"
            "你可以随时重新告诉我你的不可用时间。"
        )

    print(
        "Busy Time 确认阶段收到修改内容："
        f"{user_text}",
        flush=True,
    )

    updated_result = recognize_blocked_times(
        user_text
    )

    updated_items = updated_result.get(
        "blocked_times",
        [],
    )

    if (
        updated_result.get("intent")
        == "create_blocked_times"
        and updated_items
    ):
        confirmation_blocked_times[
            sender_open_id
        ] = updated_items

        return format_blocked_time_confirmation(
            updated_items
        )

    return (
        "我暂时没有识别出新的忙碌时间。\n\n"
        "请回复「确认」保存当前内容，"
        "回复「取消」放弃，"
        "或重新描述完整的时间段。"
    )



# =========================
# Task Breakdown / Subtasks
# =========================

def _find_breakdown_target(
    sender_open_id: str,
    task_id,
    original_text: str,
):
    """按编号或命令中出现的任务名定位待完成任务。"""

    if task_id is not None:
        task = get_task_by_id(
            sender_open_id,
            task_id,
        )

        if task is None:
            return (
                None,
                f"我没有找到编号 #{task_id} 的任务。",
            )

        if task.get("status") != "pending":
            return (
                None,
                f"任务 #{task_id} 当前不是「待完成」状态，"
                "不能生成新的执行步骤。",
            )

        return task, None

    normalized_command = normalize_task_title(
        original_text
    )
    normalized_reference = normalized_command

    for phrase in {
        "帮我",
        "请",
        "把",
        "这个任务",
        "任务",
        "重新拆分",
        "拆分",
        "拆小",
        "分解",
        "拆成步骤",
        "生成执行步骤",
    }:
        normalized_reference = (
            normalized_reference.replace(
                phrase,
                "",
            )
        )

    candidates = []

    for task in get_user_tasks(sender_open_id):
        if task.get("status") != "pending":
            continue

        normalized_title = normalize_task_title(
            task.get("title")
        )

        if (
            normalized_title
            and (
                normalized_title in normalized_command
                or (
                    normalized_reference
                    and normalized_reference
                    in normalized_title
                )
            )
        ):
            candidates.append(task)

    if len(candidates) == 1:
        return candidates[0], None

    if len(candidates) > 1:
        candidate_ids = [
            item.get("id")
            for item in candidates
        ]
        pending_task_selections[
            sender_open_id
        ] = {
            "operation": "task_breakdown",
            "candidate_task_ids": candidate_ids,
        }

        lines = [
            "我找到了多个可能需要拆分的任务：",
            "",
        ]

        for item in candidates:
            lines.append(
                f"- #{item.get('id')} {item.get('title')}"
            )

        lines.extend(
            [
                "",
                "请直接回复任务编号，例如：#7",
            ]
        )

        return None, "\n".join(lines)

    return (
        None,
        "我还没判断出需要拆分哪一个任务。\n\n"
        "请使用任务编号，例如：拆分 #7。",
    )


def format_task_breakdown_confirmation(
    task: dict,
    subtasks: list,
    fallback_used: bool = False,
) -> str:
    lines = [
        "🧩 AI 任务拆分建议",
        "",
        f"任务编号：#{task.get('id')}",
        f"任务：{task.get('title') or '未命名任务'}",
        "",
    ]

    if fallback_used:
        lines.extend(
            [
                "模型暂时不可用，以下为保守模板建议：",
                "",
            ]
        )

    for index, title in enumerate(
        subtasks,
        start=1,
    ):
        lines.append(
            f"{index}. {title}"
        )

    existing = get_task_subtasks(
        task.get("user_open_id", ""),
        task.get("id"),
    ) if task.get("user_open_id") else []

    if existing:
        lines.extend(
            [
                "",
                "确认后会替换这个任务现有的子任务列表。",
            ]
        )

    lines.extend(
        [
            "",
            "回复「确认拆分」保存这些步骤。",
            "回复「取消」放弃，本次不会写入数据库。",
        ]
    )

    return "\n".join(lines)


def process_task_breakdown_request(
    sender_open_id: str,
    command: dict,
) -> str:
    task, error_message = _find_breakdown_target(
        sender_open_id,
        command.get("task_id"),
        command.get("original_text") or "",
    )

    if task is None:
        return error_message

    result = generate_task_breakdown(task)
    subtasks = result.get("subtasks", [])

    confirmation_task_breakdowns[
        sender_open_id
    ] = {
        "task_id": task.get("id"),
        "subtasks": subtasks,
        "fallback_used": bool(
            result.get("fallback_used")
        ),
    }

    display_task = dict(task)
    display_task["user_open_id"] = sender_open_id

    return format_task_breakdown_confirmation(
        display_task,
        subtasks,
        fallback_used=result.get(
            "fallback_used",
            False,
        ),
    )


def process_task_breakdown_confirmation(
    sender_open_id: str,
    user_text: str,
) -> str:
    state = confirmation_task_breakdowns.get(
        sender_open_id
    )

    if not state:
        return "当前没有等待确认的任务拆分。"

    normalized = user_text.strip().lower()

    if normalized in {
        "取消",
        "取消拆分",
        "算了",
        "不要了",
        "no",
    }:
        confirmation_task_breakdowns.pop(
            sender_open_id,
            None,
        )

        return (
            "好的，已取消这次任务拆分，"
            "原任务和原有子任务保持不变。"
        )

    if normalized in {
        "重新拆分",
        "再拆一次",
        "重试",
    }:
        task = get_task_by_id(
            sender_open_id,
            state.get("task_id"),
        )

        if (
            task is None
            or task.get("status") != "pending"
        ):
            confirmation_task_breakdowns.pop(
                sender_open_id,
                None,
            )
            return "任务状态已经变化，无法重新拆分。"

        return process_task_breakdown_request(
            sender_open_id,
            {
                "action": "split",
                "task_id": task.get("id"),
                "original_text": f"重新拆分 #{task.get('id')}",
            },
        )

    if normalized not in {
        "确认",
        "确认拆分",
        "确定",
        "好的",
        "可以",
        "ok",
        "yes",
    }:
        return (
            "我正在等待你确认拆分建议。\n\n"
            "请回复「确认拆分」保存，或回复「取消」放弃。"
        )

    task_id = state.get("task_id")
    task = get_task_by_id(
        sender_open_id,
        task_id,
    )

    if (
        task is None
        or task.get("status") != "pending"
    ):
        confirmation_task_breakdowns.pop(
            sender_open_id,
            None,
        )
        return "任务状态已经变化，本次拆分没有写入。"

    try:
        subtasks = replace_task_subtasks(
            sender_open_id,
            task_id,
            state.get("subtasks", []),
        )
    except ValueError as exc:
        confirmation_task_breakdowns.pop(
            sender_open_id,
            None,
        )
        return f"保存子任务失败：{exc}"

    confirmation_task_breakdowns.pop(
        sender_open_id,
        None,
    )

    return format_subtask_list(
        task,
        subtasks,
        heading="✅ 子任务已保存",
    )


def format_subtask_list(
    task: dict,
    subtasks: list,
    heading: str = "🧩 任务执行步骤",
) -> str:
    progress = get_task_subtask_progress(
        task.get("user_open_id", ""),
        task.get("id"),
    ) if task.get("user_open_id") else {
        "completed": sum(
            1
            for item in subtasks
            if item.get("status") == "completed"
        ),
        "total": len(subtasks),
        "percent": (
            round(
                sum(
                    1
                    for item in subtasks
                    if item.get("status") == "completed"
                ) * 100 / len(subtasks)
            )
            if subtasks
            else 0
        ),
    }

    lines = [
        heading,
        "",
        f"任务编号：#{task.get('id')}",
        f"任务：{task.get('title') or '未命名任务'}",
        "整体进度："
        f"{progress.get('percent')}% "
        f"({progress.get('completed')}/{progress.get('total')})",
        "",
    ]

    for item in subtasks:
        icon = (
            "✅"
            if item.get("status") == "completed"
            else "⬜"
        )
        lines.append(
            f"{icon} #{task.get('id')}-{item.get('position')} "
            f"{item.get('title')}"
        )

    lines.extend(
        [
            "",
            f"完成某一步：#{task.get('id')}-1 完成了",
            f"查看最新进度：查看 #{task.get('id')} 子任务",
        ]
    )

    return "\n".join(lines)


def process_view_subtasks(
    sender_open_id: str,
    task_id,
) -> str:
    if task_id is None:
        return (
            "请告诉我要查看哪个任务的子任务。\n\n"
            "例如：查看 #7 子任务。"
        )

    task = get_task_by_id(
        sender_open_id,
        task_id,
    )

    if task is None:
        return f"我没有找到编号 #{task_id} 的任务。"

    subtasks = get_task_subtasks(
        sender_open_id,
        task_id,
    )

    if not subtasks:
        return (
            f"任务 #{task_id} 还没有子任务。\n\n"
            f"可以发送：拆分 #{task_id}"
        )

    display_task = dict(task)
    display_task["user_open_id"] = sender_open_id

    return format_subtask_list(
        display_task,
        subtasks,
    )


def process_subtask_status_command(
    sender_open_id: str,
    command: dict,
) -> str:
    task_id = command.get("task_id")
    position = command.get("position")
    task = get_task_by_id(
        sender_open_id,
        task_id,
    )

    if task is None:
        return f"我没有找到编号 #{task_id} 的任务。"

    if task.get("status") != "pending":
        return (
            f"任务 #{task_id} 当前不是「待完成」状态，"
            "不能修改子任务。"
        )

    subtasks = get_task_subtasks(
        sender_open_id,
        task_id,
    )
    selected = next(
        (
            item
            for item in subtasks
            if item.get("position") == position
        ),
        None,
    )

    if selected is None:
        return (
            f"我没有找到子任务 #{task_id}-{position}。\n\n"
            f"可以发送：查看 #{task_id} 子任务"
        )

    new_status = (
        "completed"
        if command.get("action") == "complete"
        else "pending"
    )

    if selected.get("status") == new_status:
        state_text = (
            "已完成"
            if new_status == "completed"
            else "未完成"
        )
        return (
            f"子任务 #{task_id}-{position} 已经是「{state_text}」状态。"
        )

    updated = update_task_subtask_status(
        sender_open_id,
        task_id,
        position,
        new_status,
    )

    if not updated:
        return "子任务状态更新失败，请稍后重试。"

    subtasks = get_task_subtasks(
        sender_open_id,
        task_id,
    )
    progress = get_task_subtask_progress(
        sender_open_id,
        task_id,
    )

    if new_status == "pending":
        pending_confirmation = (
            confirmation_progress_completions.get(
                sender_open_id
            )
        )
        if (
            pending_confirmation
            and pending_confirmation.get("task_id") == task_id
        ):
            confirmation_progress_completions.pop(
                sender_open_id,
                None,
            )

    lines = [
        "✅ 子任务状态已更新"
        if new_status == "completed"
        else "↩️ 子任务已恢复为未完成",
        "",
        f"#{task_id}-{position} {selected.get('title')}",
        "整体进度："
        f"{progress.get('percent')}% "
        f"({progress.get('completed')}/{progress.get('total')})",
    ]

    if (
        progress.get("total") > 0
        and progress.get("completed") == progress.get("total")
    ):
        confirmation_progress_completions[
            sender_open_id
        ] = {
            "task_id": task_id,
        }
        lines.extend(
            [
                "",
                "🎉 所有子任务都已完成。",
                f"是否将主任务「{task.get('title')}」标记为已完成？",
                "请回复「确认」或「取消」。",
            ]
        )
    elif new_status == "completed":
        lines.extend(
            [
                "",
                f"「{task.get('title')}」正在稳步推进，继续完成下一步。",
            ]
        )

    return "\n".join(lines)


def handle_subtask_command(
    sender_open_id: str,
    user_text: str,
):
    if (
        sender_open_id in confirmation_task_breakdowns
        and user_text.strip().lower()
        in {
            "确认",
            "确认拆分",
            "确定",
            "好的",
            "可以",
            "ok",
            "yes",
            "取消",
            "取消拆分",
            "算了",
            "不要了",
            "no",
            "重新拆分",
            "再拆一次",
            "重试",
        }
    ):
        return process_task_breakdown_confirmation(
            sender_open_id,
            user_text,
        )

    command = parse_subtask_command(
        user_text
    )

    if command is None:
        return None

    action = command.get("action")

    if action == "split":
        return process_task_breakdown_request(
            sender_open_id,
            command,
        )

    if action == "view":
        return process_view_subtasks(
            sender_open_id,
            command.get("task_id"),
        )

    if action in {
        "complete",
        "reopen",
    }:
        return process_subtask_status_command(
            sender_open_id,
            command,
        )

    return None


# =========================
# Task Execution Progress Flow
# =========================

def find_progress_target(
    sender_open_id: str,
    progress_result: dict,
):
    """
    找到用户正在反馈进度的 pending 任务。
    """

    task_id = progress_result.get(
        "task_id"
    )

    if task_id is not None:
        task = get_task_by_id(
            sender_open_id,
            task_id,
        )

        if task is None:
            return (
                None,
                f"我没有找到编号 #{task_id} 的任务。"
            )

        if task.get("status") != "pending":
            return (
                None,
                f"任务 #{task_id} 当前不是「待完成」状态，"
                "无法更新剩余工作量。"
            )

        return task, None

    task_title = progress_result.get(
        "task_title"
    )

    normalized_target = normalize_task_title(
        task_title
    )

    if not normalized_target:
        return (
            None,
            "我识别到你在反馈任务进度，"
            "但还没判断出是哪一个任务。\n\n"
            "请优先使用任务编号，例如：#7还需要30分钟。"
        )

    tasks = get_user_tasks(
        sender_open_id
    )

    matches = [
        task
        for task in tasks
        if (
            task.get("status") == "pending"
            and normalize_task_title(
                task.get("title")
            ) == normalized_target
        )
    ]

    if len(matches) == 1:
        return matches[0], None

    if len(matches) > 1:
        lines = [
            "我找到了多个同名待完成任务：",
            "",
        ]

        for task in matches:
            lines.append(
                f"- #{task.get('id')} {task.get('title')}"
            )

        lines.extend(
            [
                "",
                "请使用任务编号重新告诉我进度，",
                "例如：#7还需要30分钟。",
            ]
        )

        return None, "\n".join(lines)

    return (
        None,
        "我没有找到与这个名称匹配的待完成任务。\n\n"
        "你可以先发送「查看我的任务」确认任务编号。"
    )


def process_task_progress(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    更新任务剩余工作量，并立即重新生成今日执行计划。

    特殊规则：
    - remaining_minutes > 0：正常更新剩余工作量
    - remaining_minutes == 0：不直接改状态，进入“是否已完成”确认
    """
    print(
        "开始识别任务执行进度。",
        flush=True,
    )

    result = recognize_task_progress(
        user_text
    )

    print(
        "任务执行进度识别结果："
        + json.dumps(
            result,
            ensure_ascii=False,
        ),
        flush=True,
    )

    if (
        result.get("intent")
        != "update_task_progress"
    ):
        return (
            "我识别到你可能在反馈执行进度，"
            "但暂时没有提取出有效的剩余时长。\n\n"
            "例如：#7还需要30分钟。"
        )

    task, error_message = (
        find_progress_target(
            sender_open_id,
            result,
        )
    )

    if task is None:
        return error_message

    remaining_minutes = result.get(
        "remaining_minutes"
    )
    progress_percent = result.get("progress_percent")

    if progress_percent is not None:
        estimated_minutes = int(task.get("estimated_minutes") or 0)
        remaining_minutes = round(
            estimated_minutes * (100 - progress_percent) / 100
        )

    # =========================
    # 0 分钟 = 完成候选，但不擅自完成
    # =========================
    if remaining_minutes == 0:
        confirmation_progress_completions[
            sender_open_id
        ] = {
            "task_id": task.get("id"),
        }

        print(
            "0分钟进度进入完成确认状态："
            + json.dumps(
                confirmation_progress_completions[
                    sender_open_id
                ],
                ensure_ascii=False,
            ),
            flush=True,
        )

        lines = [
            "🏁 我检测到这个任务的剩余工作量已经是 0 分钟",
            "",
            f"任务编号：#{task.get('id')}",
            f"任务：{task.get('title') or '未命名任务'}",
            "",
            "这通常表示任务已经完成。",
            "为了避免我擅自修改任务状态，需要你确认一下：",
            "",
            "如果任务确实已经完成，请回复：确认",
            "如果任务还没有完成，请回复：取消",
        ]

        return "\n".join(lines)

    success = (
        update_task_remaining_minutes(
            sender_open_id,
            task.get("id"),
            remaining_minutes,
        )
    )

    if not success:
        return (
            "任务剩余工作量更新失败，请稍后重试。"
        )

    lines = [
        "🔄 执行进度已更新",
        "",
        f"任务编号：#{task.get('id')}",
        f"任务：{task.get('title') or '未命名任务'}",
        "原预计耗时："
        f"{get_duration_text(task.get('estimated_minutes'))}",
        "当前剩余："
        f"{get_duration_text(remaining_minutes)}",
        *(
            [f"整体进度：{progress_percent}%"]
            if progress_percent is not None
            else []
        ),
        get_encouragement(
            (get_user_preference(sender_open_id) or {}).get("assistant_tone"),
            has_progress=True,
        ),
        "",
        "我已经按最新剩余工作量重新计算今天的计划：",
        "",
        process_today_plan(
            sender_open_id
        ),
    ]

    return "\n".join(lines)


def process_progress_completion_confirmation(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    处理“剩余 0 分钟”触发的完成确认。

    Human-in-the-loop：
    用户明确确认后，才把任务状态改成 completed。
    """
    state = confirmation_progress_completions[
        sender_open_id
    ]

    normalized = (
        user_text
        .strip()
        .lower()
    )

    confirm_words = {
        "确认",
        "确定",
        "是",
        "是的",
        "对",
        "对的",
        "已经完成",
        "完成了",
        "做完了",
        "搞定了",
        "ok",
        "yes",
    }

    cancel_words = {
        "取消",
        "算了",
        "不是",
        "没有",
        "还没完成",
        "没完成",
        "no",
    }

    if normalized in cancel_words:
        confirmation_progress_completions.pop(
            sender_open_id,
            None,
        )

        return (
            "好的，这次不会把任务标记为完成。\n\n"
            "任务状态和剩余工作量都保持不变。"
        )

    if normalized not in confirm_words:
        return (
            "我正在等待你确认这个任务是否已经完成。\n\n"
            "请回复「确认」将任务标记为已完成，"
            "或回复「取消」保持原状态。"
        )

    task_id = state.get("task_id")

    task = get_task_by_id(
        sender_open_id,
        task_id,
    )

    if task is None:
        confirmation_progress_completions.pop(
            sender_open_id,
            None,
        )

        return (
            "这个任务已经不存在，无法继续操作。"
        )

    if task.get("status") != "pending":
        confirmation_progress_completions.pop(
            sender_open_id,
            None,
        )

        return (
            "这个任务的状态已经发生变化，"
            "本次完成操作没有执行。\n\n"
            "请发送「查看我的任务」查看最新状态。"
        )

    success = update_task_status(
        sender_open_id,
        task_id,
        "completed",
    )

    confirmation_progress_completions.pop(
        sender_open_id,
        None,
    )

    if not success:
        return (
            "任务完成状态更新失败，请稍后重试。"
        )

    lines = [
        "✅ 任务已完成",
        "",
        f"任务编号：#{task_id}",
        f"任务：{task.get('title') or '未命名任务'}",
        "",
        "任务状态已保存为「已完成」。",
        "之后「安排我的今天」不会再安排这个任务。",
        "",
        "我已经按最新任务状态重新计算今天的计划：",
        "",
        process_today_plan(
            sender_open_id
        ),
    ]

    return "\n".join(lines)


# =========================
# Task Update Flow
# =========================

def get_task_field_display(
    field: str,
    value,
) -> str:
    """
    将任务字段转换为适合飞书展示的文本。
    """

    if field == "estimated_minutes":
        return get_duration_text(value)

    if field == "priority":
        return get_priority_text(
            value or "unknown"
        )

    if field == "category":
        return get_category_text(value)

    if field == "deadline" and value is None:
        return "无明确截止日期（待安排池）"

    if value is None:
        return "未说明"

    return str(value)


def get_task_field_name(
    field: str,
) -> str:
    """
    任务字段中文名称。
    """

    field_map = {
        "title": "任务名称",
        "deadline": "截止时间",
        "estimated_minutes": "预计耗时",
        "priority": "重要程度",
        "category": "事务类别",
    }

    return field_map.get(
        field,
        field,
    )


def find_update_target(
    sender_open_id: str,
    update_result: dict,
):
    """
    根据 update_task 识别结果寻找要修改的任务。

    MVP 当前只允许修改 pending 任务。

    优先级：
    1. task_id 精确匹配
    2. task_title 精确匹配
    3. task_title 模糊包含匹配
    """

    task_id = update_result.get(
        "task_id"
    )

    if task_id is not None:

        task = get_task_by_id(
            sender_open_id,
            task_id,
        )

        if task is None:
            return (
                None,
                f"我没有找到编号 #{task_id} 的任务。"
            )

        if task.get("status") != "pending":
            return (
                None,
                f"任务 #{task_id} 当前状态不是「待完成」，"
                "MVP 暂时只支持修改待完成任务。"
            )

        return task, None

    task_title = update_result.get(
        "task_title"
    )

    normalized_target = normalize_task_title(
        task_title
    )

    if not normalized_target:
        return (
            None,
            "我识别到你想修改任务，但还没判断出要修改哪一个任务。\n\n"
            "你可以使用任务编号，例如：把 #7 改成明天晚上9点截止。"
        )

    tasks = get_user_tasks(
        sender_open_id
    )

    pending_list = [
        task
        for task in tasks
        if task.get("status") == "pending"
    ]

    exact_matches = []

    for task in pending_list:

        normalized_title = normalize_task_title(
            task.get("title")
        )

        if normalized_title == normalized_target:
            exact_matches.append(task)

    if len(exact_matches) == 1:
        return exact_matches[0], None

    if len(exact_matches) > 1:

        lines = [
            "我找到了多个同名待完成任务：",
            "",
        ]

        for task in exact_matches:
            lines.append(
                f"- #{task.get('id')} {task.get('title')}"
            )

        lines.extend(
            [
                "",
                "请使用任务编号修改，例如：",
                "把 #7 改成明天晚上9点截止。",
            ]
        )

        return None, "\n".join(lines)

    fuzzy_matches = []

    for task in pending_list:

        normalized_title = normalize_task_title(
            task.get("title")
        )

        if (
            normalized_target in normalized_title
            or normalized_title in normalized_target
        ):
            fuzzy_matches.append(task)

    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], None

    if len(fuzzy_matches) > 1:

        lines = [
            "我找到了多个可能的待完成任务：",
            "",
        ]

        for task in fuzzy_matches:
            lines.append(
                f"- #{task.get('id')} {task.get('title')}"
            )

        lines.extend(
            [
                "",
                "请使用任务编号告诉我具体要修改哪一个。",
            ]
        )

        return None, "\n".join(lines)

    return (
        None,
        "我没有找到与这个名称匹配的待完成任务。\n\n"
        "你可以先发送「查看我的任务」确认任务编号。"
    )


def format_task_update_confirmation(
    task: dict,
    updates: dict,
) -> str:
    """
    展示任务修改前后差异，并等待用户确认。
    """

    task_id = task.get("id")
    title = (
        task.get("title")
        or "未命名任务"
    )

    lines = [
        "✏️ 请确认任务修改",
        "",
        f"任务编号：#{task_id}",
        f"任务：{title}",
        "",
        "本次将修改：",
    ]

    for field, new_value in updates.items():

        old_value = task.get(field)

        field_name = get_task_field_name(
            field
        )

        old_text = get_task_field_display(
            field,
            old_value,
        )

        new_text = get_task_field_display(
            field,
            new_value,
        )

        lines.extend(
            [
                "",
                f"{field_name}：",
                f"修改前：{old_text}",
                f"修改后：{new_text}",
            ]
        )

    lines.extend(
        [
            "",
            "如果信息正确，请回复：确认",
            "如果不想修改，请回复：取消",
        ]
    )

    return "\n".join(lines)


def process_task_update(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    识别任务修改请求，定位任务并进入确认状态。
    """

    print(
        "开始识别任务修改请求。",
        flush=True,
    )

    result = recognize_task_update(
        user_text
    )

    print(
        "任务修改识别结果："
        + json.dumps(
            result,
            ensure_ascii=False,
        ),
        flush=True,
    )

    if result.get("intent") != "update_task":
        return (
            "我识别到你可能想修改任务，"
            "但暂时没有提取出完整的修改内容。\n\n"
            "例如：把 #7 改成明天晚上9点截止，预计1小时。"
        )

    updates = result.get(
        "updates",
        {},
    )

    if not updates:
        return (
            "我还没有识别出你具体想修改哪个字段。\n\n"
            "你可以修改任务名称、截止时间、预计耗时或重要程度。"
        )

    task, error_message = find_update_target(
        sender_open_id,
        result,
    )

    if task is None:
        return error_message

    # 过滤掉“新值和旧值完全相同”的字段，
    # 避免让用户确认一个实际上没有变化的修改。
    effective_updates = {}

    for field, new_value in updates.items():

        old_value = task.get(field)

        if old_value != new_value:
            effective_updates[
                field
            ] = new_value

    if not effective_updates:
        return (
            "这个任务当前的信息已经和你要求的一样，"
            "不需要再次修改。"
        )

    confirmation_task_updates[
        sender_open_id
    ] = {
        "task_id": task.get("id"),
        "updates": effective_updates,
    }

    print(
        "任务修改进入 awaiting_update_confirmation："
        + json.dumps(
            confirmation_task_updates[
                sender_open_id
            ],
            ensure_ascii=False,
        ),
        flush=True,
    )

    return format_task_update_confirmation(
        task,
        effective_updates,
    )


def process_task_update_confirmation(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    处理任务修改的确认或取消。
    """

    state = confirmation_task_updates[
        sender_open_id
    ]

    normalized_text = (
        user_text
        .strip()
        .lower()
    )

    confirm_words = {
        "确认",
        "确认修改",
        "确定",
        "好的",
        "可以",
        "没问题",
        "ok",
        "yes",
    }

    cancel_words = {
        "取消",
        "取消修改",
        "不要了",
        "算了",
        "no",
    }

    if normalized_text in cancel_words:

        confirmation_task_updates.pop(
            sender_open_id,
            None,
        )

        print(
            "用户取消任务修改。",
            flush=True,
        )

        return (
            "🗑️ 已取消这次任务修改。\n\n"
            "原任务信息保持不变。"
        )

    if normalized_text not in confirm_words:

        return (
            "我正在等待你确认刚才的任务修改。\n\n"
            "请回复「确认」应用修改，"
            "或回复「取消」放弃修改。"
        )

    task_id = state.get(
        "task_id"
    )

    updates = state.get(
        "updates",
        {},
    )

    before_task = get_task_by_id(
        sender_open_id,
        task_id,
    )

    if before_task is None:

        confirmation_task_updates.pop(
            sender_open_id,
            None,
        )

        return (
            "这个任务已经不存在，无法继续修改。"
        )

    if before_task.get("status") != "pending":

        confirmation_task_updates.pop(
            sender_open_id,
            None,
        )

        return (
            "这个任务的状态已经发生变化，"
            "MVP 暂时只允许修改待完成任务。\n\n"
            "请发送「查看我的任务」查看最新状态。"
        )

    success = update_task(
        task_id,
        sender_open_id,
        updates,
    )

    confirmation_task_updates.pop(
        sender_open_id,
        None,
    )

    if not success:
        return (
            "任务修改失败，请稍后重试。"
        )

    updated_task = get_task_by_id(
        sender_open_id,
        task_id,
    )

    if updated_task is None:
        return (
            "任务已经修改，但重新读取任务时出现异常。"
        )

    lines = [
        "✅ 任务修改成功",
        "",
        f"任务编号：#{task_id}",
        f"任务：{updated_task.get('title') or '未命名任务'}",
        f"截止时间：{get_deadline_text(updated_task)}",
        "预计耗时："
        f"{get_duration_text(updated_task.get('estimated_minutes'))}",
        "重要程度："
        f"{get_priority_text(updated_task.get('priority', 'unknown'))}",
        f"事务类别：{get_category_text(updated_task.get('category'))}",
        "",
        "修改结果已经保存到数据库。",
        "",
        "我已经按最新任务信息重新计算今天的计划：",
        "",
        process_today_plan(
            sender_open_id
        ),
    ]

    print(
        "任务修改闭环完成："
        f"task_id={task_id}, "
        f"updates={updates}",
        flush=True,
    )

    return "\n".join(lines)


# =========================
# Task Cancel / Restore Flow
# =========================

def find_status_change_target(
    sender_open_id: str,
    router_result: dict,
    action: str,
):
    """
    为取消 / 恢复操作寻找目标任务。

    如果名称匹配到多个任务：
    不直接丢失当前操作，
    而是保存本轮 action 和候选任务，
    等待用户下一条消息通过 #编号 继续。
    """

    task_id = router_result.get(
        "task_id"
    )

    target_status = (
        "pending"
        if action == "cancel"
        else "cancelled"
    )

    # =========================
    # 1. 优先通过任务编号定位
    # =========================

    if task_id is not None:
        try:
            task_id = int(task_id)
        except (
            TypeError,
            ValueError,
        ):
            task_id = None

    if task_id is not None:

        task = get_task_by_id(
            sender_open_id,
            task_id,
        )

        if task is None:
            return (
                None,
                f"我没有找到编号 #{task_id} 的任务。"
            )

        if task.get("status") != target_status:

            expected = (
                "待完成"
                if action == "cancel"
                else "已取消"
            )

            verb = (
                "取消"
                if action == "cancel"
                else "恢复"
            )

            return (
                None,
                f"任务 #{task_id} 当前不是「{expected}」状态，"
                f"无法执行{verb}。"
            )

        return task, None

    # =========================
    # 2. 通过任务名称定位
    # =========================

    task_title = router_result.get(
        "task_title"
    )

    normalized_target = normalize_task_title(
        task_title
    )

    if not normalized_target:

        verb = (
            "取消"
            if action == "cancel"
            else "恢复"
        )

        return (
            None,
            f"我识别到你想{verb}任务，"
            "但还没判断出是哪一个。\n\n"
            f"请使用任务编号，例如：#7{verb}掉。"
        )

    tasks = get_user_tasks(
        sender_open_id
    )

    candidates = [
        task
        for task in tasks
        if task.get("status") == target_status
    ]

    # =========================
    # 3. 精确名称匹配
    # =========================

    exact_matches = [
        task
        for task in candidates
        if normalize_task_title(
            task.get("title")
        ) == normalized_target
    ]

    if len(exact_matches) == 1:
        return exact_matches[0], None

    matches = exact_matches

    # =========================
    # 4. 模糊名称匹配
    # =========================

    if not matches:

        matches = [
            task
            for task in candidates
            if (
                normalized_target
                in normalize_task_title(
                    task.get("title")
                )
                or normalize_task_title(
                    task.get("title")
                )
                in normalized_target
            )
        ]

    if len(matches) == 1:
        return matches[0], None

    # =========================
    # 5. 多个候选 -> 保存会话状态
    # =========================

    if len(matches) > 1:

        candidate_ids = [
            task.get("id")
            for task in matches
            if task.get("id") is not None
        ]

        pending_task_selections[
            sender_open_id
        ] = {
            "operation": "status_change",
            "action": action,
            "candidate_task_ids": candidate_ids,
            "task_title": task_title,
        }

        print(
            "进入 awaiting_task_selection 状态："
            + json.dumps(
                pending_task_selections[
                    sender_open_id
                ],
                ensure_ascii=False,
            ),
            flush=True,
        )

        verb = (
            "取消"
            if action == "cancel"
            else "恢复"
        )

        lines = [
            "我找到了多个可能的任务：",
            "",
        ]

        for task in matches:
            lines.append(
                f"- #{task.get('id')} "
                f"{task.get('title')}"
            )

        lines.extend(
            [
                "",
                f"请选择你想{verb}的任务。",
                "直接回复任务编号即可，例如：#7",
            ]
        )

        return (
            None,
            "\n".join(lines),
        )

    # =========================
    # 6. 完全找不到
    # =========================

    expected = (
        "待完成"
        if action == "cancel"
        else "已取消"
    )

    return (
        None,
        f"我没有找到与这个名称匹配的"
        f"「{expected}」任务。\n\n"
        "你可以先发送「查看我的任务」确认任务编号。"
    )

def format_task_status_change_confirmation(
    task: dict,
    action: str,
) -> str:
    verb = "取消" if action == "cancel" else "恢复"
    result_text = (
        "取消后，它不会再进入「安排我的今天」的执行计划。"
        if action == "cancel"
        else "恢复后，它会重新成为待完成任务，并可再次进入后续排程。"
    )

    return "\n".join([
        f"📝 请确认任务{verb}",
        "",
        f"任务编号：#{task.get('id')}",
        f"任务：{task.get('title') or '未命名任务'}",
        f"截止时间：{task.get('deadline') or '未说明'}",
        f"预计耗时：{get_duration_text(task.get('estimated_minutes'))}",
        f"重要程度：{get_priority_text(task.get('priority', 'unknown'))}",
        "",
        result_text,
        "",
        "如果信息正确，请回复：确认",
        "如果不想继续，请回复：取消",
    ])



def process_pending_task_selection(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    处理上一轮因为同名任务产生的任务选择。

    示例：
    用户：整理项目截图取消掉
    Agent：找到 #7 / #4，请选择
    用户：#7
    Agent：恢复上一轮 cancel 操作并继续
    """

    state = pending_task_selections.get(
        sender_open_id
    )

    if not state:
        return (
            "当前没有等待选择的任务。"
        )

    text = user_text.strip()

    # 用户可以随时放弃本轮选择。
    if text.lower() in {
        "取消",
        "算了",
        "不要了",
        "退出",
        "no",
    }:
        pending_task_selections.pop(
            sender_open_id,
            None,
        )

        return (
            "好的，已取消这次任务选择。"
        )

    # 支持：
    # #7 / 7 / 选择#7 / 就7
    digits = "".join(
        char
        for char in text
        if char.isdigit()
    )

    if not digits:
        return (
            "请直接回复你要选择的任务编号。\n\n"
            "例如：#7"
        )

    try:
        selected_id = int(digits)
    except ValueError:
        return (
            "我没有识别出有效的任务编号。\n\n"
            "例如可以回复：#7"
        )

    candidate_ids = state.get(
        "candidate_task_ids",
        [],
    )

    if selected_id not in candidate_ids:

        lines = [
            f"#{selected_id} 不在刚才的候选任务中。",
            "",
            "你可以选择：",
        ]

        for task_id in candidate_ids:
            lines.append(
                f"- #{task_id}"
            )

        return "\n".join(lines)

    # 选择成功后先清理“待选择”状态，
    # 再进入下一阶段，避免状态冲突。
    pending_task_selections.pop(
        sender_open_id,
        None,
    )

    operation = state.get(
        "operation"
    )

    # =========================
    # Cancel / Restore
    # =========================

    if operation == "status_change":

        action = state.get(
            "action"
        )

        router_result = {
            "task_id": selected_id,
            "task_title": None,
        }

        print(
            "任务消歧完成："
            f"task_id={selected_id}, "
            f"action={action}",
            flush=True,
        )

        return process_task_status_change(
            sender_open_id,
            router_result,
            action,
        )

    if operation == "task_breakdown":
        return process_task_breakdown_request(
            sender_open_id,
            {
                "action": "split",
                "task_id": selected_id,
                "original_text": f"拆分 #{selected_id}",
            },
        )

    return (
        "任务已经选中，"
        "但当前操作类型暂时无法继续。\n\n"
        "请重新发送你的操作。"
    )

def process_task_status_change(
    sender_open_id: str,
    router_result: dict,
    action: str,
) -> str:
    task, error_message = find_status_change_target(
        sender_open_id,
        router_result,
        action,
    )

    if task is None:
        return error_message

    confirmation_task_status_changes[sender_open_id] = {
        "task_id": task.get("id"),
        "action": action,
    }

    print(
        "任务状态变更进入确认状态："
        + json.dumps(
            confirmation_task_status_changes[sender_open_id],
            ensure_ascii=False,
        ),
        flush=True,
    )

    return format_task_status_change_confirmation(task, action)


def process_task_status_change_confirmation(
    sender_open_id: str,
    user_text: str,
) -> str:
    state = confirmation_task_status_changes[sender_open_id]
    normalized = user_text.strip().lower()

    confirm_words = {
        "确认", "确定", "好的", "可以", "没问题", "ok", "yes",
        "确认取消", "确认恢复",
    }
    cancel_words = {
        "取消", "算了", "不要了", "no", "放弃",
    }

    if normalized in cancel_words:
        confirmation_task_status_changes.pop(sender_open_id, None)
        return "好的，已放弃这次任务状态变更，原任务保持不变。"

    if normalized not in confirm_words:
        return (
            "我正在等待你确认刚才的任务操作。\n\n"
            "请回复「确认」继续，或回复「取消」放弃。"
        )

    task_id = state.get("task_id")
    action = state.get("action")
    task = get_task_by_id(sender_open_id, task_id)

    if task is None:
        confirmation_task_status_changes.pop(sender_open_id, None)
        return "这个任务已经不存在，无法继续操作。"

    required_status = "pending" if action == "cancel" else "cancelled"
    new_status = "cancelled" if action == "cancel" else "pending"

    if task.get("status") != required_status:
        confirmation_task_status_changes.pop(sender_open_id, None)
        return (
            "这个任务的状态已经发生变化，本次操作没有执行。\n\n"
            "请发送「查看我的任务」查看最新状态。"
        )

    success = update_task_status(
        sender_open_id,
        task_id,
        new_status,
    )

    confirmation_task_status_changes.pop(sender_open_id, None)

    if not success:
        return "任务状态更新失败，请稍后重试。"

    if action == "cancel":
        lines = [
            "✅ 任务已取消",
            "",
            f"任务编号：#{task_id}",
            f"任务：{task.get('title') or '未命名任务'}",
            "",
            "任务状态已保存为「已取消」。",
            "之后「安排我的任务」和「安排我的今天」都不会再安排它。",
            "",
            f"如果之后还想继续，可以发送「恢复 #{task_id}」。",
        ]
    else:
        lines = [
            "✅ 任务已恢复",
            "",
            f"任务编号：#{task_id}",
            f"任务：{task.get('title') or '未命名任务'}",
            "",
            "任务已经重新变为「待完成」。",
            "之后「安排我的任务」和「安排我的今天」会重新使用它。",
        ]

    print(
        f"任务状态变更闭环完成：task_id={task_id}, "
        f"action={action}, status={new_status}",
        flush=True,
    )

    return "\n".join(lines)


# =========================
# Intent Router V2
# =========================

def format_ambiguous_reply(
    task_title: str,
    matched_task: dict | None = None,
) -> str:
    """
    对“完成PRD”这类歧义表达进行澄清。
    """

    if matched_task is not None:

        task_id = matched_task.get("id")
        existing_title = (
            matched_task.get("title")
            or task_title
            or "未命名任务"
        )

        lines = [
            "🤔 我需要确认一下你的意思",
            "",
            f"我发现你有一个待完成任务：#{task_id} {existing_title}",
            "",
            f"你刚才说「{task_title}」，这句话可能有两种意思：",
            "",
            "A｜这个已有任务已经完成了",
            f"将 #{task_id}「{existing_title}」标记为已完成",
            "",
            "B｜创建一个新的任务",
            f"新建「{task_title}」任务",
            "",
            "请直接回复：A 或 B",
        ]

        return "\n".join(lines)

    lines = [
        "🤔 我需要确认一下你的意思",
        "",
        f"你刚才说「{task_title}」。",
        "",
        "这句话既可能是一个新任务名称，",
        "也可能是在说某个已有任务已经完成。",
        "",
        "A｜标记已有任务为完成",
        "B｜创建一个新的任务",
        "",
        "请直接回复：A 或 B。",
        "如果选择 A，但存在多个类似任务，我会再让你选择任务编号。",
    ]

    return "\n".join(lines)


def find_pending_task_for_ambiguity(
    sender_open_id: str,
    task_title: str | None,
):
    """
    为 ambiguous 意图寻找一个最可能的 pending 任务。

    返回：
    - 唯一匹配任务
    - None（没有唯一匹配）
    """

    normalized_target = normalize_task_title(
        task_title
    )

    if not normalized_target:
        return None

    tasks = get_user_tasks(
        sender_open_id
    )

    pending_list = [
        task
        for task in tasks
        if task.get("status") == "pending"
    ]

    exact_matches = []

    for task in pending_list:

        normalized_title = normalize_task_title(
            task.get("title")
        )

        if (
            normalized_title
            == normalized_target
        ):
            exact_matches.append(task)

    if len(exact_matches) == 1:
        return exact_matches[0]

    fuzzy_matches = []

    for task in pending_list:

        normalized_title = normalize_task_title(
            task.get("title")
        )

        if (
            normalized_target in normalized_title
            or normalized_title in normalized_target
        ):
            fuzzy_matches.append(task)

    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]

    return None


def process_ambiguous_intent(
    sender_open_id: str,
    router_result: dict,
    original_text: str,
) -> str:
    """
    保存歧义状态，并向用户发起澄清。
    """

    task_title = (
        router_result.get("task_title")
        or original_text.strip()
    )

    matched_task = (
        find_pending_task_for_ambiguity(
            sender_open_id,
            task_title,
        )
    )

    ambiguous_intents[
        sender_open_id
    ] = {
        "task_title": task_title,
        "original_text": original_text,
        "matched_task_id": (
            matched_task.get("id")
            if matched_task
            else None
        ),
    }

    print(
        "进入 ambiguous_intent 状态："
        + json.dumps(
            ambiguous_intents[
                sender_open_id
            ],
            ensure_ascii=False,
        ),
        flush=True,
    )

    return format_ambiguous_reply(
        task_title,
        matched_task,
    )


def process_ambiguity_confirmation(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    处理 ambiguous 状态下的 A / B 选择。
    """

    state = ambiguous_intents[
        sender_open_id
    ]

    normalized = (
        user_text
        .strip()
        .lower()
    )

    complete_choices = {
        "a",
        "a.",
        "a、",
        "1",
        "1.",
        "1、",
        "完成",
        "已完成",
        "任务已完成",
        "标记完成",
        "标记为完成",
    }

    create_choices = {
        "b",
        "b.",
        "b、",
        "2",
        "2.",
        "2、",
        "新建",
        "新建任务",
        "创建",
        "创建任务",
        "新增任务",
    }

    cancel_choices = {
        "取消",
        "算了",
        "不要了",
        "退出",
    }

    if normalized in cancel_choices:

        ambiguous_intents.pop(
            sender_open_id,
            None,
        )

        return (
            "好的，已取消这次操作。\n\n"
            "你可以继续告诉我其他任务或安排。"
        )

    if normalized in complete_choices:

        task_id = state.get(
            "matched_task_id"
        )

        if task_id is None:

            ambiguous_intents.pop(
                sender_open_id,
                None,
            )

            return (
                "我暂时没有找到唯一匹配的待完成任务。\n\n"
                "请先发送「查看我的任务」，"
                "然后用任务编号告诉我，"
                "例如：#3完成了。"
            )

        task = get_task_by_id(
            sender_open_id,
            task_id,
        )

        if (
            not task
            or task.get("status")
            != "pending"
        ):

            ambiguous_intents.pop(
                sender_open_id,
                None,
            )

            return (
                "这个任务现在已经不是待完成状态了。\n\n"
                "你可以发送「查看我的任务」查看最新状态。"
            )

        updated = update_task_status(
            sender_open_id,
            task_id,
            "completed",
        )

        ambiguous_intents.pop(
            sender_open_id,
            None,
        )

        if not updated:
            return (
                "任务状态更新失败，请稍后重试。"
            )

        lines = [
            "✅ 任务已完成",
            "",
            f"任务编号：#{task_id}",
            f"任务：{task.get('title') or '未命名任务'}",
            "",
            "我已经把它从待完成任务中移除。",
            "",
            "我已经按最新任务状态重新计算今天的计划：",
            "",
            process_today_plan(
                sender_open_id
            ),
        ]

        return "\n".join(lines)

    if normalized in create_choices:

        original_text = (
            state.get("original_text")
            or state.get("task_title")
            or ""
        )

        ambiguous_intents.pop(
            sender_open_id,
            None,
        )

        print(
            "用户在歧义澄清中选择：创建新任务",
            flush=True,
        )

        return process_new_task(
            sender_open_id,
            original_text,
        )

    return (
        "我还在等你确认刚才的歧义表达。\n\n"
        "请回复：\n"
        "A｜将已有任务标记为已完成\n"
        "B｜创建一个新的任务\n\n"
        "如果不想继续，可以回复「取消」。"
    )


def process_router_v2_input(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    normal 状态下统一通过 Intent Router V2 分流。
    """

    print(
        "开始执行 Intent Router V2。",
        flush=True,
    )

    result = recognize_intent(
        user_text
    )

    print(
        "Intent Router V2 结果："
        + json.dumps(
            result,
            ensure_ascii=False,
        ),
        flush=True,
    )

    intent = result.get(
        "intent"
    )

    if intent == "ambiguous":

        return process_ambiguous_intent(
            sender_open_id,
            result,
            user_text,
        )

    if intent == "complete_task":

        completion_reply = (
            process_task_completion(
                sender_open_id,
                user_text,
            )
        )

        if completion_reply is not None:
            return completion_reply

        return (
            "我识别到你可能是在完成一个任务，"
            "但暂时没能匹配到具体任务。\n\n"
            "你可以发送「查看我的任务」，"
            "然后用任务编号告诉我，例如：#3完成了。"
        )

    if intent == "create_blocked_times":

        blocked_reply = (
            process_new_blocked_time(
                sender_open_id,
                user_text,
            )
        )

        if blocked_reply is not None:
            return blocked_reply

        return (
            "我识别到你在描述忙碌时间，"
            "但暂时没有提取出完整的开始和结束时间。\n\n"
            "例如可以说：今天下午3点到5点有课。"
        )

    if intent == "update_task_progress":

        return process_task_progress(
            sender_open_id,
            user_text,
        )

    if intent == "update_task":

        return process_task_update(
            sender_open_id,
            user_text,
        )

    if intent == "cancel_task":

        return process_task_status_change(
            sender_open_id,
            result,
            "cancel",
        )

    if intent == "restore_task":

        return process_task_status_change(
            sender_open_id,
            result,
            "restore",
        )

    if intent == "create_task":

        return process_new_task(
            sender_open_id,
            user_text,
        )

    # unknown：
    # 保留旧逻辑作为最后兜底，
    # 避免 Router 偶发 unknown 导致已有功能完全不可用。
    blocked_reply = process_new_blocked_time(
        sender_open_id,
        user_text,
    )

    if blocked_reply is not None:
        return blocked_reply

    completion_reply = process_task_completion(
        sender_open_id,
        user_text,
    )

    if completion_reply is not None:
        return completion_reply

    return process_new_task(
        sender_open_id,
        user_text,
    )


def process_normal_input(
    sender_open_id: str,
    user_text: str,
) -> str:
    """
    normal 状态下统一交给 Intent Router V2。
    """

    return process_router_v2_input(
        sender_open_id,
        user_text,
    )


# =========================
# Task Completion Intent
# =========================

def looks_like_task_completion(
    user_text: str
) -> bool:
    """
    轻量判断用户是否可能在表达“任务已经完成”。

    仅用于决定是否调用 Completion LLM，
    不直接决定最终意图。
    """

    text = user_text.strip().lower()

    keywords = {
        "完成了",
        "做完了",
        "已经完成",
        "已经做完",
        "搞定了",
        "搞定",
        "完成啦",
        "做完啦",
        "结束了",
        "已完成",
    }

    if any(
        keyword in text
        for keyword in keywords
    ):
        return True

    # 支持类似：
    # #3完成了
    # 任务3完成了
    if "#" in text and (
        "完成" in text
        or "做完" in text
        or "搞定" in text
    ):
        return True

    return False


def normalize_task_title(
    title: str | None
) -> str:
    """
    用于标题匹配的轻量归一化。
    """

    if not title:
        return ""

    text = (
        str(title)
        .strip()
        .lower()
        .replace(" ", "")
        .replace("，", "")
        .replace(",", "")
        .replace("。", "")
        .replace(".", "")
    )

    return text


def find_completion_target(
    sender_open_id: str,
    completion_result: dict,
):
    """
    根据 Completion Intent 在数据库中寻找目标任务。

    优先级：
    1. task_id 精确匹配
    2. task_title 精确归一化匹配
    3. task_title 包含匹配

    仅在 pending 任务中寻找。
    """

    task_id = completion_result.get(
        "task_id"
    )

    if task_id is not None:

        try:
            task_id = int(task_id)

        except (
            TypeError,
            ValueError,
        ):
            task_id = None

    if task_id is not None:

        task = get_task_by_id(
            sender_open_id,
            task_id,
        )

        if (
            task
            and task.get("status")
            == "pending"
        ):
            return task, None

        return (
            None,
            f"我没有找到编号 #{task_id} 的待完成任务。"
        )

    task_title = completion_result.get(
        "task_title"
    )

    normalized_target = (
        normalize_task_title(
            task_title
        )
    )

    if not normalized_target:

        return (
            None,
            "我识别到你完成了一个任务，但还没判断出是哪一个。"
        )

    tasks = get_user_tasks(
        sender_open_id
    )

    pending_tasks_list = [
        task
        for task in tasks
        if task.get("status")
        == "pending"
    ]

    exact_matches = []

    for task in pending_tasks_list:

        normalized_title = (
            normalize_task_title(
                task.get("title")
            )
        )

        if (
            normalized_title
            == normalized_target
        ):
            exact_matches.append(
                task
            )

    if len(exact_matches) == 1:
        return exact_matches[0], None

    if len(exact_matches) > 1:

        ids = "、".join(
            f"#{task.get('id')}"
            for task in exact_matches
        )

        return (
            None,
            "我找到了多个同名待完成任务："
            f"{ids}。\n"
            "请用任务编号告诉我，例如：#3完成了。"
        )

    fuzzy_matches = []

    for task in pending_tasks_list:

        normalized_title = (
            normalize_task_title(
                task.get("title")
            )
        )

        if (
            normalized_target
            in normalized_title
            or normalized_title
            in normalized_target
        ):
            fuzzy_matches.append(
                task
            )

    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], None

    if len(fuzzy_matches) > 1:

        lines = [
            "我找到了多个可能的任务：",
            "",
        ]

        for task in fuzzy_matches:
            lines.append(
                f"- #{task.get('id')} "
                f"{task.get('title')}"
            )

        lines.extend(
            [
                "",
                "请直接用任务编号告诉我，",
                "例如：#3完成了。",
            ]
        )

        return (
            None,
            "\n".join(lines),
        )

    return (
        None,
        "我没有找到和这个名称匹配的待完成任务。\n\n"
        "你可以先发送「查看我的任务」确认任务名称或编号。"
    )


def process_task_completion(
    sender_open_id: str,
    user_text: str,
) -> str | None:
    """
    尝试处理“任务已完成”意图。

    如果当前消息不是完成意图，
    返回 None，交给其他路由继续处理。
    """

    if not looks_like_task_completion(
        user_text
    ):
        return None

    print(
        "开始识别任务完成意图。",
        flush=True,
    )

    result = recognize_task_completion(
        user_text
    )

    print(
        "任务完成意图识别结果："
        + json.dumps(
            result,
            ensure_ascii=False,
        ),
        flush=True,
    )

    if (
        result.get("intent")
        != "complete_task"
    ):
        return None

    task, error_message = (
        find_completion_target(
            sender_open_id,
            result,
        )
    )

    if task is None:
        return error_message

    task_id = task.get("id")
    title = (
        task.get("title")
        or "未命名任务"
    )

    updated = update_task_status(
        sender_open_id,
        task_id,
        "completed",
    )

    if not updated:

        return (
            "任务状态更新失败。\n\n"
            "请稍后重试。"
        )

    print(
        "任务完成闭环成功："
        f"task_id={task_id}",
        flush=True,
    )

    lines = [
        "✅ 任务已完成",
        "",
        f"任务编号：#{task_id}",
        f"任务：{title}",
        "",
        "我已经把它从待完成任务中移除。",
        "",
        "我已经按最新任务状态重新计算今天的计划：",
        "",
        process_today_plan(
            sender_open_id
        ),
    ]

    return "\n".join(lines)


# =========================
# 全局命令
# =========================

def is_view_tasks_command(
    user_text: str
) -> bool:

    normalized_text = (
        user_text
        .strip()
        .lower()
    )

    commands = {
        "查看我的任务",
        "我的任务",
        "查看任务",
        "任务列表",
        "查看任务列表",
        "我的任务列表",
    }

    return (
        normalized_text
        in commands
    )


def is_schedule_tasks_command(
    user_text: str
) -> bool:

    normalized_text = (
        user_text
        .strip()
        .lower()
    )

    commands = {
        "安排我的任务",
        "安排任务",
        "帮我安排任务",
        "任务排序",
        "帮我排任务",
        "今天先做什么",
        "我该先做什么",
        "执行顺序",
    }

    return (
        normalized_text
        in commands
    )


def is_today_plan_command(
    user_text: str
) -> bool:

    normalized_text = (
        user_text
        .strip()
        .lower()
    )

    commands = {
        "安排我的今天",
        "安排今天",
        "帮我安排今天",
        "生成今日计划",
        "今日执行计划",
        "今天怎么安排",
        "今天怎么做",
    }

    return (
        normalized_text
        in commands
    )


# =========================
# 新任务处理
# =========================

def process_new_task(
    sender_open_id: str,
    user_text: str,
) -> str:

    print(
        "开始识别新任务。",
        flush=True,
    )

    task = (
        recognize_task(
            user_text
        )
    )

    print(
        "新任务识别结果："
        + json.dumps(
            task,
            ensure_ascii=False,
        ),
        flush=True,
    )

    if (
        task.get("intent")
        != "create_task"
    ):

        return (
            "我暂时没有判断出你是在创建任务。\n\n"
            "你可以试试这样说：\n"
            "“明天下午6点前把PRD修改完，"
            "预计需要2小时，这件事很重要。”"
        )

    missing_fields = (
        task.get(
            "missing_fields",
            [],
        )
    )

    if missing_fields:

        pending_tasks[
            sender_open_id
        ] = task

        print(
            "任务进入 collecting_task 状态。",
            flush=True,
        )

        return (
            format_pending_reply(
                task
            )
        )

    confirmation_tasks[
        sender_open_id
    ] = task

    print(
        "任务进入 awaiting_confirmation 状态。",
        flush=True,
    )

    return (
        format_confirmation_reply(
            task
        )
    )


# =========================
# 补充任务信息
# =========================

def process_pending_task(
    sender_open_id: str,
    user_text: str,
) -> str:

    existing_task = (
        pending_tasks[
            sender_open_id
        ]
    )

    print(
        "发现 collecting_task："
        + json.dumps(
            existing_task,
            ensure_ascii=False,
        ),
        flush=True,
    )

    updated_task = (
        complete_task(
            existing_task,
            user_text,
        )
    )

    print(
        "任务补全结果："
        + json.dumps(
            updated_task,
            ensure_ascii=False,
        ),
        flush=True,
    )

    missing_fields = (
        updated_task.get(
            "missing_fields",
            [],
        )
    )

    if missing_fields:

        pending_tasks[
            sender_open_id
        ] = updated_task

        return (
            format_pending_reply(
                updated_task
            )
        )

    pending_tasks.pop(
        sender_open_id,
        None,
    )

    confirmation_tasks[
        sender_open_id
    ] = updated_task

    print(
        "任务从 collecting_task "
        "进入 awaiting_confirmation。",
        flush=True,
    )

    return (
        format_confirmation_reply(
            updated_task
        )
    )


# =========================
# 确认阶段
# =========================

def process_confirmation(
    sender_open_id: str,
    user_text: str,
) -> str:

    task = (
        confirmation_tasks[
            sender_open_id
        ]
    )

    normalized_text = (
        user_text
        .strip()
        .lower()
    )

    confirm_words = {
        "确认",
        "确认创建",
        "确定",
        "好的",
        "可以",
        "没问题",
        "ok",
        "yes",
    }

    if (
        normalized_text
        in confirm_words
    ):

        print(
            "用户确认任务，准备写入数据库。",
            flush=True,
        )

        task_id = (
            create_task(
                sender_open_id,
                task,
            )
        )

        confirmation_tasks.pop(
            sender_open_id,
            None,
        )

        print(
            "任务创建完成："
            f"task_id={task_id}",
            flush=True,
        )

        print(
            "最终任务："
            + json.dumps(
                task,
                ensure_ascii=False,
            ),
            flush=True,
        )

        return (
            format_created_reply(
                task,
                task_id,
            )
        )

    cancel_words = {
        "取消",
        "取消创建",
        "不要了",
        "算了",
        "删除",
        "no",
    }

    if (
        normalized_text
        in cancel_words
    ):

        confirmation_tasks.pop(
            sender_open_id,
            None,
        )

        print(
            "用户取消任务。",
            flush=True,
        )

        return (
            "🗑️ 已取消这次任务创建。\n\n"
            "你可以随时继续告诉我新的任务。"
        )

    print(
        "确认阶段收到修改内容："
        f"{user_text}",
        flush=True,
    )

    updated_task = (
        complete_task(
            task,
            user_text,
        )
    )

    print(
        "修改后任务："
        + json.dumps(
            updated_task,
            ensure_ascii=False,
        ),
        flush=True,
    )

    missing_fields = (
        updated_task.get(
            "missing_fields",
            [],
        )
    )

    if missing_fields:

        confirmation_tasks.pop(
            sender_open_id,
            None,
        )

        pending_tasks[
            sender_open_id
        ] = updated_task

        print(
            "修改导致字段缺失，"
            "重新进入 collecting_task。",
            flush=True,
        )

        return (
            format_pending_reply(
                updated_task
            )
        )

    confirmation_tasks[
        sender_open_id
    ] = updated_task

    return (
        format_confirmation_reply(
            updated_task
        )
    )


# =========================
# 飞书事件处理
# =========================

MAX_COURSE_FILE_BYTES = 5 * 1024 * 1024


def process_course_file_message(
    sender_open_id: str,
    message_id: str,
    content: dict,
) -> str:
    file_key = str(content.get("file_key") or "")
    file_name = str(content.get("file_name") or "")

    if not file_key:
        return "没有读取到附件标识，请重新发送课表文件。"

    lower_file_name = file_name.lower()
    if not lower_file_name.endswith((".xlsx", ".ics")):
        return "当前支持教务系统导出的 .xlsx 或 .ics 课表文件。"

    request = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type("file")
        .build()
    )

    try:
        response = (
            api_client
            .im
            .v1
            .message_resource
            .get(request)
        )
    except Exception as exc:
        print(
            f"下载飞书课表附件时发生错误：{exc!r}",
            flush=True,
        )
        return "课表附件下载失败，请稍后重新发送文件。"

    if not response.success() or response.file is None:
        print(
            "下载飞书课表附件失败："
            f"code={response.code}, "
            f"msg={response.msg}, "
            f"log_id={response.get_log_id()}",
            flush=True,
        )
        return (
            "课表附件下载失败。请检查机器人是否具有"
            "读取消息资源的权限，然后重新发送文件。"
        )

    file_bytes = response.file.read()

    if len(file_bytes) > MAX_COURSE_FILE_BYTES:
        return "课表文件超过 5 MB，请上传原始的精简课表文件。"

    try:
        course_data = (
            parse_course_calendar(file_bytes)
            if lower_file_name.endswith(".ics")
            else parse_course_workbook(file_bytes)
        )
    except CourseImportError as exc:
        return f"课表解析失败：{exc}"

    if lower_file_name.endswith(".ics"):
        pending_course_imports[sender_open_id] = {
            "phase": "awaiting_confirmation",
            "course_data": course_data,
            "file_name": file_name,
            "occurrences": course_data["occurrences"],
        }
        return format_ics_import_confirmation(course_data)

    pending_course_imports[sender_open_id] = {
        "phase": "awaiting_first_week",
        "course_data": course_data,
        "file_name": file_name,
    }

    return (
        "📄 课表文件解析完成\n\n"
        f"{format_course_rules(course_data)}\n\n"
        "请告诉我本学期第一周星期一的日期，"
        "例如：2026-09-21。\n"
        "不想继续可回复「取消」。"
    )


def process_course_import_input(
    sender_open_id: str,
    user_text: str,
) -> str:
    state = pending_course_imports[sender_open_id]
    normalized_text = user_text.strip().lower()

    if normalized_text in {
        "取消",
        "取消导入",
        "不要了",
        "算了",
        "no",
    }:
        pending_course_imports.pop(sender_open_id, None)
        return "已取消本次课表导入。"

    if state["phase"] == "awaiting_first_week":
        try:
            first_week_monday = parse_first_week_monday(user_text)
            occurrences = build_course_occurrences(
                state["course_data"],
                first_week_monday,
            )
        except CourseImportError as exc:
            return f"第一周日期无法使用：{exc}"

        state["phase"] = "awaiting_confirmation"
        state["first_week_monday"] = first_week_monday
        state["occurrences"] = occurrences

        return format_import_confirmation(
            state["course_data"],
            occurrences,
            first_week_monday,
        )

    if normalized_text in {
        "确认",
        "确认导入",
        "确定",
        "好的",
        "可以",
        "ok",
        "yes",
    }:
        course_data = state["course_data"]
        occurrences = state["occurrences"]
        source = f"course:{course_data['semester']}"
        imported_count = replace_blocked_times_for_source(
            sender_open_id,
            source,
            occurrences,
        )
        pending_course_imports.pop(sender_open_id, None)

        return (
            "✅ 课表导入完成\n\n"
            f"学期：{course_data['semester']}\n"
            f"已写入固定课程安排：{imported_count} 条\n\n"
            "之后生成今日计划时，我会自动避开这些课程。"
        )

    return "请回复「确认导入」写入课表，或回复「取消」。"


def handle_message(
    data: P2ImMessageReceiveV1
) -> None:

    try:

        event = data.event
        message = event.message

        message_id = (
            message.message_id
        )

        if (
            is_duplicate_message(
                message_id
            )
        ):
            return

        if message.chat_type != "p2p":

            print(
                "忽略非私聊消息："
                f"chat_type={message.chat_type}, "
                f"message_type={message.message_type}",
                flush=True,
            )

            return

        content = (
            json.loads(
                message.content
                or "{}"
            )
        )

        sender_open_id = (
            event
            .sender
            .sender_id
            .open_id
        )

        if not sender_open_id:

            print(
                "消息缺少发送者 open_id，已忽略。",
                flush=True,
            )

            return

        if message.message_type == "file":
            if not has_completed_onboarding(sender_open_id):
                send_text(
                    sender_open_id,
                    "请先完成首次使用设置，再上传课表文件。",
                )
                return

            reply = process_course_file_message(
                sender_open_id,
                message_id,
                content,
            )
            send_text(sender_open_id, reply)
            return

        if message.message_type != "text":
            print(
                "忽略暂不支持的消息类型："
                f"message_type={message.message_type}",
                flush=True,
            )
            return

        user_text = str(content.get("text", "")).strip()

        if not user_text:
            print("消息缺少文本，已忽略。", flush=True)
            return

        print(f"收到用户消息：{user_text}", flush=True)

        # =====================
        # Onboarding Router
        # =====================

        onboarding_completed = (
            has_completed_onboarding(
                sender_open_id
            )
        )

        if not onboarding_completed:

            print(
                "当前用户尚未完成 Onboarding。",
                flush=True,
            )

            reply = (
                process_onboarding(
                    sender_open_id,
                    user_text,
                )
            )

            send_text(
                sender_open_id,
                reply,
            )

            return

        # =====================
        # Global Command Router
        # =====================

        category_reply = handle_category_preference_command(
            sender_open_id,
            user_text,
        )

        if category_reply is not None:
            print("识别到全局命令：事务优先级", flush=True)
            send_text(sender_open_id, category_reply)
            return

        news_reply = handle_news_command(sender_open_id, user_text)
        if news_reply is not None:
            print("识别到全局命令：兴趣资讯设置", flush=True)
            send_text(sender_open_id, news_reply)
            return

        tone_reply = handle_tone_command(sender_open_id, user_text)
        if tone_reply is not None:
            print("识别到全局命令：助手语气设置", flush=True)
            send_text(sender_open_id, tone_reply)
            return

        push_reply = handle_push_command(
            sender_open_id,
            user_text,
        )

        if push_reply is not None:

            print(
                "识别到全局命令：推送设置",
                flush=True,
            )

            send_text(
                sender_open_id,
                push_reply,
            )

            return

        subtask_reply = handle_subtask_command(
            sender_open_id,
            user_text,
        )

        if subtask_reply is not None:

            print(
                "识别到全局命令：任务拆分 / 子任务",
                flush=True,
            )

            send_text(
                sender_open_id,
                subtask_reply,
            )

            return

        risk_command_reply = handle_risk_command(
            sender_open_id,
            user_text,
        )

        if risk_command_reply is not None:

            print(
                "识别到全局命令：查看补救计划",
                flush=True,
            )

            send_text(
                sender_open_id,
                risk_command_reply,
            )

            return

        if is_pool_command(user_text):

            print(
                "识别到全局命令：查看待安排池",
                flush=True,
            )

            send_text(
                sender_open_id,
                process_view_pool(sender_open_id),
            )

            return

        if (
            is_view_tasks_command(
                user_text
            )
        ):

            print(
                "识别到全局命令：查看我的任务",
                flush=True,
            )

            reply = (
                process_view_tasks(
                    sender_open_id
                )
            )

            send_text(
                sender_open_id,
                reply,
            )

            return

        if (
            is_today_plan_command(
                user_text
            )
        ):

            print(
                "识别到全局命令：安排我的今天",
                flush=True,
            )

            reply = (
                process_today_plan(
                    sender_open_id
                )
            )

            send_text(
                sender_open_id,
                reply,
            )

            return

        if (
            is_schedule_tasks_command(
                user_text
            )
        ):

            print(
                "识别到全局命令：安排我的任务",
                flush=True,
            )

            reply = (
                process_schedule_tasks(
                    sender_open_id
                )
            )

            send_text(
                sender_open_id,
                reply,
            )

            return

        risk_reply = handle_active_risk_input(
            sender_open_id,
            user_text,
        )

        if risk_reply is not None:

            print(
                "当前状态：risk_rescue_conversation",
                flush=True,
            )

            send_text(
                sender_open_id,
                risk_reply,
            )

            return

        # =====================
        # State-first Routing
        # =====================

        # 重要原则：
        # 已经进入多轮会话状态后，优先由 State Router 继续处理。
        #
        # Intent Router V2 只负责 normal 状态下的“首次分流”，
        # 不再对 collecting_task / awaiting_task_confirmation
        # 每一轮重新做顶层意图判断。
        #
        # 这样：
        # “整理项目截图”
        # -> collecting_task
        # “明天晚上8点前，预计30分钟，比较重要”
        # -> 一定交给 complete_task() 补全上一轮任务，
        # 而不会被 Intent Router 当成一条新消息重新路由。
        #
        # 全局命令（查看任务 / 安排今天 / 安排任务）
        # 已经在上方优先处理，因此仍然可以随时打断当前状态。

        # =====================
        # State Router
        # =====================

        if sender_open_id in pending_course_imports:

            print(
                "当前状态：course_import",
                flush=True,
            )

            reply = process_course_import_input(
                sender_open_id,
                user_text,
            )

        elif (
            sender_open_id
            in pending_task_selections
        ):

            print(
                "当前状态：awaiting_task_selection",
                flush=True,
            )

            reply = (
                process_pending_task_selection(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in confirmation_task_breakdowns
        ):

            print(
                "当前状态：awaiting_task_breakdown_confirmation",
                flush=True,
            )

            reply = (
                process_task_breakdown_confirmation(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in confirmation_progress_completions
        ):

            print(
                "当前状态：awaiting_progress_completion_confirmation",
                flush=True,
            )

            reply = (
                process_progress_completion_confirmation(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in confirmation_task_status_changes
        ):

            print(
                "当前状态：awaiting_task_status_change_confirmation",
                flush=True,
            )

            reply = (
                process_task_status_change_confirmation(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in confirmation_task_updates
        ):

            print(
                "当前状态：awaiting_update_confirmation",
                flush=True,
            )

            reply = (
                process_task_update_confirmation(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in ambiguous_intents
        ):

            print(
                "当前状态：awaiting_ambiguity_confirmation",
                flush=True,
            )

            reply = (
                process_ambiguity_confirmation(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in confirmation_blocked_times
        ):

            print(
                "当前状态：awaiting_blocked_time_confirmation",
                flush=True,
            )

            reply = (
                process_blocked_time_confirmation(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in confirmation_tasks
        ):

            print(
                "当前状态：awaiting_task_confirmation",
                flush=True,
            )

            reply = (
                process_confirmation(
                    sender_open_id,
                    user_text,
                )
            )

        elif (
            sender_open_id
            in pending_tasks
        ):

            print(
                "当前状态：collecting_task",
                flush=True,
            )

            reply = (
                process_pending_task(
                    sender_open_id,
                    user_text,
                )
            )

        else:

            print(
                "当前状态：normal",
                flush=True,
            )

            reply = (
                process_normal_input(
                    sender_open_id,
                    user_text,
                )
            )

        send_text(
            sender_open_id,
            reply,
        )

    except Exception as exc:

        print(
            f"处理消息时发生错误：{exc!r}",
            flush=True,
        )

        traceback.print_exc()


# =========================
# 飞书事件注册
# =========================

event_handler = (
    lark.EventDispatcherHandler
    .builder("", "")
    .register_p2_im_message_receive_v1(
        handle_message
    )
    .build()
)


# =========================
# 启动
# =========================

def main() -> None:

    print(
        "今日执行 Agent 正在启动……",
        flush=True,
    )

    init_db()

    push_thread = threading.Thread(
        target=run_push_loop,
        args=(send_text,),
        name="active-push-runner",
        daemon=True,
    )
    push_thread.start()

    risk_thread = threading.Thread(
        target=run_risk_loop,
        args=(send_text,),
        kwargs={
            "can_alert_func": can_receive_proactive_risk,
        },
        name="risk-reminder-runner",
        daemon=True,
    )
    risk_thread.start()

    print(
        "晨间计划 / 晚间总结主动推送已启动。",
        flush=True,
    )

    print(
        "任务风险主动提醒与补救计划已启动。",
        flush=True,
    )

    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )

    print(
        "长连接客户端已创建，正在连接飞书……",
        flush=True,
    )

    ws_client.start()


if __name__ == "__main__":
    main()
