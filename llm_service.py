import os
import json
import re
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI

from preference_service import CATEGORY_KEYS, normalize_task_category


load_dotenv()


client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)


_NO_DEADLINE_PHRASES = {
    "没有截止时间",
    "没有截止日期",
    "无截止时间",
    "无截止日期",
    "无明确截止",
    "不设截止时间",
    "不设截止日期",
    "长期任务",
    "有空再做",
}


def has_explicit_no_deadline(user_text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(user_text or "").lower())
    return any(phrase in normalized for phrase in _NO_DEADLINE_PHRASES)


def apply_task_scheduling_intent(result: dict, user_text: str) -> dict:
    """Treat an explicit no-deadline statement as a deliberate pool choice."""
    result = dict(result or {})
    missing_fields = result.get("missing_fields")
    if not isinstance(missing_fields, list):
        missing_fields = []

    if has_explicit_no_deadline(user_text):
        result["deadline"] = None
        result["missing_fields"] = [
            field for field in missing_fields if field != "deadline"
        ]
        result["scheduling_bucket"] = "pool"
    else:
        result["missing_fields"] = missing_fields
        result["scheduling_bucket"] = (
            "deadline" if result.get("deadline") else "needs_deadline"
        )
    return result


def apply_task_update_scheduling_intent(result: dict, user_text: str) -> dict:
    result = dict(result or {})
    if result.get("intent") != "update_task" or not has_explicit_no_deadline(user_text):
        return result
    updates = dict(result.get("updates") or {})
    updates["deadline"] = None
    result["updates"] = updates
    return result


# =========================
# Task Recognition
# =========================

def recognize_task(user_text: str) -> dict:
    """
    将用户自然语言解析为结构化任务。
    当前阶段只负责识别，
    不负责保存数据库和自动排程。
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    system_prompt = f"""
你是“今日执行 Agent”的任务理解模块。

你的职责是：
把用户发送的自然语言解析成一个结构化任务 JSON。

当前时间：
{now}

必须只输出 JSON，不要输出 Markdown，不要解释。

JSON 格式必须严格如下：

{{
  "intent": "create_task",
  "title": "任务标题",
  "deadline": "YYYY-MM-DD HH:MM 或 null",
  "estimated_minutes": 120,
  "priority": "high",
  "category": "study",
  "missing_fields": []
}}

字段规则：

1. intent
目前只支持：
- create_task：用户正在创建一个需要完成的任务
- unknown：无法判断用户是否在创建任务

2. title
提取真正需要完成的事情。

例如：
“明天晚上把PRD改完”
提取为：
“修改PRD”

3. deadline
必须转换为：
YYYY-MM-DD HH:MM

如果用户没有说明截止时间，也没有明确表示“没有截止日期”：
deadline = null
并在 missing_fields 中加入 "deadline"

如果用户明确说“没有截止日期 / 无明确截止 / 长期任务 / 有空再做”：
deadline = null
但不要把 "deadline" 加入 missing_fields，任务会进入待安排池。

4. estimated_minutes
统一转换为分钟。

例如：
“两个小时” → 120
“半小时” → 30
“一个半小时” → 90

如果没有提供：
estimated_minutes = null
并在 missing_fields 中加入 "estimated_minutes"

5. priority
只允许：
- high
- medium
- low
- unknown

注意：
priority 当前只是根据用户语义识别出的重要程度，
不是最终排程优先级。

如果用户没有表达重要程度：
priority = "unknown"
并在 missing_fields 中加入 "priority"

6. missing_fields
记录缺失的重要字段。

不要擅自编造用户没有提供的信息。

7. category
根据任务内容识别事务类别，只允许：
- health：健康
- family：家庭
- study：学习
- work：工作与兼职
- personal：个人生活
- other：无法确定或其他事务

category 不加入 missing_fields；无法可靠判断时使用 other。
"""

    try:
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=500,
            temperature=0.1,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "DeepSeek 返回内容为空"
            )

        result = json.loads(
            content
        )

        return apply_task_scheduling_intent(
            validate_task_result(result),
            user_text,
        )

    except Exception as e:

        print(
            f"任务识别失败：{e}"
        )

        return {
            "intent": "unknown",
            "title": None,
            "deadline": None,
            "estimated_minutes": None,
            "priority": "unknown",
            "missing_fields": [],
            "error": str(e),
        }


# =========================
# Task Completion
# =========================

def complete_task(
    existing_task: dict,
    user_text: str,
) -> dict:
    """
    根据用户下一轮补充的信息，
    更新一个尚未完整的任务。
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    existing_task_json = json.dumps(
        existing_task,
        ensure_ascii=False,
    )

    system_prompt = f"""
你是“今日执行 Agent”的任务补全模块。

当前时间：
{now}

现在系统里已经有一个尚未完整的任务：

{existing_task_json}

用户接下来发送的内容，
优先理解为对这个已有任务的补充，
而不是创建一个全新的任务。

你需要结合：
1. 已有任务信息
2. 用户本轮补充内容

生成更新后的完整 JSON。

必须只输出 JSON，不要 Markdown，不要解释。

JSON 格式：

{{
  "intent": "create_task",
  "title": "任务标题",
  "deadline": "YYYY-MM-DD HH:MM 或 null",
  "estimated_minutes": 90,
  "priority": "high",
  "category": "study",
  "missing_fields": []
}}

规则：

1. 已经存在且用户本轮没有修改的字段，必须保留。

2. 如果用户补充了新的信息，就更新对应字段。

3. 如果用户明确修改旧信息，以最新信息为准。

例如：

已有：
deadline = "2026-08-17 20:00"

用户说：
“改成晚上九点”

则更新为：
deadline = "2026-08-17 21:00"

4. estimated_minutes 必须统一转换为分钟。

5. priority 只允许：
high
medium
low
unknown

6. 仍然缺失的字段要继续加入 missing_fields。

如果用户明确说任务没有截止日期，deadline = null，
并从 missing_fields 中移除 "deadline"，表示进入待安排池。

7. 不要编造用户没有提供的信息。

8. title 通常保持已有任务标题，
除非用户明确修改任务内容。

9. category 必须保留已有值；如果任务内容或用户说明表明类别变化，
只允许输出 health、family、study、work、personal、other 之一。
"""

    try:
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=500,
            temperature=0.1,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "DeepSeek 返回内容为空"
            )

        result = json.loads(
            content
        )

        return apply_task_scheduling_intent(
            validate_task_result(result),
            user_text,
        )

    except Exception as e:

        print(
            f"任务补全失败：{e}"
        )

        failed_result = (
            existing_task.copy()
        )

        failed_result[
            "error"
        ] = str(e)

        return failed_result


