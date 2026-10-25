"""
Offline session simulator.

Runs the real /api/chat logic (chat.chat_endpoint -> ScheduleBuilder -> ScheduleOptimizer)
against an in-memory copy of the Supabase tables, simulating N student conversations.
In SELECT_COUNT of them the simulated student picks a preferred alternative.

Nothing is written to Supabase. Output rows (only the new ones) are written to OUT_DIR
as JSON files that mirror the DB tables, plus personas.json with the hidden preferences.

Usage:
    py -3 simulate_sessions.py [--sessions 400] [--select 350] [--seed 42] [--out simulation_output]
"""
import os
import io
import re
import sys
import copy
import json
import math
import random
import asyncio
import argparse
import contextlib
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
import supabase as supabase_pkg

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

REFERENCE_TABLES = ["students", "course", "courseoffering", "semester_template", "specialization", "program"]
CHAT_TABLES = ["chat_sessions", "chat_messages", "generated_schedules", "schedule_classes", "studentconstraint"]
PRIMARY_KEYS = {
    "chat_sessions": "session_id",
    "chat_messages": "message_id",
    "generated_schedules": "schedule_id",
    "schedule_classes": "class_id",
    "studentconstraint": "constraint_id",
}
CRITERIA = ["G", "D", "LD", "T", "LW", "C", "A"]
DAY_NAMES_HE = {1: "ראשון", 2: "שני", 3: "שלישי", 4: "רביעי", 5: "חמישי"}


# =====================================================================
# --- In-memory Supabase replacement ---
# =====================================================================
def _same(a, b):
    if a == b:
        return True
    return a is not None and b is not None and str(a).strip() == str(b).strip()


class FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class FakeQuery:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.op = "select"
        self.filters = []
        self.order_by = None
        self.limit_n = None
        self.payload = None

    def select(self, *_args, **_kwargs):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, value):
        self.filters.append(lambda r: _same(r.get(col), value))
        return self

    def in_(self, col, values):
        values = list(values)
        self.filters.append(lambda r: any(_same(r.get(col), v) for v in values))
        return self

    def order(self, col, desc=False):
        self.order_by = (col, desc)
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def _match(self, row):
        return all(f(row) for f in self.filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = [self.db.add_row(self.table, item) for item in items]
            return FakeResult(copy.deepcopy(inserted))
        if self.op == "update":
            changed = [r for r in rows if self._match(r)]
            for r in changed:
                r.update(self.payload)
            return FakeResult(copy.deepcopy(changed))
        if self.op == "delete":
            removed = [r for r in rows if self._match(r)]
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            return FakeResult(copy.deepcopy(removed))

        result = [r for r in rows if self._match(r)]
        if self.order_by:
            col, desc = self.order_by
            result.sort(key=lambda r: str(r.get(col) or ""), reverse=desc)
        if self.limit_n is not None:
            result = result[:self.limit_n]
        return FakeResult(copy.deepcopy(result), count=len(result))


class FakeDB:
    def __init__(self, tables):
        self.tables = tables
        self.next_ids = {
            t: max([r.get(pk) or 0 for r in tables.get(t, [])] + [0]) + 1
            for t, pk in PRIMARY_KEYS.items()
        }
        self.clock = datetime.now(timezone.utc)

    def now(self):
        return self.clock.isoformat()

    def add_row(self, table, item):
        row = dict(item)
        pk = PRIMARY_KEYS.get(table)
        if pk and row.get(pk) is None:
            row[pk] = self.next_ids[table]
            self.next_ids[table] += 1
        self.clock += timedelta(seconds=random.uniform(0.3, 1.5))
        if table == "studentconstraint":
            row.setdefault("received_at", self.clock.replace(tzinfo=None).isoformat())
        else:
            row.setdefault("created_at", self.now())
        if table == "chat_sessions":
            row.setdefault("is_pinned", False)
        if table == "generated_schedules":
            row.setdefault("is_selected", False)
            row.setdefault("selection_mark", 0)
            row.setdefault("iteration1_score", None)
            row.setdefault("iteration1_rank", None)
        self.tables.setdefault(table, []).append(row)
        return row

    def table(self, name):
        return FakeQuery(self, name)

    def remove_session(self, session_id):
        sched_ids = {r["schedule_id"] for r in self.tables["generated_schedules"] if r["session_id"] == session_id}
        self.tables["schedule_classes"] = [r for r in self.tables["schedule_classes"] if r["schedule_id"] not in sched_ids]
        for t in ["chat_sessions", "chat_messages", "generated_schedules", "studentconstraint"]:
            self.tables[t] = [r for r in self.tables[t] if r.get("session_id") != session_id]


def fetch_live_tables():
    """Read-only snapshot of the live Supabase tables."""
    client = supabase_pkg.create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    tables = {}
    for t in REFERENCE_TABLES + CHAT_TABLES:
        rows, start = [], 0
        while True:
            page = client.table(t).select("*").range(start, start + 999).execute().data
            rows += page
            if len(page) < 1000:
                break
            start += 1000
        tables[t] = rows
    return tables


# =====================================================================
# --- Persona + message generation ---
# =====================================================================
NO_CONSTRAINT_MESSAGES = ["תבנה לי מערכת", "תבנה מערכת", "בנה לי מערכת בבקשה", "תיצור לי מערכת", "תשבץ לי מערכת"]
BUILD_PREFIXES = ["תבנה לי מערכת", "תבנה מערכת", "בנה לי מערכת", "תיצור לי מערכת"]


def fragment_texts(kind, value):
    if kind == "day_off":
        names = [DAY_NAMES_HE[d] for d in value]
        joined = " ו".join(names)
        return [f"בלי ימי {joined}", f"אני לא רוצה ללמוד ביום {joined}", f"ללא ימי {joined}"]
    if kind == "start_after":
        return [f"לא לפני {value}:00", f"משעה {value}"]
    if kind == "end_before":
        return [f"לא אחרי {value}:00", f"עד {value}:00"]
    if kind == "windows":
        return ["עם כמה שפחות חלונות", "בלי חלונות", "מינימום חלונות"]
    if kind == "max_days":
        return [f"עד {value} ימים", f"מקסימום {value} ימי לימוד"]
    if kind == "avoid_lecturer":
        return [f"בלי {value}", f"ללא {value}"]
    if kind == "drop_flex":
        return ["ואם צריך אפשר להוריד קורס", "ואפשר לוותר על קורס"]
    return []


EXPECTED_TYPES = {
    "day_off": "day_off",
    "start_after": "time_range",
    "end_before": "time_range",
    "windows": "windows_minimization",
    "max_days": "study_days_limit",
    "avoid_lecturer": "lecturer_constraint",
    "drop_flex": "course_drop_flexibility",
}

# Fragments that cannot share a message without confusing the local parser.
TIME_KINDS = {"start_after", "end_before"}
DAY_WORD_KINDS = {"day_off", "max_days"}


def make_persona(rng, lecturer_pool, prior=None, concentration=20.0, course_bonus_scale=1.0):
    wishes = []
    if rng.random() < 0.35:
        days = rng.sample([1, 2, 3, 4, 5], 2 if rng.random() < 0.15 else 1)
        wishes.append(("day_off", sorted(days)))
    if rng.random() < 0.25:
        wishes.append(("start_after", rng.choice([9, 10, 11, 12])))
    if rng.random() < 0.22:
        wishes.append(("end_before", rng.choice([14, 15, 16, 17, 18])))
    if rng.random() < 0.30:
        wishes.append(("windows", True))
    if rng.random() < 0.15 and not any(k == "day_off" for k, _ in wishes):
        wishes.append(("max_days", rng.choice([3, 4])))
    if rng.random() < 0.12 and lecturer_pool:
        wishes.append(("avoid_lecturer", rng.choice(lecturer_pool)))
    if rng.random() < 0.08:
        wishes.append(("drop_flex", True))

    # Hidden preference weights the persona uses when choosing among alternatives.
    # With a prior (e.g. weights learned from real users), each persona's weights are a
    # Dirichlet draw centred on it; otherwise they are drawn almost uniformly.
    if prior:
        raw = {k: rng.gammavariate(concentration * prior[k], 1.0) for k in CRITERIA}
        boost = 0.12 * concentration
    else:
        raw = {k: rng.gammavariate(1.5, 1.0) for k in CRITERIA}
        boost = 2.5
    kinds = {k for k, _ in wishes}
    if "windows" in kinds:
        raw["G"] += boost * 1.2
    if kinds & {"day_off", "max_days"}:
        raw["D"] += boost
    if kinds & TIME_KINDS:
        raw["T"] += boost
    total = sum(raw.values())
    weights = {k: round(v / total, 4) for k, v in raw.items()}

    return {
        "wishes": wishes,
        "weights": weights,
        "course_count_bonus": round(rng.uniform(0.05, 0.25) * course_bonus_scale, 3),
        "first_alt_bias": round(rng.uniform(0.0, 0.08), 3),
        "temperature": round(rng.uniform(0.02, 0.08), 3),
    }


def split_into_turns(rng, wishes):
    """Split wishes into 1-2 turns; time wishes never share a message with day-word wishes."""
    if not wishes:
        return [[]]
    time_part = [w for w in wishes if w[0] in TIME_KINDS]
    other_part = [w for w in wishes if w[0] not in TIME_KINDS]
    has_day_words = any(w[0] in DAY_WORD_KINDS for w in other_part)

    if time_part and has_day_words:
        turns = [other_part, time_part]
        rng.shuffle(turns)
        return turns
    if len(wishes) >= 2 and rng.random() < 0.2:
        cut = rng.randint(1, len(wishes) - 1)
        shuffled = wishes[:]
        rng.shuffle(shuffled)
        first, second = shuffled[:cut], shuffled[cut:]
        first_ok = not (any(w[0] in TIME_KINDS for w in first) and any(w[0] in DAY_WORD_KINDS for w in first))
        second_ok = not (any(w[0] in TIME_KINDS for w in second) and any(w[0] in DAY_WORD_KINDS for w in second))
        if first_ok and second_ok:
            return [first, second]
    return [wishes]


def build_message(rng, chat, turn_wishes, is_followup):
    """Pick a template combination the local parser maps to exactly the intended constraint types."""
    if not turn_wishes:
        return rng.choice(NO_CONSTRAINT_MESSAGES)

    expected = sorted(EXPECTED_TYPES[k] for k, v in turn_wishes for _ in (v if k == "day_off" else [v]))
    for _ in range(25):
        parts = [rng.choice(fragment_texts(k, v)) for k, v in turn_wishes]
        body = ", ".join(p for p in parts if not p.startswith("ו")) or ""
        tail = " ".join(p for p in parts if p.startswith("ו"))
        prefix = "ועכשיו" if is_followup and rng.random() < 0.5 else rng.choice(BUILD_PREFIXES)
        message = f"{prefix} {body} {tail}".replace("  ", " ").strip()
        parsed = chat._try_parse_constraints_locally(message)
        if not parsed or not parsed.get("requires_schedule_build"):
            continue
        cleaned = chat._sanitize_current_constraints(message, parsed.get("extracted_constraints") or [])
        if sorted(c.get("type") for c in cleaned) == expected:
            return message
    return None


def choose_alternative(rng, persona, alternatives):
    """Softmax choice over persona utility (lower penalties = better)."""
    utilities = []
    max_courses = max(a["total_courses"] or 0 for a in alternatives) or 1
    for a in alternatives:
        cost = sum(persona["hidden_weights"][k] * float(a.get(f"score_{k.lower()}") or 0) for k in CRITERIA)
        u = -cost + persona["course_count_bonus"] * ((a["total_courses"] or 0) / max_courses)
        if a["alt_key"] == "alt1":
            u += persona["first_alt_bias"]
        utilities.append(u)
    t = persona["temperature"]
    top = max(utilities)
    probs = [math.exp((u - top) / t) for u in utilities]
    s = sum(probs)
    probs = [p / s for p in probs]
    idx = rng.choices(range(len(alternatives)), weights=probs)[0]
    return alternatives[idx]["alt_key"], [round(p, 4) for p in probs], [round(u, 4) for u in utilities]


# =====================================================================
# --- Main simulation ---
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=400)
    ap.add_argument("--select", type=int, default=350)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--prior", default=None,
                    help="centre persona preferences on these weights, e.g. G=0.333,T=0.238,...")
    ap.add_argument("--concentration", type=float, default=20.0,
                    help="how tightly personas follow --prior (higher = less spread)")
    ap.add_argument("--course-bonus-scale", type=float, default=1.0,
                    help="scale of the personas' preference for schedules with more courses (0 = none)")
    ap.add_argument("--out", default=os.path.join(PROJECT_DIR, "simulation_output"))
    ap.add_argument("--min-three-selected", type=int, default=250,
                    help="minimum selected sessions that offered 3 alternatives")
    ap.add_argument("--three-margin", type=int, default=30,
                    help="extra 3-alternative sessions left unselected (unlabeled data for semi-supervised learning)")
    ap.add_argument("--max-attempts", type=int, default=3000)
    args = ap.parse_args()
    # Sessions with fewer than 3 alternatives are accepted only up to this cap.
    max_other_sessions = max(0, args.sessions - (args.min_three_selected + args.three_margin))

    rng = random.Random(args.seed)
    prior = None
    if args.prior:
        prior = {k: float(v) for k, v in (item.split("=") for item in args.prior.split(","))}
        total = sum(prior.values())
        prior = {k: prior.get(k, 0.0) / total for k in CRITERIA}
    random.seed(args.seed)

    print("Fetching live tables (read-only)...")
    tables = fetch_live_tables()
    original_ids = {t: {r[PRIMARY_KEYS[t]] for r in tables[t]} for t in CHAT_TABLES}
    db = FakeDB(tables)
    db.clock = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)

    supabase_pkg.create_client = lambda *a, **k: db
    sys.path.insert(0, PROJECT_DIR)
    with contextlib.redirect_stdout(io.StringIO()):
        import chat

    def _no_gemini(*_a, **_k):
        raise RuntimeError("Gemini disabled in simulation")
    chat._parse_constraints_with_gemini = _no_gemini

    templates = {(t["program_name"], t["year"], t["semester"]) for t in tables["semester_template"]}
    programs = {p["program_id"]: p["program_name"] for p in tables["program"]}
    students = [s for s in tables["students"] if s.get("program_id") in programs]

    lecturers_by_course = {}
    for o in tables["courseoffering"]:
        name = (o.get("instructor_name") or "").strip()
        if name and name != "לא צוין":
            lecturers_by_course.setdefault((o["course_id"], o["semester"]), set()).add(name)
    mandatory_by_key = {(t["program_name"], t["year"], t["semester"]): t["mandatory_course_ids"] for t in tables["semester_template"]}

    log_file = open(os.path.join(PROJECT_DIR, "simulation_run.log"), "w", encoding="utf-8")
    personas_out = []
    completed, attempts, failed = [], 0, {"no_message": 0, "no_schedule": 0, "error": 0}

    while len(completed) < args.sessions and attempts < args.max_attempts:
        attempts += 1
        student = rng.choice(students)
        semester = rng.choice([1, 2])
        key = (programs[student["program_id"]], student["study_year"], semester)
        if key not in templates:
            continue
        pool = sorted({n for cid in mandatory_by_key[key] for n in lecturers_by_course.get((int(cid), semester), [])})
        persona = make_persona(rng, pool, prior, args.concentration, args.course_bonus_scale)
        turns = split_into_turns(rng, persona["wishes"])
        messages = [build_message(rng, chat, w, i > 0) for i, w in enumerate(turns)]
        if any(m is None for m in messages):
            failed["no_message"] += 1
            continue

        db.clock += timedelta(minutes=rng.uniform(5, 180))
        session_id = None
        history = []
        try:
            for msg in messages:
                req = chat.ChatRequest(message=msg, student_id=student["student_id"], semester=semester,
                                       session_id=session_id, history=history, locked_courses=[])
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    resp = asyncio.run(chat.chat_endpoint(req, None))
                log_file.write(f"\n===== session {resp['session_id']} | student {student['student_id']} | sem {semester} | {msg}\n")
                log_file.write(buf.getvalue())
                session_id = resp["session_id"]
                history += [{"role": "user", "text": msg}, {"role": "bot", "text": resp["reply"]}]
                db.clock += timedelta(seconds=rng.uniform(20, 240))
        except Exception as e:
            failed["error"] += 1
            log_file.write(f"\n!!! error: {e!r}\n")
            if session_id:
                db.remove_session(session_id)
            continue

        alts = [r for r in db.tables["generated_schedules"] if r["session_id"] == session_id]
        if not alts:
            failed["no_schedule"] += 1
            db.remove_session(session_id)
            continue
        if len(alts) < 3 and sum(1 for p in personas_out if p["alternatives"] < 3) >= max_other_sessions:
            failed["over_cap_fewer_than_3"] = failed.get("over_cap_fewer_than_3", 0) + 1
            db.remove_session(session_id)
            continue

        completed.append(session_id)
        personas_out.append({
            "session_id": session_id,
            "student_id": student["student_id"],
            "semester": semester,
            "messages": messages,
            "wishes": [[k, v] for k, v in persona["wishes"]],
            "hidden_weights": persona["weights"],
            "course_count_bonus": persona["course_count_bonus"],
            "first_alt_bias": persona["first_alt_bias"],
            "temperature": persona["temperature"],
            "alternatives": len(alts),
        })
        print(f"[{len(completed)}/{args.sessions}] session {session_id}: {len(alts)} alternatives | {' || '.join(messages)}")

    # Selection phase: SELECT_COUNT sessions pick their preferred alternative,
    # at least MIN_THREE_SELECTED of them from sessions that offered 3 alternatives.
    three_ids = [p["session_id"] for p in personas_out if p["alternatives"] == 3]
    forced = rng.sample(three_ids, min(args.min_three_selected, len(three_ids)))
    rest = [sid for sid in completed if sid not in set(forced)]
    selecting = set(forced) | set(rng.sample(rest, max(0, min(args.select, len(completed)) - len(forced))))
    for p in personas_out:
        sid = p["session_id"]
        alts = sorted([r for r in db.tables["generated_schedules"] if r["session_id"] == sid], key=lambda r: r["alt_key"])
        if sid in selecting:
            chosen, probs, utils = choose_alternative(rng, p, alts)
            # Same update as /api/schedule/select
            db.table("generated_schedules").update({"is_selected": False}).eq("session_id", sid).execute()
            db.table("generated_schedules").update({"is_selected": True}).eq("session_id", sid).eq("alt_key", chosen).execute()
            for a in alts:
                a["selection_mark"] = 2 if a["alt_key"] == chosen else 1
            p.update(selected_alt=chosen, choice_probabilities=probs, utilities=utils)
        else:
            for a in alts:
                a["selection_mark"] = 0
            p.update(selected_alt=None)

    # Replaced alternatives (multi-turn sessions) leave their classes behind; drop the orphans.
    live_schedule_ids = {r["schedule_id"] for r in db.tables["generated_schedules"]}
    db.tables["schedule_classes"] = [r for r in db.tables["schedule_classes"] if r["schedule_id"] in live_schedule_ids]

    os.makedirs(args.out, exist_ok=True)
    for t in CHAT_TABLES:
        new_rows = [r for r in db.tables[t] if r[PRIMARY_KEYS[t]] not in original_ids[t]]
        with open(os.path.join(args.out, f"{t}.json"), "w", encoding="utf-8") as f:
            json.dump(new_rows, f, ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(args.out, "personas.json"), "w", encoding="utf-8") as f:
        json.dump(personas_out, f, ensure_ascii=False, indent=1)

    gen = [r for r in db.tables["generated_schedules"] if r["session_id"] in set(completed)]
    alt_counts = {}
    for sid in completed:
        n = sum(1 for r in gen if r["session_id"] == sid)
        alt_counts[n] = alt_counts.get(n, 0) + 1
    chosen_dist = {}
    for p in personas_out:
        if p["selected_alt"]:
            chosen_dist[p["selected_alt"]] = chosen_dist.get(p["selected_alt"], 0) + 1
    summary = {
        "sessions": len(completed),
        "sessions_with_selection": len(selecting),
        "attempts": attempts,
        "discarded": failed,
        "sessions_by_alternative_count": alt_counts,
        "selected_alt_distribution": chosen_dist,
        "selected_sessions_with_3_alternatives": sum(1 for p in personas_out if p["selected_alt"] and p["alternatives"] == 3),
        "multi_turn_sessions": sum(1 for p in personas_out if len(p["messages"]) > 1),
        "rows": {t: sum(1 for r in db.tables[t] if r[PRIMARY_KEYS[t]] not in original_ids[t]) for t in CHAT_TABLES},
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    log_file.close()


if __name__ == "__main__":
    main()
