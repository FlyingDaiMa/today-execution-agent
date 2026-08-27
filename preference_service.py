import re


CATEGORY_KEYS = (
    "health",
    "family",
    "study",
    "work",
    "personal",
    "other",
)

CATEGORY_LABELS = {
    "health": "健康",
    "family": "家庭",
    "study": "学习",
    "work": "工作与兼职",
    "personal": "个人生活",
    "other": "其他",
}

_CATEGORY_ALIASES = {
    "健康": "health",
    "健康方面": "health",
    "health": "health",
    "家庭": "family",
    "家庭事务": "family",
    "family": "family",
    "学习": "study",
    "学习方面": "study",
    "study": "study",
    "工作": "work",
    "兼职": "work",
    "工作与兼职": "work",
    "工作兼职": "work",
    "work": "work",
    "个人": "personal",
    "生活": "personal",
    "个人生活": "personal",
    "personal": "personal",
    "其他": "other",
    "其他事务": "other",
    "other": "other",
}

VIEW_CATEGORY_COMMANDS = {
    "查看事务优先级",
    "查看类别优先级",
    "我的事务优先级",
}

SET_CATEGORY_PREFIXES = (
    "设置事务优先级",
    "设置类别优先级",
)


def normalize_task_category(value):
    text = re.sub(r"\s+", "", str(value or "").strip().lower())
    return _CATEGORY_ALIASES.get(text, "other")


def get_category_label(value):
    return CATEGORY_LABELS.get(
        normalize_task_category(value),
        CATEGORY_LABELS["other"],
    )


def normalize_category_order(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("事务优先级必须是一个完整排序")

    normalized = []
    for value in values:
        text = re.sub(r"\s+", "", str(value or "").strip().lower())
        category = _CATEGORY_ALIASES.get(text)
        if category is None:
            raise ValueError(f"无法识别事务类别：{value}")
        normalized.append(category)

    if len(normalized) != len(CATEGORY_KEYS):
        raise ValueError("请将六类事务各填写一次")
    if len(set(normalized)) != len(CATEGORY_KEYS):
        raise ValueError("事务类别不能重复")
    if set(normalized) != set(CATEGORY_KEYS):
        raise ValueError("事务类别必须包含健康、家庭、学习、工作与兼职、个人生活、其他")

    return normalized


def parse_category_order_text(text):
    parts = [
        item
        for item in re.split(r"[>＞,，、;；\s]+", str(text or "").strip())
        if item
    ]
    return normalize_category_order(parts)


def parse_category_preference_command(text):
    normalized = str(text or "").strip()
    if normalized in VIEW_CATEGORY_COMMANDS:
        return {"action": "view"}

    for prefix in SET_CATEGORY_PREFIXES:
        if normalized.startswith(prefix):
            order_text = normalized[len(prefix):].strip(" ：:")
            if not order_text:
                return {"action": "set", "error": "missing_order"}
            try:
                order = parse_category_order_text(order_text)
            except ValueError as exc:
                return {"action": "set", "error": str(exc)}
            return {"action": "set", "order": order}

    return None


def format_category_order(order):
    normalized = normalize_category_order(order)
    return " ＞ ".join(CATEGORY_LABELS[item] for item in normalized)


def format_category_setup_prompt():
    return (
        "请按从高到低排列六类事务：\n\n"
        "健康、家庭、学习、工作与兼职、个人生活、其他\n\n"
        "例如：\n"
        "家庭 > 健康 > 学习 > 工作与兼职 > 个人生活 > 其他\n\n"
        "固定课程、明确不可移动安排和临近截止风险仍会优先保护，"
        "类别顺序不会越过这些硬约束。"
    )
