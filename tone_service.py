import database


TONE_LABELS = {
    "gentle": "温柔陪伴",
    "playful": "活泼夸夸",
    "professional": "简洁专业",
}

_TONE_ALIASES = {
    "温柔": "gentle",
    "温柔陪伴": "gentle",
    "活泼": "playful",
    "活泼夸夸": "playful",
    "夸夸": "playful",
    "简洁": "professional",
    "专业": "professional",
    "简洁专业": "professional",
}


def parse_tone(value):
    return _TONE_ALIASES.get(str(value or "").strip())


def parse_tone_command(text):
    command = str(text or "").strip()
    if command == "查看助手语气":
        return {"action": "view"}
    for prefix in ("设置助手语气", "设置鼓励语气"):
        if command.startswith(prefix):
            tone = parse_tone(command[len(prefix):].strip(" ：:"))
            return {"action": "set", "tone": tone}
    return None


def handle_tone_command(user_open_id, text):
    command = parse_tone_command(text)
    if command is None:
        return None
    if command["action"] == "view":
        preference = database.get_user_preference(user_open_id) or {}
        tone = preference.get("assistant_tone")
        return "当前助手语气：" + TONE_LABELS.get(tone, "未设置（使用简洁专业）")
    if not command.get("tone"):
        return "请选择：温柔陪伴、活泼夸夸或简洁专业。"
    database.update_assistant_tone(user_open_id, command["tone"])
    return "✅ 助手语气已设置为：" + TONE_LABELS[command["tone"]]


def get_encouragement(tone, has_progress=True):
    tone = tone or "professional"
    if has_progress:
        return {
            "gentle": "你已经向前推进了，按自己的节奏继续就好。",
            "playful": "推进成功！你正在把任务一点点拿下，继续冲呀！",
            "professional": "进度已记录，继续按当前计划推进。",
        }.get(tone, "进度已记录，继续按当前计划推进。")
    return {
        "gentle": "今天没有完成也没关系，我们可以把下一步再拆小一点。",
        "playful": "今天先稳住，明天把第一小步拿下！",
        "professional": "今日暂无完成记录，建议重新安排下一可执行步骤。",
    }.get(tone, "今日暂无完成记录，建议重新安排下一可执行步骤。")