# =========================
# Intent Router V2
# =========================

def _local_intent_fallback(
    user_text: str,
) -> dict:
    """
    当 Intent Router 连续出现空响应或接口异常时，
    提供一个保守的本地兜底。

    原则：
    1. 只处理少量高确定性表达
    2. 不在低置信度情况下擅自执行
    3. 模糊表达优先返回 ambiguous
    """

    text = (
        user_text
        .strip()
        .replace(" ", "")
    )

    lower_text = text.lower()

    # -------------------------
    # Busy Time 高确定性
    # -------------------------

    blocked_keywords = {
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
        "不能安排",
        "被占用",
    }

    if any(
        keyword in text
        for keyword in blocked_keywords
    ):
        return {
            "intent": "create_blocked_times",
            "task_id": None,
            "task_title": None,
            "reason": "本地兜底：检测到明确忙碌时间表达",
        }

    # -------------------------
    # Task Progress 高确定性
    # -------------------------

    progress_keywords = {
        "还需要",
        "还剩",
        "剩余",
        "还要",
        "进度",
    }

    if any(
        keyword in text
        for keyword in progress_keywords
    ):
        task_id = None

        id_match = re.search(
            r"(?:#|任务)?\s*(\d+)",
            user_text,
        )

        if id_match:
            task_id = int(
                id_match.group(1)
            )

        return {
            "intent": "update_task_progress",
            "task_id": task_id,
            "task_title": None,
            "reason": "本地兜底：检测到明确剩余工作量表达",
        }

    # -------------------------
    # Completion 高确定性
    # -------------------------

    completion_keywords = {
        "已经完成",
        "已经做完",
        "做完了",
        "完成了",
        "搞定了",
        "已完成",
        "完成啦",
        "做完啦",
    }

    if any(
        keyword in text
        for keyword in completion_keywords
    ):
        # #3完成了 / 任务3完成了
        match = re.search(
            r"(?:#|任务)?(\d+)(?=.*(?:完成|做完|搞定))",
            text,
        )

        task_id = (
            int(match.group(1))
            if match
            else None
        )

        task_title = None

        if task_id is None:
            cleaned = text

            for keyword in sorted(
                completion_keywords,
                key=len,
                reverse=True,
            ):
                cleaned = cleaned.replace(
                    keyword,
                    ""
                )

            cleaned = (
                cleaned
                .replace("已经", "")
                .replace("了", "")
                .strip("，。！？!?")
            )

            task_title = (
                cleaned
                or None
            )

        return {
            "intent": "complete_task",
            "task_id": task_id,
            "task_title": task_title,
            "reason": "本地兜底：检测到明确已完成事实",
        }

    # -------------------------
    # Cancel / Restore 高确定性
    # -------------------------

    cancel_keywords = {
        "取消任务",
        "取消掉",
        "不做了",
        "不用做了",
        "别做了",
        "撤销任务",
        "作废",
    }

    restore_keywords = {
        "恢复任务",
        "恢复#",
        "恢复 #",
        "重新恢复",
        "恢复待办",
        "继续做",
        "重新启用",
    }

    if any(
        keyword in user_text
        for keyword in restore_keywords
    ):
        task_id = None

        id_match = re.search(
            r"(?:#|任务)\s*(\d+)",
            user_text,
        )

        if id_match:
            task_id = int(
                id_match.group(1)
            )

        return {
            "intent": "restore_task",
            "task_id": task_id,
            "task_title": None,
            "reason": "本地兜底：检测到明确恢复任务表达",
        }

    if any(
        keyword in user_text
        for keyword in cancel_keywords
    ):
        task_id = None

        id_match = re.search(
            r"(?:#|任务)\s*(\d+)",
            user_text,
        )

        if id_match:
            task_id = int(
                id_match.group(1)
            )

        return {
            "intent": "cancel_task",
            "task_id": task_id,
            "task_title": None,
            "reason": "本地兜底：检测到明确取消任务表达",
        }

    # -------------------------
    # Update Task 高确定性
    # -------------------------

    update_keywords = {
        "改成",
        "修改为",
        "改为",
        "调整为",
        "调整成",
        "改到",
        "改一下",
        "修改一下",
        "把任务",
        "把#",
    }

    if (
        any(
            keyword in text
            for keyword in update_keywords
        )
        and (
            "改" in text
            or "修改" in text
            or "调整" in text
        )
    ):
        task_id = None

        id_match = re.search(
            r"(?:#|任务)\s*(\d+)",
            user_text,
        )

        if id_match:
            task_id = int(
                id_match.group(1)
            )

        return {
            "intent": "update_task",
            "task_id": task_id,
            "task_title": None,
            "reason": "本地兜底：检测到明确任务修改表达",
        }

    # -------------------------
    # Create Task 高确定性
    # -------------------------

    future_keywords = {
        "明天",
        "后天",
        "今晚",
        "今天晚上",
        "下午",
        "上午",
        "截止",
        "预计",
        "之前",
        "前完成",
        "前把",
        "帮我记一下",
        "记一下",
        "提醒我",
    }

    if any(
        keyword in text
        for keyword in future_keywords
    ):
        return {
            "intent": "create_task",
            "task_id": None,
            "task_title": None,
            "reason": "本地兜底：检测到明确未来任务表达",
        }

    # -------------------------
    # 典型歧义：完成XX
    # -------------------------

    ambiguous_patterns = [
        r"^完成.+$",
        r"^做.+$",
    ]

    if any(
        re.match(pattern, text)
        for pattern in ambiguous_patterns
    ):
        task_title = text.strip("，。！？!?")

        return {
            "intent": "ambiguous",
            "task_id": None,
            "task_title": task_title,
            "reason": "本地兜底：可能是任务名，也可能是完成意图",
        }

    return {
        "intent": "unknown",
        "task_id": None,
        "task_title": None,
        "reason": "本地兜底：无法可靠判断",
    }


