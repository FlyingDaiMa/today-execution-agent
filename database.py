import json
import sqlite3
from pathlib import Path
from datetime import datetime

from preference_service import normalize_category_order, normalize_task_category


# =========================
# Database Config
# =========================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "today_execution.db"


_PUSH_COLUMNS = {
    "morning": ("morning_push_enabled", "morning_push_time"),
    "evening": ("evening_push_enabled", "evening_push_time"),
}


def _get_push_columns(push_type: str):
    try:
        return _PUSH_COLUMNS[push_type]
    except KeyError as exc:
        raise ValueError("push_type 必须是 morning 或 evening") from exc


def _validate_push_time(push_time: str):
    if not isinstance(push_time, str):
        raise ValueError("推送时间必须使用 HH:MM 格式")

    try:
        parsed = datetime.strptime(push_time, "%H:%M")
    except ValueError as exc:
        raise ValueError("推送时间必须使用 HH:MM 格式") from exc

    if parsed.strftime("%H:%M") != push_time:
        raise ValueError("推送时间必须使用 HH:MM 格式")


def get_connection():
    """
    创建 SQLite 数据库连接。
    """

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    return connection


# =========================
# Database Initialization
# =========================

def init_db():
    """
    初始化数据库。

    当前包含：
    1. tasks
    2. user_preferences
    3. blocked_times
    4. push_delivery_log
    5. task_subtasks
    6. risk_alerts
    7. daily_plan_snapshots
    8. reminder_delivery_log
    """

    connection = get_connection()

    try:

        # -------------------------
        # Tasks
        # -------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_open_id TEXT NOT NULL,

                title TEXT NOT NULL,

                deadline TEXT,

                estimated_minutes INTEGER,

                remaining_minutes INTEGER,

                priority TEXT NOT NULL DEFAULT 'unknown',

                category TEXT NOT NULL DEFAULT 'other',

                status TEXT NOT NULL DEFAULT 'pending',

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL
            )
            """
        )

        # -------------------------
        # Tasks Schema Migration
        # -------------------------

        task_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(tasks)"
            ).fetchall()
        }

        if "remaining_minutes" not in task_columns:
            connection.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN remaining_minutes INTEGER
                """
            )

        if "category" not in task_columns:
            connection.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN category TEXT NOT NULL DEFAULT 'other'
                """
            )

        # 老任务第一次升级时，
        # 默认“剩余时长 = 原预计耗时”。
        connection.execute(
            """
            UPDATE tasks
            SET remaining_minutes = estimated_minutes
            WHERE remaining_minutes IS NULL
              AND estimated_minutes IS NOT NULL
            """
        )

        # -------------------------
        # User Preferences
        # -------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_open_id TEXT NOT NULL UNIQUE,

                priority_strategy TEXT,

                category_order TEXT,

                interest_keywords TEXT,

                news_enabled INTEGER NOT NULL DEFAULT 0,

                assistant_tone TEXT,

                onboarding_completed INTEGER NOT NULL DEFAULT 0,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL
            )
            """
        )

        preference_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(user_preferences)"
            ).fetchall()
        }

        preference_migrations = {
            "category_order": "TEXT",
            "interest_keywords": "TEXT",
            "news_enabled": "INTEGER NOT NULL DEFAULT 0",
            "assistant_tone": "TEXT",
            "morning_push_enabled": "INTEGER NOT NULL DEFAULT 0",
            "morning_push_time": "TEXT NOT NULL DEFAULT '08:00'",
            "evening_push_enabled": "INTEGER NOT NULL DEFAULT 0",
            "evening_push_time": "TEXT NOT NULL DEFAULT '22:00'",
        }

        for column_name, definition in preference_migrations.items():
            if column_name not in preference_columns:
                connection.execute(
                    f"ALTER TABLE user_preferences "
                    f"ADD COLUMN {column_name} {definition}"
                )

        # -------------------------
        # Push Delivery Log
        # -------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS push_delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_open_id TEXT NOT NULL,
                push_type TEXT NOT NULL,
                delivery_date TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                UNIQUE(user_open_id, push_type, delivery_date)
            )
            """
        )

        # -------------------------
        # Daily Plan Snapshots
        # -------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_plan_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_open_id TEXT NOT NULL,
                plan_date TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_open_id, plan_date)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_open_id TEXT NOT NULL,
                reminder_key TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                UNIQUE(user_open_id, reminder_key)
            )
            """
        )

        # -------------------------
        # Blocked Times
        # -------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_times (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_open_id TEXT NOT NULL,

                title TEXT NOT NULL,

                start_time TEXT NOT NULL,

                end_time TEXT NOT NULL,

                source TEXT NOT NULL DEFAULT 'manual',

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL
            )
            """
        )

        # -------------------------
        # Task Subtasks
        # -------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_subtasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_open_id TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_open_id, task_id, position),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_task_subtasks_task
            ON task_subtasks(user_open_id, task_id, position)
            """
        )

        # -------------------------
        # Risk Alerts / Rescue Plans
        # -------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_open_id TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                risk_date TEXT NOT NULL,
                risk_type TEXT NOT NULL,
                risk_reason TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'awaiting_status',
                user_context TEXT,
                proposal_json TEXT,
                alerted_at TEXT NOT NULL,
                state_changed_at TEXT NOT NULL,
                reminded_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(user_open_id, task_id, risk_date),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_risk_alerts_active
            ON risk_alerts(user_open_id, status, state_changed_at)
            """
        )

        connection.commit()

    finally:
        connection.close()

    print(
        f"数据库初始化完成：{DB_PATH}",
        flush=True,
    )


# =========================
# Task CRUD
# =========================

def create_task(
    user_open_id: str,
    task: dict,
) -> int:
    """
    将用户确认后的任务写入数据库。
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    connection = get_connection()

    try:

        cursor = connection.execute(
            """
            INSERT INTO tasks (
                user_open_id,
                title,
                deadline,
                estimated_minutes,
                remaining_minutes,
                priority,
                category,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_open_id,
                task.get("title"),
                task.get("deadline"),
                task.get("estimated_minutes"),
                task.get("estimated_minutes"),
                task.get(
                    "priority",
                    "unknown",
                ),
                normalize_task_category(task.get("category")),
                "pending",
                now,
                now,
            ),
        )

        connection.commit()

        task_id = cursor.lastrowid

        print(
            f"任务已写入数据库，task_id={task_id}",
            flush=True,
        )

        return task_id

    finally:
        connection.close()


def get_user_tasks(
    user_open_id: str,
) -> list:
    """
    查询某个用户的全部任务。
    """

    connection = get_connection()

    try:

        cursor = connection.execute(
            """
            SELECT
                id,
                title,
                deadline,
                estimated_minutes,
                remaining_minutes,
                priority,
                category,
                status,
                created_at,
                updated_at
            FROM tasks
            WHERE user_open_id = ?
            ORDER BY id DESC
            """,
            (user_open_id,),
        )

        rows = cursor.fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        connection.close()


def get_task_by_id(
    user_open_id: str,
    task_id: int,
):
    """
    按 task_id 查询某个用户的一条任务。
    """

    connection = get_connection()

    try:

        cursor = connection.execute(
            """
            SELECT
                id,
                title,
                deadline,
                estimated_minutes,
                remaining_minutes,
                priority,
                category,
                status,
                created_at,
                updated_at
            FROM tasks
            WHERE user_open_id = ?
              AND id = ?
            """,
            (
                user_open_id,
                task_id,
            ),
        )

        row = cursor.fetchone()

        if row is None:
            return None

        return dict(row)

    finally:
        connection.close()


def update_task_status(
    user_open_id: str,
    task_id: int,
    status: str,
) -> bool:
    """
    更新任务状态。

    当前支持：
    pending
    completed
    cancelled

    返回：
    True  -> 成功更新
    False -> 未找到对应任务
    """

    allowed_statuses = {
        "pending",
        "completed",
        "cancelled",
    }

    if status not in allowed_statuses:
        raise ValueError(
            f"不支持的任务状态：{status}"
        )

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    connection = get_connection()

    try:

        cursor = connection.execute(
            """
            UPDATE tasks
            SET
                status = ?,
                updated_at = ?
            WHERE user_open_id = ?
              AND id = ?
            """,
            (
                status,
                now,
                user_open_id,
                task_id,
            ),
        )

        connection.commit()

        updated = cursor.rowcount > 0

        if updated:
            print(
                "任务状态已更新："
                f"task_id={task_id}, "
                f"status={status}",
                flush=True,
            )
        else:
            print(
                "未找到需要更新的任务："
                f"task_id={task_id}",
                flush=True,
            )

        return updated

    finally:
        connection.close()


def update_task_remaining_minutes(
    user_open_id: str,
    task_id: int,
    remaining_minutes: int,
) -> bool:
    """
    更新待完成任务的剩余执行时长。

    规则：
    - remaining_minutes 必须为正整数
    - 只允许更新 pending 任务
    - 完成任务请继续使用 update_task_status()
    """

    try:
        remaining_minutes = int(
            remaining_minutes
        )
    except (
        TypeError,
        ValueError,
    ):
        raise ValueError(
            "remaining_minutes 必须是正整数"
        )

    if remaining_minutes <= 0:
        raise ValueError(
            "remaining_minutes 必须大于 0"
        )

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            UPDATE tasks
            SET
                remaining_minutes = ?,
                updated_at = ?
            WHERE user_open_id = ?
              AND id = ?
              AND status = 'pending'
            """,
            (
                remaining_minutes,
                now,
                user_open_id,
                task_id,
            ),
        )

        connection.commit()

        updated = cursor.rowcount > 0

        if updated:
            print(
                "任务剩余时长已更新："
                f"task_id={task_id}, "
                f"remaining_minutes={remaining_minutes}",
                flush=True,
            )
        else:
            print(
                "任务剩余时长更新失败："
                f"task_id={task_id} 不存在、"
                "不属于当前用户或不是待完成状态",
                flush=True,
            )

        return updated

    finally:
        connection.close()


