import io
import posixpath
import re
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree


class CourseImportError(ValueError):
    pass


PERIOD_TIMES = {
    1: ("08:15", "08:55"),
    2: ("09:00", "09:40"),
    3: ("09:50", "10:30"),
    4: ("10:40", "11:20"),
    5: ("11:25", "12:05"),
    6: ("13:30", "14:10"),
    7: ("14:20", "15:00"),
    8: ("15:10", "15:50"),
    9: ("16:00", "16:40"),
    10: ("18:30", "19:10"),
    11: ("19:20", "20:00"),
    12: ("20:10", "20:50"),
}

WEEKDAY_INDEX = {
    "星期一": 0,
    "星期二": 1,
    "星期三": 2,
    "星期四": 3,
    "星期五": 4,
    "星期六": 5,
    "星期日": 6,
}

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_XLSX_ENTRIES = 1000
MAX_XLSX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ICS_BYTES = 5 * 1024 * 1024


def _column_index(cell_reference):
    match = re.match(r"([A-Z]+)", cell_reference or "")

    if match is None:
        raise CourseImportError("课表中存在无法识别的单元格地址")

    result = 0

    for character in match.group(1):
        result = result * 26 + ord(character) - ord("A") + 1

    return result


def _read_shared_strings(archive):
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    return [
        "".join(
            text.text or ""
            for text in item.findall(f".//{{{_MAIN_NS}}}t")
        )
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]


def _get_first_sheet_path(archive):
    workbook_root = ElementTree.fromstring(
        archive.read("xl/workbook.xml")
    )
    first_sheet = workbook_root.find(
        f".//{{{_MAIN_NS}}}sheet"
    )

    if first_sheet is None:
        raise CourseImportError("Excel 中没有可读取的工作表")

    relationship_id = first_sheet.get(f"{{{_REL_NS}}}id")
    relationships_root = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )

    for relationship in relationships_root.findall(
        f"{{{_PACKAGE_REL_NS}}}Relationship"
    ):
        if relationship.get("Id") != relationship_id:
            continue

        target = relationship.get("Target") or ""

        if target.startswith("/"):
            return target.lstrip("/")

        return posixpath.normpath(posixpath.join("xl", target))

    raise CourseImportError("无法定位 Excel 的第一个工作表")


def _read_cells(file_bytes):
    try:
        archive = zipfile.ZipFile(io.BytesIO(file_bytes))
    except (zipfile.BadZipFile, TypeError) as exc:
        raise CourseImportError("文件不是有效的 .xlsx 课表") from exc

    with archive:
        entries = archive.infolist()

        if (
            len(entries) > MAX_XLSX_ENTRIES
            or sum(item.file_size for item in entries)
            > MAX_XLSX_UNCOMPRESSED_BYTES
        ):
            raise CourseImportError("Excel 解压后的内容过大")

        try:
            shared_strings = _read_shared_strings(archive)
            worksheet_path = _get_first_sheet_path(archive)
            worksheet_root = ElementTree.fromstring(
                archive.read(worksheet_path)
            )
        except (KeyError, ElementTree.ParseError) as exc:
            raise CourseImportError("Excel 文件结构不完整或已损坏") from exc

    cells = {}

    for cell in worksheet_root.findall(f".//{{{_MAIN_NS}}}c"):
        reference = cell.get("r") or ""
        row_match = re.search(r"(\d+)$", reference)

        if row_match is None:
            continue

        cell_type = cell.get("t")

        if cell_type == "inlineStr":
            value = "".join(
                item.text or ""
                for item in cell.findall(f".//{{{_MAIN_NS}}}t")
            )
        else:
            value_node = cell.find(f"{{{_MAIN_NS}}}v")
            value = value_node.text if value_node is not None else ""

            if cell_type == "s" and value:
                try:
                    value = shared_strings[int(value)]
                except (IndexError, ValueError) as exc:
                    raise CourseImportError(
                        "Excel 共享文本索引无效"
                    ) from exc

        cells[(_column_index(reference), int(row_match.group(1)))] = value

    return cells