def _call_intent_router_once(
    user_text: str,
    system_prompt: str,
):
    """
    单次调用 Intent Router。
    """

    response = client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL"),
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_text,
            },
        ],
        response_format={
            "type": "json_object"
        },
        max_tokens=350,
        temperature=0.1,
    )

    content = (
        response
        .choices[0]
        .message
        .content
    )

    if not content:
        raise ValueError(
            "DeepSeek 返回内容为空"
        )

    result = json.loads(
        content
    )

    return validate_intent_result(
        result
    )


def recognize_intent(
    user_text: str,
) -> dict:
    """
    统一识别用户当前消息的顶层意图。

    当前支持：
    - create_task
    - complete_task
    - create_blocked_times
    - update_task
    - update_task_progress
    - cancel_task
    - restore_task
    - ambiguous
    - unknown

    稳定性策略：
    1. DeepSeek 空响应/异常时自动重试
    2. 连续失败后进入保守本地兜底
    3. 模糊情况优先 ambiguous，不擅自执行
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    system_prompt = f"""
你是“今日执行 Agent”的顶层 Intent Router。

当前时间：
{now}

你的职责是：
判断用户当前这句话最可能属于哪一种顶层意图。

必须只输出 JSON。
不要 Markdown。
不要解释。

输出格式必须严格如下：

{{
  "intent": "create_task",
  "task_id": null,
  "task_title": "完成PRD",
  "reason": "用户在描述未来需要完成的任务"
}}

intent 只允许以下九种：

1. create_task
用户是在创建、记录、安排一个未来需要完成的任务。

例如：
“明天晚上完成PRD”
“8月19日晚上9点前完成PRD，预计3小时”
“帮我记一下修改作品集”

2. complete_task
用户明确表达某个已有任务已经完成、已经做完、已经搞定。

例如：
“#3完成了”
“完成PRD已经做完了”
“整理项目截图搞定了”

3. create_blocked_times
用户明确描述某段时间已经被上课、会议、吃饭、通勤等事情占用，
这段时间不能用于安排任务。

例如：
“今天下午3点到5点有课”
“晚上6点到7点吃饭”

4. update_task
用户明确要求修改一个已经存在的任务。

典型表达：
“把 #7 改成明天晚上9点截止，预计1小时”
“把任务7的耗时改成2小时”
“把整理项目截图的重要程度改成普通”
“修改 #3 的截止时间到后天晚上8点”

当用户明确使用“改成、改为、修改、调整、改到”等表达，
并且目标是一个已有任务时，应返回 update_task。

如果用户明确给出任务编号，
task_id 输出对应整数。

如果没有编号但明确给出任务名称，
task_title 提取核心任务名称。

注意：
update_task 只表示“修改已有任务”，
不要因为修改后的值里包含“明天、预计”等未来信息，
就误判成 create_task。

5. update_task_progress
用户明确反馈某个已有任务“现在还剩多少时间 / 还需要多少时间”。

例如：
“#7还需要30分钟”
“#7还剩1小时”
“整理项目截图还需要20分钟”

这表示任务已经执行过一部分，
现在要更新“剩余工作量”，
不是修改任务最初的预计耗时。

如果有任务编号，task_id 提取编号。
没有编号但有任务名时，task_title 提取任务名。

6. cancel_task
用户明确表示某个已有任务不再需要做，希望取消、作废或停止它。

例如：
“#7取消掉”
“任务7不做了”
“整理项目截图不用做了”
“取消任务 #4”

注意：
cancel_task 表示将已有任务状态改为 cancelled，
不是删除数据库记录。

7. restore_task
用户明确表示希望把一个已取消任务恢复为待完成。

例如：
“恢复 #7”
“把任务7恢复”
“整理项目截图继续做”
“恢复任务 #4”

restore_task 表示将 cancelled 任务恢复为 pending。

8. ambiguous
一句话存在明显的多种合理解释，
不能安全地直接执行。

最重要的例子：
“完成PRD”

这句话既可能表示：
- 新建一个任务，任务名称叫“完成PRD”
也可能表示：
- 想把已有的“完成PRD”任务标记为完成

当缺少“已经、做完了、搞定了”等明确完成事实，
同时又没有“明天、今晚、截止、预计”等未来任务信息时，
必须返回 ambiguous。

9. unknown
不属于以上任何一种，
或信息不足到无法可靠分类。

字段规则：

- task_id：
  只有用户明确给出已有任务编号时填写整数，例如“#3完成了” -> 3。
  否则为 null。
  不得编造。

- task_title：
  与任务有关时，提取核心任务标题。
  例如：
  “完成PRD已经做完了” -> “完成PRD”
  “明天晚上完成PRD” -> “完成PRD”
  “完成PRD” -> “完成PRD”
  Busy Time 或无关消息可以为 null。

- reason：
  用一句简短中文说明为什么这么分类。
  不要超过30个汉字。

关键消歧规则：

A. “完成PRD” -> ambiguous
B. “完成PRD已经做完了” -> complete_task
C. “#3完成了” -> complete_task
D. “明天晚上完成PRD” -> create_task
E. “明天下午修改作品集，预计2小时” -> create_task
F. “今天下午3点到5点有课” -> create_blocked_times
G. “把 #7 改成明天晚上9点截止，预计1小时” -> update_task
H. “把整理项目截图的重要程度改成普通” -> update_task
I. “#7还需要30分钟” -> update_task_progress
J. “整理项目截图还剩20分钟” -> update_task_progress
K. “#7取消掉” -> cancel_task
L. “整理项目截图不做了” -> cancel_task
M. “恢复 #7” -> restore_task
N. “整理项目截图继续做” -> restore_task

特别注意：
“把 #7 改成明天晚上9点截止”
虽然包含“明天”，仍然是 update_task，
因为用户是在修改已有任务。

