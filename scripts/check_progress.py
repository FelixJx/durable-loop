#!/usr/bin/env python3
#
# check_progress.py — no-progress detector for durable-loop (DEFAULT OFF).
#
# Improvement #4: if the loop makes NO substantive progress across N adjacent
# iterations, pause it for human-in-the-loop (HITL) review instead of letting it
# spin forever on a plateau. This is an anti-stuck QUALITY guard, NOT a budget cap
# — it never bounds tokens/dollars/time, it only notices "the convergence signal
# stopped moving" and asks a human to look.
#
# ⚠️ DEFAULT OFF. durable-loop's default is pure quality convergence with no
# brakes. This detector only engages when EXPLICITLY enabled via either:
#     env  DURABLE_LOOP_NOPROGRESS_N = <N>        (N>0 adjacent rounds)
#     or   checkpoint field  no_progress_limit    (N>0)
# When neither is set (or <= 0) this is a pure no-op (exit 0). The env var wins
# over the checkpoint field when both are present.
#
# Dual entry point:
#   1. CLI:       python scripts/check_progress.py <feature> [project_dir]
#                 Exit 0 = ran (no-op OR recorded progress OR paused). Exit 2 =
#                 usage error (bad feature name / missing project_dir). The pause
#                 itself is NOT signalled via a nonzero exit (see Stop note below).
#   2. Stop hook: Claude Code invokes with JSON on stdin
#                 {"session_id","transcript_path","cwd","hook_event_name":"Stop",...}
#                 Stop hooks cannot block; we only observe + mutate state files, so
#                 we ALWAYS exit 0. The pause is communicated by writing
#                 pending_approval.json and setting checkpoint.status, which the
#                 driver prompt / other hooks already honor (paused_for_approval).
#
# Mechanism: the checkpoint holds only the CURRENT progress snapshot, so to compare
# "adjacent N rounds" we keep our own append-only history file:
#     .scratch/<feature>/progress_history.json
# Each run extracts a progress SIGNAL from checkpoint.json (last_result +
# cumulative_state.metrics_snapshot + verify_history quality dimension), and only
# appends a new history entry when checkpoint.iteration advances (so multiple Stop
# events inside one iteration don't fake "progress"/"no-progress"). If the last N
# recorded signals are byte-for-byte identical → no progress → pause for approval.
#
# FAIL-OPEN everywhere: no .scratch/<feature>/, unreadable/unparseable checkpoint,
# ambiguous (>1) feature, or any unexpected error → silent no-op (exit 0). This
# hook must never block or crash an unrelated session.

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Loop statuses meaning "not actively iterating" — mirrors durable_loop_observe.py
# / durable_loop_checkpoint.py. We never (re)pause a paused/finished loop, and we
# don't append history for it. Unknown/missing status => treated as ACTIVE.
INACTIVE_STATUSES = frozenset({
    "paused", "paused_for_approval", "completed", "done",
    "stopped", "aborted", "succeeded", "failed",
})

PAUSED_STATUS = "paused_for_approval"

# CLI exit codes. The Stop-hook path always exits 0 (see module docstring).
EXIT_OK = 0
EXIT_USAGE = 2


