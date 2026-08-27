from datetime import datetime


# =========================
# Priority Score
# =========================

PRIORITY_SCORE = {
    "high": 3,
    "medium": 2,
    "low": 1,
    "unknown": 0,
}


def parse_deadline(deadline):
    """
    将数据库中的 deadline 转换为 datetime。
    """
    if not deadline:
        return None

    try:
        return datetime.strptime(
            deadline,
            "%Y-%m-%d %H:%M",
        )
    except ValueError:
        return None


def hours_until_deadline(deadline):
    """
    计算距离截止时间还有多少小时。
    """

    deadline_time = parse_deadline(deadline)

    if deadline_time is None:
        return None

    delta = deadline_time - datetime.now()

    return delta.total_seconds() / 3600


# =========================
# Scoring
# =========================

def deadline_score(task):
    """
    截止时间得分。
    越紧急，分数越高。
    """

    hours = hours_until_deadline(
        task.get("deadline")
    )

    if hours is None:
        return 0

    if hours <= 0:
        return 100

    if hours <= 6:
        return 90

    if hours <= 24:
        return 75

    if hours <= 72:
        return 55

    if hours <= 168:
        return 35

    return 15


def importance_score(task):
    """
    重要程度得分。
    """

    priority = task.get(
        "priority",
        "unknown",
    )

    return PRIORITY_SCORE.get(
        priority,
        0,
    ) * 30


def quick_win_score(task):
    """
    快速完成得分。
    耗时越短，分数越高。
    """

    minutes = task.get(
        "estimated_minutes"
    )

    if not minutes:
        return 0

    if minutes <= 30:
        return 90

    if minutes <= 60:
        return 75

    if minutes <= 120:
        return 55

    if minutes <= 240:
        return 35

    return 15


def category_preference_score(task, category_order=None):
    """Return a small tie-breaking bonus from the user's own category order."""
    if not isinstance(category_order, (list, tuple)):
        return 0

    category = str(task.get("category") or "other")
    try:
        index = category_order.index(category)
    except ValueError:
        return 0

    # Six categories receive 5..0 points. This can order otherwise comparable
    # tasks, but cannot outweigh a materially closer deadline.
    return max(0, len(category_order) - index - 1)


# =========================
# Strategy
# =========================

def calculate_score(
    task,
    strategy="balanced",
    category_order=None,
):
    """
    根据用户策略计算任务总分。
    """

    deadline = deadline_score(task)
    importance = importance_score(task)
    quick_win = quick_win_score(task)
    category = category_preference_score(task, category_order)

    if strategy == "deadline":

        score = (
            deadline * 0.70
            + importance * 0.20
            + quick_win * 0.10
        )

    elif strategy == "importance":

        score = (
            deadline * 0.20
            + importance * 0.70
            + quick_win * 0.10
        )

    elif strategy == "quick_win":

        score = (
            deadline * 0.15
            + importance * 0.15
            + quick_win * 0.70
        )

    else:
        # balanced

        score = (
            deadline * 0.40
            + importance * 0.35
            + quick_win * 0.25
        )

    return round(
        score + category,
        2,
    )


# =========================
# Sort Tasks
# =========================

def sort_tasks(
    tasks,
    strategy="balanced",
    category_order=None,
):
    """
    按照用户偏好对任务排序。
    """

    scored_tasks = []

    for task in tasks:

        item = dict(task)

        item["score"] = calculate_score(
            item,
            strategy,
            category_order,
        )

        item["category_preference_score"] = category_preference_score(
            item,
            category_order,
        )

        scored_tasks.append(item)

    scored_tasks.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    return scored_tasks


# =========================
# Local Test
# =========================

if __name__ == "__main__":

    test_tasks = [
        {
            "title": "修改AI产品经理作品集",
            "deadline": "2026-08-18 18:00",
            "estimated_minutes": 120,
            "priority": "high",
        },
        {
            "title": "整理项目截图",
            "deadline": "2026-08-22 20:00",
            "estimated_minutes": 30,
            "priority": "medium",
        },
        {
            "title": "完成PRD",
            "deadline": "2026-08-19 21:00",
            "estimated_minutes": 180,
            "priority": "high",
        },
        {
            "title": "整理面试问题",
            "deadline": "2026-08-25 20:00",
            "estimated_minutes": 60,
            "priority": "medium",
        },
    ]

    print(
        "\n===== 平衡安排测试 ====="
    )

    result = sort_tasks(
        test_tasks,
        "balanced",
    )

    for index, task in enumerate(
        result,
        start=1,
    ):
        print(
            f"{index}. "
            f"{task['title']} "
            f"| score={task['score']}"
        )