不要因为句子里出现“完成”两个字，
就直接判定为 complete_task。
"""

    max_attempts = 2
    last_error = None

    for attempt in range(
        1,
        max_attempts + 1,
    ):
        try:
            result = _call_intent_router_once(
                user_text,
                system_prompt,
            )

            if attempt > 1:
                print(
                    "Intent Router 重试成功："
                    f"attempt={attempt}",
                    flush=True,
                )

            # 如果大模型成功返回，但给出了 unknown，
            # 再用本地高确定性规则做一次兜底判断。
            #
            # 这样可以覆盖类似：
            # “今天晚上9点前完成最终验收测试，预计30分钟，比较重要”
            # 这类实际上非常明确的 create_task 表达，
            # 避免因为模型偶发保守判断而直接落入 unknown。
            if result.get("intent") == "unknown":
                local_fallback = _local_intent_fallback(
                    user_text
                )

                if (
                    local_fallback.get("intent")
                    != "unknown"
                ):
                    local_fallback[
                        "fallback_used"
                    ] = True

                    local_fallback[
                        "fallback_reason"
                    ] = (
                        "LLM 返回 unknown，"
                        "本地高确定性规则接管"
                    )

                    print(
                        "Intent Router LLM 返回 unknown，"
                        "使用本地高确定性兜底："
                        + json.dumps(
                            local_fallback,
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                    return validate_intent_result(
                        local_fallback
                    )

            return result

        except Exception as e:
            last_error = e

            print(
                "Intent Router 第"
                f"{attempt}次识别失败：{e}",
                flush=True,
            )

    fallback = _local_intent_fallback(
        user_text
    )

    fallback[
        "fallback_used"
    ] = True

    if last_error is not None:
        fallback[
            "error"
        ] = str(
            last_error
        )

    print(
        "Intent Router 使用本地兜底："
        + json.dumps(
            fallback,
            ensure_ascii=False,
        ),
        flush=True,
    )

    return validate_intent_result(
        fallback
    )


def validate_intent_result(
    result: dict,
) -> dict:
    """
    校验顶层 Intent Router 输出。
    """

    allowed_intents = {
        "create_task",
        "complete_task",
        "create_blocked_times",
        "update_task",
        "update_task_progress",
        "cancel_task",
        "restore_task",
        "ambiguous",
        "unknown",
    }

    if (
        result.get("intent")
        not in allowed_intents
    ):
        result["intent"] = "unknown"

    task_id = result.get(
        "task_id"
    )

    if task_id is not None:
        try:
            task_id = int(task_id)

            if task_id <= 0:
                task_id = None

        except (
            TypeError,
            ValueError,
        ):
            task_id = None

    task_title = result.get(
        "task_title"
    )

    if task_title is not None:
        task_title = (
            str(task_title)
            .strip()
            or None
        )

    reason = result.get(
        "reason"
    )

    if reason is None:
        reason = ""
    else:
        reason = str(reason).strip()

    result["task_id"] = task_id
    result["task_title"] = task_title
    result["reason"] = reason

    if result["intent"] in {
        "create_blocked_times",
        "unknown",
    }:
        result["task_id"] = None

        if result["intent"] == "unknown":
            result["task_title"] = None

    return result


# =========================
# Task Cancel / Restore Recognition
# =========================

def recognize_task_status_change(
    user_text: str,
) -> dict:
    """
    识别用户对已有任务的取消 / 恢复请求。

    输出：
    - cancel_task
    - restore_task
    - unknown

    这里只做语义识别，不修改数据库。
    """

    system_prompt = """
你是“今日执行 Agent”的任务状态变更理解模块。

你的职责是：
判断用户是否明确要求取消一个已有任务，
或者恢复一个已经取消的任务。

必须只输出 JSON。
不要 Markdown。
不要解释。

输出格式：

{
  "intent": "cancel_task",
  "task_id": 7,
  "task_title": null
}

或者：

{
  "intent": "restore_task",
  "task_id": null,
  "task_title": "整理项目截图"
}

如果不是取消 / 恢复任务：

{
  "intent": "unknown",
  "task_id": null,
  "task_title": null
}

规则：

1. cancel_task

用户明确表示已有任务不再做、取消、作废。

例如：
“#7取消掉”
“取消任务7”
“任务7不做了”
“整理项目截图不用做了”
“把整理项目截图取消”

2. restore_task

用户明确表示将已取消任务恢复、重新启用、继续做。

例如：
“恢复 #7”
“把任务7恢复”
“恢复整理项目截图”
“整理项目截图继续做”

3. task_id

用户明确给出编号时提取整数。

例如：
“恢复 #7” -> 7

不得编造。

4. task_title

没有编号但明确说出任务名称时，
提取任务名称。

例如：
“整理项目截图不做了”
-> “整理项目截图”

5. 不要误判：

“取消这次创建”
不是取消数据库里的已有任务，
返回 unknown。

“取消这次修改”
返回 unknown。

“#7完成了”
是 complete_task，
返回 unknown。

“把 #7 改成明晚9点”
是 update_task，
返回 unknown。

“明天完成PRD”
是 create_task，
返回 unknown。

6. 任务取消不是物理删除，
只是状态变化。

7. “恢复”只用于已有任务状态恢复，
不要理解成创建新任务。
"""

    try:

        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=300,
            temperature=0.1,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "DeepSeek 返回内容为空"
            )

        result = json.loads(
            content
        )

        return validate_task_status_change_result(
            result
        )

    except Exception as e:

        print(
            f"任务取消/恢复意图识别失败：{e}"
        )

        return {
            "intent": "unknown",
            "task_id": None,
            "task_title": None,
            "error": str(e),
        }


def validate_task_status_change_result(
    result: dict,
) -> dict:
    """
    校验取消 / 恢复识别结果。
    """

    allowed_intents = {
        "cancel_task",
        "restore_task",
        "unknown",
    }

    if (
        result.get("intent")
        not in allowed_intents
    ):
        result["intent"] = "unknown"

    task_id = result.get(
        "task_id"
    )

    if task_id is not None:
        try:
            task_id = int(task_id)

            if task_id <= 0:
                task_id = None

        except (
            TypeError,
            ValueError,
        ):
            task_id = None

    task_title = result.get(
        "task_title"
    )

    if task_title is not None:
        task_title = (
            str(task_title)
            .strip()
            or None
        )

    result["task_id"] = task_id
    result["task_title"] = task_title

    if (
        result["intent"]
        in {
            "cancel_task",
            "restore_task",
        }
        and task_id is None
        and task_title is None
    ):
        result["intent"] = "unknown"

    if result["intent"] == "unknown":
        result["task_id"] = None
        result["task_title"] = None

    return result


# =========================
# Task Progress Recognition
# =========================

def recognize_task_progress(
    user_text: str,
) -> dict:
    """
    识别任务执行反馈中的“剩余工作量”。

    示例：
    #7还需要30分钟
    -> task_id=7, remaining_minutes=30
    """

    percent_match = re.search(
        r"(?:#|任务)\s*(\d+).*?(\d{1,3})\s*%",
        str(user_text or ""),
    )
    if percent_match:
        percent = int(percent_match.group(2))
        if 0 <= percent <= 100:
            return {
                "intent": "update_task_progress",
                "task_id": int(percent_match.group(1)),
                "task_title": None,
                "remaining_minutes": None,
                "progress_percent": percent,
            }

    system_prompt = """
