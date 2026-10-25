# Scheduling Engine Changes

## Purpose

This document summarizes the recent scheduling changes made to improve failure visibility and avoid false "no valid schedule" results.

## Terminal-Only Failure Diagnostics

Detailed failure diagnostics now stay in the backend terminal and are not sent to the dashboard payload.

When no schedule is found, the backend prints a line like:

```text
[ScheduleFailure] status=... reason=... diagnostics=...
```

The dashboard still receives the normal response fields:

- `text`
- `schedules`
- `titles`
- `gradeDistribution`
- `exams`

It no longer receives `failureDiagnostics`.

## English Decision Logs

The backend now logs the major scheduling decisions in English:

- `[ChatDecision]`: request input, extracted constraints, routing, builder config, offering counts, build status, optimizer status.
- `[ScheduleBuilder]`: hard-constraint filtering, rejected course teams, conflict samples, full/partial build attempts.
- `[ScheduleOptimizer]`: hard-filter rejections, daily-load violations, scored schedule counts, returned alternatives.
- `[ScheduleFailure]`: terminal-only final failure reason and status.

## Partial Viable Schedules

Previously, the builder required all mandatory courses to fit together, then tried only a narrow fallback that dropped one mandatory course. This caused false failures when a valid 3-course or 4-course schedule existed but a larger set did not.

The builder now behaves as follows:

1. Try the full mandatory/elective schedule.
2. Try configured backup substitutions if available.
3. If still impossible, search for a partial viable schedule with at least 3 courses.
4. Return the best partial alternatives instead of failing.

The partial search considers courses that survived hard filtering and only returns conflict-free schedules.

## Builder False-Rejection Fixes

Two builder issues could reject viable schedules before the optimizer ever saw them:

- Time overlap checks compared raw strings. Times are now normalized to minutes before comparing, so values like `9:30`, `09:30`, and `10:00` are evaluated consistently.
- Course-specific day/time constraints ignored `course_session_type`. The builder now applies a lecture or tutorial constraint only to matching sessions. For example, "lecture for course X on Monday" no longer rejects a team just because its tutorial is on Tuesday.

The chat parser also only records `course_session_type` when the constraint payload explicitly includes a recognizable session type. Missing or unknown session types are left course-wide instead of silently defaulting to `lecture`.

## Ranking Behavior

The optimizer now ranks valid schedules by:

1. Number of unique courses, descending.
2. Optimizer score, descending.

This makes the first alternative prefer the largest valid course set that satisfies hard constraints, while still using schedule quality as a tie-breaker.

## Daily Load Enforcement

`daily_load` is treated as a hard constraint in the optimizer. A schedule with a day above `max_daily_hours` is rejected before scoring.

Example diagnostic:

```text
max_daily_hours_exceeded: Day 1 has 6.00h, above max_daily_hours=4.00h
```

The parser also accepts variants like:

- `4`
- `4h`
- `4 hours`
- payload keys such as `max_hours`, `hours`, `max_daily_hours`, `daily_load`, or `value`

## Files Changed

- `chat.py`: request and failure logging, terminal-only diagnostics, robust numeric constraint extraction, explicit session-type handling.
- `builder.py`: detailed decision logs, conflict samples, partial viable schedule search, normalized time comparisons, session-type-aware course constraints.
- `optimizer.py`: hard daily-load filtering, diagnostics, course-count-first ranking.
- `README.md`: high-level schedule engine notes.
