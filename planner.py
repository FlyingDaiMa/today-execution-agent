from datetime import datetime, timedelta


# =========================
# Config
# =========================

DEFAULT_START_HOUR = 9
DEFAULT_END_HOUR = 22

BREAK_MINUTES = 15


# =========================
# Time Helpers
# =========================

def round_up_time(current_time):
    """
    向上取整到最近的 15 分钟。
    """

    discard = timedelta(
        minutes=current_time.minute % 15,
        seconds=current_time.second,
        microseconds=current_time.microsecond,
    )

    if discard == timedelta(0):
        return current_time

    return (
        current_time
        - discard
        + timedelta(minutes=15)
    )


def get_planning_start(now=None):
    """
    获取当天计划开始时间。
    """

    if now is None:
        now = datetime.now()

    day_start = now.replace(
        hour=DEFAULT_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )

    if now < day_start:
        return day_start

    return round_up_time(now)


def parse_datetime(text):
    """
    解析：
    2026-08-17 15:00
    """

    try:
        return datetime.strptime(
            text,
            "%Y-%m-%d %H:%M",
        )
    except (TypeError, ValueError):
        return None


# =========================
# Task Helpers
# =========================

def is_overdue(task, now=None):
    """
    判断任务是否已经逾期。
    """

    if now is None:
        now = datetime.now()

    deadline_time = parse_datetime(
        task.get("deadline")
    )

    if deadline_time is None:
        return False

    return deadline_time < now


# =========================
# Availability
# =========================

def build_free_windows(
    now=None,
    blocked_windows=None,
):
    """
    根据忙碌时间生成今天剩余的可用时间段。

    blocked_windows 格式：

    [
        {
            "start": "2026-08-17 15:00",
            "end": "2026-08-17 17:00",
            "title": "上课",
        }
    ]
    """

    if now is None:
        now = datetime.now()

    if blocked_windows is None:
        blocked_windows = []

    planning_start = get_planning_start(
        now
    )

    day_end = now.replace(
        hour=DEFAULT_END_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )

    if planning_start >= day_end:
        return []

    parsed_blocks = []

    for block in blocked_windows:

        start = parse_datetime(
            block.get("start")
        )

        end = parse_datetime(
            block.get("end")
        )

        if start is None or end is None:
            continue

        if end <= planning_start:
            continue

        if start >= day_end:
            continue

        start = max(
            start,
            planning_start,
        )

        end = min(
            end,
            day_end,
        )

        if start < end:
            parsed_blocks.append(
                {
                    "start": start,
                    "end": end,
                    "title": block.get(
                        "title",
                        "忙碌",
                    ),
                }
            )

    parsed_blocks.sort(
        key=lambda item: item["start"]
    )

    # 合并重叠的忙碌时间
    merged_blocks = []

    for block in parsed_blocks:

        if not merged_blocks:

            merged_blocks.append(
                block
            )

            continue

        last = merged_blocks[-1]

        if block["start"] <= last["end"]:

            last["end"] = max(
                last["end"],
                block["end"],
            )

        else:

            merged_blocks.append(
                block
            )

    # 从忙碌时间反推出空闲时间
    free_windows = []

    cursor = planning_start

    for block in merged_blocks:

        if cursor < block["start"]:

            free_windows.append(
                {
                    "start": cursor,
                    "end": block["start"],
                }
            )

        cursor = max(
            cursor,
            block["end"],
        )

    if cursor < day_end:

        free_windows.append(
            {
                "start": cursor,
                "end": day_end,
            }
        )

    return free_windows


def get_remaining_capacity_minutes(
    free_windows,
    window_index,
    current_time,
):
    """Return usable minutes from the current cursor to the end of today."""
    total = 0
    for index in range(window_index, len(free_windows)):
        window = free_windows[index]
        start = window["start"]
        if index == window_index:
            start = max(start, current_time)
        if start < window["end"]:
            total += int((window["end"] - start).total_seconds() / 60)
    return total


# =========================
# Execution Planner V2
# =========================