def _normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _parse_semester(title):
    match = re.search(
        r"(\d{4})\s*-\s*(\d{4})学年第\s*(\d+)\s*学期",
        _normalize_text(title),
    )

    if match is None:
        raise CourseImportError("无法从课表标题识别学年和学期")

    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _parse_course_cell(text, weekday_name):
    normalized = _normalize_text(text)
    match = re.match(
        r"^(?P<title>.*?)(?:★|●)?\s*"
        r"\((?P<start>\d+)\s*[-—－]\s*(?P<end>\d+)节\)"
        r"\s*(?P<weeks>[^/]+?周)(?:/|$)",
        normalized,
    )

    if match is None:
        return None

    start_period = int(match.group("start"))
    end_period = int(match.group("end"))

    if (
        start_period not in PERIOD_TIMES
        or end_period not in PERIOD_TIMES
        or end_period < start_period
    ):
        raise CourseImportError("课表中存在不支持的课程节次")

    location_match = re.search(
        r"/场地\s*:\s*(.*?)/教师\s*:",
        normalized,
    )
    location = (
        _normalize_text(location_match.group(1))
        if location_match is not None
        else ""
    )
    title = match.group("title").strip().rstrip("★●").strip()

    if not title:
        raise CourseImportError("课表中存在没有课程名称的正式课程")

    return {
        "title": title,
        "weekday": weekday_name,
        "weekday_index": WEEKDAY_INDEX[weekday_name],
        "start_period": start_period,
        "end_period": end_period,
        "week_spec": match.group("weeks").replace(" ", ""),
        "location": location,
    }


def parse_course_workbook(file_bytes):
    if not isinstance(file_bytes, (bytes, bytearray)):
        raise CourseImportError("课表内容必须是二进制 Excel 文件")

    cells = _read_cells(bytes(file_bytes))
    header_row = None
    weekday_columns = {}

    for (column, row), value in cells.items():
        normalized = _normalize_text(value)

        if normalized in WEEKDAY_INDEX:
            weekday_columns[column] = normalized
            header_row = row if header_row is None else header_row

    if header_row is None or len(weekday_columns) != 7:
        raise CourseImportError(
            "未找到完整的星期一至星期日课表表头"
        )

    title = cells.get((1, 1), "")
    semester = _parse_semester(title)
    rules = []

    for (column, row), value in cells.items():
        weekday_name = weekday_columns.get(column)

        if weekday_name is None or row <= header_row or not value:
            continue

        rule = _parse_course_cell(value, weekday_name)

        if rule is not None:
            rules.append(rule)

    if not rules:
        raise CourseImportError("没有识别到正式课程安排")

    unique_rules = []
    seen = set()

    for rule in rules:
        key = (
            rule["title"],
            rule["weekday_index"],
            rule["start_period"],
            rule["end_period"],
            rule["week_spec"],
            rule["location"],
        )

        if key not in seen:
            seen.add(key)
            unique_rules.append(rule)

    return {
        "semester": semester,
        "rules": unique_rules,
    }


def _parse_ics_datetime(value):
    text = str(value or "").strip()
    for pattern in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(text.rstrip("Z"), pattern)
        except ValueError:
            continue
    raise CourseImportError(f"ICS 中存在无法识别的日期时间：{value}")