你是“今日执行 Agent”的任务执行进度理解模块。

你的职责是：
识别用户是否在反馈某个已有任务现在还剩多少执行时间。

必须只输出 JSON，不要 Markdown，不要解释。

输出格式：

{
  "intent": "update_task_progress",
  "task_id": 7,
  "task_title": null,
  "remaining_minutes": 30,
  "progress_percent": null
}

或者：

{
  "intent": "update_task_progress",
  "task_id": null,
  "task_title": "整理项目截图",
  "remaining_minutes": 20,
  "progress_percent": null
}

如果不是明确的剩余工作量反馈：

{
  "intent": "unknown",
  "task_id": null,
  "task_title": null,
  "remaining_minutes": null,
  "progress_percent": null
}

规则：

1. 只识别“当前还剩 / 还需要多少时间”。

典型表达：
“#7还需要30分钟”
“#7还剩1小时”
“整理项目截图还需要20分钟”
“任务7还要一个半小时”

2. remaining_minutes 必须统一转换为分钟。

30分钟 -> 30
1小时 -> 60
1个半小时 -> 90
2小时 -> 120

3. task_id
只有用户明确给出任务编号时提取。
不得编造。

4. task_title
如果没有编号但明确给出任务名称，
提取核心任务名称。

5. 不要把以下表达误判为进度反馈：

“把#7预计耗时改成30分钟”
-> update_task，不是 update_task_progress

“#7完成了”
-> complete_task

“#7取消掉”
-> cancel_task

“明天做30分钟PRD”
-> create_task

6. remaining_minutes 必须大于等于0。
如果用户说“还剩0分钟 / 还需要0分钟”，仍然返回 update_task_progress，
并令 remaining_minutes = 0。

注意：
0 分钟只表示“完成候选信号”。
本模块不要直接返回 complete_task，也不要擅自修改任务状态。
后续业务层会要求用户确认是否真的已经完成。

7. 如果用户用百分比反馈整体进度，例如“#7进度60%”，
则 progress_percent = 60，remaining_minutes = null。
progress_percent 只能是 0 到 100。
"""

    try:
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=300,
            temperature=0.1,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "DeepSeek 返回内容为空"
            )

        result = json.loads(
            content
        )

        return validate_task_progress_result(
            result
        )

    except Exception as e:
        print(
            f"任务执行进度识别失败：{e}"
        )

        return {
            "intent": "unknown",
            "task_id": None,
            "task_title": None,
            "remaining_minutes": None,
            "progress_percent": None,
            "error": str(e),
        }


def validate_task_progress_result(
    result: dict,
) -> dict:

    if result.get("intent") not in {
        "update_task_progress",
        "unknown",
    }:
        result["intent"] = "unknown"

    task_id = result.get("task_id")

    if task_id is not None:
        try:
            task_id = int(task_id)
            if task_id <= 0:
                task_id = None
        except (TypeError, ValueError):
            task_id = None

    task_title = result.get("task_title")

    if task_title is not None:
        task_title = (
            str(task_title).strip()
            or None
        )

    remaining_minutes = result.get(
        "remaining_minutes"
    )

    if remaining_minutes is not None:
        try:
            remaining_minutes = int(
                remaining_minutes
            )
            if remaining_minutes < 0:
                remaining_minutes = None
        except (TypeError, ValueError):
            remaining_minutes = None

    progress_percent = result.get("progress_percent")
    if progress_percent is not None:
        try:
            progress_percent = int(progress_percent)
            if not 0 <= progress_percent <= 100:
                progress_percent = None
        except (TypeError, ValueError):
            progress_percent = None

    result["task_id"] = task_id
    result["task_title"] = task_title
    result["remaining_minutes"] = (
        remaining_minutes
    )
    result["progress_percent"] = progress_percent

    if (
        result["intent"] == "update_task_progress"
        and (
            remaining_minutes is None
            and progress_percent is None
            or (
                task_id is None
                and task_title is None
            )
        )
    ):
        result["intent"] = "unknown"

    if result["intent"] == "unknown":
        result["task_id"] = None
        result["task_title"] = None
        result["remaining_minutes"] = None
        result["progress_percent"] = None

    return result


# =========================
# Task Breakdown
# =========================

def _fallback_task_breakdown(
    task: dict,
) -> dict:
    """模型不可用时提供保守、可编辑的通用拆分建议。"""

    title = (
        str(task.get("title") or "当前任务")
        .strip()
    )

    return {
        "subtasks": [
            f"明确「{title}」的目标和完成标准",
            "收集并整理完成任务所需的材料",
            f"完成「{title}」的核心内容",
            "检查质量并提交最终结果",
        ],
        "fallback_used": True,
    }


def validate_task_breakdown_result(
    result: dict,
) -> dict:
    """限制模型输出，防止任意字段进入业务层。"""

    raw_subtasks = result.get(
        "subtasks",
        [],
    )
    validated = []

    if isinstance(raw_subtasks, list):
        for item in raw_subtasks:
            if isinstance(item, dict):
                title = item.get("title")
            else:
                title = item

            title = str(title or "").strip()
            title = re.sub(
                r"^\s*(?:\d+[.、)]|[-*])\s*",
                "",
                title,
            )

            if (
                title
                and title not in validated
            ):
                validated.append(
                    title[:120]
                )

    if not 2 <= len(validated) <= 8:
        raise ValueError(
            "任务拆分结果必须包含 2 到 8 个有效子任务"
        )

    return {
        "subtasks": validated,
        "fallback_used": bool(
            result.get("fallback_used")
        ),
    }


def generate_task_breakdown(
    task: dict,
) -> dict:
    """
    为一个已存在的待完成任务生成 3 到 6 个可执行步骤。

    本函数只生成建议，不写数据库。
    """

    task_context = {
        "title": task.get("title"),
        "deadline": task.get("deadline"),
        "estimated_minutes": task.get(
            "estimated_minutes"
        ),
        "priority": task.get("priority"),
    }

    system_prompt = """