def generate_execution_plan(
    sorted_tasks,
    now=None,
    blocked_windows=None,
):
    """
    根据：
    - Scheduler 排序
    - 当前时间
    - 用户忙碌时间

    生成今日执行计划。

    V2 规则：

    1. 避开 blocked_windows
    2. 长任务允许拆分
    3. 不安排到 22:00 之后
    4. 不同任务之间预留 15 分钟
    5. 标记逾期任务
    """

    if now is None:
        now = datetime.now()

    free_windows = build_free_windows(
        now=now,
        blocked_windows=blocked_windows,
    )

    plan = []

    window_index = 0

    if not free_windows:
        return plan

    current_time = free_windows[0]["start"]

    for task in sorted_tasks:

        estimated_minutes = task.get(
            "estimated_minutes"
        )

        task_remaining_minutes = task.get(
            "remaining_minutes"
        )

        # 执行反馈上线后：
        # 优先按照“当前剩余工作量”排程。
        # 老数据没有 remaining_minutes 时，
        # 才回退到原预计耗时。
        work_minutes = (
            task_remaining_minutes
            if task_remaining_minutes is not None
            else estimated_minutes
        )

        if not work_minutes:
            continue

        remaining_minutes = int(
            work_minutes
        )

        # 待安排池任务属于可选任务：只有今天剩余空闲能完整容纳时才加入。
        # 它们不得以零碎片段挤占有截止日期的任务。
        if task.get("is_optional"):
            available_optional_minutes = get_remaining_capacity_minutes(
                free_windows,
                window_index,
                current_time,
            )
            if available_optional_minutes < remaining_minutes:
                continue

        segment_number = 1

        while (
            remaining_minutes > 0
            and window_index < len(free_windows)
        ):

            window = free_windows[
                window_index
            ]

            if current_time < window["start"]:
                current_time = window["start"]

            if current_time >= window["end"]:

                window_index += 1

                if window_index < len(
                    free_windows
                ):
                    current_time = (
                        free_windows[
                            window_index
                        ]["start"]
                    )

                continue

            available_minutes = int(
                (
                    window["end"]
                    - current_time
                ).total_seconds()
                / 60
            )

            if available_minutes <= 0:

                window_index += 1

                continue

            planned_minutes = min(
                remaining_minutes,
                available_minutes,
            )

            task_start = current_time

            task_end = (
                task_start
                + timedelta(
                    minutes=planned_minutes
                )
            )

            item = {
                "task_id": task.get("id"),
                "title": task.get("title"),

                "segment": segment_number,

                "start_time": (
                    task_start.strftime(
                        "%Y-%m-%d %H:%M"
                    )
                ),

                "end_time": (
                    task_end.strftime(
                        "%Y-%m-%d %H:%M"
                    )
                ),

                "planned_minutes": (
                    planned_minutes
                ),

                "estimated_minutes": (
                    work_minutes
                ),

                "original_estimated_minutes": (
                    estimated_minutes
                ),

                "remaining_minutes": (
                    task_remaining_minutes
                ),

                "overdue": is_overdue(
                    task,
                    now,
                ),

                "score": task.get(
                    "score"
                ),

                "category": task.get(
                    "category",
                    "other",
                ),

                "is_optional": bool(
                    task.get("is_optional")
                ),
            }

            plan.append(item)

            remaining_minutes -= (
                planned_minutes
            )

            segment_number += 1

            current_time = task_end

            # 当前空闲窗口已经用完
            if current_time >= window["end"]:

                window_index += 1

                if window_index < len(
                    free_windows
                ):
                    current_time = (
                        free_windows[
                            window_index
                        ]["start"]
                    )

            # 一个任务完整结束后，
            # 给下一个任务预留缓冲时间
            if remaining_minutes <= 0:

                current_time = (
                    current_time
                    + timedelta(
                        minutes=BREAK_MINUTES
                    )
                )

    return plan


# =========================
# Local Test
# =========================

if __name__ == "__main__":

    test_now = datetime(
        2026,
        8,
        17,
        14,
        0,
    )

    test_tasks = [
        {
            "id": 2,
            "title": "修改AI产品经理作品集",
            "deadline": "2026-08-16 18:00",
            "estimated_minutes": 120,
            "priority": "high",
            "score": 85.25,
        },
        {
            "id": 4,
            "title": "整理项目截图",
            "deadline": "2026-08-22 20:00",
            "estimated_minutes": 30,
            "priority": "high",
            "score": 68.0,
        },
        {
            "id": 3,
            "title": "完成PRD",
            "deadline": "2026-08-19 21:00",
            "estimated_minutes": 180,
            "priority": "high",
            "score": 62.25,
        },
    ]

    test_blocked_windows = [
        {
            "start": "2026-08-17 15:00",
            "end": "2026-08-17 17:00",
            "title": "上课",
        },
        {
            "start": "2026-08-17 18:00",
            "end": "2026-08-17 19:00",
            "title": "晚饭",
        },
    ]

    result = generate_execution_plan(
        test_tasks,
        now=test_now,
        blocked_windows=(
            test_blocked_windows
        ),
    )

    print(
        "\n===== "
        "Execution Planner V2 测试 "
        "====="
    )

    print(
        "\n忙碌时间："
    )

    for block in test_blocked_windows:

        print(
            f"- {block['start']} "
            f"至 {block['end']} "
            f"| {block['title']}"
        )

    print(
        "\n生成计划："
    )

    for index, item in enumerate(
        result,
        start=1,
    ):

        overdue_text = (
            " [已逾期]"
            if item["overdue"]
            else ""
        )

        segment_text = ""

        if item["segment"] > 1:

            segment_text = (
                f"（第{item['segment']}段）"
            )

        print(
            f"{index}. "
            f"{item['start_time']} - "
            f"{item['end_time']} | "
            f"{item['title']}"
            f"{segment_text}"
            f"{overdue_text}"
        )
