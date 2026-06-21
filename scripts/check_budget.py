#!/usr/bin/env python3
# pylint: disable=missing-module-docstring
#
# ⚠️⚠️ UNWIRED 2026-06-20 — pure quality convergence. The PreToolUse hook that ran
# this script was REMOVED from settings.json (budget guardrail 2026-06-19, thrashing
# 2026-06-20). Retained as reference for optional re-enable; NOT called by any hook
# now. The loop stops ONLY on verify_done quality convergence.
#
# check_budget.py — (formerly) PreToolUse THRASHING guard for durable-loop.
#
# ⚠️ BUDGET ENFORCEMENT REMOVED (2026-06-19, per user request: "only quality,
# no budget guardrail"). This hook used to block tool calls when token / dollar /
# iteration / hour usage hit its cap. That cap+block is GONE. Quality is now gated
# solely by verify_done (generator/evaluator separation). budget_used is still
# recorded in checkpoint.json for observability, but NOTHING blocks on it.
#
# What this hook STILL does: thrashing detection. If the loop is stuck re-attempting
# the same action across the last few ITERATIONS, block (exit 2) so the operator
# can intervene / reset context — this is an anti-stuck quality guard, not a budget.
#
# Usage (as a Claude Code PreToolUse hook):
#   Claude Code invokes hooks with JSON on stdin: {"session_id","tool_name","tool_input":{...}}
#   Exit codes (PreToolUse protocol):
#     0 = allow the tool call (optionally warn on stderr)
#     2 = BLOCK the tool call (stderr shown to the model)
#
# Thrashing: collapse session.log to one action per ITERATION (observe.py logs per
# tool call); if the avg pairwise similarity of the last 3 iterations' actions
# exceeds 0.90 -> block; > 0.70 -> warn.
#
# Feature / project discovery (first match wins):
#   a) tool_input fields: feature, scratch_dir, project_dir, checkpoint, .scratch
#   b) env vars: DURABLE_LOOP_FEATURE, DURABLE_LOOP_PROJECT_DIR, DURABLE_LOOP_SCRATCH
#   c) cwd: search upward for a .scratch/*/checkpoint.json (single-match only)
#   If none / ambiguous, the hook is a no-op (exit 0) — fail open.

import json
import os
import sys
from pathlib import Path
from difflib import SequenceMatcher


# --- tunables -------------------------------------------------------------
THRASH_WARN_AVG = 0.70         # avg pairwise similarity -> warn
THRASH_BLOCK_AVG = 0.90        # avg pairwise similarity -> block (exit 2)
THRASH_WINDOW = 3              # how many recent ITERATIONS to compare

# Exit codes per Claude Code PreToolUse contract.
EXIT_ALLOW = 0
EXIT_BLOCK = 2


def warn(msg: str) -> None:
    print(f"[check_budget] WARN: {msg}", file=sys.stderr)


def block(msg: str) -> None:
    print(f"[check_budget] BLOCKED: {msg}", file=sys.stderr)


