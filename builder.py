import json
import re

class BuilderCourse:
    def __init__(self, course_name, team_id, day, start_time, end_time, raw_data):
        self.course_name = course_name
        self.team_id = team_id
        self.day = day
        self.start_time = start_time[:5] if isinstance(start_time, str) else start_time
        self.end_time = end_time[:5] if isinstance(end_time, str) else end_time
        self.raw_data = raw_data

class ScheduleBuilder:
    def __init__(self):
        self.days_map_rev = {1: "ראשון", 2: "שני", 3: "שלישי", 4: "רביעי", 5: "חמישי", 6: "שישי"}

    def _log_decision(self, event: str, **details):
        print(f"[ScheduleBuilder] {event}: {json.dumps(details, ensure_ascii=False, default=str)}")

    def _session_summary(self, session):
        return {
            "day": session.get("day_of_week"),
            "start": session.get("start_time"),
            "end": session.get("end_time"),
            "type": session.get("session_type"),
            "lecturer": session.get("instructor_name"),
        }

    def _class_summary(self, c):
        return {
            "course_id": c.course_name,
            "team_id": c.team_id,
            "day": c.day,
            "start": c.start_time,
            "end": c.end_time,
            "type": c.raw_data.get("session_type", ""),
        }
    def _time_to_minutes(self, t):
        """המרת זמן לדקות בצורה סופר-עמידה לטקסטים וטעויות"""
        if t is None or str(t).strip() == "":
            return 0

        if isinstance(t, str):
            t_clean = t.lower().replace("pm", "").replace("am", "").strip()
            if ':' in t_clean:
                try:
                    parts = t_clean[:5].split(':')
                    return int(parts[0]) * 60 + int(parts[1])
                except:
                    return 0
            else:
                try:
                    import re
                    match = re.search(r'\d+\.?\d*', t_clean)
                    if match:
                        t = float(match.group())
                    else:
                        return 0
                except:
                    return 0

        if isinstance(t, (int, float)):
            value = float(t)
            if value < 24:
                return int(round(value * 60))
            if 100 <= value <= 2359:
                hours = int(value // 100)
                minutes = int(value % 100)
                if 0 <= hours <= 23 and 0 <= minutes < 60:
                    return hours * 60 + minutes
            return int(round(value))
        return 0

    def _format_time(self, t):
        minutes = self._time_to_minutes(t)
        hours = max(0, minutes) // 60
        mins = max(0, minutes) % 60
        return f"{hours:02d}:{mins:02d}"

    def _normalize_session_type(self, raw_type):
        raw = str(raw_type or "").lower().strip()
        if any(token in raw for token in ["tutorial", "תרגול", "exercise", "lab", "מעבדה"]):
            return "tutorial"
        if any(token in raw for token in ["lecture", "הרצאה"]):
            return "lecture"
        return "lecture"

    def _session_type_matches(self, raw_type, requested_type):
        if not requested_type:
            return True
        return self._normalize_session_type(raw_type) == requested_type

    def _normalize_text(self, value):
        text = str(value or "").lower()
        text = re.sub(r"ד[\"׳']?ר|פרופ[׳']?", " ", text)
        text = re.sub(r"[^0-9a-zא-ת]+", " ", text)
        return " ".join(text.replace("-", " ").split())

    def _compact_text(self, value):
        normalized = self._normalize_text(value)
        compact = normalized.replace(" ", "")
        # Names sometimes appear as פבל/פאבל. Keep exact matching first,
        # then use this compact variant for common Hebrew spelling gaps.
        return compact.replace("א", "")

    def _lecturer_matches(self, actual_name, requested_name):
        actual = self._normalize_text(actual_name)
        requested = self._normalize_text(requested_name)
        if not actual or not requested:
            return False
        if requested in actual or actual in requested:
            return True

        actual_compact = self._compact_text(actual_name)
        requested_compact = self._compact_text(requested_name)
        return bool(
            actual_compact
            and requested_compact
            and (requested_compact in actual_compact or actual_compact in requested_compact)
        )

    def _extract_generic_constraints(self, builder_config, constraint_type):
        return [
            constraint.get("payload", {})
            for constraint in builder_config.get("hard_constraints", [])
            if constraint.get("type") == constraint_type
        ]

    # --- פונקציית העזר החדשה שמזהה ימים בצורה גמישה מכל פורמט ---
    def _parse_day(self, raw_day):
        if not raw_day:
            return -1
        d = str(raw_day).lower().strip()
        if "ראשון" in d or "sun" in d or d == "1" or "א" in d: return 1
        if "שני" in d or "mon" in d or d == "2" or "ב" in d: return 2
        if "שלישי" in d or "tue" in d or d == "3" or "ג" in d: return 3
        if "רביעי" in d or "wed" in d or d == "4" or "ד" in d: return 4
        if "חמישי" in d or "thu" in d or d == "5" or "ה" in d: return 5
        if "שישי" in d or "fri" in d or d == "6" or "ו" in d: return 6
        return -1

    def _print_schedules(self, schedules, course_metadata, title="תוצאות הבילדר"):
        SEP = "─" * 60
        print(f"\n{'═' * 60}")
        print(f"  📋 {title}")
        print(f"  סה״כ מערכות שנבנו: {len(schedules)}")
        print(f"{'═' * 60}")

        for i, sched in enumerate(schedules, 1):
            print(f"\n  מערכת #{i}")
            print(f"  {SEP}")

            by_day = {}
            for c in sched:
                by_day.setdefault(c.day, []).append(c)

            for day_num in sorted(by_day.keys()):
                day_name = self.days_map_rev.get(day_num, str(day_num))
                print(f"\n  📅 יום {day_name}:")
                day_classes = sorted(by_day[day_num], key=lambda c: self._time_to_minutes(c.start_time))
                for c in day_classes:
                    c_id   = int(c.course_name)
                    c_name = course_metadata.get(c_id, {}).get("course_name", str(c_id))
                    s_type = c.raw_data.get("session_type", "")
                    print(f"     {c.start_time}–{c.end_time}  |  {c_name}  [{s_type}]  (קבוצה {c.team_id})")

            print(f"  {SEP}")

        print(f"{'═' * 60}\n")


    def _convert_to_builder_class(self, course_id, team_id, session):
        day_num = self._parse_day(session.get('day_of_week'))
        if day_num == -1:
            day_num = 1 # Fallback אם לחלוטין לא זוהה

        return BuilderCourse(
            course_name=str(course_id),
            team_id=str(team_id),
            day=day_num,
            start_time=self._format_time(session.get('start_time', '00:00')),
            end_time=self._format_time(session.get('end_time', '00:00')),
            raw_data=session
        )

    def _is_overlap(self, s1, s2):
        if s1.day != s2.day: return False
        s1_start = self._time_to_minutes(s1.start_time)
        s1_end = self._time_to_minutes(s1.end_time)
        s2_start = self._time_to_minutes(s2.start_time)
        s2_end = self._time_to_minutes(s2.end_time)
        return (s1_start < s2_end) and (s1_end > s2_start)

    def _find_conflict(self, new_sessions, current_schedule):
        for ns in new_sessions:
            for es in current_schedule:
                if self._is_overlap(ns, es):
                    return ns, es
        return None

    def _has_conflict(self, new_sessions, current_schedule):
        return self._find_conflict(new_sessions, current_schedule) is not None

    def _apply_hard_constraints(self, organized_by_team, builder_config, course_metadata):
        time_prefs = dict(builder_config.get("course_time_pref", {}))
        # תיקון: chat.py שולח אילוץ קורס בשם course_schedule_pref.
        # בלי השורה הזו, ה-Builder לא אוכף בפועל אילוצי יום/שעה לקורס ספציפי.
        time_prefs.update(builder_config.get("course_schedule_pref", {}))
        day_prefs  = dict(builder_config.get("course_on_day", {}))
        session_type_prefs = dict(builder_config.get("course_session_type", {}))
        unwanted_days = list(builder_config.get("unwanted_days", []))
        global_time = dict(builder_config.get("global_time_range", {}))
        specific_day_time = {
            key: dict(value)
            for key, value in builder_config.get("specific_day_time_range", {}).items()
        }
        avoid_lecturers = [
            name for name in builder_config.get("avoid_lecturers", [])
            if str(name or "").strip()
        ]
        hard_constraints = builder_config.get("hard_constraints", [])

        for payload in self._extract_generic_constraints(builder_config, "day_off"):
            day_num = self._parse_day(payload.get("day"))
            if day_num != -1 and day_num not in unwanted_days:
                unwanted_days.append(day_num)

        for payload in self._extract_generic_constraints(builder_config, "time_range"):
            time_data = {
                "from": payload.get("from"),
                "until": payload.get("until"),
            }
            day_num = self._parse_day(payload.get("day"))
            if day_num != -1:
                specific_day_time[day_num] = time_data
            else:
                global_time.update({k: v for k, v in time_data.items() if v is not None})

        for payload in self._extract_generic_constraints(builder_config, "lecturer_constraint"):
            action = str(payload.get("action") or "avoid").lower().strip()
            lecturer_name = str(payload.get("lecturer_name") or "").strip()
            if action == "avoid" and lecturer_name and lecturer_name not in avoid_lecturers:
                avoid_lecturers.append(lecturer_name)

        for payload in (
            self._extract_generic_constraints(builder_config, "course_time_pref")
            + self._extract_generic_constraints(builder_config, "course_schedule_pref")
        ):
            course_id = payload.get("course_id")
            if course_id is None:
                continue
            try:
                course_id = int(course_id)
            except (TypeError, ValueError):
                continue

            entry = {}
            if payload.get("from") is not None:
                entry["from"] = payload.get("from")
            if payload.get("until") is not None:
                entry["until"] = payload.get("until")
            if payload.get("exact_start_time") is not None:
                entry["exact_start_time"] = payload.get("exact_start_time")
            if entry:
                time_prefs[course_id] = entry

            day_num = self._parse_day(payload.get("day"))
            if day_num != -1:
                day_prefs[course_id] = day_num

            session_type = payload.get("session_type")
            if session_type:
                session_type_prefs[course_id] = session_type

        for payload in self._extract_generic_constraints(builder_config, "course_on_day"):
            course_id = payload.get("course_id")
            if course_id is None:
                continue
            try:
                course_id = int(course_id)
            except (TypeError, ValueError):
                continue

            day_num = self._parse_day(payload.get("day"))
            if day_num != -1:
                day_prefs[course_id] = day_num

            entry = {}
            if payload.get("from") is not None:
                entry["from"] = payload.get("from")
            if payload.get("until") is not None:
                entry["until"] = payload.get("until")
            if payload.get("exact_start_time") is not None:
                entry["exact_start_time"] = payload.get("exact_start_time")
            if entry:
                time_prefs[course_id] = entry

            session_type = payload.get("session_type")
            if session_type:
                session_type_prefs[course_id] = session_type

        self._log_decision(
            "Hard constraint filtering started",
            course_count=len(organized_by_team),
            team_count=sum(len(teams) for teams in organized_by_team.values()),
            hard_constraint_count=len(hard_constraints),
            hard_constraints=hard_constraints,
            course_time_preferences=time_prefs,
            course_day_preferences=day_prefs,
            course_session_types=session_type_prefs,
            unwanted_days=unwanted_days,
            global_time_range=global_time,
            specific_day_time_range=specific_day_time,
            avoid_lecturers=avoid_lecturers,
        )

        print("\n" + "═"*50)
        print("🔍 מתחיל סינון קורסים לפי אילוצי מערכת (Hard Constraints)")
        print(f"   ימים חסומים: {unwanted_days}")
        print(f"   שעות גלובליות: {global_time}")
        print(f"   שעות ליום ספציפי: {specific_day_time}")
        print(f"   מרצים חסומים: {avoid_lecturers}")
        print("═"*50)

        # תיקון קריטי: הוספנו את specific_day_time לתנאי כדי שלא ידלג על בדיקת השעות!
        if not time_prefs and not day_prefs and not session_type_prefs and not unwanted_days and not global_time and not specific_day_time and not avoid_lecturers:
            self._log_decision("No builder hard constraints detected; all course teams remain available")
            print("   אין אילוצים קשיחים - כל הקורסים עוברים אוטומטית.\n")
            return organized_by_team

        filtered = {}
        constraint_warnings = builder_config.setdefault("constraint_warnings", [])

        for course_id, teams in organized_by_team.items():
            t_pref = time_prefs.get(course_id)
            d_pref = day_prefs.get(course_id)
            session_type_pref = session_type_prefs.get(course_id)
            c_name = course_metadata.get(course_id, {}).get("course_name", str(course_id))
            filtered_teams = {}
            for team_id, sessions in teams.items():

                is_valid_global = True
                team_reject_reason = None
                for s in sessions:
                    day_num = self._parse_day(s.get("day_of_week"))
                    # סגירת הפרצה: אם היום לא זוהה, נניח שזה ראשון כדי שהאילוצים יחולו עליו!
                    if day_num == -1:
                        self._log_decision(
                            "Session day could not be parsed; falling back to Sunday",
                            course_id=course_id,
                            course_name=c_name,
                            team_id=team_id,
                            raw_day=s.get("day_of_week"),
                            session=self._session_summary(s),
                        )
                        day_num = 1

                    if day_num in unwanted_days:
                        is_valid_global = False
                        team_reject_reason = {
                            "code": "unwanted_day",
                            "detail": f"Session is on blocked day {day_num}",
                            "day": day_num,
                            "session": self._session_summary(s),
                        }
                        break

                    instructor_name = s.get("instructor_name", "")
                    blocked_lecturer = next(
                        (
                            lecturer_name for lecturer_name in avoid_lecturers
                            if self._lecturer_matches(instructor_name, lecturer_name)
                        ),
                        None
                    )
                    if blocked_lecturer:
                        is_valid_global = False
                        team_reject_reason = {
                            "code": "blocked_lecturer",
                            "detail": f"Session lecturer {instructor_name} matches avoided lecturer {blocked_lecturer}",
                            "lecturer": instructor_name,
                            "blocked_lecturer": blocked_lecturer,
                            "session": self._session_summary(s),
                        }
                        break

                    start = self._format_time(s.get("start_time", "00:00"))
                    end   = self._format_time(s.get("end_time", "00:00"))

                    # בדיקת שעות גלובליות לכל השבוע
                    t_global_from = global_time.get("from")
                    v_g_from = self._time_to_minutes(t_global_from)
                    if t_global_from is not None and v_g_from > 0 and self._time_to_minutes(start) < v_g_from:
                        is_valid_global = False
                        team_reject_reason = {
                            "code": "global_start_before_allowed",
                            "detail": f"Session starts at {start}, before global from={t_global_from}",
                            "session": self._session_summary(s),
                            "required_from": t_global_from,
                        }
                        break

                    t_global_until = global_time.get("until")
                    v_g_until = self._time_to_minutes(t_global_until)
                    if t_global_until is not None and v_g_until > 0 and self._time_to_minutes(end) > v_g_until:
                        is_valid_global = False
                        team_reject_reason = {
                            "code": "global_end_after_allowed",
                            "detail": f"Session ends at {end}, after global until={t_global_until}",
                            "session": self._session_summary(s),
                            "required_until": t_global_until,
                        }
                        break

                    # בדיקת שעות קשיחה ליום ספציפי
                    if day_num in specific_day_time:
                        d_time = specific_day_time[day_num]

                        t_day_from = d_time.get("from")
                        v_from = self._time_to_minutes(t_day_from)
                        if t_day_from is not None and v_from > 0 and self._time_to_minutes(start) < v_from:
                            is_valid_global = False
                            team_reject_reason = {
                                "code": "specific_day_start_before_allowed",
                                "detail": f"Session starts at {start}, before day {day_num} from={t_day_from}",
                                "day": day_num,
                                "session": self._session_summary(s),
                                "required_from": t_day_from,
                            }
                            break

                        t_day_until = d_time.get("until")
                        v_until = self._time_to_minutes(t_day_until)
                        if t_day_until is not None and v_until > 0 and self._time_to_minutes(end) > v_until:
                            is_valid_global = False
                            team_reject_reason = {
                                "code": "specific_day_end_after_allowed",
                                "detail": f"Session ends at {end}, after day {day_num} until={t_day_until}",
                                "day": day_num,
                                "session": self._session_summary(s),
                                "required_until": t_day_until,
                            }
                            break

                if not is_valid_global:
                    self._log_decision(
                        "Team rejected by builder hard constraint",
                        course_id=course_id,
                        course_name=c_name,
                        team_id=team_id,
                        reason=team_reject_reason,
                        sessions=[self._session_summary(s) for s in sessions],
                    )
                    continue

                if d_pref is None and not t_pref and not session_type_pref:
                    filtered_teams[team_id] = sessions
                    continue

                all_sessions_valid = True
                matching_session_seen = False

                if d_pref is not None or t_pref:
                    for s in sessions:
                        if not self._session_type_matches(s.get("session_type"), session_type_pref):
                            continue

                        matching_session_seen = True
                        day_num = self._parse_day(s.get("day_of_week"))
                        if day_num == -1: day_num = 1
                        start = self._format_time(s.get("start_time", "00:00"))
                        end   = self._format_time(s.get("end_time", "00:00"))

                        session_ok = True

                        if d_pref is not None:
                            if day_num != d_pref:
                                session_ok = False
                                team_reject_reason = {
                                    "code": "course_day_preference_mismatch",
                                    "detail": f"Session day {day_num} does not match required day {d_pref}",
                                    "required_day": d_pref,
                                    "actual_day": day_num,
                                    "session": self._session_summary(s),
                                }

                        if session_ok and t_pref:
                            if t_pref.get("from") and self._time_to_minutes(start) < self._time_to_minutes(t_pref["from"]):
                                session_ok = False
                                team_reject_reason = {
                                    "code": "course_time_preference_start_before_allowed",
                                    "detail": f"Session starts at {start}, before required from={t_pref['from']}",
                                    "required_from": t_pref.get("from"),
                                    "session": self._session_summary(s),
                                }
                            if t_pref.get("until") and self._time_to_minutes(end) > self._time_to_minutes(t_pref["until"]):
                                session_ok = False
                                team_reject_reason = {
                                    "code": "course_time_preference_end_after_allowed",
                                    "detail": f"Session ends at {end}, after required until={t_pref['until']}",
                                    "required_until": t_pref.get("until"),
                                    "session": self._session_summary(s),
                                }

                            if session_ok and t_pref.get("exact_start_time"):
                                required_exact = self._format_time(t_pref.get("exact_start_time"))
                                if start != required_exact:
                                    session_ok = False
                                    team_reject_reason = {
                                        "code": "course_exact_time_mismatch",
                                        "detail": f"Session starts at {start}, but required exact_start_time={required_exact}",
                                        "required_exact_start_time": required_exact,
                                        "session": self._session_summary(s),
                                    }

                        if not session_ok:
                            all_sessions_valid = False
                            break

                    if session_type_pref and not matching_session_seen:
                        all_sessions_valid = False
                        team_reject_reason = {
                            "code": "no_matching_session_type",
                            "detail": f"No session matched requested session type {session_type_pref}",
                            "requested_session_type": session_type_pref,
                        }

                if all_sessions_valid:
                    filtered_teams[team_id] = sessions
                else:
                    self._log_decision(
                        "Team rejected by course-specific preference",
                        course_id=course_id,
                        course_name=c_name,
                        team_id=team_id,
                        reason=team_reject_reason,
                        sessions=[self._session_summary(s) for s in sessions],
                    )
            if not filtered_teams and teams:
                if avoid_lecturers:
                    msg = f"שים לב: האילוץ להימנע מהמרצים {', '.join(avoid_lecturers)} הוריד את כל הקבוצות האפשריות של קורס '{c_name}', ולכן הקורס לא ישובץ במערכת."
                else:
                    msg = f"שים לב: האילוצים שהגדרת הורידו את כל הקבוצות האפשריות של קורס '{c_name}'. כי אתה דרשת לא לקחת קורס בימים/שעות מסוימות, ובכל הקבוצות - הן הרצאה והן תרגול קיימות בחלק מהימים שאתה לא יכול להגיע, לכן הקורס לא ישובץ במערכת."
                self._log_decision(
                    "Course rejected completely by builder hard constraints",
                    course_id=course_id,
                    course_name=c_name,
                    original_team_count=len(teams),
                )
                print(f"  ❌ נפסל לחלוטין: קורס '{c_name}' (קוד {course_id}) - אף קבוצה לא עמדה באילוצים.")
                constraint_warnings.append(msg)

            if filtered_teams:
                self._log_decision(
                    "Course passed builder hard constraints",
                    course_id=course_id,
                    course_name=c_name,
                    remaining_team_count=len(filtered_teams),
                    original_team_count=len(teams),
                )
                print(f"  ✅ עבר סינון: קורס '{c_name}' (קוד {course_id}) | נותרו {len(filtered_teams)} קבוצות מתוך {len(teams)}")
                filtered[course_id] = filtered_teams

        print("═"*50 + "\n")
        self._log_decision(
            "Hard constraint filtering completed",
            remaining_course_count=len(filtered),
            remaining_team_count=sum(len(teams) for teams in filtered.values()),
            rejected_course_count=len(organized_by_team) - len(filtered),
        )
        return filtered

    def _coerce_positive_number(self, value):
        """Normalize numeric constraints that may arrive as numbers, strings or HH:MM-like values."""
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().lower().replace(",", ".")
        try:
            import re
            match = re.search(r"\d+(?:\.\d+)?", text)
            return float(match.group()) if match else None
        except Exception:
            return None

    def _validate_schedule_constraints(self, schedule: list, builder_config: dict) -> bool:
        """
        Final hard validation for constraints that depend on the complete schedule:
        - max_study_days
        - max_daily_hours

        Note:
        windows_minimization is intentionally not enforced here as a hard constraint.
        It is handled later by the optimizer ranking logic, so schedules with windows
        can still be returned when no better alternative exists.
        """
        max_study_days = self._coerce_positive_number(builder_config.get("max_study_days"))
        max_daily_hours = self._coerce_positive_number(builder_config.get("max_daily_hours"))
        max_consecutive_hours = self._coerce_positive_number(builder_config.get("max_consecutive_hours"))

        for payload in self._extract_generic_constraints(builder_config, "daily_load"):
            value = self._coerce_positive_number(payload.get("max_hours"))
            if value is not None:
                max_daily_hours = value

        for payload in self._extract_generic_constraints(builder_config, "study_days_limit"):
            value = self._coerce_positive_number(payload.get("max_days"))
            if value is not None:
                max_study_days = value

        for payload in self._extract_generic_constraints(builder_config, "max_consecutive_hours"):
            value = self._coerce_positive_number(payload.get("max_hours"))
            if value is not None:
                max_consecutive_hours = value

        if max_study_days is None and max_daily_hours is None and max_consecutive_hours is None:
            return True

        days_dict = {}
        for c in schedule:
            start = self._time_to_minutes(c.start_time)
            end = self._time_to_minutes(c.end_time)
            days_dict.setdefault(c.day, []).append({"start": start, "end": end})

        for sessions in days_dict.values():
            sessions.sort(key=lambda x: x["start"])

        if max_study_days is not None and len(days_dict) > int(max_study_days):
            return False

        for sessions in days_dict.values():
            if max_daily_hours is not None:
                total_minutes = sum(s["end"] - s["start"] for s in sessions)
                if total_minutes > max_daily_hours * 60:
                    return False

            if max_consecutive_hours is not None and sessions:
                block_start = sessions[0]["start"]
                block_end = sessions[0]["end"]

                for i in range(1, len(sessions)):
                    gap = sessions[i]["start"] - block_end

                    # הפסקה של פחות משעה נחשבת רצף לימודים אחד
                    if gap < 60:
                        block_end = max(block_end, sessions[i]["end"])
                    else:
                        if (block_end - block_start) > max_consecutive_hours * 60:
                            return False
                        block_start = sessions[i]["start"]
                        block_end = sessions[i]["end"]

                if (block_end - block_start) > max_consecutive_hours * 60:
                    return False

        return True

    def build_all_valid_schedules(self, builder_config, organized_by_team, course_metadata=None):
        if course_metadata is None:
            course_metadata = {}

        initial_course_count = len(organized_by_team)
        initial_team_count = sum(len(teams) for teams in organized_by_team.values())
        self._log_decision(
            "Build started",
            initial_course_count=initial_course_count,
            initial_team_count=initial_team_count,
            mandatory_courses=builder_config.get("mandatory", []),
            elective_courses=builder_config.get("electives", []),
            removed_courses=builder_config.get("removed_courses", []),
        )

        organized_by_team = self._apply_hard_constraints(organized_by_team, builder_config, course_metadata)

        print(f"📊 סיכום סל קורסים לבנייה: {len(organized_by_team)} קורסים שרדו את הפילטור.")
        self._log_decision(
            "Course pool after hard constraints",
            initial_course_count=initial_course_count,
            remaining_course_count=len(organized_by_team),
            remaining_team_count=sum(len(teams) for teams in organized_by_team.values()),
        )

        min_schedule_courses = int(builder_config.get("min_schedule_courses", 3) or 3)

        if len(organized_by_team) < min_schedule_courses:
            print(f"⚠️ הופעלה עצירת חירום: נותרו פחות מ-{min_schedule_courses} קורסים חוקיים.")
            warnings = list(builder_config.get("constraint_warnings", []))
            warnings.append(
                f"שמתי לב שלאחר סינון הקורסים לפי האילוצים שהגדרת, נותרו פחות מ-{min_schedule_courses} "
                "קורסים אפשריים במערכת. כדי לפתור זאת, אפשר לשנות אילוצים או להוסיף קורסים אחרים."
            )
            self._log_decision(
                "Build stopped: insufficient courses after hard filtering",
                remaining_course_count=len(organized_by_team),
                minimum_required=min_schedule_courses,
                warnings=warnings,
            )
            return [], "insufficient_courses", warnings

        mandatory_ids = list(set(builder_config.get("mandatory", [])))
        backups       = builder_config.get("backups", {})
        electives     = list(set(builder_config.get("electives", [])))

        allow_course_drop = bool(builder_config.get("allow_course_drop", False))
        try:
            max_course_drops = int(builder_config.get("max_course_drops", 0) or 0)
        except (TypeError, ValueError):
            max_course_drops = 0

        max_course_drops = max(0, min(max_course_drops, 3))

        def get_valid_courses(course_list):
            return [c for c in course_list if c in organized_by_team and organized_by_team[c]]

        base_courses    = get_valid_courses(mandatory_ids)
        valid_electives = get_valid_courses(electives)
        missing_mandatory = [c for c in mandatory_ids if c not in base_courses]
        missing_electives = [c for c in electives if c not in valid_electives]
        self._log_decision(
            "Build course sets resolved",
            mandatory_requested=mandatory_ids,
            mandatory_available=base_courses,
            mandatory_missing=missing_mandatory,
            electives_requested=electives,
            electives_available=valid_electives,
            electives_missing=missing_electives,
        )
        def schedule_signature(sched):
            return "|".join(
                f"{c.course_name}-D{c.day}-{c.start_time}-{c.end_time}"
                for c in sorted(sched, key=lambda c: (c.course_name, c.day, c.start_time, c.end_time))
            )

        def schedule_course_count(sched):
            return len({int(c.course_name) for c in sched})

        base_course_set = set(base_courses)
        requested_course_set = set(base_courses) | set(valid_electives)

        def schedule_base_course_count(sched):
            return len({int(c.course_name) for c in sched if int(c.course_name) in base_course_set})

        def schedule_requested_course_count(sched):
            return len({int(c.course_name) for c in sched if int(c.course_name) in requested_course_set})

        def missing_requested_courses(sched):
            scheduled_ids = {int(c.course_name) for c in sched}
            return sorted(list(requested_course_set - scheduled_ids))

        def sort_schedule_candidates(schedules):
            return sorted(
                schedules,
                key=lambda sched: (-schedule_course_count(sched), -schedule_base_course_count(sched), schedule_signature(sched)),
            )

        def get_schedules(mandatory_subset, elective_subset, phase="primary"):
            constrained = (
                set(builder_config.get("course_time_pref", {}).keys())
                | set(builder_config.get("course_schedule_pref", {}).keys())
                | set(builder_config.get("course_on_day", {}).keys())
            )

            if constrained:
                def sort_key(c_id):
                    return (c_id not in constrained, len(organized_by_team[c_id]))
                sorted_mandatory = sorted(mandatory_subset, key=sort_key)
            else:
                sorted_mandatory = sorted(mandatory_subset, key=lambda c: len(organized_by_team[c]))

            sorted_electives = sorted(elective_subset, key=lambda c: len(organized_by_team[c]))

            all_courses   = sorted_mandatory + sorted_electives
            mandatory_set = set(mandatory_subset)
            results       = []
            search_stats = {
                "phase": phase,
                "courses_to_place": all_courses,
                "team_options_checked": 0,
                "conflict_rejections": 0,
                "conflict_samples": [],
            }

            self._log_decision(
                "Schedule search started",
                phase=phase,
                mandatory_courses=sorted_mandatory,
                elective_courses=sorted_electives,
                total_courses=len(all_courses),
            )

            def dfs(idx, current, electives_added):
                if idx == len(all_courses):
                    if self._validate_schedule_constraints(current, builder_config):
                        results.append((current[:], electives_added))
                    return

                c_id        = all_courses[idx]
                is_elective = c_id not in mandatory_set

                added_any = False
                for t_id, sessions in organized_by_team[c_id].items():
                    search_stats["team_options_checked"] += 1
                    objs = [self._convert_to_builder_class(c_id, t_id, s) for s in sessions]
                    conflict = self._find_conflict(objs, current)
                    if not conflict:
                        added_any = True
                        dfs(idx + 1, current + objs, electives_added + (1 if is_elective else 0))
                    else:
                        search_stats["conflict_rejections"] += 1
                        if len(search_stats["conflict_samples"]) < 10:
                            new_session, existing_session = conflict
                            search_stats["conflict_samples"].append({
                                "course_id": c_id,
                                "team_id": t_id,
                                "new_session": self._class_summary(new_session),
                                "conflicts_with": self._class_summary(existing_session),
                            })

                if is_elective:
                    dfs(idx + 1, current, electives_added)

            dfs(0, [], 0)

            if not results:
                self._log_decision("Schedule search completed with no combinations", **search_stats)
                return [], 0

            max_electives = max(count for _, count in results)
            results_sorted = sorted(results, key=lambda x: -x[1])
            self._log_decision(
                "Schedule search completed",
                phase=phase,
                result_count=len(results_sorted),
                max_electives=max_electives,
                team_options_checked=search_stats["team_options_checked"],
                conflict_rejections=search_stats["conflict_rejections"],
                conflict_samples=search_stats["conflict_samples"],
            )
            return sort_schedule_candidates([sched for sched, _ in results_sorted]), max_electives

        def get_partial_schedules(course_pool, phase="partial"):
            sorted_courses = sorted(set(course_pool), key=lambda c: len(organized_by_team[c]))
            results = []
            seen_signatures = set()
            search_stats = {
                "phase": phase,
                "courses_to_consider": sorted_courses,
                "minimum_courses": min_schedule_courses,
                "team_options_checked": 0,
                "conflict_rejections": 0,
                "pruned_too_few_remaining": 0,
                "conflict_samples": [],
            }

            self._log_decision(
                "Partial schedule search started",
                phase=phase,
                candidate_courses=sorted_courses,
                minimum_courses=min_schedule_courses,
            )

            def dfs(idx, current, selected_count):
                remaining = len(sorted_courses) - idx
                if selected_count + remaining < min_schedule_courses:
                    search_stats["pruned_too_few_remaining"] += 1
                    return

                if idx == len(sorted_courses):
                    if selected_count >= min_schedule_courses and self._validate_schedule_constraints(current, builder_config):
                        sig = schedule_signature(current)
                        if sig not in seen_signatures:
                            seen_signatures.add(sig)
                            results.append(current[:])
                    return

                c_id = sorted_courses[idx]
                for t_id, sessions in organized_by_team[c_id].items():
                    search_stats["team_options_checked"] += 1
                    objs = [self._convert_to_builder_class(c_id, t_id, s) for s in sessions]
                    conflict = self._find_conflict(objs, current)
                    if not conflict:
                        dfs(idx + 1, current + objs, selected_count + 1)
                    else:
                        search_stats["conflict_rejections"] += 1
                        if len(search_stats["conflict_samples"]) < 10:
                            new_session, existing_session = conflict
                            search_stats["conflict_samples"].append({
                                "course_id": c_id,
                                "team_id": t_id,
                                "new_session": self._class_summary(new_session),
                                "conflicts_with": self._class_summary(existing_session),
                            })

                dfs(idx + 1, current, selected_count)

            dfs(0, [], 0)
            sorted_results = sort_schedule_candidates(results)
            best_course_count = schedule_course_count(sorted_results[0]) if sorted_results else 0

            self._log_decision(
                "Partial schedule search completed",
                phase=phase,
                result_count=len(sorted_results),
                best_course_count=best_course_count,
                team_options_checked=search_stats["team_options_checked"],
                conflict_rejections=search_stats["conflict_rejections"],
                pruned_too_few_remaining=search_stats["pruned_too_few_remaining"],
                conflict_samples=search_stats["conflict_samples"],
            )

            return sorted_results, best_course_count

        # --- שלב א' ---
        schedules, electives_added = get_schedules(base_courses, valid_electives, phase="primary")
        status = "full_mandatory"

        if schedules:
            self._log_decision("Primary schedule search succeeded", schedule_count=len(schedules), electives_added=electives_added)
            self._print_schedules(schedules, course_metadata, f"שלב א׳ — חובה בלבד ({len(schedules)} מערכות)")
        else:
            self._log_decision("Primary schedule search failed; checking backups", backup_count=len(backups))

        # --- שלב ב': גיבויים ---
        if not schedules and backups:
            self._log_decision("Backup search started", backups=backups)
            print("⚠️  לא נמצאה מערכת עם קורסי החובה — מנסה גיבויים...")
            for orig_id, backup_id in backups.items():
                if orig_id in base_courses and backup_id in organized_by_team:
                    alt_courses = [backup_id if c == orig_id else c for c in base_courses]
                    schedules, electives_added = get_schedules(alt_courses, valid_electives, phase=f"backup_for_{orig_id}")
                    if schedules:
                        status = f"used_backup_for_{orig_id}"
                        orig_name   = course_metadata.get(orig_id,   {}).get("course_name", str(orig_id))
                        backup_name = course_metadata.get(backup_id, {}).get("course_name", str(backup_id))
                        self._log_decision(
                            "Backup schedule search succeeded",
                            original_course_id=orig_id,
                            backup_course_id=backup_id,
                            schedule_count=len(schedules),
                            electives_added=electives_added,
                        )
                        print(f"✅  שובץ גיבוי '{backup_name}' במקום '{orig_name}'")
                        self._print_schedules(schedules, course_metadata, f"שלב ב׳ — עם גיבוי ({len(schedules)} מערכות)")
                        break
                else:
                    self._log_decision(
                        "Backup option skipped",
                        original_course_id=orig_id,
                        backup_course_id=backup_id,
                        original_in_base=orig_id in base_courses,
                        backup_available=backup_id in organized_by_team,
                    )

        # --- שלב ג': Fallback ---
        if not schedules:
            self._log_decision(
                "Fallback search started: building partial minimum viable schedule",
                base_course_count=len(base_courses),
                elective_course_count=len(valid_electives),
                minimum_courses=min_schedule_courses,
            )
            print("⚠️  גיבויים לא עזרו — מנסה לבנות מערכת חלקית עם קורסים שעומדים באילוצים...")
            candidate_courses = list(dict.fromkeys(base_courses + valid_electives))
            schedules, best_course_count = get_partial_schedules(candidate_courses, phase="partial_minimum_viable")
            electives_added = max(
                (
                    len({int(c.course_name) for c in sched if int(c.course_name) in set(valid_electives)})
                    for sched in schedules
                ),
                default=0,
            )
            status = f"partial_{best_course_count}_courses" if schedules else "failed"
            if schedules:
                self._log_decision(
                    "Fallback search succeeded",
                    schedule_count=len(schedules),
                    best_course_count=best_course_count,
                    electives_added=electives_added,
                )
                self._print_schedules(schedules, course_metadata, f"שלב ג׳ — מערכת חלקית תקינה ({len(schedules)} מערכות)")
                scheduled_course_ids = {int(c.course_name) for c in schedules[0]}
                dropped_courses = [c for c in base_courses if c not in scheduled_course_ids]

                if dropped_courses:
                    dropped_names = [
                        course_metadata.get(dropped_id, {}).get("course_name", str(dropped_id))
                        for dropped_id in dropped_courses
                    ]
                    warning_msg = f"שים לב: בגלל האילוצים שהגדרת אי אפשר היה לשבץ את כל הקורסים יחד. לכן הצגתי מערכת חלקית תקינה עם {best_course_count} קורסים, והקורסים שלא נכנסו לחלופה הראשונה הם: {', '.join(dropped_names)}."
                    builder_config.setdefault("constraint_warnings", []).append(warning_msg)

            else:
                self._log_decision(
                    "Fallback search failed: no legal schedule combinations",
                    base_courses=base_courses,
                    valid_electives=valid_electives,
                    minimum_courses=min_schedule_courses,
                )
                print("❌  לא נמצאה אף מערכת חוקית.")

        # --- שלב ג2: מערכות חלקיות לפי אישור המשתמש ---
        if allow_course_drop and schedules:
            self._log_decision(
                "Course drop flexibility started",
                current_schedule_count=len(schedules),
                max_course_drops=max_course_drops,
                base_courses=base_courses,
                valid_electives=valid_electives,
            )

            candidate_courses = list(dict.fromkeys(base_courses + valid_electives))
            partial_schedules, best_course_count = get_partial_schedules(
                candidate_courses,
                phase="user_allowed_course_drop"
            )

            if partial_schedules:
                full_requested_count = len(requested_course_set)
                min_required_courses = max(1, full_requested_count - max_course_drops)

                allowed_partial_schedules = [
                    sched for sched in partial_schedules
                    if schedule_requested_course_count(sched) >= min_required_courses
                    and schedule_requested_course_count(sched) < full_requested_count
                ]

                existing_signatures = {schedule_signature(sched) for sched in schedules}
                allowed_partial_schedules = [
                    sched for sched in allowed_partial_schedules
                    if schedule_signature(sched) not in existing_signatures
                ]

                if allowed_partial_schedules:
                    schedules = schedules + allowed_partial_schedules
                    status += f"_with_user_allowed_course_drop_{max_course_drops}"

                    self._log_decision(
                        "Course drop flexibility added partial schedules",
                        added_partial_count=len(allowed_partial_schedules),
                        total_schedule_count=len(schedules),
                        min_required_courses=min_required_courses,
                        max_course_drops=max_course_drops,
                        sample_missing_courses=[
                            missing_requested_courses(sched)
                            for sched in allowed_partial_schedules[:5]
                        ],
                    )

                    builder_config.setdefault("constraint_warnings", []).append(
                        f"בהתאם לבקשתך, בדקתי גם חלופות שבהן ניתן לוותר על עד {max_course_drops} קורסים כדי לשפר את המערכת. אם תוצג חלופה חלקית, חשוב לבדוק אילו קורסים לא שובצו."
                    )
                else:
                    self._log_decision(
                        "Course drop flexibility found no allowed partial schedules",
                        partial_count=len(partial_schedules),
                        min_required_courses=min_required_courses,
                        max_course_drops=max_course_drops,
                    )

        # --- שלב ד': הוספת בחירות ---
        if valid_electives and schedules:
            print(f"\n🎓 שיבוץ קורסי בחירה: {[course_metadata.get(e, {}).get('course_name', str(e)) for e in valid_electives]}")
            self._print_schedules(schedules, course_metadata, f"לאחר הוספת בחירות — {electives_added}/{len(valid_electives)} שובצו")

        # --- warnings ---
        warnings = list(builder_config.get("constraint_warnings", []))

        if valid_electives and electives_added < len(valid_electives):
            scheduled_elective_ids = set()
            for sched in schedules:
                for c in sched:
                    if int(c.course_name) in set(valid_electives):
                        scheduled_elective_ids.add(int(c.course_name))

            for e_id in valid_electives:
                if e_id not in scheduled_elective_ids:
                    e_name = course_metadata.get(e_id, {}).get("course_name", str(e_id))
                    warnings.append(f"לא הצלחתי לשבץ את קורס '{e_name}' מכיוון שכל הקבוצות שלו מתנגשות עם קורסי החובה שלך.")

        status += f"_with_{electives_added}_electives"
        self._log_decision(
            "Build completed",
            status=status,
            schedule_count=len(schedules),
            electives_added=electives_added,
            warning_count=len(warnings),
            warnings=warnings,
        )

        return schedules, status, warnings