你是“今日执行 Agent”的任务拆分模块。

请把一个复杂任务拆成 3 到 6 个按执行顺序排列、可以直接勾选完成的步骤。

必须只输出 JSON，不要 Markdown，不要解释：

{
  "subtasks": [
    {"title": "第一个可执行步骤"},
    {"title": "第二个可执行步骤"}
  ]
}

规则：
1. 每一步必须具体、简短，以动词开头。
2. 不得增加原任务没有要求的交付物或外部联系人。
3. 各步骤不能重复，顺序应符合实际执行过程。
4. 不要把“完成整个任务”作为单独步骤。
5. 不要输出预计耗时、状态、数据库编号或其他字段。
"""

    try:
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        task_context,
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=600,
            temperature=0.2,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "任务拆分模型返回内容为空"
            )

        return validate_task_breakdown_result(
            json.loads(content)
        )

    except Exception as exc:
        print(
            f"任务拆分生成失败，使用本地兜底：{exc}",
            flush=True,
        )

        return validate_task_breakdown_result(
            _fallback_task_breakdown(task)
        )


# =========================
# Task Update Recognition
# =========================

def recognize_task_update(
    user_text: str,
) -> dict:
    """
    识别用户对已有任务的修改请求。

    只负责理解：
    - 要修改哪个任务
    - 哪些字段发生变化

    不负责：
    - 查询数据库
    - 真正更新任务
    - 用户确认
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    system_prompt = f"""
你是“今日执行 Agent”的任务修改理解模块。

当前时间：
{now}

你的职责是：
把用户对“已有任务”的修改请求解析成结构化 JSON。

必须只输出 JSON。
不要 Markdown。
不要解释。

输出格式严格如下：

{{
  "intent": "update_task",
  "task_id": 7,
  "task_title": null,
  "updates": {{
    "deadline": "YYYY-MM-DD HH:MM",
    "estimated_minutes": 60,
    "category": "study"
  }}
}}

或者按任务名称：

{{
  "intent": "update_task",
  "task_id": null,
  "task_title": "整理项目截图",
  "updates": {{
    "priority": "medium"
  }}
}}

如果不是明确的任务修改请求：

{{
  "intent": "unknown",
  "task_id": null,
  "task_title": null,
  "updates": {{}}
}}

规则：

1. 只有用户明确要求“修改已经存在的任务”时，
才返回 update_task。

典型关键词：
- 改成
- 改为
- 修改
- 调整
- 改到

2. task_id

如果用户明确给出任务编号，例如：
“把 #7 改成明天晚上9点截止”
“任务7耗时改成1小时”

则：
task_id = 7

不得编造 task_id。

3. task_title

如果没有任务编号，但用户明确说出了任务名称，例如：
“把整理项目截图的重要程度改成普通”

则：
task_title = "整理项目截图"

如果有明确 task_id，
task_title 可以为 null。

4. updates

只允许以下字段：

- title
- deadline
- estimated_minutes
- priority
- category

绝对不要输出：
- id
- user_open_id
- status
- created_at
- updated_at

5. 只输出用户明确修改的字段。

例如：

用户：
“把 #7 改成明天晚上9点截止，预计1小时”

updates：
{{
  "deadline": "对应日期 21:00",
  "estimated_minutes": 60
}}

不要顺便输出 title 或 priority。

6. deadline

必须转换成：
YYYY-MM-DD HH:MM

需要结合当前时间理解：
今天 / 明天 / 后天 / 周一等自然语言时间。

如果用户明确要求改成“没有截止日期 / 无明确截止 / 长期任务 / 有空再做”，
则在 updates 中输出：
"deadline": null
这表示将任务移入待安排池，不是缺少信息。

7. estimated_minutes

统一转换为分钟：

“30分钟” -> 30
“1小时” -> 60
“一个半小时” -> 90
“2小时” -> 120

8. priority

只允许：
- high
- medium
- low
- unknown

语义映射：

“非常重要 / 很重要 / 高优先级”
-> high

“普通 / 一般 / 中等”
-> medium

“不重要 / 低优先级”
-> low

9. title

只有用户明确要求修改任务标题时才输出。

例如：
“把 #7 的名字改成整理作品集截图”

updates：
{{
"title": "整理作品集截图"
}}

category 只允许：health、family、study、work、personal、other。
只有用户明确修改任务类别时才放入 updates。

10. 不要把“创建新任务”误判为 update_task。

例如：
“明天晚上9点完成PRD”
是 create_task，不是 update_task。

11. 不要把“任务已经完成”误判为 update_task。

例如：
“#7完成了”
是 complete_task，不是 update_task。

12. 如果用户说的是任务修改，
但没有给出任何可执行的修改字段，
返回 unknown。

例如：
“我想改一下任务”
-> unknown
"""

    try:

        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=500,
            temperature=0.1,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "DeepSeek 返回内容为空"
            )

        result = json.loads(
            content
        )

        return validate_task_update_result(
            apply_task_update_scheduling_intent(
                result,
                user_text,
            )
        )

    except Exception as e:

        print(
            f"任务修改意图识别失败：{e}"
        )

        return {
            "intent": "unknown",
            "task_id": None,
            "task_title": None,
            "updates": {},
            "error": str(e),
        }