def read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def discover_paths(hook_input: dict):
    """Return (checkpoint_path, session_log_path, scratch_dir) or (None,None,None).
    checkpoint_path is unused now (budget removed) but kept for discovery symmetry."""
    tool_input = hook_input.get("tool_input", {}) or {}

    feature = (
        tool_input.get("feature")
        or os.environ.get("DURABLE_LOOP_FEATURE")
        or tool_input.get("_durable_loop_feature")
    )
    scratch_dir = (
        tool_input.get("scratch_dir")
        or os.environ.get("DURABLE_LOOP_SCRATCH")
        or tool_input.get(".scratch")
    )
    project_dir = (
        tool_input.get("project_dir")
        or os.environ.get("DURABLE_LOOP_PROJECT_DIR")
        or os.getcwd()
    )

    checkpoint = tool_input.get("checkpoint") or os.environ.get("DURABLE_LOOP_CHECKPOINT")
    if checkpoint:
        cp = Path(checkpoint)
        return cp, cp.parent / "session.log", cp.parent

    if scratch_dir and feature:
        sd = Path(scratch_dir)
        return sd / feature / "checkpoint.json", sd / feature / "session.log", sd / feature

    if feature:
        sd = Path(project_dir) / ".scratch"
        return sd / feature / "checkpoint.json", sd / feature / "session.log", sd / feature

    cur = Path(project_dir).resolve()
    for candidate in [cur, *cur.parents]:
        scratch = candidate / ".scratch"
        if not scratch.is_dir():
            continue
        cps = list(scratch.glob("*/checkpoint.json"))
        if len(cps) == 1:
            cp = cps[0]
            return cp, cp.parent / "session.log", cp.parent
        if len(cps) > 1:
            warn(">1 feature under .scratch/ found ("
                 + ", ".join(c.parent.name for c in cps)
                 + ") — thrashing guard is NO-OP. Set DURABLE_LOOP_FEATURE=<name> "
                 + "to re-enable for one loop.")
            return None, None, None
    return None, None, None


def parse_session_action(line: str) -> str:
    """Extract the 'action' field from a session.log JSON line, else raw text."""
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
        action = obj.get("action") or obj.get("observation") or ""
        return str(action)
    except json.JSONDecodeError:
        return line


def avg_pairwise_ratio(texts: list) -> float:
    if len(texts) < 2:
        return 0.0
    ratios = []
    for i in range(len(texts) - 1):
        for j in range(i + 1, len(texts)):
            ratios.append(SequenceMatcher(None, texts[i], texts[j]).ratio())
    return sum(ratios) / len(ratios) if ratios else 0.0


def check_thrashing(session_log_path: Path) -> int:
    if not session_log_path.is_file():
        return EXIT_ALLOW
    try:
        lines = session_log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return EXIT_ALLOW

    # Collapse to one action per ITERATION (observe.py logs one line per tool call;
    # thrashing is defined across iterations, so comparing raw last-N tool calls
    # false-positives on legitimate same-iteration retries). Fall back to raw
    # actions only for legacy logs with no iteration info.
    by_iter = {}
    raw_actions = []
    for l in lines:
        l = l.strip()
        if not l:
            continue
        try:
            obj = json.loads(l)
        except json.JSONDecodeError:
            raw_actions.append(l)
            continue
        it = obj.get("iter")
        act = obj.get("action") or obj.get("observation")
        if it is not None and act:
            by_iter[str(it)] = str(act)
        elif act:
            raw_actions.append(str(act))
    recent = list(by_iter.values())[-THRASH_WINDOW:] if by_iter else raw_actions[-THRASH_WINDOW:]
    recent = [t for t in recent if t]
    if len(recent) < THRASH_WINDOW:
        return EXIT_ALLOW  # not enough history yet

    avg = avg_pairwise_ratio(recent)
    if avg >= THRASH_BLOCK_AVG:
        block(
            f"thrashing detected: last {THRASH_WINDOW} iterations are {avg*100:.0f}% "
            f"textually identical. The loop is stuck re-attempting the same thing — "
            f"do a full-context reset from handoff.md or escalate to a different strategy. "
            f"Recent actions: {recent}"
        )
        return EXIT_BLOCK
    if avg >= THRASH_WARN_AVG:
        warn(
            f"possible thrashing: last {THRASH_WINDOW} iterations are {avg*100:.0f}% similar."
        )
    return EXIT_ALLOW


def main() -> int:
    hook_input = read_stdin_json()
    # Only act when a feature/.scratch can be resolved; otherwise fail open.
    _, session_log_path, _ = discover_paths(hook_input)
    if session_log_path is None:
        return EXIT_ALLOW  # unrelated tool call / ambiguous — never block
    return check_thrashing(session_log_path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open on any unexpected error
        warn(f"hook crashed (failing open): {exc}")
        sys.exit(EXIT_ALLOW)
