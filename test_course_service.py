import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import course_service
import database
from planner import generate_execution_plan


def _inline_cell(reference, value):
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>'
        f"{escape(str(value))}"
        "</t></is></c>"
    )


def build_test_workbook():
    cells = {
        "A1": "2026-2027学年第1学期 测试课表",
        "A2": "时间段",
        "B2": "节次",
        "C2": "星期一",
        "D2": "星期二",
        "E2": "星期三",
        "F2": "星期四",
        "G2": "星期五",
        "H2": "星期六",
        "I2": "星期日",
        "C3": (
            "软件工程★\n(1-3节)1-2周,4周/校区:测试校区/"
            "场地:教学楼101/教师:张老师/考核方式:考试"
        ),
        "E5": (
            "人工智能基础★\n(3-5节)3周/校区:测试校区/"
            "场地:实验楼202/教师:李老师/考核方式:考查"
        ),
        "A15": "实践课程：WEB开发●某老师(共3周)/1-3周;",
    }
    rows = {}

    for reference, value in cells.items():
        row = int("".join(character for character in reference if character.isdigit()))
        rows.setdefault(row, []).append(_inline_cell(reference, value))

    row_xml = "".join(
        f'<row r="{row}">{"".join(row_cells)}</row>'
        for row, row_cells in sorted(rows.items())
    )
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        f"{row_xml}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships"><sheets>'
        '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
        '</sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    output = io.BytesIO()

    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)

    return output.getvalue()


class CourseServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_parse_xlsx_and_ignore_practice_course(self):
        result = course_service.parse_course_workbook(build_test_workbook())

        self.assertEqual(result["semester"], "2026-2027-1")
        self.assertEqual(len(result["rules"]), 2)
        self.assertEqual(result["rules"][0]["title"], "软件工程")
        self.assertEqual(result["rules"][0]["week_spec"], "1-2周,4周")
        self.assertNotIn(
            "WEB开发",
            " ".join(rule["title"] for rule in result["rules"]),
        )

    def test_build_occurrences_uses_periods_weeks_and_dates(self):
        course_data = course_service.parse_course_workbook(
            build_test_workbook()
        )
        occurrences = course_service.build_course_occurrences(
            course_data,
            date(2026, 9, 21),
        )

        self.assertEqual(len(occurrences), 4)
        self.assertEqual(occurrences[0]["start_time"], "2026-09-21 08:15")
        self.assertEqual(occurrences[0]["end_time"], "2026-09-21 10:30")
        self.assertEqual(occurrences[2]["start_time"], "2026-10-07 09:50")
        self.assertEqual(occurrences[3]["start_time"], "2026-10-12 08:15")

    def test_parse_weekly_ics_course(self):
        payload = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:产品设计
LOCATION:博学楼105
DTSTART:20260922T081500
DTEND:20260922T103000
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
END:VCALENDAR
""".encode("utf-8")

        result = course_service.parse_course_calendar(payload)

        self.assertEqual(len(result["occurrences"]), 3)
        self.assertEqual(
            result["occurrences"][0]["start_time"],
            "2026-09-22 08:15",
        )
        self.assertIn("博学楼105", result["occurrences"][0]["title"])

    def test_first_week_date_accepts_dots_and_requires_monday(self):
        self.assertEqual(
            course_service.parse_first_week_monday("2026.9.21星期一"),
            date(2026, 9, 21),
        )

        with self.assertRaises(course_service.CourseImportError):
            course_service.parse_first_week_monday("2026-09-22")

    def test_replace_same_semester_is_idempotent_and_preserves_manual(self):
        database.create_blocked_time(
            "u1",
            "手工会议",
            "2026-09-21 14:00",
            "2026-09-21 15:00",
            source="manual",
        )
        first = [
            {
                "title": "课程：软件工程",
                "start_time": "2026-09-21 08:15",
                "end_time": "2026-09-21 10:30",
            }
        ]
        second = [
            {
                "title": "课程：软件工程",
                "start_time": "2026-09-28 08:15",
                "end_time": "2026-09-28 10:30",
            }
        ]

        database.replace_blocked_times_for_source(
            "u1", "course:2026-2027-1", first
        )
        database.replace_blocked_times_for_source(
            "u1", "course:2026-2027-1", second
        )
        rows = database.get_user_blocked_times("u1")

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["source"] for row in rows},
            {"manual", "course:2026-2027-1"},
        )
        self.assertIn("2026-09-28", rows[1]["start_time"])

    def test_imported_course_is_respected_by_planner(self):
        course_data = course_service.parse_course_workbook(
            build_test_workbook()
        )
        occurrences = course_service.build_course_occurrences(
            course_data,
            date(2026, 9, 21),
        )
        blocked_windows = [
            {
                "title": item["title"],
                "start": item["start_time"],
                "end": item["end_time"],
            }
            for item in occurrences
            if item["start_time"].startswith("2026-09-21")
        ]
        plan = generate_execution_plan(
            [
                {
                    "id": 1,
                    "title": "完成作业",
                    "estimated_minutes": 60,
                    "remaining_minutes": 60,
                    "deadline": "2026-09-21 18:00",
                }
            ],
            now=datetime(2026, 9, 21, 8, 0),
            blocked_windows=blocked_windows,
        )

        self.assertEqual(plan[0]["start_time"], "2026-09-21 10:30")


if __name__ == "__main__":
    unittest.main()
