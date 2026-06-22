#!/usr/bin/env python3
#
# replay_trace.py — render .scratch/<feature>/session.log into a readable timeline.
#
# The PostToolUse observe hook (durable_loop_observe.py) appends one JSON line per
# tool call to session.log, each tagged with run_id / iter / phase / tool / action.
# This CLI reads that JSONL trace and renders a human-readable timeline: grouped by
# run_id (a fresh start = one run; the same .scratch/<feature>/ may accumulate
# several runs across crashes/resumes) then by iteration, summarizing per-iteration
# tool-call counts, phase transitions, and — if the log carries cost/timing fields
# — a cost/duration rollup.
#
# Usage:
#   python replay_trace.py <feature> [project_dir]
#
# Reads <project_dir>/.scratch/<feature>/session.log (default project_dir: cwd).
#
# Fail-open / friendly: a missing .scratch/<feature>/ dir or a missing/empty
# session.log is NOT an error — it prints a friendly hint and exits 0. Malformed
# (non-JSON) lines are tolerated and counted, never fatal. This is a read-only
# reporter; it never writes, never blocks, and never raises into the caller.
#
# Exit 0 always on a well-formed invocation (even with no log). Exit 2 only on a
# usage error (bad feature name / missing project_dir), matching verify_done.py.

import argparse
import json
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Fields that, if present on a log line, are summed into the cost/timing rollup.
# (key -> human label). Absent across all lines => the rollup is omitted.
COST_FIELDS = (
    ("cost", "cost"),
    ("dollars", "dollars"),
    ("tokens", "tokens"),
    ("duration", "duration_s"),
    ("duration_s", "duration_s"),
    ("elapsed", "duration_s"),
    ("ms", "ms"),
)


def die(msg: str) -> "NoReturn":
    print(f"replay_trace.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def load_log(session_log: Path):
    """Return (records, malformed_count). records is a list of dicts in file
    order. Lines that are blank are skipped; lines that are not JSON objects are
    counted as malformed but otherwise ignored (fail-soft)."""
    records = []
    malformed = 0
    for line in session_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            malformed += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            malformed += 1
    return records, malformed


def _as_str(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def group_runs(records):
    """Group records by run_id, preserving first-seen order. Records with no
    run_id (older logs) collapse into a single '(no run_id)' bucket so legacy
    logs still render. Returns a list of (run_id, [records])."""
    order = []
    buckets = {}
    for r in records:
        rid = _as_str(r.get("run_id")) or "(no run_id)"
        if rid not in buckets:
            buckets[rid] = []
            order.append(rid)
        buckets[rid].append(r)
    return [(rid, buckets[rid]) for rid in order]


def group_iters(run_records):
    """Within one run, group by iter preserving first-seen order.
    Returns a list of (iter_value, [records])."""
    order = []
    buckets = {}
    for r in run_records:
        it = _as_str(r.get("iter")) or "?"
        if it not in buckets:
            buckets[it] = []
            order.append(it)
        buckets[it].append(r)
    return [(it, buckets[it]) for it in order]


def phase_transitions(run_records):
    """Ordered list of distinct phases as they first appear / change."""
    seq = []
    for r in run_records:
        ph = _as_str(r.get("phase"))
        if not ph or ph == "?":
            continue
        if not seq or seq[-1] != ph:
            seq.append(ph)
    return seq


def cost_rollup(records):
    """Sum any numeric cost/timing fields present. Returns {label: total} for
    labels that actually appeared, else {}."""
    totals = {}
    for r in records:
        for key, label in COST_FIELDS:
            if key not in r:
                continue
            v = r.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                totals[label] = totals.get(label, 0) + v
    return totals


def render(feature: str, session_log: Path, records, malformed) -> None:
    print(f"== replay_trace: feature='{feature}' log='{session_log}' ==")
    print(f"   {len(records)} event(s)"
          + (f", {malformed} malformed line(s) skipped" if malformed else ""))
    print()

    runs = group_runs(records)
    for ri, (rid, run_recs) in enumerate(runs, 1):
        print(f"run {ri}/{len(runs)}  run_id={rid}  ({len(run_recs)} event(s))")
        phases = phase_transitions(run_recs)
        if phases:
            print("  phases: " + " -> ".join(phases))
        for it, it_recs in group_iters(run_recs):
            tools = {}
            for r in it_recs:
                t = _as_str(r.get("tool")) or "?"
                tools[t] = tools.get(t, 0) + 1
            tool_summary = ", ".join(f"{t}x{n}" for t, n in
                                     sorted(tools.items(), key=lambda kv: (-kv[1], kv[0])))
            print(f"  iter {it}: {len(it_recs)} call(s)" + (f"  [{tool_summary}]" if tool_summary else ""))
        rollup = cost_rollup(run_recs)
        if rollup:
            parts = ", ".join(f"{label}={_fmt_num(val)}" for label, val in sorted(rollup.items()))
            print(f"  cost/timing: {parts}")
        print()

    grand = cost_rollup(records)
    if grand:
        parts = ", ".join(f"{label}={_fmt_num(val)}" for label, val in sorted(grand.items()))
        print("-------------------------------------------")
        print(f"TOTAL cost/timing: {parts}")


def _fmt_num(v):
    if isinstance(v, float):
        # trim trailing zeros without scientific notation noise
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a durable-loop session.log into a readable timeline.")
    ap.add_argument("feature", help="name matching .scratch/<feature>/")
    ap.add_argument("project_dir", nargs="?", default=".", help="project root (default: cwd)")
    args = ap.parse_args()

    if not NAME_RE.match(args.feature):
        die(f"invalid feature name '{args.feature}'")

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project_dir does not exist: {project_dir}")

    feature_dir = project_dir / ".scratch" / args.feature
    session_log = feature_dir / "session.log"

    # Fail-open friendliness: no dir / no log / empty log is informational, not an error.
    if not feature_dir.is_dir():
        print(f"replay_trace: no .scratch/{args.feature}/ under {project_dir} — nothing to replay.")
        print("  Run init_loop.py first, then let the loop run so the observe hook fills session.log.")
        return 0
    if not session_log.is_file():
        print(f"replay_trace: no session.log in {feature_dir} — nothing to replay yet.")
        return 0

    records, malformed = load_log(session_log)
    if not records:
        if malformed:
            print(f"replay_trace: session.log has {malformed} unparseable line(s) and no readable events yet.")
        else:
            print(f"replay_trace: session.log in {feature_dir} is empty — no tool calls logged yet.")
        return 0

    render(args.feature, session_log, records, malformed)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — reporter must never raise into the caller
        print(f"replay_trace.py: fail-open: {exc}", file=sys.stderr)
        sys.exit(0)