def validate_task_update_result(
    result: dict,
) -> dict:
    """
    校验任务修改识别结果。

    这是 LLM 与数据库之间的第一层安全边界。
    """

    if result.get("intent") not in {
        "update_task",
        "unknown",
    }:
        result["intent"] = "unknown"

    task_id = result.get(
        "task_id"
    )

    if task_id is not None:
        try:
            task_id = int(task_id)

            if task_id <= 0:
                task_id = None

        except (
            TypeError,
            ValueError,
        ):
            task_id = None

    task_title = result.get(
        "task_title"
    )

    if task_title is not None:
        task_title = (
            str(task_title)
            .strip()
            or None
        )

    raw_updates = result.get(
        "updates"
    )

    if not isinstance(
        raw_updates,
        dict,
    ):
        raw_updates = {}

    allowed_fields = {
        "title",
        "deadline",
        "estimated_minutes",
        "priority",
        "category",
    }

    updates = {
        key: value
        for key, value in raw_updates.items()
        if key in allowed_fields
    }

    # title
    if "title" in updates:

        if updates["title"] is None:
            updates.pop("title")

        else:
            title = str(
                updates["title"]
            ).strip()

            if title:
                updates["title"] = title
            else:
                updates.pop("title")

    # deadline
    if "deadline" in updates:

        deadline = updates[
            "deadline"
        ]

        if deadline is not None:
            try:
                datetime.strptime(
                    str(deadline),
                    "%Y-%m-%d %H:%M",
                )

                updates[
                    "deadline"
                ] = str(deadline)

            except ValueError:
                updates.pop("deadline")

    # estimated_minutes
    if "estimated_minutes" in updates:

        try:
            minutes = int(
                updates[
                    "estimated_minutes"
                ]
            )

            if minutes <= 0:
                raise ValueError

            updates[
                "estimated_minutes"
            ] = minutes

        except (
            TypeError,
            ValueError,
        ):
            updates.pop(
                "estimated_minutes",
                None,
            )

    # priority
    if "priority" in updates:

        allowed_priorities = {
            "high",
            "medium",
            "low",
            "unknown",
        }

        if (
            updates["priority"]
            not in allowed_priorities
        ):
            updates.pop(
                "priority",
                None,
            )

    if "category" in updates:
        raw_category = str(updates["category"] or "").strip().lower()
        if raw_category not in CATEGORY_KEYS:
            updates.pop("category", None)

    result["task_id"] = task_id
    result["task_title"] = task_title
    result["updates"] = updates

    if (
        result["intent"] == "update_task"
        and (
            not updates
            or (
                task_id is None
                and task_title is None
            )
        )
    ):
        result["intent"] = "unknown"

    if result["intent"] == "unknown":
        result["task_id"] = None
        result["task_title"] = None
        result["updates"] = {}

    return result


# =========================
# Task Completion Recognition
# =========================

def recognize_task_completion(
    user_text: str,
) -> dict:
    """
    识别用户是否在表达“某个已有任务已经完成”。

    支持：
    - 通过任务编号定位，例如：#3完成了
    - 通过任务标题定位，例如：PRD做完了

    这里只负责理解意图，
    不负责查询数据库或修改任务状态。
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    system_prompt = f"""
你是“今日执行 Agent”的任务完成意图识别模块。

当前时间：
{now}

你的职责是判断用户是否在表达：
“某个已经存在的任务现在已经做完了”。

必须只输出 JSON。
不要 Markdown。
不要解释。

输出格式严格如下：

{{
  "intent": "complete_task",
  "task_id": 3,
  "task_title": null
}}

或者：

{{
  "intent": "complete_task",
  "task_id": null,
  "task_title": "完成PRD"
}}

如果不是任务完成意图：

{{
  "intent": "unknown",
  "task_id": null,
  "task_title": null
}}

规则：

1. 只有用户明确表达“任务已经完成 / 做完 / 搞定 / 完成了”等已完成事实时，
才返回 complete_task。

2. 不要把“未来要完成某件事”误识别成任务完成。
例如：
“明天完成PRD”
“今晚把PRD做完”
“9点前完成PRD”
这些都是待办任务，不是已完成。

3. 如果用户明确给出了任务编号，例如：
“#3完成了”
“任务3做完了”
则 task_id 输出整数 3，task_title 可以为 null。

4. 如果用户没有给任务编号，但提到了任务名称，例如：
“PRD已经做完了”
“整理项目截图完成了”
则 task_id = null，task_title 提取能够用于匹配已有任务的核心标题。

5. task_title 尽量保留任务的核心语义，不要加入“做完了”“完成了”“搞定了”等状态词。

6. 如果无法确定用户是在完成哪个任务，返回 unknown。

