#
import os
import json
import re
import time
import threading
import warnings
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from supabase import create_client, Client
try:
    from google import genai
except ImportError:
    genai = None
legacy_genai = None
if genai is None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            import google.generativeai as legacy_genai
    except ImportError:
        legacy_genai = None

from builder import ScheduleBuilder
from optimizer import ScheduleOptimizer

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Gemini is optional: with USE_GEMINI unset/false, only the local parser is used and
# unrecognized messages get a reply with example phrasings instead of an API call.
USE_GEMINI = os.getenv("USE_GEMINI", "false").strip().lower() == "true" and bool(GEMINI_API_KEY)
gemini_client = None
if USE_GEMINI:
    if genai is not None:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    elif legacy_genai is not None:
        legacy_genai.configure(api_key=GEMINI_API_KEY)
    else:
        USE_GEMINI = False
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_MODEL_NAME = "gemini-2.5-flash"
GEMINI_JSON_CONFIG = {
    "temperature": 0,
    "response_mime_type": "application/json",
}

SCHEDULE_CONSTRAINT_TYPES = {
    "day_off",
    "time_range",
    "daily_load",
    "windows_minimization",
    "course_drop_flexibility",
    "study_days_limit",
    "weekly_balance",
    "lecturer_constraint",
    "course_modification",
    "course_schedule_pref",
    "max_consecutive_hours",
    "min_daily_hours",
    "credit_range",
    "assessment_preference",
}

TRANSIENT_CONSTRAINT_TYPES = {"info_request", "language_preference", "assessment_preference"}