def parse_course_calendar(file_bytes):
    if not isinstance(file_bytes, (bytes, bytearray)):
        raise CourseImportError("课表内容必须是二进制 ICS 文件")
    if len(file_bytes) > MAX_ICS_BYTES:
        raise CourseImportError("ICS 文件超过 5 MB")

    try:
        text = bytes(file_bytes).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CourseImportError("ICS 文件必须使用 UTF-8 编码") from exc

    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    events = re.findall(
        r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT",
        unfolded,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not events:
        raise CourseImportError("ICS 中没有可读取的课程事件")

    occurrences = []
    seen = set()
    for event_text in events:
        fields = {}
        for line in event_text.splitlines():
            if ":" not in line:
                continue
            raw_key, value = line.split(":", 1)
            key = raw_key.split(";", 1)[0].upper()
            fields.setdefault(key, value.strip())

        title = _normalize_text(fields.get("SUMMARY"))
        if not title:
            continue
        start = _parse_ics_datetime(fields.get("DTSTART"))
        end = _parse_ics_datetime(fields.get("DTEND"))
        if end <= start:
            raise CourseImportError(f"ICS 课程结束时间不晚于开始时间：{title}")
        location = _normalize_text(fields.get("LOCATION"))

        starts = [start]
        rrule = fields.get("RRULE")
        if rrule:
            parts = {}
            for item in rrule.split(";"):
                if "=" in item:
                    key, value = item.split("=", 1)
                    parts[key.upper()] = value
            if parts.get("FREQ") != "WEEKLY":
                raise CourseImportError("当前 ICS 只支持每周重复课程")
            interval = max(1, int(parts.get("INTERVAL", "1")))
            count = min(30, max(1, int(parts.get("COUNT", "30"))))
            until = (
                _parse_ics_datetime(parts["UNTIL"])
                if parts.get("UNTIL")
                else None
            )
            starts = []
            cursor = start
            for _ in range(count):
                if until is not None and cursor > until:
                    break
                starts.append(cursor)
                cursor += timedelta(weeks=interval)

        duration = end - start
        for occurrence_start in starts:
            occurrence_end = occurrence_start + duration
            display_title = f"课程：{title}"
            if location:
                display_title += f"（{location}）"
            item = {
                "title": display_title,
                "start_time": occurrence_start.strftime("%Y-%m-%d %H:%M"),
                "end_time": occurrence_end.strftime("%Y-%m-%d %H:%M"),
            }
            key = (item["title"], item["start_time"], item["end_time"])
            if key not in seen:
                seen.add(key)
                occurrences.append(item)

    occurrences.sort(key=lambda item: item["start_time"])
    if not occurrences:
        raise CourseImportError("ICS 中没有识别到有效课程")
    semester = f"ICS-{occurrences[0]['start_time'][:7]}"
    return {"semester": semester, "occurrences": occurrences}


def format_ics_import_confirmation(course_data):
    occurrences = course_data["occurrences"]
    return (
        "📚 ICS 课表解析完成\n\n"
        f"课程事件：{len(occurrences)} 条\n"
        f"日期范围：{occurrences[0]['start_time'][:10]} 至 "
        f"{occurrences[-1]['start_time'][:10]}\n\n"
        "如果信息正确，请回复「确认导入」；\n"
        "不想导入请回复「取消」。"
    )


def _expand_week_spec(week_spec):
    normalized = (
        str(week_spec or "")
        .replace("，", ",")
        .replace("、", ",")
        .replace("－", "-")
        .replace("—", "-")
        .replace("第", "")
        .replace(" ", "")
    )
    weeks = set()

    for part in normalized.split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?周?", part)

        if match is None:
            raise CourseImportError(f"无法识别教学周：{week_spec}")

        start_week = int(match.group(1))
        end_week = int(match.group(2) or start_week)

        if start_week < 1 or end_week < start_week or end_week > 30:
            raise CourseImportError(f"教学周范围无效：{week_spec}")

        weeks.update(range(start_week, end_week + 1))

    return sorted(weeks)


def parse_first_week_monday(text):
    match = re.search(
        r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})",
        str(text or ""),
    )

    if match is None:
        raise CourseImportError("请使用 YYYY-MM-DD 格式输入日期")

    try:
        result = date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
    except ValueError as exc:
        raise CourseImportError("第一周星期一日期无效") from exc

    if result.weekday() != 0:
        raise CourseImportError("这个日期不是星期一，请重新核对")

    return result


def build_course_occurrences(course_data, first_week_monday):
    if isinstance(first_week_monday, str):
        first_week_monday = parse_first_week_monday(first_week_monday)

    if not isinstance(first_week_monday, date):
        raise CourseImportError("第一周日期无效")

    occurrences = []
    seen = set()

    for rule in course_data.get("rules", []):
        start_clock = PERIOD_TIMES[rule["start_period"]][0]
        end_clock = PERIOD_TIMES[rule["end_period"]][1]

        for week in _expand_week_spec(rule["week_spec"]):
            course_date = (
                first_week_monday
                + timedelta(
                    days=(week - 1) * 7 + rule["weekday_index"]
                )
            )
            start_time = f"{course_date.isoformat()} {start_clock}"
            end_time = f"{course_date.isoformat()} {end_clock}"
            title = f"课程：{rule['title']}"

            if rule.get("location"):
                title += f"（{rule['location']}）"

            key = (title, start_time, end_time)

            if key in seen:
                continue

            seen.add(key)
            occurrences.append(
                {
                    "title": title,
                    "start_time": start_time,
                    "end_time": end_time,
                }
            )

    occurrences.sort(key=lambda item: item["start_time"])

    if not occurrences:
        raise CourseImportError("课表没有生成任何有效课程日期")

    return occurrences


def format_course_rules(course_data):
    lines = [
        f"识别到 {len(course_data.get('rules', []))} 条课程安排规则：",
    ]

    for rule in course_data.get("rules", []):
        lines.append(
            f"- {rule['weekday']} "
            f"{rule['start_period']}-{rule['end_period']}节 "
            f"{rule['title']}（{rule['week_spec']}）"
        )

    return "\n".join(lines)


def format_import_confirmation(course_data, occurrences, first_week_monday):
    first_date = occurrences[0]["start_time"][:10]
    last_date = occurrences[-1]["start_time"][:10]

    return (
        "📚 课表日期已生成\n\n"
        f"学期：{course_data['semester']}\n"
        f"第一周星期一：{first_week_monday.isoformat()}\n"
        f"正式课程规则：{len(course_data['rules'])} 条\n"
        f"将写入固定安排：{len(occurrences)} 条\n"
        f"日期范围：{first_date} 至 {last_date}\n\n"
        "如果信息正确，请回复「确认导入」；\n"
        "不想导入请回复「取消」。"
    )