7. 不要编造 task_id。
"""

    try:
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_MODEL"),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=300,
            temperature=0.1,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "DeepSeek 返回内容为空"
            )

        result = json.loads(content)

        return validate_task_completion_result(
            result
        )

    except Exception as e:
        print(
            f"任务完成意图识别失败：{e}"
        )

        return {
            "intent": "unknown",
            "task_id": None,
            "task_title": None,
            "error": str(e),
        }


def validate_task_completion_result(
    result: dict,
) -> dict:
    """
    校验任务完成意图识别结果。
    """

    if result.get("intent") not in {
        "complete_task",
        "unknown",
    }:
        result["intent"] = "unknown"

    task_id = result.get("task_id")
    task_title = result.get("task_title")

    if task_id is not None:
        try:
            task_id = int(task_id)
            if task_id <= 0:
                task_id = None
        except (TypeError, ValueError):
            task_id = None

    if task_title is not None:
        task_title = str(task_title).strip() or None

    result["task_id"] = task_id
    result["task_title"] = task_title

    if result.get("intent") == "complete_task":
        if task_id is None and task_title is None:
            result["intent"] = "unknown"

    if result.get("intent") == "unknown":
        result["task_id"] = None
        result["task_title"] = None

    return result


# =========================
# Busy Time Recognition
# =========================

def recognize_blocked_times(
    user_text: str,
) -> dict:
    """
    将用户自然语言中的忙碌时间，
    解析成结构化 blocked_times。

    一句话可以包含多个忙碌时间。
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    system_prompt = f"""
你是“今日执行 Agent”的时间可用性理解模块。

你的职责是：
识别用户描述的“无法安排任务的时间”。

这些时间可能包括：
- 上课
- 开会
- 吃饭
- 通勤
- 面试
- 约会
- 固定活动
- 其他用户明确表示不能安排任务的时间

当前时间：
{now}

必须只输出 JSON。
不要 Markdown。
不要解释。

输出格式严格如下：

{{
  "intent": "create_blocked_times",
  "blocked_times": [
    {{
      "title": "上课",
      "start_time": "YYYY-MM-DD HH:MM",
      "end_time": "YYYY-MM-DD HH:MM"
    }}
  ]
}}

如果一句话中有多个忙碌时间，
必须全部放入 blocked_times 数组。

例如用户说：

“今天下午3点到5点有课，
晚上6点到7点吃饭”

应该输出类似：

{{
  "intent": "create_blocked_times",
  "blocked_times": [
    {{
      "title": "上课",
      "start_time": "当前日期 15:00",
      "end_time": "当前日期 17:00"
    }},
    {{
      "title": "吃饭",
      "start_time": "当前日期 18:00",
      "end_time": "当前日期 19:00"
    }}
  ]
}}

实际输出时，
日期必须转换成：

YYYY-MM-DD HH:MM

规则：

1. title
提取用户这一时间段正在进行的事情。

例如：
“3点到5点有课”
→ “上课”

“6点到7点吃饭”
→ “吃饭”

2. start_time
必须是：
YYYY-MM-DD HH:MM

3. end_time
必须是：
YYYY-MM-DD HH:MM

4. 不要把任务截止时间误识别成忙碌时间。

例如：
“晚上9点前完成PRD”
不是 Busy Time。

5. 只有用户明确表达：
某段时间已经被某件事情占用，
才识别为 Busy Time。

6. 如果无法识别出明确的忙碌时间，
返回：

{{
  "intent": "unknown",
  "blocked_times": []
}}

7. 不要擅自编造开始时间或结束时间。
"""

    try:

        response = client.chat.completions.create(
            model=os.getenv(
                "DEEPSEEK_MODEL"
            ),
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            response_format={
                "type": "json_object"
            },
            max_tokens=700,
            temperature=0.1,
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            raise ValueError(
                "DeepSeek 返回内容为空"
            )

        result = json.loads(
            content
        )

        return (
            validate_blocked_time_result(
                result
            )
        )

    except Exception as e:

        print(
            f"忙碌时间识别失败：{e}"
        )

        return {
            "intent": "unknown",
            "blocked_times": [],
            "error": str(e),
        }


# =========================
# Validation
# =========================

def validate_task_result(
    result: dict
) -> dict:
    """
    对任务模型输出进行基础校验。
    """

    allowed_intents = {
        "create_task",
        "unknown",
    }

    allowed_priorities = {
        "high",
        "medium",
        "low",
        "unknown",
    }

    allowed_categories = set(CATEGORY_KEYS)

    if (
        result.get("intent")
        not in allowed_intents
    ):
        result["intent"] = "unknown"

    if (
        result.get("priority")
        not in allowed_priorities
    ):
        result["priority"] = "unknown"

    if result.get("category") not in allowed_categories:
        result["category"] = normalize_task_category(
            result.get("category")
        )

    if "title" not in result:
        result["title"] = None

    if "deadline" not in result:
        result["deadline"] = None

    if (
        "estimated_minutes"
        not in result
    ):
        result[
            "estimated_minutes"
        ] = None

    if not isinstance(
        result.get(
            "missing_fields"
        ),
        list,
    ):
        result[
            "missing_fields"
        ] = []

    return result


def validate_blocked_time_result(
    result: dict,
) -> dict:
    """
    校验 Busy Time 识别结果。
    """

    if (
        result.get("intent")
        not in {
            "create_blocked_times",
            "unknown",
        }
    ):
        result["intent"] = "unknown"

    blocked_times = result.get(
        "blocked_times"
    )

    if not isinstance(
        blocked_times,
        list,
    ):
        blocked_times = []

    valid_items = []

    for item in blocked_times:

        if not isinstance(
            item,
            dict,
        ):
            continue

        title = item.get(
            "title"
        )

        start_time = item.get(
            "start_time"
        )

        end_time = item.get(
            "end_time"
        )

        if (
            not title
            or not start_time
            or not end_time
        ):
            continue

        # 基础时间格式验证
        try:

            start_dt = datetime.strptime(
                start_time,
                "%Y-%m-%d %H:%M",
            )

            end_dt = datetime.strptime(
                end_time,
                "%Y-%m-%d %H:%M",
            )

        except ValueError:
            continue

        # 结束时间必须晚于开始时间
        if end_dt <= start_dt:
            continue

        valid_items.append(
            {
                "title": str(title),
                "start_time": start_time,
                "end_time": end_time,
            }
        )

    result[
        "blocked_times"
    ] = valid_items

    if not valid_items:
        result["intent"] = "unknown"

    return result


# =========================
# Local Test
# =========================

if __name__ == "__main__":

    print(
        "===== Intent Router V4 任务生命周期测试 ====="
    )

    router_tests = [
        "完成PRD",
        "#3完成了",
        "明天晚上完成PRD",
        "今天下午3点到5点有课",
        "把 #7 改成明天晚上9点截止，预计1小时",
        "#7取消掉",
        "整理项目截图不做了",
        "恢复 #7",
        "整理项目截图继续做",
    ]

    for user_text in router_tests:

        print(
            f"\n用户：{user_text}"
        )

        result = recognize_intent(
            user_text
        )

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

    print(
        "\n===== Task Cancel / Restore Recognition 测试 ====="
    )

    status_tests = [
        "#7取消掉",
        "取消任务7",
        "整理项目截图不做了",
        "恢复 #7",
        "把任务7恢复",
        "整理项目截图继续做",
        "取消这次创建",
        "取消这次修改",
        "#7完成了",
        "把 #7 改成明晚9点",
        "明天完成PRD",
    ]

    for user_text in status_tests:

        print(
            f"\n用户：{user_text}"
        )

        result = recognize_task_status_change(
            user_text
        )

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

    print(
        "\n===== Task Update 回归测试 ====="
    )

    update_text = (
        "把 #7 改成明天晚上9点截止，预计1小时"
    )

    print(
        json.dumps(
            recognize_task_update(
                update_text
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

    print(
        "\n===== Completion 回归测试 ====="
    )

    print(
        json.dumps(
            recognize_task_completion(
                "#3完成了"
            ),
            ensure_ascii=False,
            indent=2,
        )
    )

    print(
        "\n===== Busy Time 回归测试 ====="
    )

    print(
        json.dumps(
            recognize_blocked_times(
                "今天下午3点到5点有课"
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
