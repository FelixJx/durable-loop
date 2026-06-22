#!/usr/bin/env python3
#
# durable_loop_observe.py — PostToolUse hook: auto-append command-level session.log.
#
# Solves the "session.log 1 line / event-label not command-level" failure found
# in the hqt-phase0 audit. The actor no longer has to remember to log — every
# tool call is logged automatically at command granularity, so session.log can
# actually reconstruct what the agent did.
#
# Hook: PostToolUse (fires after every tool call)
# stdin (Claude Code PostToolUse protocol):
#   {"session_id","transcript_path","cwd","hook_event_name",
#    "tool_name","tool_input":{...},"tool_response":{...}}
# Exit 0 ALWAYS (observe-only, never blocks the agent).
#
# Feature discovery (mirrors check_budget.py):
#   env DURABLE_LOOP_FEATURE  →  <cwd>/.scratch/<feature>/
#   else upward search for a single .scratch/*/checkpoint.json
#   Fail open (exit 0, no-op) when no durable-loop feature is active, so this
#   hook is safe to install globally — it only writes when a loop is running.
#
# Writes one JSON line per tool call to .scratch/<feature>/session.log:
#   {"ts","run_id","iter","tool","action","resp","phase"}
# run_id/iter/phase read from checkpoint.json so the log stays in sync with
# state. run_id lets replay_trace.py group a session.log into distinct runs
# (fresh starts) even when the same .scratch/<feature>/ is reused across crashes
# and resumes. Missing run_id (older checkpoints) degrades to "" — backward
# compatible.

import json
import os
import sys
import datetime
from pathlib import Path


# Loop statuses meaning "not actively iterating". When the discovered checkpoint
# is in one of these states the hook no-ops, so a stranded paused/finished loop
# in a parent dir (e.g. ~/.scratch) is not flooded with unrelated sessions'
# tool calls. Unknown/missing status => treated as ACTIVE (log as before).
INACTIVE_STATUSES = frozenset({
    "paused", "paused_for_approval", "completed", "done",
    "stopped", "aborted", "succeeded", "failed",
})


def read_stdin() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def discover_feature(cwd: str):
    """Return (session_log_path, checkpoint_path) or (None, None) if no active loop."""
    feature = os.environ.get("DURABLE_LOOP_FEATURE")
    if feature:
        root = Path(os.environ.get("DURABLE_LOOP_PROJECT_DIR") or cwd)
        sd = root / ".scratch"
        return sd / feature / "session.log", sd / feature / "checkpoint.json"
    cur = Path(cwd).resolve()
    for candidate in [cur, *cur.parents]:
        scratch = candidate / ".scratch"
        if not scratch.is_dir():
            continue
        cps = list(scratch.glob("*/checkpoint.json"))
        if len(cps) == 1:
            return cps[0].parent / "session.log", cps[0]
        if len(cps) > 1:
            print("[durable_loop_observe] WARN: >1 feature under .scratch/ found — "
                  "session.log append is NO-OP. Set DURABLE_LOOP_FEATURE=<name> to "
                  "re-enable for one loop.", file=sys.stderr)
            return None, None  # ambiguous — fail open
    return None, None


def summarize(tool_name: str, tool_input: dict, tool_response) -> tuple:
    """Build a command-level action string + short response summary."""
    ti = tool_input or {}
    action = tool_name
    for k in ("file_path", "path", "command", "cmd", "pattern", "query", "url", "script"):
        v = ti.get(k)
        if v:
            action = f"{tool_name} {str(v)[:100]}"
            break

    tr = tool_response
    resp = ""
    if isinstance(tr, dict):
        content = tr.get("content")
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            resp = (texts[0][:140] if texts else "ok")
        elif "error" in tr:
            resp = f"error: {str(tr['error'])[:140]}"
        elif "output" in tr:
            resp = str(tr.get("output", ""))[:140] or "ok"
        else:
            resp = "ok"
    elif isinstance(tr, str):
        resp = tr[:140]
    else:
        resp = "ok"
    return action, resp


def read_iter_phase(cp_path: Path):
    """Return (iteration, phase, run_id) from the checkpoint. run_id defaults to
    "" for older checkpoints that predate the field (backward compatible)."""
    if not cp_path or not cp_path.exists():
        return "?", "?", ""
    try:
        d = json.loads(cp_path.read_text(encoding="utf-8"))
        return d.get("iteration", "?"), d.get("phase", "?"), d.get("run_id", "")
    except (OSError, json.JSONDecodeError, ValueError):
        return "?", "?", ""


def main() -> int:
    ev = read_stdin()
    cwd = ev.get("cwd") or os.getcwd()
    sess_path, cp_path = discover_feature(cwd)
    if sess_path is None:
        return 0  # no active durable-loop feature — fail open

    # Scope guard: skip paused/finished loops so unrelated sessions don't flood a
    # stranded checkpoint's session.log. Unknown/missing status => log as before.
    if cp_path and cp_path.exists():
        try:
            st = json.loads(cp_path.read_text(encoding="utf-8")).get("status", "")
            if str(st).strip() in INACTIVE_STATUSES:
                return 0
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    tool_name = ev.get("tool_name", "?")
    tool_input = ev.get("tool_input", {}) or {}
    tool_response = ev.get("tool_response")
    action, resp = summarize(tool_name, tool_input, tool_response)
    it, ph, run_id = read_iter_phase(cp_path)
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    entry = {
        "ts": ts,
        "run_id": run_id,
        "iter": it,
        "tool": tool_name,
        "action": action,
        "resp": resp,
        "phase": ph,
    }
    # POSIX O_APPEND makes small writes atomic — safe even if multiple PostToolUse
    # events fire in quick succession.
    sess_path.parent.mkdir(parents=True, exist_ok=True)
    with open(sess_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — observe hook must never break the session
        print(f"[durable_loop_observe] fail-open: {exc}", file=sys.stderr)
        sys.exit(0)