UNRECOGNIZED_MESSAGE_REPLY = (
    "לא הצלחתי להבין את הבקשה. אפשר לנסות ניסוח כמו:\n"
    "• תבנה לי מערכת\n"
    "• תבנה לי מערכת בלי ימי שלישי\n"
    "• תבנה מערכת לא לפני 10:00\n"
    "• תבנה מערכת עד 16:00 עם כמה שפחות חלונות\n"
    "• תבנה לי מערכת עד 4 ימים\n"
    "• תבנה לי מערכת בלי <שם מרצה>\n"
    "• איזה קורסים יש באנגלית?"
)
DAY_NAMES_HE = {1: "ראשון", 2: "שני", 3: "שלישי", 4: "רביעי", 5: "חמישי", 6: "שישי"}
DAY_LETTERS_HE = {"א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5, "ו": 6}
ENGLISH_DAY_ALIASES = {
    "sunday": 1,
    "sun": 1,
    "monday": 2,
    "mon": 2,
    "tuesday": 3,
    "tue": 3,
    "tues": 3,
    "wednesday": 4,
    "wed": 4,
    "thursday": 5,
    "thu": 5,
    "thur": 5,
    "thurs": 5,
    "friday": 6,
    "fri": 6,
}

# =====================================================================
# --- מודלים של בקשות API ---
# =====================================================================
class ChatRequest(BaseModel):
    message: str
    student_id: int
    semester: int = 1
    session_id: Optional[int] = None
    history: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    locked_courses: Optional[List[str]] = Field(default_factory=list)

class ScheduleSelectRequest(BaseModel):
    session_id: int
    alt_key: str

class RenameRequest(BaseModel):
    title: str

class PinRequest(BaseModel):
    is_pinned: bool

# =====================================================================
# --- פונקציות עזר לשליפת נתונים ממסד הנתונים ---
# =====================================================================
def get_student_data(student_id: int, target_semester: int) -> Optional[Dict[str, Any]]:
    student_res = supabase.table('students').select('*').eq('student_id', student_id).execute()

    if not student_res.data:
        print(f"❌ שגיאה: סטודנט {student_id} לא נמצא במסד הנתונים.")
        return None

    student = student_res.data[0]

    student.pop('password', None)
    student.pop('academic_status', None)

    student_year = student.get('study_year', 1)
    program_id = student.get('program_id')

    # ---------------------------------------------------------
    # שם התוכנית
    # ---------------------------------------------------------
    program_name = None

    if program_id:
        program_res = supabase.table('program') \
            .select('program_name') \
            .eq('program_id', program_id) \
            .execute()

        if program_res.data:
            program_name = program_res.data[0].get('program_name')

    student['program_name'] = program_name

    # ---------------------------------------------------------
    # ההתמחויות נמצאות ישירות בטבלת students
    # אין יותר שימוש ב-studentspecialization
    # ---------------------------------------------------------
    specialization_ids = []

    for spec_id in [
        student.get('primary_specialization_id'),
        student.get('secondary_specialization_id')
    ]:
        if spec_id is not None and spec_id not in specialization_ids:
            specialization_ids.append(spec_id)

    student['specialization_ids'] = specialization_ids

    # ---------------------------------------------------------
    # שליפת שמות ההתמחויות
    # ---------------------------------------------------------
    specialization_names = []

    if specialization_ids and program_id:
        names_res = supabase.table('specialization') \
            .select('specialization_name') \
            .in_('specialization_id', specialization_ids) \
            .eq('program_id', program_id) \
            .execute()

        if names_res.data:
            specialization_names = [
                row.get('specialization_name')
                for row in names_res.data
            ]

    student['specialization_names'] = specialization_names

    return {
        "student_id": student_id,
        "study_year": student_year,
        "semester": target_semester,
        "program_id": program_id,
        "program_name": program_name,
        "specialization_ids": specialization_ids,
        "specialization_names": specialization_names,
        "raw_data": student
    }

def get_required_courses(student_data: dict, target_semester: int) -> dict:
    if not student_data:
        return {"mandatory": [], "specialization": {}}

    student_year = student_data.get('study_year')
    program_name = student_data.get('program_name')
    specialization_ids = student_data.get('specialization_ids', [])

    if not student_year or not program_name:
        return {"mandatory": [], "specialization": {}}

    query = supabase.table('semester_template') \
        .select('mandatory_course_ids') \
        .eq('year', student_year) \
        .eq('semester', target_semester) \
        .eq('program_name', program_name)

    template_res = query.execute()
    mandatory_course_ids = []
    if template_res.data:
        raw_ids = template_res.data[0].get('mandatory_course_ids', [])
        mandatory_course_ids = [int(x) for x in raw_ids if str(x).strip().isdigit() or isinstance(x, int)]

    specialization_courses = {}

    if specialization_ids:
        spec_res = supabase.table('specialization') \
            .select('specialization_name, elective_course_ids') \
            .in_('specialization_id', specialization_ids) \
            .execute()

        if spec_res.data:
            for row in spec_res.data:
                spec_name = row.get('specialization_name', 'התמחות לא ידועה')
                raw_elec = row.get('elective_course_ids') or []
                specialization_courses[spec_name] = [int(x) for x in raw_elec if str(x).strip().isdigit() or isinstance(x, int)]

    return {
        "mandatory": mandatory_course_ids,
        "specialization": specialization_courses
    }

def _clean_course_ids(raw_ids):
    return [
        int(x)
        for x in (raw_ids or [])
        if str(x).strip().isdigit() or isinstance(x, int)
    ]


def get_summer_courses_data(student_data: dict, target_semester: int) -> dict:
    """
    סל קורסים מיוחד לסמסטר קיץ בלבד.

    בקיץ:
    - לא מכניסים קורסים אוטומטית לבנייה.
    - רק מציגים למשתמש את הקורסים האפשריים.
    - רק אם המשתמש מבקש להוסיף קורס, הוא ייכנס לבנייה.

    מקור הקורסים:
    1. סילבוס שנה נוכחית, סמסטרים 1 ו-2
    2. סילבוס שנה הבאה, סמסטרים 1 ו-2
    3. קורסי התמחות של המשתמש
    4. סינון לפי courseoffering.semester = 3
    """
    if target_semester != 3:
        return get_required_courses(student_data, target_semester)

    empty_result = {
        "mandatory": [],
        "electives": [],
        "specialization": {},
        "summer_current_year": [],
        "summer_next_year": [],
        "summer_specialization": {},
        "is_summer_pool": True,
    }

    if not student_data:
        return empty_result

    student_year = student_data.get("study_year")
    program_name = student_data.get("program_name")
    specialization_ids = student_data.get("specialization_ids", [])

    if not student_year or not program_name:
        return empty_result

    def _get_template_courses_for_year(year_value):
        res = supabase.table("semester_template") \
            .select("mandatory_course_ids") \
            .eq("year", year_value) \
            .in_("semester", [1, 2]) \
            .eq("program_name", program_name) \
            .execute()

        course_ids = []
        if res.data:
            for row in res.data:
                course_ids.extend(
                    _clean_course_ids(row.get("mandatory_course_ids", []))
                )

        return list(dict.fromkeys(course_ids))

    current_year_syllabus_courses = _get_template_courses_for_year(student_year)
    next_year_syllabus_courses = _get_template_courses_for_year(student_year + 1)

    specialization_courses = {}

    if specialization_ids:
        spec_res = supabase.table("specialization") \
            .select("specialization_name, elective_course_ids") \
            .in_("specialization_id", specialization_ids) \
            .execute()

        if spec_res.data:
            for row in spec_res.data:
                spec_name = row.get("specialization_name", "התמחות לא ידועה")
                specialization_courses[spec_name] = _clean_course_ids(
                    row.get("elective_course_ids", [])
                )

    all_ids = set(current_year_syllabus_courses) | set(next_year_syllabus_courses)

    for course_list in specialization_courses.values():
        all_ids.update(course_list)

    offered_ids = set()

    if all_ids:
        offerings_res = supabase.table("courseoffering") \
            .select("course_id") \
            .in_("course_id", list(all_ids)) \
            .eq("semester", 3) \
            .execute()

        if offerings_res.data:
            offered_ids = {
                int(row["course_id"])
                for row in offerings_res.data
                if row.get("course_id") is not None
            }

    current_year_courses = [
        cid for cid in current_year_syllabus_courses
        if cid in offered_ids
    ]

    next_year_courses = [
        cid for cid in next_year_syllabus_courses
        if cid in offered_ids
    ]

    filtered_specialization = {}
    for spec_name, course_list in specialization_courses.items():
        filtered = [cid for cid in course_list if cid in offered_ids]
        if filtered:
            filtered_specialization[spec_name] = filtered

    return {
        # חשוב:
        # בקיץ לא מכניסים שום קורס אוטומטית לבנייה.
        "mandatory": [],
        "electives": [],
        "specialization": {},

        # אלה רק להצגה ולזיהוי קורסים שניתן להוסיף.
        "summer_current_year": current_year_courses,
        "summer_next_year": next_year_courses,
        "summer_specialization": filtered_specialization,
        "is_summer_pool": True,
    }

def get_course_metadata(course_ids: list, target_semester: int) -> dict:
    if not course_ids:
        return {}

    columns = [
        'course_id', 'course_name', 'is_attendance_mandatory', 'credits', 'language',
        'exam_weight', 'mid_semester_exam_weight', 'project_weight', 'assignments_weight',
        'semester1_test', 'semester1_test_2',
        'semester2_test', 'semester2_test_2',
        'summer_test', 'summer_test_2'
    ]

    res = supabase.table('course') \
        .select(','.join(columns)) \
        .in_('course_id', course_ids) \
        .execute()

    metadata = {}
    if res.data:
        for row in res.data:
            exam_weight = row.get('exam_weight') or 0
            has_exam = exam_weight > 0

            moed_a = None
            moed_b = None

            if has_exam:
                if target_semester == 1:
                    moed_a = row.get('semester1_test')
                    moed_b = row.get('semester1_test_2')
                elif target_semester == 2:
                    moed_a = row.get('semester2_test')
                    moed_b = row.get('semester2_test_2')
                elif target_semester == 3:
                    moed_a = row.get('summer_test')
                    moed_b = row.get('summer_test_2')

            metadata[row['course_id']] = {
                "course_name": row.get('course_name', str(row['course_id'])),
                "is_attendance_mandatory": row.get('is_attendance_mandatory', False),
                "credit_points": row.get('credits', '-'),
                "language": row.get('language'),
                "weights": {
                    "exam": exam_weight,
                    "mid_semester": row.get('mid_semester_exam_weight') or 0,
                    "project": row.get('project_weight') or 0,
                    "assignments": row.get('assignments_weight') or 0
                },
                "has_exam": has_exam,
                "exam_dates": {
                    "moed_a": moed_a,
                    "moed_b": moed_b
                } if has_exam else None
            }

    return metadata

def get_course_offerings(courses_data: dict, target_semester: int) -> dict:
    mandatory_ids = courses_data.get("mandatory", [])
    elective_ids = courses_data.get("electives", [])
    specialization_dict = courses_data.get("specialization", {})

    all_course_ids = set(mandatory_ids) | set(elective_ids)
    for courses in specialization_dict.values():
        all_course_ids.update(courses)

    if not all_course_ids:
        return {}

    course_info_res = supabase.table('course') \
        .select('course_id, course_name, is_attendance_mandatory') \
        .in_('course_id', list(all_course_ids)) \
        .execute()

    course_info_map = {}
    if course_info_res.data:
        for row in course_info_res.data:
            course_info_map[row['course_id']] = {
                'is_attendance_mandatory': row.get('is_attendance_mandatory', False),
                'course_name': row.get('course_name', str(row['course_id']))
            }

    offerings_res = supabase.table('courseoffering') \
        .select('*') \
        .in_('course_id', list(all_course_ids)) \
        .eq('semester', target_semester) \
        .execute()

    raw_data = offerings_res.data or []

    def _norm_id(value):
        # מנרמל מזהה מרצה למחרוזת, כדי ש-123, "123", 123.0 ו-" 123 " יתאימו זה לזה
        if value is None:
            return None
        text = str(value).strip()
        if text.endswith(".0"):
            text = text[:-2]
        return text or None

    instructor_ids = set()
    for row in raw_data:
        i_id = _norm_id(row.get('instructor_id'))
        if i_id is not None:
            instructor_ids.add(i_id)

    if raw_data and not instructor_ids and not any(r.get('instructor_name') for r in raw_data):
        print(f"⚠️ אף שורה ב-courseoffering לא מכילה instructor_id. העמודות בשורה: {list(raw_data[0].keys())}")

    instructor_map = {}
    if instructor_ids:
        lecturer_res = supabase.table('lecturer') \
            .select('instructor_id, instructor_name') \
            .in_('instructor_id', list(instructor_ids)) \
            .execute()

        for row in (lecturer_res.data or []):
            name = (row.get('instructor_name') or "").strip()
            if name:
                instructor_map[_norm_id(row.get('instructor_id'))] = name

        missing = instructor_ids - set(instructor_map.keys())
        if missing:
            print(f"⚠️ {len(missing)} מזהי מרצים מ-courseoffering לא נמצאו בטבלת lecturer (או שאין להם שם): {sorted(missing)[:20]}")

    organized_by_team = {}
    for row in raw_data:
        c_id = row['course_id']

        # --- התיקון הקריטי: ביטול החיתוך ---
        # לוקחים את ה-ID המדויק מהמסד נתונים, ללא תנאים וללא חיתוכי ספרות
        team_id = str(row['course_team_id']).strip()

        info = course_info_map.get(c_id, {})
        row['is_attendance_mandatory'] = info.get('is_attendance_mandatory', False)
        row['course_name'] = info.get('course_name', str(c_id))

        # שם המרצה כבר מגיע ישירות מ-courseoffering; משתמשים בטבלת lecturer רק כגיבוי
        existing_name = str(row.get('instructor_name') or "").strip()
        i_id = _norm_id(row.get('instructor_id'))
        row['instructor_name'] = existing_name or instructor_map.get(i_id) or "לא צוין"

        if c_id not in organized_by_team:
            organized_by_team[c_id] = {}
        if team_id not in organized_by_team[c_id]:
            organized_by_team[c_id][team_id] = []

        organized_by_team[c_id][team_id].append(row)

    # --- בדיקת תקינות נתונים (Logs) ---
    # מוודא את ההצהרה שלך: "אין כזה דבר 3 מופעים, לכל קורס יש הרצאה ותרגול באותו מספר"
    for c_id, teams in organized_by_team.items():
        for t_id, sessions in teams.items():
            if len(sessions) > 2:
                c_name = course_info_map.get(c_id, {}).get("course_name", str(c_id))
                print(f"⚠️ אזהרת מסד נתונים: לקורס '{c_name}' (קבוצה {t_id}) יש {len(sessions)} מופעים שונים ב-DB! ודא שאין שורות כפולות.")

    return organized_by_team

# =====================================================================
# --- עיבוד נתונים ל-Builder ולאופטימייזר ---
# =====================================================================
def classify_constraint_target(constraint_type: str) -> dict:
    mapping = {
        # --- אילוצים קשיחים (Builder) ---
        "day_off": {"optimizer_weight": False, "builder_config_only": True},
        "time_range": {"optimizer_weight": False, "builder_config_only": True},
        "daily_load": {"optimizer_weight": False, "builder_config_only": True},
        "course_drop_flexibility": {"optimizer_weight": False, "builder_config_only": True},
        "course_modification": {"optimizer_weight": False, "builder_config_only": True},
        "course_schedule_pref": {"optimizer_weight": False, "builder_config_only": True},
        
        # --- אילוצים גמישים (Optimizer) ---
        "windows_minimization": {"optimizer_weight": True, "builder_config_only": False},
        # study_days_limit הוא אילוץ קשיח בפועל: אם המשתמש ביקש עד X ימי לימוד,
        # לא מחזירים חלופה שחורגת מזה.
        "study_days_limit": {"optimizer_weight": True, "builder_config_only": True},

        "weekly_balance": {"optimizer_weight": True, "builder_config_only": False},

        # max_consecutive_hours צריך להגיע גם ל-Builder כדי שיסונן לפני הדירוג.
        "max_consecutive_hours": {"optimizer_weight": True, "builder_config_only": True},

        # min_daily_hours נשאר רך: לא תמיד אפשר להכריח מינימום שעות בכל יום.
        "min_daily_hours": {"optimizer_weight": True, "builder_config_only": False},

        # credit_range מטופל ב-prepare_builder_config לטובת בדיקות/פידבק.
        "credit_range": {"optimizer_weight": False, "builder_config_only": True},

        # אם המשתמש אומר "בלי מרצה X" זה צריך להיאכף כבר בבילדר.
        "lecturer_constraint": {"optimizer_weight": False, "builder_config_only": True},
        
        # --- מידע ויועץ (לא מגיע לאף מנוע) ---
        "info_request": {"optimizer_weight": False, "builder_config_only": False},
        "language_preference": {"optimizer_weight": False, "builder_config_only": False},
        "assessment_preference": {"optimizer_weight": False, "builder_config_only": False}
    }
    return mapping.get(constraint_type, {"optimizer_weight": False, "builder_config_only": False})

def split_constraints_by_target(extracted_constraints):
    result = {
        "builder_config_constraints": [],
        "optimizer_constraints": [],
    }
    for const in extracted_constraints:
        c_type = const.get("type")
        target = classify_constraint_target(c_type)
        if target["builder_config_only"]:
            result["builder_config_constraints"].append(const)
        if target["optimizer_weight"]:
            result["optimizer_constraints"].append(const)
    return result

def _coerce_numeric_constraint(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower().replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None

def _first_payload_value(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None

def _append_builder_hard_constraint(builder_config, constraint_type, payload, importance=3):
    constraint = {
        "type": constraint_type,
        "importance": importance,
        "payload": payload or {},
    }
    key = json.dumps(constraint, ensure_ascii=False, sort_keys=True, default=str)
    seen = builder_config.setdefault("_hard_constraint_keys", set())
    if key not in seen:
        builder_config.setdefault("hard_constraints", []).append(constraint)
        seen.add(key)

def _log_decision(event: str, **details):
    print(f"[ChatDecision] {event}: {json.dumps(details, ensure_ascii=False, default=str)}")

def _build_failure_diagnostics(valid_schedules, build_status, builder_warnings, optimizer_diagnostics, user_prefs):
    diagnostics = {
        "build_status": build_status,
        "builder_valid_schedule_count": len(valid_schedules),
        "builder_warning_count": len(builder_warnings),
        "optimizer": optimizer_diagnostics or {},
        "user_preferences": user_prefs,
        "user_message": None,
    }

    if valid_schedules:
        hard_filtered = diagnostics["optimizer"].get("hard_filtered_count", 0)
        scored = diagnostics["optimizer"].get("scored_count", 0)
        reasons = diagnostics["optimizer"].get("hard_filter_reasons", {})
        samples = diagnostics["optimizer"].get("hard_filter_samples", [])

        if hard_filtered and not scored:
            diagnostics["reason"] = "optimizer_hard_filter_rejected_all"
            reason_text = ", ".join(f"{name}: {count}" for name, count in reasons.items()) or "unknown hard-filter reason"
            sample_detail = ""
            if samples:
                sample_reason = samples[0].get("reason", {})
                sample_detail = f" דוגמה: {sample_reason.get('detail', '')}"
            diagnostics["user_message"] = (
                f"סיבת הכישלון הטכנית: נבנו {len(valid_schedules)} מערכות אפשריות, "
                f"אבל כולן נפסלו בשלב הסינון הסופי בגלל: {reason_text}.{sample_detail}"
            )
        elif not scored:
            diagnostics["reason"] = "optimizer_scored_zero_schedules"
            diagnostics["user_message"] = (
                f"סיבת הכישלון הטכנית: ה-Builder בנה {len(valid_schedules)} מערכות, "
                "אבל אף אחת לא הגיעה לשלב הדירוג הסופי. בדוק את לוגי ה-Optimizer לפירוט."
            )
        else:
            diagnostics["reason"] = "ranked_schedules_missing_after_optimizer"
            diagnostics["user_message"] = (
                "סיבת הכישלון הטכנית: נמצאו מערכות לדירוג, אך לא הוחזרה חלופה להצגה. "
                "בדוק את לוגי ה-Optimizer ואת מבנה התוצאה."
            )
    else:
        diagnostics["reason"] = "builder_returned_zero_schedules"
        if builder_warnings:
            diagnostics["user_message"] = "סיבת הכישלון הטכנית: ה-Builder לא הצליח להרכיב קומבינציה חוקית. " + " ".join(builder_warnings)
        else:
            diagnostics["user_message"] = (
                f"סיבת הכישלון הטכנית: ה-Builder החזיר 0 מערכות עם סטטוס {build_status}, "
                "ולא התקבלה אזהרה מפורטת נוספת."
            )

    return diagnostics

def _parse_json_object(raw_text: str) -> dict:
    if not raw_text:
        return {}

    try:
        parsed = json.loads(raw_text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if not match:
        return {"error": "No JSON found in response"}

    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        return {"error": str(exc)}

def _build_constraint_parser_instruction(student_year, semester, program_name):
    return f"""
אתה מחלץ אילוצי מערכת שעות מהודעת הסטודנט הנוכחית בלבד.
הקשר: שנה {student_year}, סמסטר {semester}, תוכנית {program_name}.

החזר JSON בלבד:
{{
  "requires_schedule_build": true/false,
  "is_no_constraints_request": true/false,
  "explanation": "תגובה קצרה בעברית",
  "extracted_constraints": [
    {{"type": "day_off|time_range|daily_load|windows_minimization|course_drop_flexibility|study_days_limit|weekly_balance|lecturer_constraint|course_modification|course_schedule_pref|language_preference|assessment_preference|max_consecutive_hours|min_daily_hours|credit_range|info_request", "importance": 1-3, "payload": {{}}}}
    ]
}}

כללים קצרים:
- אל תחזיר אילוצים קודמים; רק מה שמופיע בהודעה הנוכחית.
- בקשת בנייה/שינוי/איפוס מערכת: requires_schedule_build=true.
- שאלת מידע בלבד על קורסים, קבוצות, שעות או מרצים: requires_schedule_build=false והוסף info_request.
- בקשת מערכת "בלי אילוצים", "איפוס" או "נקה הכל": is_no_constraints_request=true, extracted_constraints=[].
- course_modification payload: action=add/remove/swap וגם target_course או remove_course/add_course.
- time_range payload: start_time, end_time, preferred_time=morning/evening, ו-day אם מדובר ביום מסוים.
- lecturer_constraint payload: action=avoid/prefer, lecturer_name בלבד. lecturer_name חייב להיות רק שם המרצה או שם חלקי שלו, בלי מילים כמו "קורסים של", "בלי", "ימי", או חלקים אחרים מהמשפט. אם יש כמה מרצים, החזר אילוץ נפרד לכל מרצה.
- יום/ימים שהסטודנט לא רוצה ללמוד בהם הם day_off, לא lecturer_constraint.
- day_off payload: day. course_schedule_pref payload: target_course/course_name/course_id, day, start_time/end_time לטווח, exact_start_time לשעה מדויקת, session_type=lecture/tutorial/lab, match_mode=any/all.
- language_preference/info_request: לשאלות על קורסים באנגלית/עברית החזר requires_schedule_build=false, query_type=language_courses, language=english/hebrew.
- course_drop_flexibility: רק אם המשתמש מאשר במפורש להוריד קורס/לוותר על קורס כדי לשפר מערכת; payload: enabled=true, max_drops=1 כברירת מחדל, reason=windows_minimization/daily_load/other.
- daily_load ("עד X שעות ביום", "מקסימום שעות"): payload: {{"max_hours": X}}.
- weekly_balance ("מערכת מאוזנת", "לפזר עומס", "פיזור שווה"): payload: {{}}.
- assessment_preference/info_request: לשאלות על קורסים לפי מבחן/פרויקט/מטלות החזר requires_schedule_build=false, query_type=assessment_courses, prefer=has_exam/less_exams/project_no_exam/project_based/assignments_based/low_assessment_load.
- min_daily_hours/max_consecutive_hours: payload עם min_hours או max_hours. credit_range: min_credits/max_credits.
"""

def _parse_constraints_with_gemini(message, student_year, semester, program_name):
    system_instruction = _build_constraint_parser_instruction(
        student_year,
        semester,
        program_name
    )

    if gemini_client is not None:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=message,
            config={
                "temperature": 0,
                "response_mime_type": "application/json",
                "system_instruction": system_instruction,
            },
        )
        return _parse_json_object(response.text)

    model = legacy_genai.GenerativeModel(
        GEMINI_MODEL_NAME,
        system_instruction=system_instruction,
    )
    try:
        response = model.generate_content(message, generation_config=GEMINI_JSON_CONFIG)
    except TypeError:
        response = model.generate_content(message)

    return _parse_json_object(getattr(response, "text", ""))

def _normalize_day_text(value):
    text = str(value or "").lower().strip()
    text = (
        text
        .replace("׳", "'")
        .replace("’", "'")
        .replace("`", "'")
        .replace("״", '"')
        .replace("”", '"')
    )
    return re.sub(r"\s+", " ", text)

def _append_unique_day(day_nums, day_num):
    if day_num and day_num in DAY_NAMES_HE and day_num not in day_nums:
        day_nums.append(day_num)

def _normalize_day_number(raw_day):
    if raw_day is None or raw_day == "":
        return None

    if isinstance(raw_day, (int, float)) and int(raw_day) in DAY_NAMES_HE:
        return int(raw_day)

    text = _normalize_day_text(raw_day).strip(" ,.;:!?")
    if text.isdigit() and int(text) in DAY_NAMES_HE:
        return int(text)
    day_letter_match = re.fullmatch(r"(?:ב?יום|ב?ימי|ימים)?\s*([אבגדהו])['\"]?", text)
    if day_letter_match:
        return DAY_LETTERS_HE.get(day_letter_match.group(1))
    if "ראשון" in text or "sunday" in text or "sun" == text:
        return 1
    if "שני" in text or "monday" in text or "mon" == text:
        return 2
    if "שלישי" in text or "tuesday" in text or text in {"tue", "tues"}:
        return 3
    if "רביעי" in text or "wednesday" in text or "wed" == text:
        return 4
    if "חמישי" in text or "thursday" in text or text in {"thu", "thur", "thurs"}:
        return 5
    if "שישי" in text or "friday" in text or "fri" == text:
        return 6
    return None

def _split_people_names(value):
    if value is None or value == "":
        return []

    if isinstance(value, (list, tuple, set)):
        names = []
        for item in value:
            names.extend(_split_people_names(item))
        return names

    text = str(value).strip(" ,.;:!?")
    if not text:
        return []

    or_parts = [
        part.strip(" ,.;:!?")
        for part in re.split(r"\s+או(?:\s+של)?\s+", text)
        if part.strip(" ,.;:!?")
    ]
    if len(or_parts) > 1:
        names = []
        for part in or_parts:
            names.extend(_split_people_names(part))
        return names

    comma_parts = [part.strip(" ,.;:!?") for part in re.split(r"[,;/]+", text) if part.strip(" ,.;:!?")]
    if len(comma_parts) > 1:
        names = []
        for part in comma_parts:
            names.extend(_split_people_names(part))
        return names

    and_parts = [part.strip(" ,.;:!?") for part in re.split(r"\s+and\s+", text, flags=re.IGNORECASE) if part.strip(" ,.;:!?")]
    if len(and_parts) > 1:
        names = []
        for part in and_parts:
            names.extend(_split_people_names(part))
        return names

    # Hebrew "and" is often prefixed to the last name: "רון וולף ופבל".
    # Split from the right so surnames that start with ו, like "וולף", stay intact.
    parts = [text]
    changed = True
    while changed:
        changed = False
        current = parts.pop(0)
        split_at = current.rfind(" ו")
        if split_at != -1:
            prefix = current[:split_at].strip(" ,.;:!?")
            suffix_raw = current[split_at + 1:].strip(" ,.;:!?")
            suffix = suffix_raw[1:] if suffix_raw.startswith("ו") else suffix_raw
            if prefix and len(suffix) >= 3 and not suffix_raw.startswith("וו"):
                parts = [prefix, suffix] + parts
                changed = True
                continue
        parts.insert(0, current)

    unique_names = []
    seen = set()
    for name in parts:
        key = name.lower().strip()
        if key and key not in seen:
            unique_names.append(name.strip())
            seen.add(key)
    return unique_names

def _clean_lecturer_candidate(value):
    text = str(value or "").strip(" ,.;:!?")
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^(?:קורסים?|שיעורים?|הרצאות|תרגולים)\s+של\s+", "", text)
    text = re.sub(r"^(?:מרצים?|מתרגלים?|המרצה|מרצה)\s+של\s+", "", text)
    text = re.sub(r"^(?:מרצים?|מתרגלים?|המרצה|מרצה)\s+", "", text)
    text = re.sub(r"^של\s+", "", text)
    text = re.sub(
        r"^(?:the\s+)?(?:courses?|classes?|lecturers?|instructors?|teachers?)\s+(?:of|by|with)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip(" ,.;:!?")

def _looks_like_non_lecturer_candidate(value):
    text = str(value or "").lower().strip()
    if not text:
        return True

    non_lecturer_terms = [
        "אילוץ",
        "אילוצים",
        "חלון",
        "חלונות",
        "לימוד",
        "לימודים",
        "ללמוד",
        "שיעור",
        "שיעורים",
        "מערכת",
        "יום",
        "ימי",
        "ימים",
        "שעה",
        "שעות",
        "בכלל",
        "constraint",
        "constraints",
        "day",
        "days",
        "time",
        "hours",
        "study",
        "schedule",
    ]
    if any(term in text for term in non_lecturer_terms):
        return True

    return any(day_name in text for day_name in DAY_NAMES_HE.values())

def _payload_values(payload, *keys, splitter=None):
    values = []
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            raw_value = payload.get(key)
            if splitter:
                values.extend(splitter(raw_value))
            elif isinstance(raw_value, (list, tuple, set)):
                values.extend(raw_value)
            else:
                values.append(raw_value)
    return values

def _day_numbers_from_payload(payload):
    raw_days = _payload_values(payload, "day", "days", "day_of_week", "days_of_week", "day_of_weeks")
    day_nums = []
    for raw_day in raw_days:
        if isinstance(raw_day, (list, tuple, set)):
            for item in raw_day:
                day_num = _normalize_day_number(item)
                if day_num and day_num not in day_nums:
                    day_nums.append(day_num)
            continue

        text = str(raw_day or "")
        found_known_day = False
        for day_num, day_name in DAY_NAMES_HE.items():
            if day_name in text or str(day_num) == text.strip():
                if day_num not in day_nums:
                    day_nums.append(day_num)
                found_known_day = True

        if not found_known_day:
            day_num = _normalize_day_number(raw_day)
            if day_num and day_num not in day_nums:
                day_nums.append(day_num)

    return day_nums

def _extract_day_letters_from_segment(segment):
    days = []
    normalized = _normalize_day_text(segment)
    for part in re.split(r"[,;/\s]+|או", normalized):
        token = part.strip(" ,.;:!?'\")(\"")
        if len(token) > 1 and token.startswith("ו"):
            token = token[1:]
        token = token.strip(" ,.;:!?'\")(\"")
        if token in DAY_LETTERS_HE:
            _append_unique_day(days, DAY_LETTERS_HE[token])
    return days

def _extract_days_from_text(message: str):
    text = _normalize_day_text(message)
    days = []
    for day_num, day_name in DAY_NAMES_HE.items():
        if re.search(rf"(?<![א-ת])[וב]{{0,2}}{re.escape(day_name)}(?![א-ת])", text):
            _append_unique_day(days, day_num)

    for day_text, day_num in ENGLISH_DAY_ALIASES.items():
        if re.search(rf"\b{re.escape(day_text)}\b", text):
            _append_unique_day(days, day_num)

    day_letter_segment_pattern = (
        r"(?:ב?יום|ב?ימי|ימים)\s+"
        r"([אבגדהו]['\"]?(?:\s*(?:,|/|;|או|\s+ו?)\s*[אבגדהו]['\"]?)*)"
        r"(?=\s|$|[,.;:!?])"
    )
    for match in re.finditer(day_letter_segment_pattern, text):
        for day_num in _extract_day_letters_from_segment(match.group(1)):
            _append_unique_day(days, day_num)

    explicit_negative_letter_pattern = (
        r"(?:בלי|ללא|אל|לא)\s+"
        r"(?:ב?יום|ב?ימי|ימים)?\s*"
        r"([אבגדהו]['\"]?)(?=\s|$|[,.;:!?])"
    )
    for match in re.finditer(explicit_negative_letter_pattern, text):
        for day_num in _extract_day_letters_from_segment(match.group(1)):
            _append_unique_day(days, day_num)

    return days

def _has_day_off_intent(message: str):
    text = _normalize_day_text(message)
    day_off_keywords = [
        "יום חופש",
        "ימי חופש",
        "לא ללמוד",
        "לא רוצה ללמוד",
        "לא רוצה שיעורים",
        "בלי לימודים",
        "ללא לימודים",
        "בלי יום",
        "בלי ימי",
        "בלי ימים",
        "ללא יום",
        "ללא ימי",
        "ללא ימים",
        "בלי שיעורים",
        "ללא שיעורים",
        "לא זמין",
        "לא זמינה",
        "לא פנוי",
        "לא פנויה",
        "אל תשבץ",
        "אל תשבצי",
        "תמנע",
        "להימנע",
        "free day",
        "day off",
        "no classes",
        "without classes",
        "not available",
        "unavailable",
    ]
    if any(keyword in text for keyword in day_off_keywords):
        return True

    has_day = bool(_extract_days_from_text(message))
    if not has_day:
        return False

    day_off_patterns = [
        r"(?:בלי|ללא)\s+(?:ב?יום|ב?ימי|ימים)\b",
        r"(?:בלי|ללא)\s+(?:קורסים|שיעורים|הרצאות|תרגולים|לימודים)\s+(?:ב?יום|ב?ימי|ימים)?\b",
        r"(?:לא\s+רוצה|לא)\s+(?:ללמוד|שיעורים|קורסים|הרצאות|תרגולים)\b",
        r"(?:אל\s+תשבץ|אל\s+תשבצי|תמנע|להימנע)\b",
        r"(?:not\s+available|unavailable|no\s+classes|without\s+classes)\b",
    ]
    return any(re.search(pattern, text) for pattern in day_off_patterns)

def _message_requests_constraint_reset(message: str) -> bool:
    text = str(message or "").lower()
    reset_keywords = [
        "בלי אילוצים",
        "ללא אילוצים",
        "אפס אילוצים",
        "איפוס אילוצים",
        "נקה אילוצים",
        "תבטל את כל האילוצים",
        "בטל את כל האילוצים",
        "reset constraints",
        "clear constraints",
        "no constraints",
        "without constraints",
    ]
    return any(keyword in text for keyword in reset_keywords)

def _message_replaces_existing_constraints(message: str) -> bool:
    text = str(message or "").lower()
    replace_keywords = [
        "במקום",
        "תחליף",
        "להחליף",
        "שנה את",
        "עדכן את",
        "replace",
        "instead of",
        "change my",
        "update my",
    ]
    return any(keyword in text for keyword in replace_keywords)

def _has_build_intent(message: str) -> bool:
    text = str(message or "").lower()
    build_keywords = [
        "תבנה",
        "בנה",
        "תיצור",
        "צור",
        "מערכת",
        "שבץ",
        "תשבץ",
        "schedule",
        "build",
        "create",
    ]
    return any(keyword in text for keyword in build_keywords)

def _has_info_intent(message: str) -> bool:
    text = str(message or "").lower()
    info_keywords = [
        "אופציות",
        "אפשרות",
        "אפשרויות",
        "מתי יש",
        "איזה שעות",
        "קבוצות",
        "מרצים",
        "פרטים",
        "מי המרצה",
        "איזה קורסי",
        "איזה קורס",
        "קורסים",
        "שפה",
        "עברית",
        "אנגלית",
        "english",
        "hebrew",
        "language",
        "elective",
        "options",
        "lecturer",
        "when is",
        "מבחן",
        "מבחנים",
        "ללא מבחן",
        "בלי מבחן",
        "פרויקט",
        "פרויקטים",
        "עבודות",
        "מטלות",
        "בוחן",
        "בחנים",
        "exam",
        "exams",
        "project",
        "projects",
        "assignments",
        "homework",
    ]
    return any(keyword in text for keyword in info_keywords)

def _has_assessment_intent(message: str) -> bool:
    text = str(message or "").lower()

    assessment_keywords = [
        "מבחן",
        "מבחנים",
        "בלי מבחן",
        "ללא מבחן",
        "פחות מבחנים",
        "כמה שפחות מבחנים",
        "פרויקט",
        "פרויקטים",
        "עבודות",
        "מטלות",
        "בוחן",
        "בחנים",
        "exam",
        "exams",
        "no exam",
        "without exam",
        "less exams",
        "project",
        "projects",
        "assignments",
        "homework",
    ]

    return any(keyword in text for keyword in assessment_keywords)


def _detect_assessment_preference(message: str) -> str:
    text = str(message or "").lower()

    project_words = [
        "פרויקט",
        "פרויקטים",
        "רק עם פרויקטים",
        "רק פרויקטים",
        "project",
        "projects",
        "project only",
        "only project",
        "projects only",
    ]

    no_exam_words = [
        "בלי מבחן",
        "ללא מבחן",
        "פחות מבחנים",
        "כמה שפחות מבחנים",
        "ללא מבחנים",
        "בלי מבחנים",
        "אין להם מבחן",
        "שאין להם מבחן",
        "ללא בחינה",
        "בלי בחינה",
        "no exam",
        "without exam",
        "less exams",
        "no exams",
        "without exams",
    ]

    has_exam_words = [
    "יש להם מבחן",
    "שיש להם מבחן",
    "עם מבחן",
    "כולל מבחן",
    "קורסים עם מבחן",
    "קורסים שיש להם מבחן",
    "has exam",
    "with exam",
    "includes exam",
]

    assignments_words = [
        "עבודות",
        "מטלות",
        "assignments",
        "homework",
    ]

    if any(word in text for word in project_words) and any(word in text for word in no_exam_words):
        return "project_no_exam"

    if any(phrase in text for phrase in [
        "רק עם פרויקטים",
        "רק פרויקטים",
        "רק פרויקט",
        "project only",
        "only project",
        "projects only",
    ]):
        return "project_no_exam"

    if any(word in text for word in has_exam_words):
        return "has_exam"

    if any(word in text for word in no_exam_words):
        return "less_exams"

    if any(word in text for word in project_words):
        return "project_based"

    if any(word in text for word in assignments_words):
        return "assignments_based"

    return "low_assessment_load"

def _extract_first_day_from_text(message: str):
    days = _extract_days_from_text(message)
    return days[0] if days else None

def _extract_hour_from_text(message: str):
    match = re.search(r"(\d{1,2})(?::(\d{2}))?", str(message or ""))
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours + minutes / 60.0

def _extract_avoided_lecturers_from_text(message: str):
    text = str(message or "").strip()
    names = []

    boundary = (
        r"(?=,|;|\.|\s+אני\b|\s+לא\s+רוצה\b|\s+לא\s+ללמוד\b|"
        r"\s+בימי\b|\s+ביום\b|\s+(?:ו?בלי|ו?ללא)\s+|$)"
    )
    for match in re.finditer(rf"(?:בלי|ללא)\s+(.+?){boundary}", text):
        raw_segment = match.group(1).strip(" ,.;:!?")
        if not raw_segment:
            continue
        for raw_name in _split_people_names(raw_segment):
            lecturer_name = _clean_lecturer_candidate(raw_name)
            if lecturer_name and not _looks_like_non_lecturer_candidate(lecturer_name):
                names.append(lecturer_name)

    unique_names = []
    seen = set()
    for name in names:
        key = name.lower()
        if key not in seen:
            unique_names.append(name)
            seen.add(key)

    return unique_names

def _try_parse_constraints_locally(message: str):
    text = str(message or "").lower()

    if _message_requests_constraint_reset(message):
        return {
            "requires_schedule_build": True,
            "is_no_constraints_request": True,
            "explanation": "אבנה מערכת חדשה בלי אילוצים.",
            "extracted_constraints": [],
            "source": "local",
        }

    if _has_assessment_intent(message) and not _has_build_intent(message):
        prefer = _detect_assessment_preference(message)

        return {
            "requires_schedule_build": False,
            "is_no_constraints_request": False,
            "explanation": "אבדוק אילו קורסים מתאימים להעדפת ההערכה שלך.",
            "extracted_constraints": [
                {
                    "type": "assessment_preference",
                    "importance": 1,
                    "payload": {
                    "query_type": "assessment_courses",
                    "prefer": prefer
                    }
                }
            ],
            "source": "local",
        }    

    if _has_info_intent(message) and not _has_build_intent(message):
        language = None

        if any(word in text for word in ["אנגלית", "english"]):
            language = "english"
        elif any(word in text for word in ["עברית", "hebrew"]):
            language = "hebrew"

        if language:
            return {
                "requires_schedule_build": False,
                "is_no_constraints_request": False,
                "explanation": "אבדוק אילו קורסים בשפה המבוקשת רלוונטיים אליך.",
                "extracted_constraints": [
                    {
                        "type": "info_request",
                        "importance": 1,
                        "payload": {
                            "query_type": "language_courses",
                            "language": language
                        }
                    }
                ],
                "source": "local",
            }

        return {
            "requires_schedule_build": False,
            "is_no_constraints_request": False,
            "explanation": "אבדוק את נתוני הקורסים ואענה לך.",
            "extracted_constraints": [{"type": "info_request", "importance": 1, "payload": {"query_type": "course_info"}}],
            "source": "local",
        }

    constraints = []
    for lecturer_name in _extract_avoided_lecturers_from_text(message):
        constraints.append({
            "type": "lecturer_constraint",
            "importance": 3,
            "payload": {
                "action": "avoid",
                "lecturer_name": lecturer_name,
            },
        })
    
    if _has_assessment_intent(message):
        constraints.append({
            "type": "assessment_preference",
            "importance": 1,
            "payload": {
                "query_type": "assessment_courses",
                "prefer": _detect_assessment_preference(message)
            }
        })

    day_nums = _extract_days_from_text(message)
    day_num = day_nums[0] if day_nums else None
    end_time_intent = any(word in text for word in ["לא ללמוד אחרי", "לא אחרי", "עד ", "before", "until"])
    start_time_intent = any(word in text for word in ["לא לפני", "משעה", "אחרי ", "after"])
    has_time_word = end_time_intent or start_time_intent

    study_days_match = re.search(
    r"(?:עד|מקסימום|לא יותר מ|גג)\s*(\d+)\s*(?:ימי לימוד|ימים|יום)",
    text
)

    if study_days_match:
        max_days = int(study_days_match.group(1))
        constraints.append({
            "type": "study_days_limit",
            "importance": 3,
            "payload": {
                "max_days": max_days
            }
        })

    if day_nums and not has_time_word and _has_day_off_intent(message):
        for blocked_day in day_nums:
            constraints.append({"type": "day_off", "importance": 3, "payload": {"day": DAY_NAMES_HE[blocked_day]}})

    
    hour_value = _extract_hour_from_text(message)

    has_study_days_phrase = bool(re.search(
        r"(?:ימי לימוד|ימי מערכת|ימים|יום)",
        text
    ))

    if hour_value is not None and has_time_word and not has_study_days_phrase and (not day_nums or _has_day_off_intent(message)):
        target_days = day_nums or [None]
        for target_day in target_days:
            payload = {}
            if target_day:
                payload["day"] = DAY_NAMES_HE[target_day]
            if end_time_intent:
                payload["end_time"] = hour_value
            elif start_time_intent:
                payload["start_time"] = hour_value
            constraints.append({"type": "time_range", "importance": 3, "payload": payload})

    windows_min_phrases = [
        "בלי חלונות",
        "ללא חלונות",
        "מינימום חלונות",
        "כמה שפחות חלונות",
        "פחות חלונות",
        "צמצום חלונות",
        "לצמצם חלונות",
        "בלי הפסקות",
        "ללא הפסקות",
        "מינימום הפסקות",
        "כמה שפחות הפסקות",
        "פחות הפסקות",
        "צמצום הפסקות",
        "לצמצם הפסקות",
        "no gaps",
        "fewer gaps",
        "minimum gaps",
    ]
    if any(phrase in text for phrase in windows_min_phrases):
        constraints.append({"type": "windows_minimization", "importance": 2, "payload": {}})

    course_drop_phrases = [
        "אפשר להוריד קורס",
        "אפשר להוריד קוס",
        "תוריד קורס",
        "תוריד קוס",
        "להוריד קורס",
        "להוריד קוס",
        "מוריד קורס",
        "הורדת קורס",
        "לוותר על קורס",
        "לוותר על קוס",
        "אפשר לוותר על קורס",
        "אם צריך תוריד",
        "אם צריך להוריד",
        "אם צריך אפשר להוריד",
        "מעדיף להוריד",
        "אני מעדיף להוריד",
        "אפילו מעדיף להוריד",
        "פחות קורסים",
        "קורס אחד פחות",
        "מערכת חלקית",
        "drop a course",
        "remove a course",
        "fewer courses",
        "partial schedule",
    ]
    if any(phrase in text for phrase in course_drop_phrases):
        constraints.append({
            "type": "course_drop_flexibility",
            "importance": 2,
            "payload": {
                "enabled": True,
                "max_drops": 1,
                "reason": "windows_minimization" if any(w in text for w in ["חלונות", "הפסקות", "gaps"]) else "other"
            }
        })

    if constraints:
        return {
            "requires_schedule_build": True,
            "is_no_constraints_request": False,
            "explanation": "זיהיתי את האילוצים ואבנה מערכת בהתאם.",
            "extracted_constraints": constraints,
            "source": "local",
        }

    constraint_hint_keywords = [
        "עם ",
        "בלי ",
        "לא ",
        "מקסימום",
        "לפחות",
        "שעות",
        "ימים",
        "תוסיף",
        "הוסף",
        "להוסיף",
        "תוריד",
        "תסיר",
        "להסיר",
        "קורס",
        "במקום",
        "add",
        "remove",
        "swap",
        "course",
        "max",
        "minimum",
    ]
    if _has_build_intent(message) and not any(keyword in text for keyword in constraint_hint_keywords):
        return {
            "requires_schedule_build": True,
            "is_no_constraints_request": False,
            "explanation": "אבנה עבורך מערכת.",
            "extracted_constraints": [],
            "source": "local",
        }

    return None

def _clean_schedule_constraint(constraint):
    if not isinstance(constraint, dict):
        return None

    c_type = constraint.get("type")
    if c_type not in SCHEDULE_CONSTRAINT_TYPES and c_type not in TRANSIENT_CONSTRAINT_TYPES:
        return None

    payload = constraint.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        importance = int(constraint.get("importance", 1))
    except (TypeError, ValueError):
        importance = 1
    importance = max(1, min(3, importance))

    return {
        "type": c_type,
        "importance": importance,
        "payload": payload,
    }

def _expand_multi_value_constraint(constraint):
    if not isinstance(constraint, dict):
        return []

    c_type = constraint.get("type")
    payload = constraint.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    base = {
        "type": c_type,
        "importance": constraint.get("importance", 1),
    }

    if c_type == "lecturer_constraint":
        raw_names = _payload_values(
            payload,
            "lecturer_name",
            "lecturer_names",
            "instructor_name",
            "instructor_names",
            "lecturer",
            "lecturers",
            splitter=_split_people_names,
        )
        names = []
        for raw_name in raw_names:
            lecturer_name = _clean_lecturer_candidate(raw_name)
            if lecturer_name and not _looks_like_non_lecturer_candidate(lecturer_name):
                names.append(lecturer_name)
        if names:
            return [
                {
                    **base,
                    "payload": {
                        "action": payload.get("action") or "avoid",
                        "lecturer_name": name,
                    },
                }
                for name in names
            ]
        return []

    if c_type == "day_off":
        day_nums = _day_numbers_from_payload(payload)
        if day_nums:
            return [
                {
                    **base,
                    "payload": {
                        **payload,
                        "day": DAY_NAMES_HE[day_num],
                    },
                }
                for day_num in day_nums
            ]

    if c_type == "time_range":
        day_nums = _day_numbers_from_payload(payload)
        if day_nums:
            return [
                {
                    **base,
                    "payload": {
                        **payload,
                        "day": DAY_NAMES_HE[day_num],
                    },
                }
                for day_num in day_nums
            ]

    if c_type in {"course_schedule_pref", "course_modification"}:
        target_values = _payload_values(
            payload,
            "target_course",
            "target_courses",
            "course_name",
            "course_names",
            "course_id",
            "course_ids",
        )
        if len(target_values) > 1:
            return [
                {
                    **base,
                    "payload": {
                        **payload,
                        "target_course": target,
                    },
                }
                for target in target_values
            ]

    return [constraint]

def _expand_multi_value_constraints(constraints):
    expanded = []
    for constraint in constraints or []:
        expanded.extend(_expand_multi_value_constraint(constraint))
    return expanded

def _sanitize_current_constraints(message, constraints):
    sanitized = [
        cleaned
        for cleaned in (_clean_schedule_constraint(c) for c in _expand_multi_value_constraints(constraints))
        if cleaned
    ]

    if _has_day_off_intent(message):
        existing_days = {
            _normalize_day_number((constraint.get("payload") or {}).get("day"))
            for constraint in sanitized
            if constraint.get("type") == "day_off"
        }
        for day_num in _extract_days_from_text(message):
            if day_num not in existing_days:
                sanitized.append({
                    "type": "day_off",
                    "importance": 3,
                    "payload": {"day": DAY_NAMES_HE[day_num]},
                })
                existing_days.add(day_num)

    return sanitized

def _constraint_identity(constraint):
    c_type = constraint.get("type")
    payload = constraint.get("payload", {})

    if c_type == "day_off":
        day = _normalize_day_number(payload.get("day") or payload.get("day_of_week"))
        return (c_type, day or json.dumps(payload, ensure_ascii=False, sort_keys=True))

    if c_type == "time_range":
        day = _normalize_day_number(payload.get("day") or payload.get("day_of_week"))
        scope = day if day else "global"
        return (c_type, scope)

    if c_type == "course_schedule_pref":
        target = payload.get("target_course") or payload.get("course_name") or payload.get("course_id")
        day = payload.get("day") or payload.get("day_of_week")
        start = payload.get("start_time") or payload.get("from")
        end = payload.get("end_time") or payload.get("until")
        session_type = payload.get("session_type")
        return (
            c_type,
            str(target or "").lower().strip(),
            str(day or "").lower().strip(),
            str(start or "").lower().strip(),
            str(end or "").lower().strip(),
            str(session_type or "").lower().strip(),
        )

    if c_type == "course_modification":
        action = str(payload.get("action", "")).lower().strip()
        target = payload.get("target_course") or payload.get("remove_course") or payload.get("add_course")
        return (c_type, action, str(target or "").lower().strip())

    return (c_type, json.dumps(payload, ensure_ascii=False, sort_keys=True))

def _merge_active_constraints(existing_constraints, parsed_constraints, is_no_constraints_request, message):
    if is_no_constraints_request or _message_requests_constraint_reset(message):
        return []

    existing_clean = [
        cleaned for cleaned in (_clean_schedule_constraint(c) for c in _expand_multi_value_constraints(existing_constraints))
        if cleaned
    ]
    parsed_clean = [
        cleaned for cleaned in (_clean_schedule_constraint(c) for c in _expand_multi_value_constraints(parsed_constraints))
        if cleaned
    ]

    if not parsed_clean:
        return existing_clean

    if _message_replaces_existing_constraints(message):
        replace_types = {c["type"] for c in parsed_clean}
        existing_clean = [c for c in existing_clean if c["type"] not in replace_types]

    merged = {}
    for constraint in existing_clean:
        merged[_constraint_identity(constraint)] = constraint
    for constraint in parsed_clean:
        merged[_constraint_identity(constraint)] = constraint

    return list(merged.values())

def _format_day(raw_day):
    day_num = _normalize_day_number(raw_day)
    return DAY_NAMES_HE.get(day_num, str(raw_day or "לא צוין"))

def _format_time(raw_time):
    if raw_time is None or raw_time == "":
        return "לא צוין"
    if isinstance(raw_time, str):
        return raw_time[:5] if len(raw_time) >= 5 else raw_time
    if isinstance(raw_time, (int, float)):
        hours = int(raw_time)
        minutes = int(round((float(raw_time) - hours) * 60))
        return f"{hours:02d}:{minutes:02d}"
    return str(raw_time)

def _resolve_course_from_text(message, course_metadata):
    text = str(message or "").lower()

    for cid in course_metadata:
        if re.search(rf"\b{re.escape(str(cid))}\b", text):
            return cid

    matches = []
    for cid, meta in course_metadata.items():
        name = str(meta.get("course_name", "")).lower().strip()
        if name and name in text:
            matches.append(cid)

    return matches[0] if len(matches) == 1 else None

def _course_list_text(course_ids, course_metadata, limit=15):
    items = []
    for cid in course_ids[:limit]:
        meta = course_metadata.get(cid, {})
        items.append(f"{meta.get('course_name', cid)} ({cid})")
    if len(course_ids) > limit:
        items.append(f"ועוד {len(course_ids) - limit} קורסים")
    return ", ".join(items)

def _normalize_course_language(value):
    text = str(value or "").lower().strip()

    if any(word in text for word in ["english", "אנגלית", "eng"]):
        return "english"

    if any(word in text for word in ["hebrew", "עברית", "heb"]):
        return "hebrew"

    return text


def _build_language_courses_reply(requested_language, courses_data, course_metadata):
    requested_language = _normalize_course_language(requested_language)

    mandatory_ids = set(courses_data.get("mandatory", []))

    specialization_ids = set()
    for course_list in courses_data.get("specialization", {}).values():
        specialization_ids.update(course_list)

    relevant_ids = list(mandatory_ids | specialization_ids)

    matching_courses = []

    for cid in relevant_ids:
        meta = course_metadata.get(cid, {})
        course_language = _normalize_course_language(meta.get("language"))

        if course_language == requested_language:
            group_name = "חובה" if cid in mandatory_ids else "התמחות/בחירה"
            matching_courses.append({
                "course_id": cid,
                "course_name": meta.get("course_name", str(cid)),
                "credits": meta.get("credit_points", "-"),
                "group": group_name,
            })

    language_he = "אנגלית" if requested_language == "english" else "עברית"

    if not matching_courses:
        return f"לא מצאתי קורסים בשפה {language_he} שרלוונטיים אליך בסמסטר הזה."

    lines = []
    for course in sorted(matching_courses, key=lambda item: item["course_name"]):
        lines.append(
            f"- {course['course_name']} (קוד: {course['course_id']}, "
            f"{course['credits']} נק\"ז, סוג: {course['group']})"
        )

    return (
        f"מצאתי {len(matching_courses)} קורסים בשפה {language_he} שרלוונטיים אליך בסמסטר הזה:\n"
        + "\n".join(lines)
        + "\n\nשים לב: לפי הדרישה האקדמית שלך צריך להשלים 2 קורסים באנגלית, ולכן אפשר לבחור מתוך הרשימה את הקורסים שמתאימים לך."
    )

def _build_assessment_courses_reply(prefer, courses_data, course_metadata):
    prefer = str(prefer or "low_assessment_load").lower().strip()

    mandatory_ids = set(courses_data.get("mandatory", []))

    specialization_ids = set()
    for course_list in courses_data.get("specialization", {}).values():
        specialization_ids.update(course_list)

    relevant_ids = list(mandatory_ids | specialization_ids)

    matching_courses = []

    for cid in relevant_ids:
        meta = course_metadata.get(cid, {})
        weights = meta.get("weights", {}) or {}

        try:
            exam = float(weights.get("exam", 0) or 0)
            project = float(weights.get("project", 0) or 0)
            mid = float(weights.get("mid_semester", 0) or 0)
            assignments = float(weights.get("assignments", 0) or 0)
        except (TypeError, ValueError):
            exam = project = mid = assignments = 0

        is_match = False
        reason = ""

        if prefer == "has_exam":
            if exam > 0:
                is_match = True
                reason = f"כולל מבחן סופי במשקל {exam:g}%"

        elif prefer == "less_exams":
            if exam == 0:
                is_match = True
                reason = "ללא מבחן סופי"

        elif prefer == "project_no_exam":
            if project > 0 and exam == 0:
                is_match = True
                reason = f"כולל פרויקט במשקל {project:g}% וללא מבחן סופי"

        elif prefer == "project_based":
            if project > 0:
                is_match = True
                reason = f"כולל פרויקט במשקל {project:g}%"

        elif prefer == "assignments_based":
            if assignments > 0 and exam == 0:
                is_match = True
                reason = f"מבוסס מטלות במשקל {assignments:g}% וללא מבחן סופי"

        else:
            assessment_load = 0
            if exam > 0:
                assessment_load += 5
            if project > 0:
                assessment_load += 2
            if mid > 0 or assignments > 0:
                assessment_load += 1

            if assessment_load <= 2:
                is_match = True
                reason = "עומס הערכה נמוך יחסית"

        if is_match:
            group_name = "חובה" if cid in mandatory_ids else "התמחות/בחירה"
            matching_courses.append({
                "course_id": cid,
                "course_name": meta.get("course_name", str(cid)),
                "credits": meta.get("credit_points", "-"),
                "group": group_name,
                "reason": reason,
                "exam": exam,
                "project": project,
                "mid": mid,
                "assignments": assignments,
            })

    prefer_he = {
        "has_exam": "קורסים עם מבחן סופי",
        "less_exams": "קורסים ללא מבחן סופי",
        "project_based": "קורסים מבוססי פרויקט",
        "assignments_based": "קורסים מבוססי מטלות וללא מבחן",
        "low_assessment_load": "קורסים עם עומס הערכה נמוך",
        "project_no_exam": "קורסים מבוססי פרויקט ללא מבחן סופי",
    }.get(prefer, "קורסים עם עומס הערכה נמוך")

    if not matching_courses:
        return f"לא מצאתי {prefer_he} שרלוונטיים אליך בסמסטר הזה."

    lines = []
    for course in sorted(matching_courses, key=lambda item: item["course_name"]):
        lines.append(
            f"- {course['course_name']} (קוד: {course['course_id']}, "
            f"{course['credits']} נק\"ז, סוג: {course['group']}) — {course['reason']}"
        )

    return (
        f"מצאתי {len(matching_courses)} {prefer_he} שרלוונטיים אליך בסמסטר הזה:\n"
        + "\n".join(lines)
        + "\n\nאם תרצה, תכתוב לי איזה קורס להוסיף למערכת ואבנה לך מערכת חדשה בהתאם."
    )

def _build_course_detail_reply(course_id, course_metadata, organized_by_team):
    meta = course_metadata.get(course_id, {})
    course_name = meta.get("course_name", str(course_id))
    credits = meta.get("credit_points", "-")
    language = meta.get("language") or "לא צוינה"
    teams = organized_by_team.get(course_id, {})

    if not teams:
        return f"מצאתי את {course_name} ({course_id}), {credits} נק\"ז, שפת הוראה: {language}. אין לי כרגע קבוצות פעילות לקורס הזה בסמסטר המבוקש."

    team_lines = []
    for team_id, sessions in sorted(teams.items(), key=lambda item: str(item[0]))[:6]:
        session_parts = []
        for session in sorted(sessions, key=lambda s: (str(s.get("day_of_week", "")), str(s.get("start_time", "")))):
            session_type = session.get("session_type") or "מפגש"
            day = _format_day(session.get("day_of_week"))
            start = _format_time(session.get("start_time"))
            end = _format_time(session.get("end_time"))
            lecturer = session.get("instructor_name") or "לא צוין"
            session_parts.append(f"{session_type} ביום {day} {start}-{end} עם {lecturer}")
        team_lines.append(f"קבוצה {team_id}: " + "; ".join(session_parts))

    return f"{course_name} ({course_id}), {credits} נק\"ז, שפת הוראה: {language}. " + " | ".join(team_lines)

def _build_summer_courses_intro(courses_data, course_metadata):
    if not courses_data.get("is_summer_pool"):
        return ""

    lines = ["מצאתי עבורך את הקורסים הרלוונטיים לסמסטר קיץ:"]

    current_ids = courses_data.get("summer_current_year", [])
    next_ids = courses_data.get("summer_next_year", [])
    spec_dict = courses_data.get("summer_specialization", {})

    if current_ids:
        lines.append("\nקורסים לפי שנת הלימודים הנוכחית:")
        for cid in current_ids:
            meta = course_metadata.get(cid, {})
            lines.append(f"- {meta.get('course_name', cid)} (קוד: {cid})")
    else:
        lines.append("\nלא נמצאו קורסי קיץ לפי שנת הלימודים הנוכחית.")

    if next_ids:
        lines.append("\nקורסים שניתן לשקול להקדים משנה הבאה:")
        for cid in next_ids:
            meta = course_metadata.get(cid, {})
            lines.append(f"- {meta.get('course_name', cid)} (קוד: {cid})")

    if spec_dict:
        lines.append("\nקורסי התמחות לפי ההתמחויות שלך:")
        for spec_name, course_ids in spec_dict.items():
            lines.append(f"\n{spec_name}:")
            for cid in course_ids:
                meta = course_metadata.get(cid, {})
                lines.append(f"- {meta.get('course_name', cid)} (קוד: {cid})")

        lines.append(
        "\nבשלב זה לא שיבצתי קורסים אוטומטית למערכת. "
        "כדי לבנות מערכת לקיץ, כתוב לי איזה קורס או קורסים להוסיף לפי שם הקורס או הקוד שלו."
    )

    return "\n".join(lines)

def _summer_has_selected_courses(active_constraints, locked_courses=None):
    """
    בקיץ בונים מערכת רק אם המשתמש בחר בפועל קורס להוספה.
    """
    if locked_courses:
        return True

    for constraint in active_constraints or []:
        if constraint.get("type") != "course_modification":
            continue

        payload = constraint.get("payload") or {}
        action = str(payload.get("action") or "").lower().strip()

        target = (
            payload.get("target_course")
            or payload.get("add_course")
            or payload.get("course_id")
            or payload.get("course_name")
        )

        if action in {"add", "swap"} and target:
            return True

    return False

def _build_info_only_reply(message, courses_data, course_metadata, organized_by_team):
    text = str(message or "").lower()

    if any(word in text for word in ["אנגלית", "english"]):
        return _build_language_courses_reply("english", courses_data, course_metadata)

    if any(word in text for word in ["עברית", "hebrew"]):
        return _build_language_courses_reply("hebrew", courses_data, course_metadata)
    
    if _has_assessment_intent(message):
        prefer = _detect_assessment_preference(message)
        return _build_assessment_courses_reply(prefer, courses_data, course_metadata)

    course_id = _resolve_course_from_text(message, course_metadata)
    if course_id is not None:
        return _build_course_detail_reply(course_id, course_metadata, organized_by_team)
    mandatory_ids = [cid for cid in courses_data.get("mandatory", []) if cid in course_metadata]
    elective_ids = [
        cid for cid in course_metadata
        if cid not in set(mandatory_ids)
    ]

    if any(word in text for word in ["חובה", "mandatory"]):
        if mandatory_ids:
            return "קורסי החובה שלך לסמסטר הזה הם: " + _course_list_text(mandatory_ids, course_metadata) + "."
        return "לא מצאתי קורסי חובה לסמסטר המבוקש."

    if any(word in text for word in ["בחירה", "אופציות", "אפשרויות", "elective", "options"]):
        if elective_ids:
            return "קורסי הבחירה הזמינים עבורך הם: " + _course_list_text(elective_ids, course_metadata) + "."
        return "לא מצאתי קורסי בחירה פתוחים עבורך בסמסטר הזה."

    all_ids = list(course_metadata.keys())
    if all_ids:
        return "מצאתי את הקורסים הרלוונטיים לסמסטר הזה: " + _course_list_text(all_ids, course_metadata, limit=12) + ". אפשר לשאול אותי על קורס מסוים כדי לקבל קבוצות, שעות ומרצים."

    return "אין לי כרגע מספיק נתוני קורסים כדי לענות על זה."

def extract_optimizer_preferences(ai_analysis):
    user_preferences = {
        "unwanted_days": [],
        "preferred_time": None,
        "time_from": None,
        "time_until": None,
        "max_study_days": None,
        "windows_minimization": False,
        "weekly_balance": False,
        "max_daily_hours": None,
        "min_daily_hours": None,
        "max_consecutive_hours": None,
        "allow_course_drop": False,
        "max_course_drops": 0
    }
    custom_weights_adjustments = {}

    constraints = ai_analysis.get('extracted_constraints', [])
    days_map = {"ראשון": 1, "שני": 2, "שלישי": 3, "רביעי": 4, "חמישי": 5, "שישי": 6}

    for const in constraints:
        c_type = const.get("type")
        importance = const.get("importance", 1)
        payload = const.get("payload", {})

        if c_type == "day_off":
            day_str = payload.get("day")
            if day_str in days_map:
                user_preferences["unwanted_days"].append(days_map[day_str])
            custom_weights_adjustments['P'] = max(custom_weights_adjustments.get('P', 0), importance)

        elif c_type == "windows_minimization":
            user_preferences["windows_minimization"] = True
            custom_weights_adjustments['G'] = max(custom_weights_adjustments.get('G', 0), importance)

        elif c_type == "course_drop_flexibility":
            enabled = payload.get("enabled", True)
            max_drops = _coerce_numeric_constraint(payload.get("max_drops", 1))
            if enabled:
                user_preferences["allow_course_drop"] = True
                user_preferences["max_course_drops"] = max(1, int(max_drops or 1))

        elif c_type == "time_range":
            # נעדכן את האופטימייזר רק אם מדובר באילוץ גלובלי (ללא ציון יום ספציפי)
            if "day" not in payload and "day_of_week" not in payload:
                user_preferences["time_from"] = payload.get("start_time")
                user_preferences["time_until"] = payload.get("end_time")
                user_preferences["preferred_time"] = payload.get("preferred_time", "morning")
            custom_weights_adjustments['T'] = max(custom_weights_adjustments.get('T', 0), importance)

        elif c_type == "study_days_limit":
            user_preferences["max_study_days"] = payload.get("max_days", 5)
            custom_weights_adjustments['D'] = max(custom_weights_adjustments.get('D', 0), importance)

        elif c_type == "weekly_balance":
            user_preferences["weekly_balance"] = True
            custom_weights_adjustments['LW'] = max(custom_weights_adjustments.get('LW', 0), importance)

        elif c_type in ["daily_load", "min_daily_hours", "max_consecutive_hours"]:
            if c_type == "daily_load":
                value = _first_payload_value(payload, "max_hours", "hours", "max_daily_hours", "daily_load", "value")
                user_preferences["max_daily_hours"] = _coerce_numeric_constraint(value)
            elif c_type == "min_daily_hours":
                value = _first_payload_value(payload, "min_hours", "hours", "min_daily_hours", "daily_load", "value")
                user_preferences["min_daily_hours"] = _coerce_numeric_constraint(value)
            elif c_type == "max_consecutive_hours":
                value = _first_payload_value(payload, "max_hours", "hours", "max_consecutive_hours", "value")
                user_preferences["max_consecutive_hours"] = _coerce_numeric_constraint(value)
            custom_weights_adjustments['LD'] = max(custom_weights_adjustments.get('LD', 0), importance)

    return user_preferences, custom_weights_adjustments

def prepare_builder_config(gemini_json_array, db_mandatory_courses=None, course_metadata=None, locked_courses=None):
    if course_metadata is None:
        course_metadata = {}
    if locked_courses is None:
        locked_courses = []

    base_mandatory = set(db_mandatory_courses) if db_mandatory_courses else set()
    electives = set()
    unresolved_queries = []

    # --- הוספת אילוצים מקורסים נעולים ---
    for locked_course in locked_courses:
        # מציאת הקורס במטא-דאטה
        course_id = None
        for cid, meta in course_metadata.items():
            if meta.get("course_name") == locked_course:
                course_id = cid
                break

        if course_id:
            # הוספת אילוץ שמחייב את הקורס להיות נכלל
            gemini_json_array.append({
                "type": "course_modification",
                "payload": {
                    "action": "add",
                    "target_course": str(course_id)
                }
            })

    # אתחול מילון התצורה, כולל המפתחות לאילוצים גלובליים
    builder_config = {
        "mandatory": [],
        "electives": [],
        "removed_courses": [],
        "unresolved_queries": [],
        "feedback_messages": [],
        "hard_constraints": [],
        "credit_range": None,
        "course_schedule_pref": {},
        "unwanted_days": [],
        "global_time_range": {},
        "specific_day_time_range": {},
        "max_daily_hours": None,
        "max_study_days": None,
        "max_consecutive_hours": None,
        "windows_minimization": False,
        "allow_course_drop": False,
        "max_course_drops": 0,
        "avoid_lecturers": [],
    }

    def _resolve_course(query):
        if not query:
            return None, None
        q_lower = str(query).lower().strip()

        for cid, data in course_metadata.items():
            if str(cid) == q_lower:
                return int(cid), data.get("group", "בחירה")

        for cid, data in course_metadata.items():
            course_name = str(data.get("course_name", "")).lower().strip()
            if q_lower == course_name:
                return int(cid), data.get("group", "בחירה")

        partial_matches = [
            (cid, data) for cid, data in course_metadata.items()
            if q_lower in str(data.get("course_name", "")).lower()
        ]
        if len(partial_matches) == 1:
            cid, data = partial_matches[0]
            return int(cid), data.get("group", "בחירה")
        elif len(partial_matches) > 1:
            return None, None

        return None, None
    
    def _normalize_pref_time_value(value):
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            h = int(value)
            m = int(round((float(value) - h) * 60))
            return f"{h:02d}:{m:02d}"
        text = str(value).strip()
        match = re.search(r"(\d{1,2}):(\d{2})", text)
        if match:
            return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
        match = re.search(r"\d{1,2}", text)
        return f"{int(match.group(0)):02d}:00" if match else None


    def _normalize_session_type_value(value):
        raw = str(value or "").lower().strip()
        if any(t in raw for t in ["tutorial", "exercise", "lab", "תרגול", "מעבדה"]):
            return "tutorial"
        if any(t in raw for t in ["lecture", "lec", "הרצאה"]):
            return "lecture"
        return None
    
    for constraint in gemini_json_array:
        c_type = constraint.get("type")
        importance = constraint.get("importance", 1)
        payload = constraint.get("payload", {})

        # --- לכידת אילוצים גלובליים עם זיהוי גמיש ---
        if c_type == "day_off":
            for day_num in _day_numbers_from_payload(payload):
                builder_config["unwanted_days"].append(day_num)
                _append_builder_hard_constraint(
                    builder_config,
                    "day_off",
                    {"day": day_num},
                    importance,
                )

        elif c_type == "time_range":
            # הגנה אגרסיבית: LLM לפעמים משנה את שם המפתח
            start_val = payload.get("start_time")
            if start_val is None: start_val = payload.get("from")
            if start_val is None: start_val = payload.get("time")

            end_val = payload.get("end_time")
            if end_val is None: end_val = payload.get("until")

            time_data = {
                "from": start_val,
                "until": end_val
            }

            day_nums = _day_numbers_from_payload(payload)
            if day_nums:
                for day_num in day_nums:
                    builder_config.setdefault("specific_day_time_range", {})[day_num] = time_data
                    _append_builder_hard_constraint(
                        builder_config,
                        "time_range",
                        {
                            "day": day_num,
                            "from": start_val,
                            "until": end_val,
                        },
                        importance,
                    )
            else:
                builder_config["global_time_range"] = time_data
                _append_builder_hard_constraint(
                    builder_config,
                    "time_range",
                    {
                        "day": None,
                        "from": start_val,
                        "until": end_val,
                    },
                    importance,
                )

        elif c_type == "daily_load":
            value = _first_payload_value(payload, "max_hours", "hours", "max_daily_hours", "daily_load", "value")
            max_h = _coerce_numeric_constraint(value)
            if max_h is not None:
                builder_config["max_daily_hours"] = max_h
                _append_builder_hard_constraint(
                    builder_config,
                    "daily_load",
                    {"max_hours": max_h},
                    importance,
                )

        elif c_type == "study_days_limit":
            value = _first_payload_value(payload, "max_days", "days", "study_days", "value")
            max_d = _coerce_numeric_constraint(value)
            if max_d is not None:
                builder_config["max_study_days"] = int(max_d)
                _append_builder_hard_constraint(
                    builder_config,
                    "study_days_limit",
                    {"max_days": int(max_d)},
                    importance,
                )

        elif c_type == "max_consecutive_hours":
            value = _first_payload_value(payload, "max_hours", "hours", "max_consecutive_hours", "value")
            max_h = _coerce_numeric_constraint(value)
            if max_h is not None:
                builder_config["max_consecutive_hours"] = max_h
                _append_builder_hard_constraint(
                    builder_config,
                    "max_consecutive_hours",
                    {"max_hours": max_h},
                    importance,
                )

        elif c_type == "windows_minimization":
            builder_config["windows_minimization"] = True
            _append_builder_hard_constraint(
                builder_config,
                "windows_minimization",
                {},
                importance,
            )

        elif c_type == "course_drop_flexibility":
            enabled = payload.get("enabled", True)
            max_drops = payload.get("max_drops", 1)

            try:
                max_drops = int(max_drops)
            except (TypeError, ValueError):
                max_drops = 1

            max_drops = max(1, min(max_drops, 3))

            if enabled:
                builder_config["allow_course_drop"] = True
                builder_config["max_course_drops"] = max(
                    builder_config.get("max_course_drops", 0),
                    max_drops,
                )

                _append_builder_hard_constraint(
                    builder_config,
                    "course_drop_flexibility",
                    {
                        "enabled": True,
                        "max_drops": max_drops,
                        "reason": payload.get("reason", "other"),
                    },
                    importance,
                )

        elif c_type == "lecturer_constraint":
            action = str(payload.get("action") or "avoid").lower().strip()
            lecturer_names = _payload_values(
                payload,
                "lecturer_name",
                "lecturer_names",
                "instructor_name",
                "instructor_names",
                "lecturer",
                "lecturers",
                splitter=_split_people_names,
            )
            for lecturer_name in lecturer_names:
                lecturer_name = _clean_lecturer_candidate(lecturer_name)
                if (
                    not lecturer_name
                    or _looks_like_non_lecturer_candidate(lecturer_name)
                    or action not in {"avoid", "exclude", "without", "בלי", "לא"}
                ):
                    continue
                builder_config["avoid_lecturers"].append(lecturer_name)
                _append_builder_hard_constraint(
                    builder_config,
                    "lecturer_constraint",
                    {
                        "action": "avoid",
                        "lecturer_name": lecturer_name,
                    },
                    importance,
                )

        elif c_type == "course_modification":
            action = payload.get("action")
            target = payload.get("target_course")
            if action == "add":
                cid, group = _resolve_course(target)
                if cid:
                    if group == "חובה":
                        base_mandatory.add(cid)
                    else:
                        electives.add(cid)
                elif target: unresolved_queries.append(target)

            elif action == "remove":
                cid, _ = _resolve_course(target)
                if cid:
                    base_mandatory.discard(cid)
                    electives.discard(cid)
                    builder_config["removed_courses"].append(cid)
                elif target: unresolved_queries.append(target)

            elif action == "swap":
                cid_remove, _ = _resolve_course(payload.get("remove_course"))
                cid_add, group_add = _resolve_course(payload.get("add_course"))

                if cid_remove:
                    base_mandatory.discard(cid_remove)
                    electives.discard(cid_remove)
                    builder_config["removed_courses"].append(cid_remove)
                elif payload.get("remove_course"):
                    unresolved_queries.append(payload.get("remove_course"))

                if cid_add:
                    if group_add == "חובה":
                        base_mandatory.add(cid_add)
                    else:
                        electives.add(cid_add)
                elif payload.get("add_course"):
                    unresolved_queries.append(payload.get("add_course"))

        elif c_type == "credit_range":
            min_cr = payload.get("min_credits", 0)
            max_cr = payload.get("max_credits", 100)
            builder_config["credit_range"] = {"min": min_cr, "max": max_cr}
            

            total_mandatory_credits = 0
            for cid in base_mandatory:
                meta = course_metadata.get(cid, {})
                try:
                    total_mandatory_credits += float(meta.get("credit_points", 0))
                except (ValueError, TypeError):
                    pass

            if total_mandatory_credits > max_cr:
                builder_config["feedback_messages"].append(f"⚖️ שים לב: ביקשת ללמוד מקסימום {max_cr} נקודות זכות, אבל קורסי החובה שלך הסמסטר כבר דורשים {total_mandatory_credits} נק\"ז. אם תרצה להפחית עומס, אנא כתוב לי איזה קורס חובה להסיר.")
            elif total_mandatory_credits < min_cr:
                builder_config["feedback_messages"].append(f"⚖️ שים לב: ביקשת ללמוד לפחות {min_cr} נקודות זכות, אבל קורסי החובה שלך כרגע נותנים רק {total_mandatory_credits} נק\"ז. אנא כתוב לי אילו קורסי בחירה תרצה שאוסיף לך למערכת כדי להשלים את הפער.")

        elif c_type == "info_request":
            query_type = payload.get("query_type", "")
            if query_type == "electives":
                available_electives = []
                for cid, meta in course_metadata.items():
                    if int(cid) not in base_mandatory:
                        available_electives.append(f"{meta.get('course_name')} (קוד: {cid})")

                if available_electives:
                    msg = "📚 הנה קורסי הבחירה שפתוחים עבורך הסמסטר:\n* " + "\n* ".join(available_electives) + "\n\nאיזה מהם תרצה שאוסיף למערכת?"
                    builder_config["feedback_messages"].append(msg)
                else:
                    builder_config["feedback_messages"].append("📚 סרקתי את המערכת, אך לא מצאתי קורסי בחירה פתוחים עבורך הסמסטר.")

        elif c_type == "course_schedule_pref":
            target = (
                payload.get("target_course")
                or payload.get("course_name")
                or payload.get("course_id")
            )

            cid, _ = _resolve_course(target)

            if cid:
                day_num = None
                day_raw = payload.get("day") or payload.get("day_of_week")

                if day_raw:
                    day_num = _normalize_day_number(day_raw)

                start_time = (
                    payload.get("start_time")
                    or payload.get("from")
                    or payload.get("time_from")
                )

                end_time = (
                    payload.get("end_time")
                    or payload.get("until")
                    or payload.get("time_until")
                )

                start_time = _normalize_pref_time_value(start_time)
                end_time = _normalize_pref_time_value(end_time)

                exact_start_time = _normalize_pref_time_value(
                    payload.get("exact_start_time") or payload.get("exact_time")
                )

                session_type = _normalize_session_type_value(payload.get("session_type"))

                match_mode = str(payload.get("match_mode") or "any").lower().strip()
                if match_mode not in {"any", "all"}:
                    match_mode = "any"

                pref_entry = {
                    "day": day_num,
                    "from": start_time,
                    "until": end_time,
                    "exact_start_time": exact_start_time,
                    "session_type": session_type,
                    "match_mode": match_mode,
                }

                # ניקוי ערכים ריקים
                pref_entry = {
                    key: value
                    for key, value in pref_entry.items()
                    if value is not None
                }

                if pref_entry:
                    builder_config["course_schedule_pref"][cid] = pref_entry

                    hard_payload = {
                        "course_id": cid,
                        **pref_entry
                    }

                    _append_builder_hard_constraint(
                        builder_config,
                        "course_schedule_pref",
                        hard_payload,
                        importance,
                    )

            elif target:
                unresolved_queries.append(target)

    builder_config["mandatory"] = list(base_mandatory)
    builder_config["electives"] = list(electives)
    builder_config["unresolved_queries"] = unresolved_queries
    builder_config.pop("_hard_constraint_keys", None)

    return builder_config

# =====================================================================
# --- מנגנון התשובות והשמירה (Background Agent) ---
# =====================================================================
def _format_missing_courses_for_reply(missing_course_ids, course_metadata):
    names = []
    for cid in missing_course_ids:
        meta = course_metadata.get(cid, {})
        course_name = meta.get("course_name", str(cid))
        names.append(f"{course_name} ({cid})")
    return ", ".join(names)

def generate_bot_response_text(builder_config, builder_warnings, all_ranked_schedules, weights_adj, student_message, course_metadata, courses_data=None):
    local_warnings = list(builder_warnings)
    unresolved = builder_config.get("unresolved_queries", [])
    for bad_query in unresolved:
        local_warnings.append(f"לא מצאתי בקטלוג קורס בשם '{bad_query}'.")

    feedback_msgs = builder_config.get("feedback_messages", [])
    local_warnings.extend(feedback_msgs)

    if not all_ranked_schedules:
        failure_msg = "לצערי, לא הצלחתי למצוא אף מערכת תקינה שעומדת בדרישות הקדם וקורסי החובה שלך. נסה להוריד קורס או לשנות אילוצים."
        if local_warnings:
            failure_msg += "\n" + "\n".join(local_warnings)
        return failure_msg

    top_3 = all_ranked_schedules[:3]
    others = all_ranked_schedules[3:]
    metric_names = {'P': 'ימי חופש', 'G': 'צמצום חלונות', 'D': 'מספר ימי לימוד מבוקש', 'T': 'שעות לימוד רצויות', 'LD': 'עומס יומי מקסימלי', 'LW': 'איזון שבועי'}

    windows_requested = bool(builder_config.get("windows_minimization", False))
    allow_course_drop = bool(builder_config.get("allow_course_drop", False))
    max_course_drops = builder_config.get("max_course_drops", 0)

    expected_course_ids = set()
    for cid in builder_config.get("mandatory", []):
        try:
            expected_course_ids.add(int(cid))
        except (TypeError, ValueError):
            pass

    for cid in builder_config.get("electives", []):
        try:
            expected_course_ids.add(int(cid))
        except (TypeError, ValueError):
            pass

    satisfied = []
    tradeoffs = []
    impossible = []

    for metric in weights_adj.keys():
        if metric == "G":
            continue
        name = metric_names.get(metric, metric)
        best_top3 = min([s['breakdown'].get(metric, 1.0) for s in top_3]) if top_3 else 1.0
        best_other = min([s['breakdown'].get(metric, 1.0) for s in others]) if others else 1.0

        if best_top3 == 0:
            satisfied.append(name)
        elif best_other == 0:
            tradeoffs.append(name)
        else:
            impossible.append(name)

    best_score = top_3[0]['score']
    sentences = [f"בניתי עבורך מערכת מתאימה בציון {best_score}/100 והצגתי את החלופות הטובות ביותר שמצאתי."]

    if builder_config.get("windows_minimization"):
        best_windows = top_3[0].get("window_count")
        best_window_hours = top_3[0].get("window_hours")
        if best_windows is not None:
            sentences.append(
                f"בגלל שביקשת כמה שפחות חלונות, דירגתי את החלופות לפי מספר קורסים ואז לפי מספר חלונות; בחלופה המובילה יש {best_windows} חלונות"
                + (f" בסך {best_window_hours} שעות." if best_window_hours is not None else ".")
            )

    if builder_config.get("allow_course_drop"):
        sentences.append(
            f"בנוסף, מאחר שאישרת להוריד קורס, בדקתי גם קבוצת מערכות עם עד {builder_config.get('max_course_drops', 1)} קורסים פחות כדי לנסות לצמצם חלונות."
        )

    constraint_parts = []
    if satisfied:
        constraint_parts.append("עמדתי באילוצים: " + ", ".join(satisfied))
    if tradeoffs:
        constraint_parts.append("בחלק מהאילוצים היו חלופות אפשריות אבל פחות מאוזנות: " + ", ".join(tradeoffs))
    if impossible:
        constraint_parts.append("חלק מהבקשות לא הסתדרו טכנית עם הקורסים וההתנגשויות: " + ", ".join(impossible))
    if constraint_parts:
        sentences.append("; ".join(constraint_parts) + ".")
    else:
        sentences.append("לא זיהיתי אילוצי נוחות מיוחדים מעבר לבניית המערכת עצמה.")

    if windows_requested and not allow_course_drop:
        sentences.append(
            "יש אפשרות גם להוריד את אחד הקורסים כדי לצמצם חלונות. "
            "אם ברצונך לעשות זאת, תרשום לי כאן בצ׳אט ואבדוק חלופות עם פחות קורסים."
        )

    if allow_course_drop and expected_course_ids:
        missing_lines = []

        for index, ranked in enumerate(top_3, start=1):
            scheduled_ids = set()
            for c in ranked.get("schedule", []):
                try:
                    scheduled_ids.add(int(c.course_name))
                except (TypeError, ValueError):
                    pass

            missing_ids = sorted(expected_course_ids - scheduled_ids)

            if missing_ids:
                missing_text = _format_missing_courses_for_reply(missing_ids, course_metadata)
                missing_lines.append(f"בחלופה {index} לא שובץ: {missing_text}.")
            else:
                missing_lines.append(f"בחלופה {index} שובצו כל הקורסים.")

        if missing_lines:
            sentences.append("פירוט קורסים שלא שובצו: " + " ".join(missing_lines)) 

    if courses_data and _has_assessment_intent(student_message):
        prefer = _detect_assessment_preference(student_message)
        assessment_reply = _build_assessment_courses_reply(
            prefer,
            courses_data,
            course_metadata
        )
        sentences.append(assessment_reply)

    if local_warnings:
        sentences.append("חשוב לדעת: " + " ".join(local_warnings[:2]))

    return " ".join(sentences[:7])

# =====================================================================
# --- ML criterion weights (learned from user choices) ---
# =====================================================================
ML_WEIGHTS_CACHE_SECONDS = 300
# Graph files are useful locally; on a hosted server the disk is temporary, so set ML_SAVE_PLOTS=false.
ML_SAVE_PLOTS = os.getenv("ML_SAVE_PLOTS", "true").strip().lower() == "true"
_ml_weights_cache = {"weights": None, "loaded_at": 0.0}
_ml_train_lock = threading.Lock()


def _get_best_criterion_weights():
    """Weights of the latest best ML run, cached; None means optimizer defaults."""
    if time.time() - _ml_weights_cache["loaded_at"] < ML_WEIGHTS_CACHE_SECONDS:
        return _ml_weights_cache["weights"]

    weights = None
    try:
        res = supabase.table('ml_criterion_weights').select('*').eq('is_best_run', True) \
            .order('created_at', desc=True).limit(1).execute()
        if res.data:
            row = res.data[0]
            weights = {
                key: float(row[f"weight_{key.lower()}"])
                for key in ["G", "D", "LD", "T", "LW", "C", "A"]
            }
            _log_decision("Loaded ML criterion weights", run_id=row.get("run_id"), model_name=row.get("model_name"), weights=weights)
    except Exception as e:
        _log_decision("ML criterion weights unavailable, using defaults", error=str(e))

    _ml_weights_cache.update(weights=weights, loaded_at=time.time())
    return weights


def _run_ml_retrain(force: bool = False):
    """Background task: retrain all models when enough new labeled sessions exist."""
    if not _ml_train_lock.acquire(blocking=False):
        _log_decision("ML retrain already running, skipped")
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import analytics_pipeline_final

        result = analytics_pipeline_final.maybe_retrain(supabase, force=force, make_plots=ML_SAVE_PLOTS)
        if result.get("trained"):
            _ml_weights_cache["loaded_at"] = 0.0
            _log_decision("ML retrain finished", best_model=result.get("best_model"), weights=result.get("best_weights"))
        return result
    except Exception as e:
        _log_decision("ML retrain failed", error=str(e))
        return None
    finally:
        _ml_train_lock.release()


@app.post("/api/ml/train")
async def train_ml_models(background_tasks: BackgroundTasks, force: bool = True):
    background_tasks.add_task(_run_ml_retrain, force)
    return {"success": True, "message": "ML training started in the background"}


# =====================================================================
# --- Accounts (the browser never reads the students table directly) ---
# =====================================================================
class LoginRequest(BaseModel):
    user_name: str
    password: str


class RegisterRequest(BaseModel):
    user_name: str
    password: str
    program_id: int
    study_year: int
    national_id: Optional[str] = None  # no such column in students; accepted and ignored


class ProfileUpdateRequest(BaseModel):
    user_name: str
    program_id: int
    study_year: int
    email: Optional[str] = None  # no such column in students; accepted and ignored


@app.post("/api/login")
async def login(req: LoginRequest):
    res = supabase.table('students').select('student_id, user_name, program_id, study_year') \
        .eq('user_name', req.user_name.strip()).eq('password', req.password.strip()).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=401, detail="שם משתמש או סיסמה שגויים")
    return res.data[0]


@app.post("/api/register")
async def register(req: RegisterRequest):
    try:
        user_name = req.user_name.strip()
        if supabase.table('students').select('student_id').eq('user_name', user_name).limit(1).execute().data:
            return {"success": False, "error": "שם המשתמש כבר קיים"}

        last = supabase.table('students').select('student_id').order('student_id', desc=True).limit(1).execute().data
        next_id = (last[0]['student_id'] + 1) if last else 1
        supabase.table('students').insert({
            "student_id": next_id,
            "user_name": user_name,
            "program_id": req.program_id,
            "study_year": req.study_year,
            "password": req.password,
            "academic_status": "Active",
        }).execute()
        return {"success": True, "student_id": next_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.put("/api/profile/{student_id}")
async def update_profile(student_id: int, req: ProfileUpdateRequest):
    try:
        res = supabase.table('students').update({
            "user_name": req.user_name.strip(),
            "program_id": req.program_id,
            "study_year": req.study_year,
        }).eq('student_id', student_id).execute()
        if not res.data:
            return {"success": False, "error": "המשתמש לא נמצא"}
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/health")
async def health():
    # Touches the DB so a periodic ping also keeps the free Supabase project active.
    supabase.table('program').select('program_id').limit(1).execute()
    return {"status": "ok"}


def persist_session_constraints(student_id: int, session_id: int, extracted_constraints: list):
    constraints_to_insert = []
    for c in extracted_constraints or []:
        if c.get("type") in TRANSIENT_CONSTRAINT_TYPES:
            continue
        constraints_to_insert.append({
            "student_id": student_id,
            "session_id": session_id,
            "constraint_type": c.get("type"),
            "constraint_payload": c.get("payload", {}),
            "importance_level": c.get("importance", 1),
            "active": True
        })

    supabase.table('studentconstraint').delete().eq('session_id', session_id).execute()

    if constraints_to_insert:
        supabase.table('studentconstraint').insert(constraints_to_insert).execute()

    _log_decision(
        "Session constraints persisted",
        session_id=session_id,
        input_constraint_count=len(extracted_constraints or []),
        persisted_constraint_count=len(constraints_to_insert),
        persisted_constraint_types=[c["constraint_type"] for c in constraints_to_insert],
    )

def save_chat_data_in_background(student_id: int, session_id: int, user_text: str, bot_reply_text: str, schedules_dict: dict, titles_dict: dict, extracted_constraints: list, schedule_scores_dict: dict = None):
    """
    סוכן רקע: שומר את כל נתוני השיחה, המערכות והאילוצים ל-Supabase.
    תיקון #10: שמירת האילוצים מבוצעת בצורה בטוחה — backup לפני מחיקה.
    """
    try:
        schedule_scores_dict = schedule_scores_dict or {}
        # 1. שמירת ההודעות
        supabase.table('chat_messages').insert([
            {"session_id": session_id, "sender": "user", "message_text": user_text},
            {"session_id": session_id, "sender": "bot", "message_text": bot_reply_text}
        ]).execute()

        # 2. שמירת האילוצים לפני שמירת המערכות, כדי שכשל בשמירת מערכת לא יפיל את האילוצים.
        persist_session_constraints(student_id, session_id, extracted_constraints)

        # 3. שמירת המערכות המדורגות
        if schedules_dict:
            supabase.table('generated_schedules').delete().eq('session_id', session_id).execute()

            for alt_key, classes in schedules_dict.items():
                alt_title = titles_dict.get(alt_key, f"חלופה {alt_key}")

                unique_courses = set()
                total_minutes = 0
                for c in classes:
                    unique_courses.add(c.get("courseName"))
                    start_h, start_m = map(int, str(c.get("startTime", "00:00")).split(':'))
                    end_h, end_m = map(int, str(c.get("endTime", "00:00")).split(':'))
                    total_minutes += ((end_h * 60) + end_m) - ((start_h * 60) + start_m) + 10

                scores = schedule_scores_dict.get(alt_key, {})

                new_sched = supabase.table('generated_schedules').insert({
                    "session_id": session_id,
                    "alt_key": alt_key,
                    "title": alt_title,
                    "total_courses": len(unique_courses),
                    "total_hours": round(total_minutes / 60.0, 1),
                    "final_score": scores.get("final_score"),
                    "penalty": scores.get("penalty"),
                    "score_g": scores.get("score_g"),
                    "score_d": scores.get("score_d"),
                    "score_ld": scores.get("score_ld"),
                    "score_t": scores.get("score_t"),
                    "score_lw": scores.get("score_lw"),
                    "score_c": scores.get("score_c"),
                    "score_a": scores.get("score_a"),
                }).execute()

                schedule_id = new_sched.data[0]['schedule_id']

                classes_to_insert = []
                for c in classes:
                    start_str = str(c.get('startTime', '00:00'))
                    end_str = str(c.get('endTime', '00:00'))

                    classes_to_insert.append({
                        "schedule_id": schedule_id,
                        "course_name": c.get("courseName", ""),
                        "class_type": c.get("type", "הרצאה"),
                        "lecturer": c.get("lecturer", ""),
                        "day_of_week": c.get("day", 1),
                        "start_time": start_str + ":00" if len(start_str) == 5 else start_str,
                        "end_time": end_str + ":00" if len(end_str) == 5 else end_str
                    })

                if classes_to_insert:
                    supabase.table('schedule_classes').insert(classes_to_insert).execute()

    except Exception as e:
        print(f"❌ שגיאה בשמירת נתונים ברקע: {e}")

# =====================================================================
# --- ראוט הצ'אט הראשי (Main Chat Endpoint) ---
# =====================================================================
@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, background_tasks: BackgroundTasks):
    # 0. ניהול שיחות
    current_session_id = req.session_id
    _log_decision(
        "Chat request received",
        student_id=req.student_id,
        semester=req.semester,
        session_id=current_session_id,
        message=req.message,
        history_count=len(req.history or []),
        locked_courses=req.locked_courses,
    )
    if not current_session_id and req.student_id:
        sem_str = {1: 'א', 2: 'ב', 3: 'קיץ'}.get(req.semester, 'א')
        short_text = req.message[:25] + "..." if len(req.message) > 25 else req.message
        dynamic_title = f"סמסטר {sem_str} | {short_text}"

        new_session = supabase.table('chat_sessions').insert({
            "student_id": req.student_id,
            "semester": sem_str,
            "title": dynamic_title
        }).execute()
        current_session_id = new_session.data[0]['session_id']
        _log_decision("Created new chat session", session_id=current_session_id, title=dynamic_title)

    # 1. שליפת נתוני הסטודנט
    student_data = get_student_data(req.student_id, req.semester)

    if not student_data:
        _log_decision("Student lookup failed", student_id=req.student_id)
        raise HTTPException(status_code=404, detail="Student not found in database")
    _log_decision(
        "Student data loaded",
        student_id=req.student_id,
        study_year=student_data.get("study_year"),
        program_id=student_data.get("program_id"),
        program_name=student_data.get("program_name"),
        specialization_ids=student_data.get("specialization_ids"),
    )

    # 2. קורסי חובה
    if req.semester == 3:
        courses_data = get_summer_courses_data(student_data, req.semester)
    else:
        courses_data = get_required_courses(student_data, req.semester)
    _log_decision(
        "Required courses loaded",
        mandatory_count=len(courses_data.get("mandatory", [])),
        mandatory_courses=courses_data.get("mandatory", []),
        specialization_course_counts={k: len(v) for k, v in courses_data.get("specialization", {}).items()},
    )

    student_year = student_data["study_year"]
    program_name = student_data.get("program_name", "לא צוין")

    # 3. משיכת אילוצים היסטוריים
    existing_constraints_db = []
    if current_session_id:
        db_const_res = supabase.table('studentconstraint').select('*').eq('session_id', current_session_id).eq('active', True).execute()
        if hasattr(db_const_res, 'data') and db_const_res.data:
            for row in db_const_res.data:
                existing_constraints_db.append({
                    "type": row['constraint_type'],
                    "importance": row['importance_level'],
                    "payload": row.get('constraint_payload', {})
                })
    _log_decision(
        "Existing active constraints loaded",
        session_id=current_session_id,
        constraint_count=len(existing_constraints_db),
        constraints=existing_constraints_db,
    )
    # 4. Current-turn constraint parsing. Obvious requests skip Gemini entirely.
    parser_source = "local"
    output_data = _try_parse_constraints_locally(req.message)
    if output_data is None and not USE_GEMINI:
        _log_decision("Message not recognized locally and Gemini is disabled", message=req.message)
        bot_reply_text = f"```json\n{json.dumps({'text': UNRECOGNIZED_MESSAGE_REPLY, 'schedules': None, 'titles': None, 'gradeDistribution': None, 'exams': None}, ensure_ascii=False)}\n```"
        if req.student_id and current_session_id:
            save_chat_data_in_background(
                student_id=req.student_id,
                session_id=current_session_id,
                user_text=req.message,
                bot_reply_text=bot_reply_text,
                schedules_dict={},
                titles_dict={},
                extracted_constraints=existing_constraints_db,
            )
        return {"reply": bot_reply_text, "session_id": current_session_id}
    if output_data is None:
        parser_source = "gemini"
        try:
            output_data = _parse_constraints_with_gemini(
                message=req.message,
                student_year=student_year,
                semester=req.semester,
                program_name=program_name,
            )
        except Exception as e:
            output_data = {
                "requires_schedule_build": True,
                "is_no_constraints_request": False,
                "explanation": "קרתה שגיאה קטנה, תוכל לחזור על הבקשה?",
                "extracted_constraints": [],
                "error": str(e),
            }

    parsed_constraints = _sanitize_current_constraints(req.message, output_data.get("extracted_constraints") or [])
    output_data["extracted_constraints"] = parsed_constraints
    active_constraints = _merge_active_constraints(
        existing_constraints=existing_constraints_db,
        parsed_constraints=parsed_constraints,
        is_no_constraints_request=output_data.get("is_no_constraints_request", False),
        message=req.message,
    )
    _log_decision(
        "Constraint parser response parsed",
        requires_schedule_build=output_data.get("requires_schedule_build"),
        is_no_constraints_request=output_data.get("is_no_constraints_request"),
        extracted_constraint_count=len(parsed_constraints),
        extracted_constraints=parsed_constraints,
        active_constraint_count=len(active_constraints),
        active_constraints=active_constraints,
        parser_source=parser_source,
        parse_error=output_data.get("error"),
    )

    # =================================================================
    # 6. שליפת מטא-דאטה לפני הניתוב
    # =================================================================
    all_potential_ids = set(courses_data.get("mandatory", []))

    for spec_courses in courses_data.get("specialization", {}).values():
        all_potential_ids.update(spec_courses)

    if courses_data.get("is_summer_pool"):
        all_potential_ids.update(courses_data.get("summer_current_year", []))
        all_potential_ids.update(courses_data.get("summer_next_year", []))

        for spec_courses in courses_data.get("summer_specialization", {}).values():
            all_potential_ids.update(spec_courses)
    course_metadata = get_course_metadata(list(all_potential_ids), req.semester)

    for m_id in courses_data.get("mandatory", []):
        if m_id in course_metadata:
            course_metadata[m_id]["group"] = "חובה"
    _log_decision(
        "Course metadata loaded",
        potential_course_count=len(all_potential_ids),
        metadata_count=len(course_metadata),
        missing_metadata_course_ids=[cid for cid in all_potential_ids if cid not in course_metadata],
    )

    # =====================================================================
    # --- מנגנון ניתוב שאלות מידע (Short-Circuit) ---
    # =====================================================================
    extracted_constraints = parsed_constraints

    is_info_only = False

    if not output_data.get("requires_schedule_build", True):
        is_info_only = True

    # סל 3: האילוצים שדורשים תשובה מילולית בלבד (Short-Circuit)
    text_only_types = ["info_request", "language_preference", "assessment_preference"]
    has_info_request = any(c.get("type") in text_only_types for c in extracted_constraints)
    info_keywords = [
    "אופציות",
    "אפשרות",
    "אפשרויות",
    "מתי יש",
    "איזה שעות",
    "קבוצות",
    "מרצים",
    "פרטים",
    "מי המרצה",
    "איזה קורסי",
    "איזה קורס",
    "קורסים",
    "שפה",
    "עברית",
    "אנגלית",
    "english",
    "hebrew",
    "language",
    "מבחן",
    "מבחנים",
    "ללא מבחן",
    "בלי מבחן",
    "פחות מבחנים",
    "כמה שפחות מבחנים",
    "פרויקט",
    "פרויקטים",
    "עבודות",
    "מטלות",
    "בוחן",
    "בחנים",
    "exam",
    "exams",
    "no exam",
    "without exam",
    "less exams",
    "project",
    "projects",
    "assignments",
    "homework",
]
    if has_info_request or any(word in req.message.lower() for word in info_keywords):
        build_keywords = ["תבנה", "בנה", "תיצור", "צור", "מערכת", "תוסיף", "תוריד", "תסיר", "שים לי", "תקבע", "תשבץ", "אני רוצה"]
        if not any(word in req.message.lower() for word in build_keywords):
            is_info_only = True
    _log_decision(
        "Route decision completed",
        is_info_only=is_info_only,
        has_info_request=has_info_request,
        requires_schedule_build=output_data.get("requires_schedule_build", True),
    )
    if (
        req.semester == 3
        and output_data.get("requires_schedule_build", True)
        and not _summer_has_selected_courses(active_constraints, req.locked_courses)
    ):
        is_info_only = True
        _log_decision(
            "Summer build request without selected courses converted to info-only",
            session_id=current_session_id,
            active_constraints=active_constraints,
        )

    if is_info_only:
        # תיקון #7: organized_by_team נשלף כאן בפעם הראשונה (ולא פעמיים)
        organized_by_team_info = get_course_offerings(courses_data, req.semester)
        _log_decision(
            "Info-only route selected",
            offering_course_count=len(organized_by_team_info),
            offering_team_count=sum(len(teams) for teams in organized_by_team_info.values()),
        )
        chat_reply = _build_info_only_reply(
            message=req.message,
            courses_data=courses_data,
            course_metadata=course_metadata,
            organized_by_team=organized_by_team_info,
        )

        summer_intro = _build_summer_courses_intro(courses_data, course_metadata)

        if req.semester == 3 and summer_intro:
            chat_reply = summer_intro + "\n\n" + chat_reply

        frontend_json_payload = {
            "text": chat_reply,
            "schedules": None,
            "titles": None,
            "gradeDistribution": None,
            "exams": None
        }

        bot_reply_text = f"```json\n{json.dumps(frontend_json_payload, ensure_ascii=False)}\n```"

        if req.student_id and current_session_id:
            save_chat_data_in_background(
                student_id=req.student_id,
                session_id=current_session_id,
                user_text=req.message,
                bot_reply_text=bot_reply_text,
                schedules_dict={},
                titles_dict={},
                extracted_constraints=active_constraints
            )
        return {
            "reply": bot_reply_text,
            "session_id": current_session_id
        }
    # =====================================================================

   # =====================================================================

    # === מנגנון הניתוב הארכיטקטוני (Gatekeeper) ===
    split_result = split_constraints_by_target(active_constraints)
    builder_constraints = split_result["builder_config_constraints"]
    optimizer_constraints = split_result["optimizer_constraints"]

    # 7. בניית ה-Config ל-Builder (מקבל רק builder_constraints!)
    db_mandatory_ids = courses_data.get("mandatory", [])
    builder_config = prepare_builder_config(builder_constraints, db_mandatory_ids, course_metadata, req.locked_courses)
    if req.semester == 3:
        selected_electives = list(builder_config.get("electives", []))

        if selected_electives:
            builder_config["mandatory"] = list(dict.fromkeys(
                builder_config.get("mandatory", []) + selected_electives
            ))
            builder_config["electives"] = []

        if builder_config.get("mandatory"):
            builder_config["min_schedule_courses"] = max(
                1,
                len(builder_config.get("mandatory", []))
            )
        else:
            builder_config["min_schedule_courses"] = 1
    _log_decision(
        "Builder config prepared",
        mandatory=builder_config.get("mandatory", []),
        electives=builder_config.get("electives", []),
        removed_courses=builder_config.get("removed_courses", []),
        unwanted_days=builder_config.get("unwanted_days", []),
        global_time_range=builder_config.get("global_time_range", {}),
        specific_day_time_range=builder_config.get("specific_day_time_range", {}),
        course_schedule_pref=builder_config.get("course_schedule_pref", {}),
        course_session_type=builder_config.get("course_session_type", {}),
        avoid_lecturers=builder_config.get("avoid_lecturers", []),
        allow_course_drop=builder_config.get("allow_course_drop", False),
        max_course_drops=builder_config.get("max_course_drops", 0),
        hard_constraints=builder_config.get("hard_constraints", []),
        unresolved_queries=builder_config.get("unresolved_queries", []),
        feedback_messages=builder_config.get("feedback_messages", []),
    )

    # 8. עדכון courses_data
    courses_data["mandatory"] = builder_config["mandatory"]
    courses_data["electives"] = builder_config["electives"]

    for removed_cid in builder_config.get("removed_courses", []):
        if removed_cid in courses_data["mandatory"]:
            courses_data["mandatory"].remove(removed_cid)
        if removed_cid in courses_data.get("electives", []):
            courses_data["electives"].remove(removed_cid)
        for spec_name, spec_list in courses_data.get("specialization", {}).items():
            if removed_cid in spec_list:
                spec_list.remove(removed_cid)

    # 9. תיקון #7: שליפה אחת בלבד של organized_by_team (כאן, לא בשלב 6)
    organized_by_team = get_course_offerings(courses_data, req.semester)
    _log_decision(
        "Course offerings loaded",
        offering_course_count=len(organized_by_team),
        offering_team_count=sum(len(teams) for teams in organized_by_team.values()),
        offering_session_count=sum(len(sessions) for teams in organized_by_team.values() for sessions in teams.values()),
        course_ids=list(organized_by_team.keys()),
    )

    # תיקון: השלמת course_metadata עם קורסי בחירה שהתווספו דרך הצ'אט
    # (הם לא היו ידועים בשלב 6 כי builder_config מוכן רק בשלב 7)
    elective_ids_new = [
        eid for eid in builder_config.get("electives", [])
        if eid not in course_metadata
    ]
    if elective_ids_new:
        extra_metadata = get_course_metadata(elective_ids_new, req.semester)
        course_metadata.update(extra_metadata)
        _log_decision("Extra elective metadata loaded", elective_ids=elective_ids_new, metadata_count=len(extra_metadata))

    # 10. Builder
    builder = ScheduleBuilder()
    valid_schedules, build_status, builder_warnings = builder.build_all_valid_schedules(
        builder_config, organized_by_team, course_metadata
    )
    _log_decision(
        "Builder completed",
        build_status=build_status,
        valid_schedule_count=len(valid_schedules),
        builder_warning_count=len(builder_warnings),
        builder_warnings=builder_warnings,
    )

    # 11. Optimizer
    user_prefs, weights_adj = extract_optimizer_preferences({"extracted_constraints": optimizer_constraints})
    _log_decision("Optimizer preferences extracted", user_preferences=user_prefs, weight_adjustments=weights_adj)

    optimizer = ScheduleOptimizer(weights=_get_best_criterion_weights())
    all_ranked_schedules = optimizer.evaluate_and_rank_schedules(
        valid_schedules,
        user_prefs,
        course_metadata
    )
    optimizer_diagnostics = getattr(optimizer, "last_diagnostics", {})
    _log_decision(
        "Optimizer completed",
        ranked_schedule_count=len(all_ranked_schedules),
        diagnostics=optimizer_diagnostics,
    )

    failure_diagnostics = None
    if not all_ranked_schedules:
        failure_diagnostics = _build_failure_diagnostics(
            valid_schedules=valid_schedules,
            build_status=build_status,
            builder_warnings=builder_warnings,
            optimizer_diagnostics=optimizer_diagnostics,
            user_prefs=user_prefs,
        )
        _log_decision("No suitable schedule found", diagnostics=failure_diagnostics)
        print(
            "[ScheduleFailure] "
            f"status={build_status} "
            f"reason={failure_diagnostics.get('reason')} "
            f"diagnostics={json.dumps(failure_diagnostics, ensure_ascii=False, default=str)}"
        )

    top_3_schedules = all_ranked_schedules[:3]

    # 12. ארגון המערכות המדורגות
    schedules_dict = {}
    titles_dict = {}
    actually_scheduled_course_ids = set()

    schedule_scores_dict = {}

    for i, ranked in enumerate(top_3_schedules):
        alt_key = f"alt{i+1}"
        titles_dict[alt_key] = f"חלופה {i+1} (ציון: {ranked['score']})"
        breakdown = ranked.get("breakdown", {}) or {}

        schedule_scores_dict[alt_key] = {
            "final_score": ranked.get("score"),
            "penalty": ranked.get("penalty"),
            "score_g": breakdown.get("G", 0),
            "score_d": breakdown.get("D", 0),
            "score_ld": breakdown.get("LD", 0),
            "score_t": breakdown.get("T", 0),
            "score_lw": breakdown.get("LW", 0),
            "score_c": breakdown.get("C", 0),
            "score_a": breakdown.get("A", 0),
            "window_count": ranked.get("window_count"),
            "window_hours": ranked.get("window_hours"),
            "course_count": ranked.get("course_count"),
        }

        sched = ranked['schedule']
        temp_list = []
        for c in sched:
            c_id_int = int(c.course_name)
            actually_scheduled_course_ids.add(c_id_int)
            base_course_name = c.raw_data.get("course_name", str(c.course_name))

            temp_list.append({
                "courseName": base_course_name,
                "day": c.day,
                "startTime": c.start_time,
                "endTime": c.end_time,
                "type": c.raw_data.get("session_type", ""),
                "lecturer": c.raw_data.get("instructor_name") or "לא צוין"
            })
        schedules_dict[alt_key] = temp_list

    # תיקון #9: אם אין מערכות — schedules_dict יהיה None ולא {}
    if not schedules_dict:
        schedules_dict = None
        titles_dict = None

    # הכנת נתוני ציונים ומבחנים
    grade_distribution = []
    exams_list = []

    if actually_scheduled_course_ids:
        for c_id, meta in course_metadata.items():
            if c_id in actually_scheduled_course_ids:
                weights = meta.get("weights", {})
                is_elective = c_id in courses_data.get("electives", [])

                grade_distribution.append({
                    "courseName": meta.get("course_name", str(c_id)),
                    "creditPoints": meta.get("credit_points", "-"),
                    "finalExamWeight": f"{weights.get('exam', 0)}%",
                    "midExamWeight": f"{weights.get('mid_semester', 0)}%",
                    "projectWeight": f"{weights.get('project', 0)}%",
                    "homeworkWeight": f"{weights.get('assignments', 0)}%",
                    "is_elective": is_elective,
                    "is_attendance_mandatory": meta.get("is_attendance_mandatory", False)
                })

                if meta.get("has_exam") and meta.get("exam_dates"):
                    dates = meta.get("exam_dates")
                    if dates.get("moed_a") or dates.get("moed_b"):
                        exams_list.append({
                            "courseName": meta.get("course_name", str(c_id)),
                            "dateA": dates.get("moed_a") or "",
                            "dateB": dates.get("moed_b") or ""
                        })

    # 13. סוכן התשובות
    bot_text = generate_bot_response_text(
    builder_config=builder_config,
    builder_warnings=builder_warnings,
    all_ranked_schedules=all_ranked_schedules,
    weights_adj=weights_adj,
    student_message=req.message,
    course_metadata=course_metadata,
    courses_data=courses_data
)
    summer_intro = _build_summer_courses_intro(courses_data, course_metadata)

    if req.semester == 3 and summer_intro:
        bot_text = summer_intro + "\n\n" + bot_text

    frontend_json_payload = {
        "text": bot_text,
        "schedules": schedules_dict,
        "titles": titles_dict,
        "gradeDistribution": grade_distribution if grade_distribution else None,
        "exams": exams_list if exams_list else None
    }
    _log_decision(
        "Response payload prepared",
        has_schedules=schedules_dict is not None,
        schedule_keys=list(schedules_dict.keys()) if schedules_dict else [],
        terminal_only_failure_diagnostics=failure_diagnostics is not None,
    )

    bot_reply_text = f"```json\n{json.dumps(frontend_json_payload, ensure_ascii=False)}\n```"

    if req.student_id and current_session_id:
        # שמירה סנכרונית כדי שה-Frontend יקבל נתונים מעודכנים בלבד!
        save_chat_data_in_background(
            student_id=req.student_id,
            session_id=current_session_id,
            user_text=req.message,
            bot_reply_text=bot_reply_text,
            schedules_dict=schedules_dict if schedules_dict is not None else {},
            titles_dict=titles_dict if titles_dict is not None else {},
            extracted_constraints=active_constraints,
            schedule_scores_dict=schedule_scores_dict if schedules_dict is not None else {}
        )

    return {
        "reply": bot_reply_text,
        "session_id": current_session_id
    }

# =====================================================================
# --- ראוטים לניהול היסטוריה ---
# =====================================================================
@app.get("/api/history/{student_id}")
async def get_history(student_id: int):
    global supabase
    try:
        try:
            res = supabase.table('chat_sessions').select('*').eq('student_id', student_id).order('created_at', desc=False).execute()
        except Exception as e:
            if "Server disconnected" in str(e) or "protocol" in str(e).lower():
                print("⚠️ השרת התנתק. מחדש את החיבור ל-Supabase ומנסה שנית...")
                supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
                res = supabase.table('chat_sessions').select('*').eq('student_id', student_id).order('created_at', desc=False).execute()
            else:
                raise e

        sessions = res.data if res and hasattr(res, 'data') and res.data else []
        if not sessions: return []

        try:
            saved_res = supabase.table('generated_schedules').select('session_id').eq('is_selected', True).execute()
            saved_data = saved_res.data if saved_res and hasattr(saved_res, 'data') and saved_res.data else []
        except Exception as db_err:
            print(f"Warning: Could not fetch schedules for hearts: {db_err}")
            saved_data = []

        saved_ids = set()
        for row in saved_data:
            if row and 'session_id' in row:
                saved_ids.add(row['session_id'])

        for s in sessions:
            if 'session_id' in s:
                s['has_saved_schedule'] = s['session_id'] in saved_ids
            else:
                s['has_saved_schedule'] = False

        return sessions
    except Exception as e:
        import traceback
        print(f"\n❌ CRITICAL ERROR in get_history for student {student_id}:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history/session/{session_id}")
async def get_specific_session(session_id: int):
    try:
        session_res = supabase.table('chat_sessions').select('*').eq('session_id', session_id).execute()
        session_info = session_res.data[0] if session_res.data else {}

        constraints_res = supabase.table('studentconstraint').select('*').eq('session_id', session_id).eq('active', True).execute()
        active_constraints = constraints_res.data if constraints_res.data else []

        messages_res = supabase.table('chat_messages').select('*').eq('session_id', session_id).order('created_at').execute()
        schedules_res = supabase.table('generated_schedules').select('*').eq('session_id', session_id).execute()

        schedules_dict = {}
        titles_dict = {}
        saved_alt = None

        for sched in schedules_res.data:
            alt_key = sched['alt_key']
            titles_dict[alt_key] = sched['title']
            if sched.get('is_selected') == True:
                saved_alt = alt_key

            classes_res = supabase.table('schedule_classes').select('*').eq('schedule_id', sched['schedule_id']).execute()

            formatted_classes = []
            for c in classes_res.data:
                start_t = str(c.get('start_time', '00:00'))[:5]
                end_t = str(c.get('end_time', '00:00'))[:5]
                formatted_classes.append({
                    "day": c.get('day_of_week'),
                    "startTime": start_t,
                    "endTime": end_t,
                    "courseName": c.get('course_name'),
                    "type": c.get('class_type'),
                    "lecturer": c.get('lecturer')
                })
            schedules_dict[alt_key] = formatted_classes

        grades_list = []
        exams_list = []

        for msg in messages_res.data:
            if msg.get('sender') == 'bot':
                msg_text = msg.get('message_text', '')
                json_match = re.search(r'```json\s*(.*?)\s*```', msg_text, re.DOTALL)
                if json_match:
                    try:
                        parsed_data = json.loads(json_match.group(1))

                        if 'text' in parsed_data:
                            msg['message_text'] = parsed_data['text']

                        if 'gradeDistribution' in parsed_data and parsed_data['gradeDistribution']:
                            grades_list = parsed_data['gradeDistribution']
                        if 'exams' in parsed_data and parsed_data['exams']:
                            exams_list = parsed_data['exams']
                    except Exception:
                        pass

        return {
            "session_info": session_info,
            "active_constraints": active_constraints,
            "messages": messages_res.data,
            "schedules": schedules_dict,
            "titles": titles_dict,
            "grades": grades_list,
            "exams": exams_list,
            "selected_alt": saved_alt
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule/select")
async def save_choice(req: ScheduleSelectRequest, background_tasks: BackgroundTasks):
    try:
        # selection_mark: 2 = selected, 1 = shown in the same session but not selected
        supabase.table('generated_schedules').update({"is_selected": False, "selection_mark": 1}).eq("session_id", req.session_id).execute()
        res2 = supabase.table('generated_schedules').update({"is_selected": True, "selection_mark": 2}).eq("session_id", req.session_id).eq("alt_key", req.alt_key).execute()

        if res2.data:
            # Every user choice is training data; models retrain once enough new choices accumulate.
            background_tasks.add_task(_run_ml_retrain)
            return {"success": True, "message": "המערכת נשמרה בהצלחה"}
        else:
            return {"success": False, "error": "לא נמצאה שורה כזו במסד הנתונים"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.put("/api/history/session/{session_id}/rename")
async def rename_session(session_id: int, req: RenameRequest):
    try:
        supabase.table('chat_sessions').update({"title": req.title}).eq('session_id', session_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/history/session/{session_id}/pin")
async def pin_session(session_id: int, req: PinRequest):
    try:
        supabase.table('chat_sessions').update({"is_pinned": req.is_pinned}).eq('session_id', session_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/history/session/{session_id}")
async def delete_session(session_id: int):
    try:
        schedules_res = supabase.table('generated_schedules').select('schedule_id').eq('session_id', session_id).execute()
        if schedules_res.data:
            sched_ids = [s['schedule_id'] for s in schedules_res.data]
            supabase.table('schedule_classes').delete().in_('schedule_id', sched_ids).execute()

        supabase.table('generated_schedules').delete().eq('session_id', session_id).execute()
        supabase.table('chat_messages').delete().eq('session_id', session_id).execute()
        supabase.table('studentconstraint').delete().eq('session_id', session_id).execute()
        try:
            # Legacy table; missing in the current DB, so it must not block deleting the session.
            supabase.table('coursepreference').delete().eq('session_id', session_id).execute()
        except Exception as e:
            _log_decision("coursepreference cleanup skipped", session_id=session_id, error=str(e))
        supabase.table('chat_sessions').delete().eq('session_id', session_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    #champer