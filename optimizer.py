import math
import json
import re


DEFAULT_CRITERION_WEIGHTS = {
    "D": 0.370,   # Active study days
    "T": 0.230,   # Time preference
    "LD": 0.160,  # Daily load
    "LW": 0.100,  # Weekly balance
    "G": 0.070,   # Gap windows
    "C": 0.050,   # Course assessment load
    "A": 0.020,   # Mandatory attendance
}


class ScheduleOptimizer:
    def __init__(self, weights: dict = None):
        """
        Constraint weights for soft scoring.
        `weights` comes from the best ML run (ml_criterion_weights.is_best_run);
        DEFAULT_CRITERION_WEIGHTS is the fallback when no run exists yet.

        Hard constraints are handled before scoring:
          - unwanted_days
          - max_study_days, when explicitly set by user
          - time_from / time_until, when explicitly set by user
          - max_daily_hours, when explicitly set by user

        Soft scoring priority:
          1. D  - Active study days
          2. T  - Time preference
          3. LD - Daily load
          4. LW - Weekly balance
          5. G  - Gap windows
        """
        self.weights = DEFAULT_CRITERION_WEIGHTS.copy()
        if weights and set(weights) == set(DEFAULT_CRITERION_WEIGHTS):
            total = sum(float(v) for v in weights.values())
            if total > 0:
                self.weights = {k: float(v) / total for k, v in weights.items()}

        self.last_diagnostics = {}

        # Gap windows now start counting only when gap is longer than 1 hour.
        self.gap_threshold_hours = 1.0

    # =========================================================
    # Helpers
    # =========================================================

    def _log_decision(self, event: str, **details):
        print(f"[ScheduleOptimizer] {event}: {json.dumps(details, ensure_ascii=False, default=str)}")

    def _time_to_float(self, time_str: str) -> float:
        """Convert 'HH:MM' to a float, for example '09:30' becomes 9.5."""
        if not time_str or time_str == "-":
            return 0.0
        h, m = map(int, time_str.split(":"))
        return h + m / 60.0

    def _coerce_hour_value(self, value):
        """Normalize hour values that may arrive as 4, '4h', or '09:30'."""
        if value is None or value == "":
            return None

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip().lower().replace(",", ".")

        time_match = re.search(r"(\d{1,2}):(\d{2})", text)
        if time_match:
            hours = int(time_match.group(1))
            minutes = int(time_match.group(2))
            return hours + minutes / 60.0

        number_match = re.search(r"\d+(?:\.\d+)?", text)
        if number_match:
            return float(number_match.group())

        return None

    def _daily_hours(self, classes: list) -> float:
        return sum(c["duration"] for c in classes)

    def _schedule_course_count(self, schedule: list) -> int:
        return len({c.course_name for c in schedule})

    def _schedule_signature(self, schedule: list) -> str:
        return "|".join(
            f"{c.course_name}-D{c.day}-{c.start_time}-{c.end_time}"
            for c in sorted(schedule, key=lambda c: (c.day, c.start_time))
        )

    def _build_days_dict(self, schedule: list) -> dict:
        """
        Organize schedule sessions by day.

        Returns:
            {
                day_num: [
                    {
                        "start": float,
                        "end": float,
                        "duration": float,
                        "course_name": str,
                        "raw": original_class_object
                    }
                ]
            }
        """
        days_dict = {}

        for c in schedule:
            start = self._time_to_float(c.start_time)
            end = self._time_to_float(c.end_time)

            days_dict.setdefault(c.day, []).append({
                "start": start,
                "end": end,
                "duration": end - start,
                "course_name": c.course_name,
                "raw": c,
            })

        for classes in days_dict.values():
            classes.sort(key=lambda x: x["start"])

        return days_dict

    def _count_active_days(self, schedule: list) -> int:
        return len({c.day for c in schedule})

    def _window_metrics(self, schedule: list) -> dict:
        """
        Count gap windows in a schedule.

        A window is counted only when the gap is longer than self.gap_threshold_hours.
        Current default: 1 hour.
        """
        days_dict = self._build_days_dict(schedule)

        window_count = 0
        window_hours = 0.0

        for classes in days_dict.values():
            for i in range(len(classes) - 1):
                gap = classes[i + 1]["start"] - classes[i]["end"]

                if gap > self.gap_threshold_hours:
                    window_count += 1
                    window_hours += gap

        return {
            "window_count": window_count,
            "window_hours": round(window_hours, 2),
        }

    # =========================================================
    # Stage 1: Hard Constraints Filter
    # =========================================================

    def explain_hard_constraint_failure(self, schedule: list, user_preferences: dict):
        """
        Returns a structured reason if a schedule violates a hard constraint.

        Hard constraints:
          - unwanted_days
          - max_study_days, if user explicitly set it
          - time_from, if user explicitly set it
          - time_until, if user explicitly set it
          - max_daily_hours, if user explicitly set it

        Important distinction:
          - unwanted_days means specific days the user does not want to study.
          - max_study_days means maximum number of active study days.
        """
        unwanted_days = set(user_preferences.get("unwanted_days", []))
        target_days = self._coerce_hour_value(user_preferences.get("max_study_days"))
        time_from = self._coerce_hour_value(user_preferences.get("time_from"))
        time_until = self._coerce_hour_value(user_preferences.get("time_until"))
        max_daily = self._coerce_hour_value(user_preferences.get("max_daily_hours"))

        days_dict = self._build_days_dict(schedule)

        # Hard D: if user set max_study_days, do not cross it.
        if target_days is not None:
            active_days = len(days_dict)
            if active_days > target_days:
                return {
                    "code": "max_study_days_exceeded",
                    "detail": f"Schedule has {active_days} active days, above max_study_days={int(target_days)}",
                    "actual_days": active_days,
                    "limit_days": int(target_days),
                }

        # Hard max daily hours, only if user explicitly set it.
        if max_daily is not None:
            for day, classes in days_dict.items():
                day_hours = self._daily_hours(classes)
                if day_hours > max_daily + 1e-9:
                    return {
                        "code": "max_daily_hours_exceeded",
                        "detail": f"Day {day} has {day_hours:.2f}h, above max_daily_hours={max_daily:.2f}h",
                        "day": day,
                        "actual_hours": round(day_hours, 2),
                        "limit_hours": round(max_daily, 2),
                    }

        for c in schedule:
            # Hard unwanted days.
            if c.day in unwanted_days:
                return {
                    "code": "unwanted_day",
                    "detail": f"Course {c.course_name} is scheduled on blocked day {c.day}",
                    "course_id": c.course_name,
                    "day": c.day,
                }

            # Hard T: explicit time window means do not cross it.
            if time_from is not None or time_until is not None:
                start = self._time_to_float(c.start_time)
                end = self._time_to_float(c.end_time)

                if time_from is not None and start < time_from:
                    return {
                        "code": "starts_before_time_from",
                        "detail": f"Course {c.course_name} starts at {start:.2f}, before time_from={time_from:.2f}",
                        "course_id": c.course_name,
                        "day": c.day,
                        "start": c.start_time,
                        "time_from": time_from,
                    }

                if time_until is not None and end > time_until:
                    return {
                        "code": "ends_after_time_until",
                        "detail": f"Course {c.course_name} ends at {end:.2f}, after time_until={time_until:.2f}",
                        "course_id": c.course_name,
                        "day": c.day,
                        "end": c.end_time,
                        "time_until": time_until,
                    }

        return None

    def filter_hard_constraints(self, schedule: list, user_preferences: dict) -> bool:
        """
        Returns False if the schedule violates any hard constraint.
        Returns True if the schedule is clean.
        """
        return self.explain_hard_constraint_failure(schedule, user_preferences) is None

    # =========================================================
    # Stage 2: Soft Constraints Scorer
    # =========================================================

    def calculate_penalty(self, schedule: list, user_preferences: dict, course_metadata: dict = None,) -> tuple[float, dict]:
        """
        Scores a schedule on 5 soft metrics, each normalized to [0, 1].

        Lower penalty is better.
        Final score is calculated later as:
            score = 100 * (1 - penalty)
        """
        days_dict = self._build_days_dict(schedule)
        num_days = len(days_dict)
        total_hours = sum(c["duration"] for classes in days_dict.values() for c in classes)

        if num_days == 0 or total_hours <= 0:
            return 1.0, {k: 1.0 for k in self.weights}

        # -----------------------------------------------------
        # G: Gap Windows
        # -----------------------------------------------------
        # Gap windows only count if the gap is longer than 1 hour.
        # Example:
        #   Class A ends 10:00
        #   Class B starts 11:30
        #   Gap is 1.5h, counted
        #
        # Gap of exactly 1h or less is ignored.
        has_windows_constraint = user_preferences.get("windows_minimization", False)

        gap_hours = []

        for classes in days_dict.values():
            for i in range(len(classes) - 1):
                gap = classes[i + 1]["start"] - classes[i]["end"]
                if gap > self.gap_threshold_hours:
                    gap_hours.append(gap)

        if has_windows_constraint:
            # Strict mode, but still much lighter than before.
            # 5+ gap hours reaches full G penalty.
            raw_gap = sum(gap_hours)
            p_G = min(1.0, raw_gap / 5.0)
        else:
            # Lenient mode.
            # Count whole-hour blocks only, after the 1h threshold.
            total_window_hours = sum(math.floor(gap) for gap in gap_hours)
            p_G = min(5, total_window_hours) / 5.0

        # -----------------------------------------------------
        # D: Active Study Days
        # -----------------------------------------------------
        # If user sets max_study_days, it is already enforced as hard.
        # Here we only score D softly when max_study_days was not provided.
        target_days = self._coerce_hour_value(user_preferences.get("max_study_days"))

        if target_days is not None:
            p_D = 0.0
        else:
            # Default soft behavior:
            # Prefer compact schedules, but do not hard reject.
            # 1-2 days = best.
            # 6+ days = worst.
            day_score = max(0, num_days - 2)
            p_D = min(day_score, 4) / 4.0

        # -----------------------------------------------------
        # LD: Daily Load
        # -----------------------------------------------------
        # Default daily load logic changed:
        #   <=5h       -> 0 points
        #   >5h to 7h  -> 2 points
        #   >7h        -> 3 points
        #
        # If user sets min_daily_hours or max_daily_hours, those are used.
        # max_daily_hours is also hard-filtered before this scoring stage.
        max_daily = self._coerce_hour_value(user_preferences.get("max_daily_hours"))
        min_daily = self._coerce_hour_value(user_preferences.get("min_daily_hours"))
        max_consecutive = self._coerce_hour_value(user_preferences.get("max_consecutive_hours"))

        max_per_day = 3.0 + (3.0 if max_consecutive is not None else 0.0)
        total_ld = 0.0

        for classes in days_dict.values():
            day_hours = self._daily_hours(classes)
            day_penalty = 0.0

            # Hours sub-score, 0 to 3.
            if max_daily is None and min_daily is None:
                if day_hours <= 5:
                    day_penalty += 0.0
                elif day_hours <= 7:
                    day_penalty += 2.0
                else:
                    day_penalty += 3.0
            else:
                if max_daily is not None and day_hours > max_daily:
                    day_penalty += 3.0
                elif min_daily is not None and day_hours < min_daily:
                    day_penalty += 3.0

            # Consecutive hours sub-score, 0 to 3.
            if max_consecutive is not None and classes:
                block_start = classes[0]["start"]
                block_end = classes[0]["end"]
                violated = False

                for i in range(1, len(classes)):
                    gap = classes[i]["start"] - block_end

                    # Less than 1 hour break means classes are treated as one block.
                    if gap < 1.0:
                        block_end = max(block_end, classes[i]["end"])
                    else:
                        if math.ceil(block_end - block_start) > max_consecutive:
                            violated = True
                            break

                        block_start = classes[i]["start"]
                        block_end = classes[i]["end"]

                if not violated and math.ceil(block_end - block_start) > max_consecutive:
                    violated = True

                if violated:
                    day_penalty += 3.0

            total_ld += day_penalty

        p_LD = total_ld / (max_per_day * num_days)

        # -----------------------------------------------------
        # T: Time Preference
        # -----------------------------------------------------
        # If time_from or time_until are set, they are hard constraints.
        # Surviving schedules should have p_T = 0 for that custom window.
        #
        # If no explicit hard time window exists:
        #   preferred_time == "evening" means penalize hours before 17:00.
        #   otherwise default means penalize hours after 17:00.
        time_from = self._coerce_hour_value(user_preferences.get("time_from"))
        time_until = self._coerce_hour_value(user_preferences.get("time_until"))
        raw_pref = user_preferences.get("preferred_time")

        bad_hours = 0.0

        if time_from is not None or time_until is not None:
            # This should usually be 0 because hard filtering already removed violations.
            for classes in days_dict.values():
                for c in classes:
                    if time_from is not None and c["start"] < time_from:
                        bad_hours += max(0.0, min(time_from, c["end"]) - c["start"])

                    if time_until is not None and c["end"] > time_until:
                        bad_hours += max(0.0, c["end"] - max(time_until, c["start"]))

        elif raw_pref == "evening":
            # Evening preference: penalize hours before 17:00.
            bad_hours = sum(
                max(0.0, min(17.0, c["end"]) - c["start"])
                for classes in days_dict.values()
                for c in classes
                if c["start"] < 17.0
            )

        else:
            # Default preference: penalize hours after 17:00.
            bad_hours = sum(
                max(0.0, c["end"] - max(17.0, c["start"]))
                for classes in days_dict.values()
                for c in classes
                if c["end"] > 17.0
            )

        percent_bad = (bad_hours / total_hours * 100) if total_hours > 0 else 0.0

        if percent_bad == 0:
            score_T = 0
        elif percent_bad <= 20:
            score_T = 1
        elif percent_bad <= 50:
            score_T = 3
        else:
            score_T = 5

        p_T = score_T / 5.0

        # -----------------------------------------------------
        # LW: Weekly Balance
        # -----------------------------------------------------
        # Penalize uneven load distribution across active study days.
        avg_daily = total_hours / num_days
        wants_balance = user_preferences.get("weekly_balance", False)
        threshold = 1.0 if wants_balance else 2.0

        unbalanced = sum(
            1
            for classes in days_dict.values()
            if abs(sum(c["duration"] for c in classes) - avg_daily) > threshold
        )

        p_LW = min(unbalanced, 3) / 3.0

                # -----------------------------------------------------
        # C: Course Assessment Load
        # A: Mandatory Attendance
        # -----------------------------------------------------
        course_metadata = course_metadata or {}

        unique_courses = set()
        for c in schedule:
            try:
                unique_courses.add(int(c.course_name))
            except (TypeError, ValueError):
                unique_courses.add(c.course_name)

        sum_score_c = 0.0
        sum_score_a = 0.0
        num_courses = len(unique_courses)

        for course_id in unique_courses:
            meta = course_metadata.get(course_id, course_metadata.get(str(course_id), {}))

            # A: Mandatory attendance
            if meta.get("is_attendance_mandatory"):
                sum_score_a += 1.0

            # C: Assessment components
            course_score = 0.0
            weights = meta.get("weights", {})

            if weights.get("exam", 0) > 0:
                course_score += 5.0
            if weights.get("project", 0) > 0:
                course_score += 2.0
            if weights.get("mid_semester", 0) > 0 or weights.get("assignments", 0) > 0:
                course_score += 1.0

            sum_score_c += min(5.0, course_score)

        p_C = sum_score_c / (5.0 * num_courses) if num_courses > 0 else 0.0
        p_A = sum_score_a / float(num_courses) if num_courses > 0 else 0.0

        # -----------------------------------------------------
        # Weighted penalty
        # -----------------------------------------------------
        penalty = (
            self.weights["D"] * p_D +
            self.weights["T"] * p_T +
            self.weights["LD"] * p_LD +
            self.weights["LW"] * p_LW +
            self.weights["G"] * p_G +
            self.weights["C"] * p_C +
            self.weights["A"] * p_A
        )

        breakdown = {
            "D": p_D,
            "T": p_T,
            "LD": p_LD,
            "LW": p_LW,
            "G": p_G,
            "C": p_C,
            "A": p_A,
        }

        return penalty, breakdown

    # =========================================================
    # Stage 3: Filter, Score, Rank
    # =========================================================

    def evaluate_and_rank_schedules(
        self,
        valid_schedules: list,
        user_preferences: dict,
        course_metadata: dict = None,
    ) -> list:
        """
        1. Deduplicate schedules by signature.
        2. Drop schedules that violate hard constraints.
        3. Score remaining schedules with calculate_penalty.
        4. Return top 3 sorted by course count, then score.
        """
        ranked = []
        seen_sig = set()

        diagnostics = {
            "input_schedule_count": len(valid_schedules),
            "duplicate_count": 0,
            "hard_filtered_count": 0,
            "hard_filter_reasons": {},
            "hard_filter_samples": [],
            "scored_count": 0,
            "returned_count": 0,
        }

        self._log_decision(
            "Evaluation started",
            input_schedule_count=len(valid_schedules),
            user_preferences=user_preferences,
        )

        for idx, sched in enumerate(valid_schedules):
            # Deduplication.
            sig = self._schedule_signature(sched)
            if sig in seen_sig:
                diagnostics["duplicate_count"] += 1
                continue

            seen_sig.add(sig)

            # Stage 1: hard filter.
            hard_failure = self.explain_hard_constraint_failure(sched, user_preferences)

            if hard_failure:
                diagnostics["hard_filtered_count"] += 1

                reason_code = hard_failure["code"]
                diagnostics["hard_filter_reasons"][reason_code] = (
                    diagnostics["hard_filter_reasons"].get(reason_code, 0) + 1
                )

                if len(diagnostics["hard_filter_samples"]) < 10:
                    diagnostics["hard_filter_samples"].append({
                        "schedule_index": idx,
                        "reason": hard_failure,
                        "signature": sig,
                    })

                self._log_decision(
                    "Schedule rejected by hard constraint",
                    schedule_index=idx,
                    reason=hard_failure,
                )

                continue

            # Stage 2: soft scoring.
            penalty, breakdown = self.calculate_penalty(sched, user_preferences, course_metadata)
            score = round(100 * (1 - penalty), 2)
            window_metrics = self._window_metrics(sched)

            diagnostics["scored_count"] += 1

            ranked.append({
                "original_index": idx,
                "schedule": sched,
                "score": score,
                "penalty": round(penalty, 4),
                "breakdown": breakdown,
                "course_count": self._schedule_course_count(sched),
                "window_count": window_metrics["window_count"],
                "window_hours": window_metrics["window_hours"],
            })

        # Keep original behavior unless the user explicitly asked to minimize windows.
        # With windows_minimization:
        #   1. More courses first
        #   2. Fewer windows
        #   3. Fewer total window hours
        #   4. Higher regular score
        #
        # With windows_minimization + allow_course_drop:
        #   the user explicitly asked to go one group below, so we prefer the
        #   best available course-count group below the full group, up to max_course_drops.
        #   Inside that group, fewer windows come first.
        if user_preferences.get("windows_minimization", False):
            allow_drop = bool(user_preferences.get("allow_course_drop", False))
            try:
                max_drops = int(user_preferences.get("max_course_drops", 1) or 1)
            except (TypeError, ValueError):
                max_drops = 1
            max_drops = max(1, max_drops)

            selected_group = ranked
            selected_course_count = None

            if allow_drop and ranked:
                full_course_count = max(item["course_count"] for item in ranked)

                for drop_count in range(1, max_drops + 1):
                    target_course_count = full_course_count - drop_count
                    if target_course_count <= 0:
                        continue

                    candidate_group = [
                        item for item in ranked
                        if item["course_count"] == target_course_count
                    ]

                    if candidate_group:
                        selected_group = candidate_group
                        selected_course_count = target_course_count
                        break

                if selected_course_count is not None:
                    self._log_decision(
                        "Course drop ranking group selected",
                        full_course_count=full_course_count,
                        selected_course_count=selected_course_count,
                        max_drops=max_drops,
                        selected_group_count=len(selected_group),
                    )
                else:
                    self._log_decision(
                        "Course drop requested but no lower course-count group found; keeping full group",
                        full_course_count=full_course_count,
                        max_drops=max_drops,
                    )

            selected_group.sort(
                key=lambda x: (
                    x["window_count"],
                    x["window_hours"],
                    -x["score"],
                    -x["course_count"],
                )
            )
            ranked = selected_group
        else:
            ranked.sort(key=lambda x: (x["course_count"], x["score"]), reverse=True)

        diagnostics["returned_count"] = min(3, len(ranked))
        self.last_diagnostics = diagnostics

        if ranked:
            top = ranked[0]
            days_in_top = len(set(c.day for c in top["schedule"]))

            print(
                f"[Optimizer] {len(ranked)} schedules scored | "
                f"Winner: {days_in_top} days, score={top['score']} | "
                f"Breakdown: { {k: round(v, 2) for k, v in top['breakdown'].items()} }"
            )

            self._log_decision(
                "Evaluation completed",
                scored_count=diagnostics["scored_count"],
                returned_count=diagnostics["returned_count"],
                best_score=top["score"],
                best_course_count=top["course_count"],
                best_window_count=top.get("window_count"),
                best_window_hours=top.get("window_hours"),
                best_breakdown=top["breakdown"],
            )

        else:
            self._log_decision(
                "Evaluation completed with no ranked schedules",
                diagnostics=diagnostics,
            )

        return ranked[:3]