# WEB

Backend and dashboard for building student schedules from chat constraints.

## AI Token Optimization

Recent chat-layer changes reduce Gemini usage in the main `/api/chat` flow.

- Obvious requests are parsed locally before calling Gemini, including simple build requests, no-constraint requests, info-only questions, day-off constraints, simple time-window constraints, and window-minimization requests.
- Saved constraints are merged in backend code instead of being sent to Gemini and copied back in every response.
- Info-only answers are generated from local course metadata and course offerings instead of sending the full course JSON to Gemini.
- Final schedule explanations are generated deterministically from builder and optimizer results instead of using a second Gemini response-generation call.
- The remaining Gemini call is limited to ambiguous current-message constraint extraction, with JSON response mode and `temperature: 0`.

Expected impact: common chat requests now use zero Gemini calls, and complex scheduling requests use one compact Gemini call instead of up to three larger calls.

## Constraint Flow

The chat endpoint now normalizes active constraints into a shared `builder_config["hard_constraints"]` list before schedule generation.

- Persisted DB constraints and current-message constraints are merged into one active set.
- Multi-value constraints are expanded before schedule building, so one payload can represent several values.
- `prepare_builder_config` converts supported constraints into the generic `hard_constraints` shape: `{ type, importance, payload }`.
- `ScheduleBuilder` reads the generic list and applies meeting-level constraints such as blocked days, time windows, course-specific day/time rules, and avoided lecturers.
- Full-schedule constraints such as `daily_load`, `study_days_limit`, and `windows_minimization` are also registered in the generic list and validated after candidate schedules are assembled.
- Legacy config fields are still populated for compatibility, but the generic list is the shared path for adding future constraints.
- Lecturer constraints are sanitized before persistence and building: phrases such as `קורסים של רון וולף או של פבל` become separate clean lecturer payloads for `רון וולף` and `פבל`.
- Day-blocking language such as `לא רוצה ללמוד בימי שלישי` is routed to `day_off` and is not stored as a lecturer name.
- Day detection supports Hebrew day names with prefixes and conjunctions (`בשלישי`, `ושלישי`), day-letter shorthand (`יום ג'`, `ימי ג וד`), and English weekday aliases (`Tue`, `Thursday`).

This means a saved DB constraint is translated into the same structure the Builder uses to filter and validate schedules.

Examples supported by the shared flow:

- `בלי רון וולף ופבל` expands into two `lecturer_constraint` entries.
- `בלי קורסים של עומר פרי ובלי ימי שלישי` expands into one lecturer constraint and one `day_off`.
- `בלי קורסים של רון וולף או של פבל, אני לא רוצה ללמוד בימי שלישי` expands into two lecturer constraints and one `day_off`.
- `ללא ימים ראשון ושלישי` expands into two `day_off` entries.
- A DB payload with `lecturer_names`, `days`, `target_courses`, or similar plural fields is split into individual constraints before application.

## Schedule Engine Notes

Recent scheduling changes:

- Failure diagnostics are printed to the backend terminal only. They are not returned in the dashboard JSON payload.
- The scheduler logs major decisions in English with `[ChatDecision]`, `[ScheduleBuilder]`, `[ScheduleOptimizer]`, and `[ScheduleFailure]` prefixes.
- `daily_load` is enforced as a hard optimizer constraint, so schedules that exceed `max_daily_hours` are rejected before ranking.
- If a full mandatory-course schedule is not possible, the builder searches for a partial viable schedule with at least 3 courses.
- Ranking prefers schedules with more courses first, then uses the optimizer score as the secondary ordering.
- Course-specific day/time constraints now respect explicit session type, so a lecture constraint does not reject a valid team because its tutorial is on another day.
- Builder time comparisons are normalized to minutes to avoid false overlap or false non-overlap decisions from string-formatted times like `9:30`.

See [SCHEDULING_ENGINE_CHANGES.md](SCHEDULING_ENGINE_CHANGES.md) for details.