def update_task(
    task_id: int,
    user_open_id: str,
    updates: dict,
) -> bool:
    """
    修改已有任务的信息。

    当前允许修改：
    - title
    - deadline
    - estimated_minutes
    - priority
    - category

    不允许通过这个函数修改：
    - id
    - user_open_id
    - status
    - created_at
    """

    allowed_fields = {
        "title",
        "deadline",
        "estimated_minutes",
        "priority",
        "category",
    }

    # 只保留允许修改的字段
    valid_updates = {
        key: value
        for key, value in updates.items()
        if key in allowed_fields
    }

    if "category" in valid_updates:
        valid_updates["category"] = normalize_task_category(
            valid_updates["category"]
        )

    if not valid_updates:
        print(
            "没有可更新的任务字段",
            flush=True,
        )
        return False

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    set_parts = []
    values = []

    for field, value in valid_updates.items():

        set_parts.append(
            f"{field} = ?"
        )

        values.append(value)

    # 每次修改任务，都更新时间
    set_parts.append(
        "updated_at = ?"
    )

    values.append(now)

    # 最后两个参数用于 WHERE
    values.extend(
        [
            task_id,
            user_open_id,
        ]
    )

    sql = f"""
        UPDATE tasks
        SET {", ".join(set_parts)}
        WHERE id = ?
        AND user_open_id = ?
    """

    connection = get_connection()

    try:

        cursor = connection.execute(
            sql,
            values,
        )

        connection.commit()

        if cursor.rowcount == 0:

            print(
                f"任务修改失败："
                f"task_id={task_id} 不存在"
                f"或不属于当前用户",
                flush=True,
            )

            return False

        print(
            f"任务修改成功："
            f"task_id={task_id}, "
            f"updates={valid_updates}",
            flush=True,
        )

        return True

    finally:

        connection.close()