def warn(msg: str) -> None:
    print(f"[check_progress] {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":
    # CLI usage errors only (bad args). Hook/runtime problems fail open instead.
    print(f"check_progress.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(EXIT_USAGE)


def read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def discover_checkpoint(feature, project_dir, cwd):
    """Return checkpoint_path or None (fail open when no single active loop).

    Mirrors durable_loop_checkpoint.discover_checkpoint discovery order:
      1. explicit feature (CLI arg or DURABLE_LOOP_FEATURE) under <root>/.scratch/
      2. upward search from cwd for exactly one .scratch/*/checkpoint.json
    Ambiguous (>1) or none => None (no-op)."""
    feature = feature or os.environ.get("DURABLE_LOOP_FEATURE")
    if feature:
        if not NAME_RE.match(feature):
            return None
        root = Path(project_dir or os.environ.get("DURABLE_LOOP_PROJECT_DIR") or cwd)
        cp = root / ".scratch" / feature / "checkpoint.json"
        return cp if cp.exists() else None
    cur = Path(cwd).resolve()
    for candidate in [cur, *cur.parents]:
        scratch = candidate / ".scratch"
        if not scratch.is_dir():
            continue
        cps = list(scratch.glob("*/checkpoint.json"))
        if len(cps) == 1:
            return cps[0]
        if len(cps) > 1:
            warn(">1 feature under .scratch/ found ("
                 + ", ".join(c.parent.name for c in cps)
                 + ") — no-progress detector is NO-OP. Set DURABLE_LOOP_FEATURE=<name> "
                 + "to re-enable for one loop.")
            return None  # ambiguous — fail open
    return None


def resolve_limit(cp: dict) -> int:
    """N adjacent no-progress rounds before pausing. 0/absent => disabled (no-op).

    env DURABLE_LOOP_NOPROGRESS_N wins over checkpoint field no_progress_limit.
    Any non-int / negative value degrades to 0 (disabled) — fail open, never crash."""
    env_val = os.environ.get("DURABLE_LOOP_NOPROGRESS_N")
    if env_val is not None and str(env_val).strip() != "":
        try:
            n = int(str(env_val).strip())
            return n if n > 0 else 0
        except (TypeError, ValueError):
            return 0  # garbage env => treat as disabled, don't fall through to field
    try:
        n = int(cp.get("no_progress_limit", 0) or 0)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def progress_signal(cp: dict) -> str:
    """Build a stable, comparable string from the checkpoint's progress signals.

    Combines the dimensions that move when the loop is actually advancing:
      - last_result                              (what the latest iteration achieved)
      - cumulative_state.metrics_snapshot        (quality metrics — the convergence target)
      - verify_history quality dimension         (per-round verdict/quality, if present)
    Serialized deterministically (sorted keys) so two identical states compare equal.
    Defensive: every field is optional and may be missing on older checkpoints."""
    cs = cp.get("cumulative_state")
    if not isinstance(cs, dict):
        cs = {}
    metrics = cs.get("metrics_snapshot")
    if not isinstance(metrics, dict):
        metrics = {}

    # verify_history: optional list (see verify_done.py docstring). Extract the
    # quality-bearing fields of the LATEST entry only — the streak of identical
    # latest verdicts across iterations is what "no progress" means here.
    vh_quality = None
    vh = cp.get("verify_history")
    if isinstance(vh, list) and vh:
        last = vh[-1]
        if isinstance(last, dict):
            vh_quality = {
                k: last.get(k)
                for k in ("verdict", "quality", "pass_count", "fail_count", "score")
                if k in last
            }
        else:
            vh_quality = last  # primitive verdict (e.g. "PASS"/"NOT DONE")

    signal = {
        "last_result": cp.get("last_result", ""),
        "metrics_snapshot": metrics,
        "verify_quality": vh_quality,
    }
    try:
        return json.dumps(signal, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(signal)  # last-ditch: never raise on weird payloads


def load_history(history_path: Path) -> list:
    """Read progress_history.json -> list of records. Fail open to []."""
    if not history_path.is_file():
        return []
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX; best-effort on Windows


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def evaluate(cp_path: Path) -> str:
    """Core logic. Returns a short status string for the CLI to print.

    Returns one of: 'disabled', 'inactive', 'recorded', 'no-progress', 'paused',
    'insufficient-history'. Never raises on expected I/O/parse errors (fail open)."""
    try:
        cp = json.loads(cp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return "disabled"  # unreadable checkpoint — fail open, don't make it worse
    if not isinstance(cp, dict):
        return "disabled"

    limit = resolve_limit(cp)
    if limit <= 0:
        return "disabled"  # default OFF — no env / field opt-in

    # Never (re)act on a paused/finished loop: don't append history, don't re-pause.
    if str(cp.get("status", "")).strip() in INACTIVE_STATUSES:
        return "inactive"

    feature_dir = cp_path.parent
    history_path = feature_dir / "progress_history.json"
    history = load_history(history_path)

    signal = progress_signal(cp)
    try:
        iteration = int(cp.get("iteration", 0) or 0)
    except (TypeError, ValueError):
        iteration = 0

    # Only record one history point PER ITERATION. Multiple Stop events within the
    # same iteration must not be counted as separate "rounds" (that would both
    # fake no-progress AND fake progress depending on timing). If this iteration is
    # already the latest recorded one, refresh its signal in place instead of
    # appending — the snapshot may have been updated mid-iteration.
    if history and history[-1].get("iteration") == iteration:
        history[-1] = {"iteration": iteration, "signal": signal, "ts": now_iso()}
        appended = False
    else:
        history.append({"iteration": iteration, "signal": signal, "ts": now_iso()})
        appended = True

    # Cap history growth (keep a little more than the window for safety).
    max_keep = max(limit * 4, 16)
    if len(history) > max_keep:
        history = history[-max_keep:]
    atomic_write_json(history_path, history)

    # Need N distinct recorded rounds to judge N adjacent rounds of no progress.
    if len(history) < limit:
        return "insufficient-history" if appended else "recorded"

    recent = history[-limit:]
    signals = {r.get("signal") for r in recent}
    if len(signals) == 1:
        # All N adjacent rounds share the identical progress signal => no progress.
        pause_for_approval(cp_path, cp, limit, recent)
        return "paused"

    return "recorded"


def pause_for_approval(cp_path: Path, cp: dict, limit: int, recent: list) -> None:
    """Write pending_approval.json and flip checkpoint.status to paused_for_approval.

    Both writes are best-effort/fail-open; a failure here must not crash the hook."""
    feature_dir = cp_path.parent
    feature = cp.get("feature") or feature_dir.name
    last_iter = recent[-1].get("iteration")
    first_iter = recent[0].get("iteration")
    reason = (
        f"no-progress: the loop's convergence signal (last_result + "
        f"metrics_snapshot + verify quality) was identical across {limit} adjacent "
        f"iterations (iter {first_iter}..{last_iter}). Paused for human review — "
        f"inspect handoff.md, decide whether to change strategy, reset context, or "
        f"declare done, then clear pending_approval.json and reset status to resume."
    )

    pending = {
        "feature": feature,
        "reason": reason,
        "detector": "check_progress",
        "no_progress_limit": limit,
        "iterations": [r.get("iteration") for r in recent],
        "stuck_signal": recent[-1].get("signal", ""),
        "created_at": now_iso(),
        "status": "pending_approval",
    }
    try:
        atomic_write_json(feature_dir / "pending_approval.json", pending)
    except OSError as exc:
        warn(f"could not write pending_approval.json (failing open): {exc}")

    # Flip status so the existing HITL machinery (driver prompt + observe/checkpoint
    # INACTIVE_STATUSES) treats the loop as paused. Preserve the prior status so a
    # human can see/restore it.
    try:
        prev = str(cp.get("status", ""))
        if prev != PAUSED_STATUS:
            cp["status"] = PAUSED_STATUS
            cp["paused_reason"] = reason
            cp["status_before_pause"] = prev
            cp["last_updated"] = now_iso()
            atomic_write_json(cp_path, cp)
    except OSError as exc:
        warn(f"could not update checkpoint status (failing open): {exc}")

    print(f"[check_progress] PAUSED feature='{feature}' for approval: {reason}",
          file=sys.stderr)


def run(feature, project_dir, cwd) -> int:
    cp_path = discover_checkpoint(feature, project_dir, cwd)
    if cp_path is None:
        return EXIT_OK  # no single active loop — fail open / no-op
    status = evaluate(cp_path)
    # CLI-friendly one-liner on stdout; hooks ignore stdout.
    print(f"[check_progress] {status} ({cp_path.parent.name})")
    return EXIT_OK


def main() -> int:
    # Distinguish CLI invocation (argv has a feature) from Stop-hook invocation
    # (no argv, JSON on stdin). argparse handles the CLI; stdin handles the hook.
    if len(sys.argv) > 1:
        ap = argparse.ArgumentParser(
            description="No-progress detector for durable-loop (default OFF).")
        ap.add_argument("feature", help="name matching .scratch/<feature>/")
        ap.add_argument("project_dir", nargs="?", default=None,
                        help="project root (default: cwd / DURABLE_LOOP_PROJECT_DIR)")
        args = ap.parse_args()
        if not NAME_RE.match(args.feature):
            die(f"invalid feature name '{args.feature}'")
        return run(args.feature, args.project_dir, os.getcwd())

    # Stop-hook path: discover from stdin's cwd. Always exit 0.
    ev = read_stdin_json()
    cwd = ev.get("cwd") or os.getcwd()
    return run(None, None, cwd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise  # argparse / die() usage exits pass through unchanged
    except Exception as exc:  # noqa: BLE001 — hook must never break the session
        warn(f"crashed (failing open): {exc}")
        sys.exit(EXIT_OK)