# =========================
# Task Subtasks
# =========================

def replace_task_subtasks(
    user_open_id: str,
    task_id: int,
    titles: list,
) -> list:
    """
    原子替换某个任务的全部子任务。

    只有用户确认 AI 建议后才调用本函数。
    """

    normalized_titles = []

    for title in titles:
        normalized = str(title or "").strip()

        if normalized:
            normalized_titles.append(
                normalized[:120]
            )

    if not 2 <= len(normalized_titles) <= 8:
        raise ValueError(
            "子任务数量必须在 2 到 8 条之间"
        )

    if len(set(normalized_titles)) != len(normalized_titles):
        raise ValueError(
            "子任务标题不能重复"
        )

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    connection = get_connection()

    try:
        task_row = connection.execute(
            """
            SELECT status
            FROM tasks
            WHERE user_open_id = ?
              AND id = ?
            """,
            (
                user_open_id,
                task_id,
            ),
        ).fetchone()

        if task_row is None:
            raise ValueError(
                "任务不存在或不属于当前用户"
            )

        if task_row["status"] != "pending":
            raise ValueError(
                "只能为待完成任务保存子任务"
            )

        with connection:
            connection.execute(
                """
                DELETE FROM task_subtasks
                WHERE user_open_id = ?
                  AND task_id = ?
                """,
                (
                    user_open_id,
                    task_id,
                ),
            )

            connection.executemany(
                """
                INSERT INTO task_subtasks (
                    user_open_id,
                    task_id,
                    position,
                    title,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        user_open_id,
                        task_id,
                        position,
                        title,
                        now,
                        now,
                    )
                    for position, title in enumerate(
                        normalized_titles,
                        start=1,
                    )
                ],
            )

        return get_task_subtasks(
            user_open_id,
            task_id,
        )

    finally:
        connection.close()


def get_task_subtasks(
    user_open_id: str,
    task_id: int,
) -> list:
    """按顺序查询一个任务的全部子任务。"""

    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                id,
                task_id,
                position,
                title,
                status,
                created_at,
                updated_at
            FROM task_subtasks
            WHERE user_open_id = ?
              AND task_id = ?
            ORDER BY position ASC
            """,
            (
                user_open_id,
                task_id,
            ),
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        connection.close()


def update_task_subtask_status(
    user_open_id: str,
    task_id: int,
    position: int,
    status: str,
) -> bool:
    """勾选或恢复一个子任务。"""

    if status not in {
        "pending",
        "completed",
    }:
        raise ValueError(
            f"不支持的子任务状态：{status}"
        )

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    connection = get_connection()

    try:
        task_row = connection.execute(
            """
            SELECT status
            FROM tasks
            WHERE user_open_id = ?
              AND id = ?
            """,
            (
                user_open_id,
                task_id,
            ),
        ).fetchone()

        if (
            task_row is None
            or task_row["status"] != "pending"
        ):
            return False

        cursor = connection.execute(
            """
            UPDATE task_subtasks
            SET
                status = ?,
                updated_at = ?
            WHERE user_open_id = ?
              AND task_id = ?
              AND position = ?
            """,
            (
                status,
                now,
                user_open_id,
                task_id,
                position,
            ),
        )

        connection.commit()

        return cursor.rowcount > 0

    finally:
        connection.close()


def get_task_subtask_progress(
    user_open_id: str,
    task_id: int,
) -> dict:
    """返回子任务完成数量与整体百分比。"""

    subtasks = get_task_subtasks(
        user_open_id,
        task_id,
    )

    total = len(subtasks)
    completed = sum(
        1
        for item in subtasks
        if item.get("status") == "completed"
    )
    percent = (
        round(completed * 100 / total)
        if total
        else 0
    )

    return {
        "total": total,
        "completed": completed,
        "percent": percent,
    }


# =========================
# User Preferences
# =========================

def get_user_preference(
    user_open_id: str,
):
    """
    查询用户偏好。

    如果用户还没有初始化记录，
    返回 None。
    """

    connection = get_connection()

    try:

        cursor = connection.execute(
            """
            SELECT
                id,
                user_open_id,
                priority_strategy,
                category_order,
                interest_keywords,
                news_enabled,
                assistant_tone,
                onboarding_completed,
                morning_push_enabled,
                morning_push_time,
                evening_push_enabled,
                evening_push_time,
                created_at,
                updated_at
            FROM user_preferences
            WHERE user_open_id = ?
            """,
            (user_open_id,),
        )

        row = cursor.fetchone()

        if row is None:
            return None

        result = dict(row)
        raw_category_order = result.get("category_order")

        if raw_category_order:
            try:
                result["category_order"] = normalize_category_order(
                    json.loads(raw_category_order)
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                result["category_order"] = None
        else:
            result["category_order"] = None

        raw_keywords = result.get("interest_keywords")
        if raw_keywords:
            try:
                keywords = json.loads(raw_keywords)
                result["interest_keywords"] = (
                    keywords if isinstance(keywords, list) else []
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                result["interest_keywords"] = []
        else:
            result["interest_keywords"] = []

        return result

    finally:
        connection.close()


def save_user_preference(
    user_open_id: str,
    priority_strategy: str,
) -> None:
    """
    保存用户的优先级策略。
    """

    allowed_strategies = {
        "deadline",
        "importance",
        "quick_win",
        "balanced",
    }

    if priority_strategy not in allowed_strategies:
        raise ValueError(
            f"不支持的优先级策略：{priority_strategy}"
        )

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    connection = get_connection()

    try:

        connection.execute(
            """
            INSERT INTO user_preferences (
                user_open_id,
                priority_strategy,
                onboarding_completed,
                morning_push_enabled,
                evening_push_enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, 1, 0, 0, ?, ?)

            ON CONFLICT(user_open_id)
            DO UPDATE SET
                priority_strategy = excluded.priority_strategy,
                onboarding_completed = 1,
                updated_at = excluded.updated_at
            """,
            (
                user_open_id,
                priority_strategy,
                now,
                now,
            ),
        )

        connection.commit()

        print(
            "用户偏好已保存："
            f"user={user_open_id}, "
            f"strategy={priority_strategy}",
            flush=True,
        )

    finally:
        connection.close()


def has_completed_onboarding(
    user_open_id: str,
) -> bool:
    """
    判断用户是否已经完成首次设置。
    """

    preference = get_user_preference(
        user_open_id
    )

    if preference is None:
        return False

    return bool(
        preference.get(
            "onboarding_completed"
        )
    )


def update_push_preference(
    user_open_id: str,
    push_type: str,
    enabled=None,
    push_time=None,
) -> bool:
    if enabled is None and push_time is None:
        raise ValueError("enabled 和 push_time 至少提供一个")

    enabled_column, time_column = _get_push_columns(push_type)

    if push_time is not None:
        _validate_push_time(push_time)

    now = datetime.now().isoformat(timespec="seconds")
    connection = get_connection()

    try:
        connection.execute(
            """
            INSERT INTO user_preferences (
                user_open_id,
                morning_push_enabled,
                evening_push_enabled,
                created_at,
                updated_at
            )
            VALUES (?, 0, 0, ?, ?)
            ON CONFLICT(user_open_id) DO NOTHING
            """,
            (user_open_id, now, now),
        )

        assignments = []
        params = []

        if enabled is not None:
            assignments.append(f"{enabled_column} = ?")
            params.append(1 if enabled else 0)

        if push_time is not None:
            assignments.append(f"{time_column} = ?")
            params.append(push_time)

        assignments.append("updated_at = ?")
        params.append(now)
        params.append(user_open_id)

        connection.execute(
            f"""
            UPDATE user_preferences
            SET {", ".join(assignments)}
            WHERE user_open_id = ?
            """,
            params,
        )
        connection.commit()
        return True
    finally:
        connection.close()


def save_onboarding_strategy(
    user_open_id: str,
    priority_strategy: str,
) -> None:
    """保存 Onboarding 第一步，但暂不把首次设置标记为完成。"""
    allowed_strategies = {
        "deadline",
        "importance",
        "quick_win",
        "balanced",
    }
    if priority_strategy not in allowed_strategies:
        raise ValueError(f"不支持的优先级策略：{priority_strategy}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT INTO user_preferences (
                user_open_id,
                priority_strategy,
                onboarding_completed,
                morning_push_enabled,
                evening_push_enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, 0, 0, 0, ?, ?)
            ON CONFLICT(user_open_id)
            DO UPDATE SET
                priority_strategy = excluded.priority_strategy,
                onboarding_completed = 0,
                updated_at = excluded.updated_at
            """,
            (user_open_id, priority_strategy, now, now),
        )
        connection.commit()
    finally:
        connection.close()


def update_category_order(
    user_open_id: str,
    category_order: list,
    complete_onboarding: bool = False,
) -> None:
    """保存用户亲自选择的事务类别排序。"""
    normalized = normalize_category_order(category_order)
    serialized = json.dumps(normalized, ensure_ascii=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    completed = 1 if complete_onboarding else 0

    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT INTO user_preferences (
                user_open_id,
                category_order,
                onboarding_completed,
                morning_push_enabled,
                evening_push_enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT(user_open_id)
            DO UPDATE SET
                category_order = excluded.category_order,
                onboarding_completed = CASE
                    WHEN excluded.onboarding_completed = 1 THEN 1
                    ELSE user_preferences.onboarding_completed
                END,
                updated_at = excluded.updated_at
            """,
            (user_open_id, serialized, completed, now, now),
        )
        connection.commit()
    finally:
        connection.close()


def update_news_preference(
    user_open_id: str,
    keywords=None,
    enabled=None,
) -> None:
    if keywords is None and enabled is None:
        raise ValueError("keywords 和 enabled 至少提供一个")

    normalized_keywords = None
    if keywords is not None:
        normalized_keywords = []
        for value in keywords:
            keyword = str(value or "").strip()
            if keyword and keyword not in normalized_keywords:
                normalized_keywords.append(keyword[:40])
        if not 1 <= len(normalized_keywords) <= 5:
            raise ValueError("兴趣关键词数量必须在 1 到 5 个之间")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT INTO user_preferences (
                user_open_id,
                onboarding_completed,
                morning_push_enabled,
                evening_push_enabled,
                created_at,
                updated_at
            ) VALUES (?, 0, 0, 0, ?, ?)
            ON CONFLICT(user_open_id) DO NOTHING
            """,
            (user_open_id, now, now),
        )
        assignments = ["updated_at = ?"]
        values = [now]
        if normalized_keywords is not None:
            assignments.append("interest_keywords = ?")
            values.append(json.dumps(normalized_keywords, ensure_ascii=False))
        if enabled is not None:
            assignments.append("news_enabled = ?")
            values.append(1 if enabled else 0)
        values.append(user_open_id)
        connection.execute(
            f"UPDATE user_preferences SET {', '.join(assignments)} "
            "WHERE user_open_id = ?",
            values,
        )
        connection.commit()
    finally:
        connection.close()


def update_assistant_tone(user_open_id: str, assistant_tone: str) -> None:
    if assistant_tone not in {"gentle", "playful", "professional"}:
        raise ValueError("不支持的助手语气")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT INTO user_preferences (
                user_open_id,
                assistant_tone,
                onboarding_completed,
                morning_push_enabled,
                evening_push_enabled,
                created_at,
                updated_at
            ) VALUES (?, ?, 0, 0, 0, ?, ?)
            ON CONFLICT(user_open_id)
            DO UPDATE SET
                assistant_tone = excluded.assistant_tone,
                updated_at = excluded.updated_at
            """,
            (user_open_id, assistant_tone, now, now),
        )
        connection.commit()
    finally:
        connection.close()


def list_push_enabled_users(push_type: str):
    enabled_column, time_column = _get_push_columns(push_type)
    connection = get_connection()
    try:
        rows = connection.execute(
            f"""
            SELECT
                user_open_id,
                {enabled_column} AS push_enabled,
                {time_column} AS push_time
            FROM user_preferences
            WHERE {enabled_column} = 1
              AND onboarding_completed = 1
            ORDER BY user_open_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def has_push_been_delivered(
    user_open_id: str,
    push_type: str,
    delivery_date: str,
) -> bool:
    _get_push_columns(push_type)
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT 1
            FROM push_delivery_log
            WHERE user_open_id = ?
              AND push_type = ?
              AND delivery_date = ?
            LIMIT 1
            """,
            (user_open_id, push_type, delivery_date),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def record_push_delivery(
    user_open_id: str,
    push_type: str,
    delivery_date: str,
    delivered_at=None,
) -> bool:
    _get_push_columns(push_type)

    if delivered_at is None:
        delivered_at = datetime.now().isoformat(timespec="seconds")

    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO push_delivery_log (
                user_open_id,
                push_type,
                delivery_date,
                delivered_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (user_open_id, push_type, delivery_date, delivered_at),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def save_daily_plan_snapshot(
    user_open_id: str,
    plan_date: str,
    plan: list,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    current_plan = plan if isinstance(plan, list) else []
    connection = get_connection()
    try:
        existing_row = connection.execute(
            """
            SELECT plan_json
            FROM daily_plan_snapshots
            WHERE user_open_id = ? AND plan_date = ?
            """,
            (user_open_id, plan_date),
        ).fetchone()

        baseline_plan = list(current_plan)
        if existing_row is not None:
            try:
                existing_payload = json.loads(existing_row["plan_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                existing_payload = []

            if isinstance(existing_payload, dict):
                existing_baseline = existing_payload.get("baseline_plan")
                if not isinstance(existing_baseline, list):
                    existing_baseline = existing_payload.get("current_plan")
            else:
                existing_baseline = existing_payload

            if not isinstance(existing_baseline, list):
                existing_baseline = []

            baseline_plan = list(existing_baseline)
            existing_task_ids = {
                str(item.get("task_id"))
                for item in baseline_plan
                if isinstance(item, dict) and item.get("task_id") is not None
            }
            new_task_ids = {
                str(item.get("task_id"))
                for item in current_plan
                if (
                    isinstance(item, dict)
                    and item.get("task_id") is not None
                    and str(item.get("task_id")) not in existing_task_ids
                )
            }
            baseline_plan.extend(
                item
                for item in current_plan
                if (
                    isinstance(item, dict)
                    and item.get("task_id") is not None
                    and str(item.get("task_id")) in new_task_ids
                )
            )

        payload = json.dumps(
            {
                "baseline_plan": baseline_plan,
                "current_plan": current_plan,
            },
            ensure_ascii=False,
        )
        connection.execute(
            """
            INSERT INTO daily_plan_snapshots (
                user_open_id, plan_date, plan_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_open_id, plan_date)
            DO UPDATE SET
                plan_json = excluded.plan_json,
                updated_at = excluded.updated_at
            """,
            (user_open_id, plan_date, payload, now, now),
        )
        connection.commit()
    finally:
        connection.close()


def get_daily_plan_snapshot(user_open_id: str, plan_date: str):
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT plan_json, created_at, updated_at
            FROM daily_plan_snapshots
            WHERE user_open_id = ? AND plan_date = ?
            """,
            (user_open_id, plan_date),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["plan_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = []

        if isinstance(payload, dict):
            plan = payload.get("current_plan")
            baseline_plan = payload.get("baseline_plan")
        else:
            plan = payload
            baseline_plan = payload

        if not isinstance(plan, list):
            plan = []
        if not isinstance(baseline_plan, list):
            baseline_plan = plan

        return {
            "plan_date": plan_date,
            "plan": plan,
            "baseline_plan": baseline_plan,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    finally:
        connection.close()


def has_reminder_been_delivered(user_open_id: str, reminder_key: str) -> bool:
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT 1 FROM reminder_delivery_log
            WHERE user_open_id = ? AND reminder_key = ?
            """,
            (user_open_id, reminder_key),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def record_reminder_delivery(
    user_open_id: str,
    reminder_key: str,
    delivered_at: str,
) -> bool:
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO reminder_delivery_log (
                user_open_id, reminder_key, delivered_at
            ) VALUES (?, ?, ?)
            """,
            (user_open_id, reminder_key, delivered_at),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


# =========================
# Risk Alerts / Rescue Plans
# =========================

_ACTIVE_RISK_STATUSES = {
    "awaiting_status",
    "awaiting_confirmation",
}


def list_onboarded_users() -> list:
    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT user_open_id
            FROM user_preferences
            WHERE onboarding_completed = 1
            ORDER BY user_open_id
            """
        ).fetchall()
        return [row["user_open_id"] for row in rows]
    finally:
        connection.close()


def create_risk_alert(
    user_open_id: str,
    task_id: int,
    risk_date: str,
    risk_type: str,
    risk_reason: str,
    snapshot_json: str,
    alerted_at: str,
):
    """Create one daily alert per user/task, returning its id if new."""
    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO risk_alerts (
                user_open_id,
                task_id,
                risk_date,
                risk_type,
                risk_reason,
                snapshot_json,
                status,
                alerted_at,
                state_changed_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'awaiting_status', ?, ?, ?)
            """,
            (
                user_open_id,
                int(task_id),
                risk_date,
                risk_type,
                risk_reason,
                snapshot_json,
                alerted_at,
                alerted_at,
                alerted_at,
            ),
        )
        connection.commit()
        return cursor.lastrowid if cursor.rowcount > 0 else None
    finally:
        connection.close()


def delete_risk_alert(alert_id: int, user_open_id: str) -> bool:
    """Remove an unsent alert so a later polling pass may retry it."""
    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            DELETE FROM risk_alerts
            WHERE id = ? AND user_open_id = ?
            """,
            (int(alert_id), user_open_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def get_active_risk_alert(user_open_id: str):
    connection = get_connection()

    try:
        placeholders = ", ".join("?" for _ in _ACTIVE_RISK_STATUSES)
        row = connection.execute(
            f"""
            SELECT *
            FROM risk_alerts
            WHERE user_open_id = ?
              AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_open_id, *sorted(_ACTIVE_RISK_STATUSES)),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def save_risk_rescue_proposal(
    alert_id: int,
    user_open_id: str,
    user_context: str,
    proposal_json: str,
    changed_at: str,
) -> bool:
    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            UPDATE risk_alerts
            SET user_context = ?,
                proposal_json = ?,
                status = 'awaiting_confirmation',
                state_changed_at = ?,
                reminded_at = NULL,
                updated_at = ?
            WHERE id = ?
              AND user_open_id = ?
              AND status IN ('awaiting_status', 'awaiting_confirmation')
            """,
            (
                user_context,
                proposal_json,
                changed_at,
                changed_at,
                int(alert_id),
                user_open_id,
            ),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def close_risk_alert(
    alert_id: int,
    user_open_id: str,
    status: str,
    changed_at: str,
) -> bool:
    if status not in {"confirmed", "dismissed", "resolved"}:
        raise ValueError(f"不支持的风险状态：{status}")

    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            UPDATE risk_alerts
            SET status = ?,
                state_changed_at = ?,
                updated_at = ?
            WHERE id = ?
              AND user_open_id = ?
              AND status IN ('awaiting_status', 'awaiting_confirmation')
            """,
            (
                status,
                changed_at,
                changed_at,
                int(alert_id),
                user_open_id,
            ),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def list_due_risk_reminders(cutoff_at: str) -> list:
    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT *
            FROM risk_alerts
            WHERE status IN ('awaiting_status', 'awaiting_confirmation')
              AND reminded_at IS NULL
              AND state_changed_at <= ?
            ORDER BY state_changed_at, id
            """,
            (cutoff_at,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def mark_risk_alert_reminded(
    alert_id: int,
    reminded_at: str,
) -> bool:
    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            UPDATE risk_alerts
            SET reminded_at = ?, updated_at = ?
            WHERE id = ?
              AND reminded_at IS NULL
              AND status IN ('awaiting_status', 'awaiting_confirmation')
            """,
            (reminded_at, reminded_at, int(alert_id)),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def get_latest_confirmed_risk_plan(user_open_id: str):
    connection = get_connection()

    try:
        row = connection.execute(
            """
            SELECT *
            FROM risk_alerts
            WHERE user_open_id = ?
              AND status = 'confirmed'
              AND proposal_json IS NOT NULL
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (user_open_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


# =========================
# Blocked Times
# =========================

def create_blocked_time(
    user_open_id: str,
    title: str,
    start_time: str,
    end_time: str,
    source: str = "manual",
) -> int:
    """
    创建一条忙碌时间记录。

    start_time / end_time 格式：
    YYYY-MM-DD HH:MM
    """

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    connection = get_connection()

    try:

        cursor = connection.execute(
            """
            INSERT INTO blocked_times (
                user_open_id,
                title,
                start_time,
                end_time,
                source,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_open_id,
                title,
                start_time,
                end_time,
                source,
                now,
                now,
            ),
        )

        connection.commit()

        blocked_time_id = cursor.lastrowid

        print(
            "忙碌时间已写入数据库，"
            f"id={blocked_time_id}",
            flush=True,
        )

        return blocked_time_id

    finally:
        connection.close()


def replace_blocked_times_for_source(
    user_open_id: str,
    source: str,
    blocked_times: list,
) -> int:
    """
    原子替换某个来源的全部忙碌时间。

    课程重复导入时只替换同一学期来源，
    不影响手工记录或其他学期。
    """

    if not source:
        raise ValueError("source 不能为空")

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = []

    for item in blocked_times:
        title = item.get("title")
        start_time = item.get("start_time")
        end_time = item.get("end_time")

        if not title or not start_time or not end_time:
            raise ValueError("忙碌时间缺少标题、开始或结束时间")

        rows.append(
            (
                user_open_id,
                title,
                start_time,
                end_time,
                source,
                now,
                now,
            )
        )

    connection = get_connection()

    try:
        connection.execute(
            """
            DELETE FROM blocked_times
            WHERE user_open_id = ?
              AND source = ?
            """,
            (user_open_id, source),
        )

        if rows:
            connection.executemany(
                """
                INSERT INTO blocked_times (
                    user_open_id,
                    title,
                    start_time,
                    end_time,
                    source,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        connection.commit()
        return len(rows)

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def get_user_blocked_times(
    user_open_id: str,
    date_text: str | None = None,
) -> list:
    """
    查询某个用户的忙碌时间。

    如果指定 date_text：
    例如 2026-08-17
    则只返回当天记录。
    """

    connection = get_connection()

    try:

        if date_text:

            cursor = connection.execute(
                """
                SELECT
                    id,
                    title,
                    start_time,
                    end_time,
                    source,
                    created_at,
                    updated_at
                FROM blocked_times
                WHERE user_open_id = ?
                  AND substr(start_time, 1, 10) = ?
                ORDER BY start_time ASC
                """,
                (
                    user_open_id,
                    date_text,
                ),
            )

        else:

            cursor = connection.execute(
                """
                SELECT
                    id,
                    title,
                    start_time,
                    end_time,
                    source,
                    created_at,
                    updated_at
                FROM blocked_times
                WHERE user_open_id = ?
                ORDER BY start_time ASC
                """,
                (user_open_id,),
            )

        rows = cursor.fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        connection.close()


def delete_blocked_times_for_date(
    user_open_id: str,
    date_text: str,
) -> int:
    """
    删除用户某一天全部忙碌时间。

    返回删除条数。
    """

    connection = get_connection()

    try:

        cursor = connection.execute(
            """
            DELETE FROM blocked_times
            WHERE user_open_id = ?
              AND substr(start_time, 1, 10) = ?
            """,
            (
                user_open_id,
                date_text,
            ),
        )

        connection.commit()

        deleted_count = cursor.rowcount

        print(
            "删除忙碌时间完成："
            f"user={user_open_id}, "
            f"date={date_text}, "
            f"count={deleted_count}",
            flush=True,
        )

        return deleted_count

    finally:
        connection.close()


# =========================
# Local Test
# =========================

if __name__ == "__main__":

    init_db()

    print(
        "\n===== 任务修改测试 ====="
    )

    test_user = "update_task_test_user"

    # -------------------------
    # 1. 创建测试任务
    # -------------------------

    task_id = create_task(
        test_user,
        {
            "title": "整理项目截图",
            "deadline": "2026-08-18 20:00",
            "estimated_minutes": 30,
            "priority": "high",
        },
    )

    print(
        "\n1. 修改前："
    )

    tasks = get_user_tasks(
        test_user
    )

    for task in tasks:

        if task["id"] == task_id:
            print(task)

    # -------------------------
    # 2. 修改任务
    # -------------------------

    print(
        "\n2. 开始修改："
    )

    success = update_task(
        task_id,
        test_user,
        {
            "deadline": "2026-08-18 21:00",
            "estimated_minutes": 60,
        },
    )

    print(
        "修改结果：",
        success,
    )

    # -------------------------
    # 3. 查询修改结果
    # -------------------------

    print(
        "\n3. 修改后："
    )

    tasks = get_user_tasks(
        test_user
    )

    for task in tasks:

        if task["id"] == task_id:
            print(task)
