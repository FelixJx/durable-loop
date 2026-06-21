#!/usr/bin/env python3
#
# durable_loop_checkpoint.py — Stop hook: auto-sync budget/idempotency/decisions.
#
# Solves three soft-constraint failures found in the hqt-phase0 audit:
#   1. budget_used.tokens/dollars stuck at 0  →  count tokens from transcript
#   2. idempotency_keys empty despite writes   →  rebuild keys from tool_use log
#   3. decisions.log vs checkpoint.decisions_made drift (3 vs 2) →  sync
#
# Hook: Stop (fires when the agent finishes a turn / session goes idle)
# stdin (Claude Code Stop protocol):
#   {"session_id","transcript_path","cwd","hook_event_name":"Stop",...}
# Exit 0 always (Stop hooks cannot block; they observe/mutate state files only).
#
# Design: FULL-REBUILD (idempotent). Every Stop re-derives tokens/idempotency/
# decisions from the entire transcript and merges into checkpoint.json. No delta
# state file needed — re-running on the same transcript yields the same result.
# This is the ONLY hook that writes checkpoint.json, so no concurrent-write risk
# with durable_loop_observe.py (which only appends session.log).
#
# Atomic write: checkpoint.json.tmp → mv. Fail-open on any error.

import json
import os
import re
import sys
import datetime
import hashlib
from pathlib import Path


# Loop statuses meaning "not actively iterating". Hooks treat these as INACTIVE
# and no-op, so a stranded paused/finished checkpoint in a parent dir is not
# polluted by unrelated sessions. Unknown/missing status => treated as ACTIVE.
INACTIVE_STATUSES = frozenset({
    "paused", "paused_for_approval", "completed", "done",
    "stopped", "aborted", "succeeded", "failed",
})


# --- model pricing (USD per 1,000,000 tokens) ---------------------------
# Used ONLY to estimate budget_used.dollars from transcript usage — this is an
# approximation for guardrails, not a billing source. Claude values are public
# Anthropic API list prices; GLM values are rough estimates for the z.ai global
# endpoint. Tune to your provider. Keys are matched as lowercase substrings of
# the transcript's `model` field, so e.g. "claude-opus-4-8" -> "claude-opus".
MODEL_PRICING = {
    "claude-opus":   {"input": 15.0,  "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "claude-sonnet": {"input": 3.0,   "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-haiku":  {"input": 1.0,   "output": 5.0,  "cache_read": 0.10, "cache_creation": 1.25},
    "claude-fable":  {"input": 3.0,   "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "glm-":          {"input": 0.60,  "output": 2.20, "cache_read": 0.10, "cache_creation": 0.75},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75}


def price_for_model(model: str) -> dict:
    m = (model or "").lower()
    for key, rates in MODEL_PRICING.items():
        if key in m:
            return rates
    return DEFAULT_PRICING


def cost_for_usage(model: str, usage: dict) -> float:
    """Estimate USD cost of one assistant message's token usage."""
    rates = price_for_model(model)
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return (
        inp * rates["input"]
        + out * rates["output"]
        + cr * rates["cache_read"]
        + cc * rates["cache_creation"]
    ) / 1_000_000.0





def read_stdin() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def discover_checkpoint(cwd: str):
    """Return checkpoint_path or None (fail open when no single active loop)."""
    feature = os.environ.get("DURABLE_LOOP_FEATURE")
    if feature:
        root = Path(os.environ.get("DURABLE_LOOP_PROJECT_DIR") or cwd)
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
            print("[durable_loop_checkpoint] WARN: >1 feature under .scratch/ found ("
                  + ", ".join(c.parent.name for c in cps)
                  + ") — checkpoint sync is NO-OP. Set DURABLE_LOOP_FEATURE=<name> "
                  + "to re-enable for one loop.", file=sys.stderr)
            return None  # ambiguous
    return None


def parse_transcript(transcript_path: str):
    """Walk transcript jsonl. Return (total_tokens, total_dollars, side_effect_keys)."""
    total_tokens = 0
    total_dollars = 0.0
    side_effect_keys = []
    if not transcript_path or not os.path.exists(transcript_path):
        return 0, 0.0, []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(msg, dict):
                continue
            # 1. token usage from assistant messages
            usage = msg.get("usage") or {}
            if isinstance(usage, dict):
                total_tokens += (
                    int(usage.get("input_tokens", 0) or 0)
                    + int(usage.get("output_tokens", 0) or 0)
                    + int(usage.get("cache_read_input_tokens", 0) or 0)
                    + int(usage.get("cache_creation_input_tokens", 0) or 0)
                )
                model = msg.get("model") or entry.get("model") or ""
                total_dollars += cost_for_usage(model, usage)
            # 2. side-effect tool_use blocks → idempotency keys
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp = block.get("input", {}) or {}
                    key = _derive_side_effect_key(name, inp)
                    if key:
                        side_effect_keys.append(key)
    return total_tokens, total_dollars, side_effect_keys


def first_transcript_ts(transcript_path: str) -> str:
    """Return the timestamp of the first entry in the transcript, or '' if none."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                ts = entry.get("timestamp") if isinstance(entry, dict) else None
                if ts:
                    return str(ts)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return ""


# Patterns that mark a shell command as a non-idempotent side effect. Matched on
# the raw command (compiled IGNORECASE). Word-boundary anchored so 'install' does
# not match 'installer'. Covers VCS, HTTP mutations, package install/publish, DB
# migrations, containers/IaC, cloud CLIs, and shelled-out PowerShell.
_SIDE_EFFECT_PATTERNS = [
    # --- VCS ---
    r"\bgit\s+(commit|push|tag\s+-a|amend|rebase)\b",
    r"\bgh\s+(pr\s+create|release\s+create|api\s+.+(post|put|patch|delete))\b",
    # --- HTTP mutations (any client, with or without explicit -X) ---
    # NOTE: match -X / --request on the ORIGINAL case — `curl -x` is --proxy, not the
    # method, so we must not lowercase-before-matching the method flag.
    r"\bcurl\b[^\n]*(-X\s*(POST|PUT|PATCH|DELETE)\b|--request\s+(POST|PUT|PATCH|DELETE)\b|-d\b|--data\b|--data-raw\b|-F\b|--form\b)",
    r"\bwget\b[^\n]*--post-data\b",
    r"\binvoke-restmethod\b[^\n]*-method\b",
    r"\binvoke-webrequest\b[^\n]*-method\b",
    r"\b(requests|httpx|urllib\d?|aiohttp)\.[a-z]+\.(post|put|patch|delete)\b",
    r"\bfetch\s*\([^)]*method\s*[:=]\s*['\"]?(post|put|patch|delete)",
    # --- package install / publish ---
    r"\b(pip|pip3|uv|poetry)\s+(install|publish)\b",
    r"\b(npm|yarn|pnpm)\s+(install|publish|i\b|add\b|ci\b)\b",
    r"\btwine\s+upload\b", r"\bcargo\s+publish\b",
    r"\bdotnet\s+nuget\s+push\b", r"\bmvn\s+deploy\b", r"\bgradle\s+publish\b",
    # --- DB migrations ---
    r"\balembic\s+(upgrade|downgrade)\b",
    r"\bprisma\s+(migrate|db\s+push)\b", r"\bknex\s+migrate\b",
    r"\b(rails|bundle\s+exec\s+rails)\s+db:migrate\b",
    r"\bmanage\.py\s+migrate\b", r"\bdjango-admin\s+migrate\b",
    r"\bflyway\s+(migrate|clean)\b", r"\bliquibase\s+update\b",
    # --- containers / IaC mutations ---
    r"\bdocker\s+(push|compose\s+up|swarm)\b",
    r"\b(terraform|tf)\s+(apply|destroy|import)\b",
    r"\bhelm\s+(install|upgrade|uninstall)\b",
    r"\bkubectl\s+(apply|create|replace|patch|delete|rollout|scale)\b",
    r"\bpulumi\s+(up|destroy|refresh)\b",
    # --- cloud CLIs that mutate ---
    r"\baws\s+[a-z0-9-]+\s+(create|update|delete|deploy|run|put|invoke|terminate)\b",
    r"\bgcloud\s+[^\n]*(deploy|instances\s+create)\b",
    r"\baz\s+[a-z]+\s+(create|update|delete|deploy)\b",
    # --- shelled-out PowerShell (Windows primary shell) ---
    r"\bpowershell(exe)?\b", r"\bpwsh(exe)?\b",
]
_SIDE_EFFECT_RE = re.compile("|".join(_SIDE_EFFECT_PATTERNS), re.IGNORECASE)

# MCP tool names whose command is a mutation by definition (PowerShell can do
# anything), so any non-empty invocation is treated as a side effect.
_POWERSHELL_TOOLS = ("mcp__windows-mcp__PowerShell", "mcp__windows-mcp__App")


def _is_side_effect_command(cmd: str) -> bool:
    """True if cmd looks like a non-idempotent mutation (network write, DB
    migration, publish, infra change, or PowerShell shelled out)."""
    if not cmd:
        return False
    return bool(_SIDE_EFFECT_RE.search(cmd))


def _derive_side_effect_key(tool_name: str, tool_input: dict) -> str:
    """Derive an idempotency key for a side-effecting tool call. '' if not a side effect."""
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        fp = str(tool_input.get("file_path") or tool_input.get("path") or "unknown")
        # basename keeps it human-readable; the 8-char path hash disambiguates
        # files that share a name in different dirs (src/config.py vs tests/config.py).
        return f"write-{Path(fp).name}-{hashlib.sha256(fp.encode('utf-8')).hexdigest()[:8]}"
    if tool_name in _POWERSHELL_TOOLS:
        # PowerShell via MCP can do any mutation, so any non-empty invocation counts.
        cmd = str(tool_input.get("command", ""))
        if cmd.strip():
            return f"exec-{hashlib.sha256(cmd.encode('utf-8')).hexdigest()[:8]}"
        return ""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if _is_side_effect_command(cmd):
            return f"exec-{hashlib.sha256(cmd.encode('utf-8')).hexdigest()[:8]}"
    return ""


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX


def main() -> int:
    ev = read_stdin()
    cwd = ev.get("cwd") or os.getcwd()
    cp_path = discover_checkpoint(cwd)
    if cp_path is None:
        return 0  # no active loop — fail open

    try:
        cp = json.loads(cp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return 0  # unreadable checkpoint — don't make it worse

    # Scope guard: only sync state for an ACTIVELY-RUNNING loop. A paused/finished
    # checkpoint stranded in a parent dir (e.g. ~/.scratch) would otherwise be
    # auto-discovered by every unrelated session and have THAT session's tokens/
    # dollars written into it — corrupting the paused loop's observability and
    # historically tripping false budget blocks. Unknown/missing status => active.
    if str(cp.get("status", "")).strip() in INACTIVE_STATUSES:
        return 0

    feature_dir = cp_path.parent
    transcript = ev.get("transcript_path", "")
    total_tokens, total_dollars, side_keys = parse_transcript(transcript)

    # 1. budget: tokens (from transcript) + dollars (estimated) + hours (from started_at)
    budget_used = cp.setdefault("budget_used", {})
    if isinstance(budget_used, dict):
        budget_used["tokens"] = total_tokens
        budget_used["dollars"] = round(total_dollars, 6)
        started = cp.get("started_at", "")
        if not started:
            # fresh start forgot to fill started_at — derive from transcript's
            # first timestamp, else now. Locks the timer so hours accumulates.
            started = first_transcript_ts(transcript) or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            cp["started_at"] = started
        try:
            st = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            now = datetime.datetime.now(st.tzinfo) if st.tzinfo else datetime.datetime.now()
            budget_used["hours"] = round((now - st).total_seconds() / 3600.0, 2)
        except (ValueError, TypeError):
            pass

    # 2. idempotency: union of existing keys + transcript-derived side-effect keys
    existing_keys = cp.get("idempotency_keys", [])
    if not isinstance(existing_keys, list):
        existing_keys = []
    merged = list(dict.fromkeys(existing_keys + side_keys))  # dedupe, preserve order
    cp["idempotency_keys"] = merged

    # NOTE: decisions.log ↔ checkpoint.decisions_made sync is intentionally NOT
    # done here. The two sources use different formats (full ADR text in the log
    # vs short summary in the checkpoint), so string-merge produces duplicates
    # (verified: 2+3 → 5 with dupes). The driver_prompt enforces "write each
    # decision to BOTH sources in the SAME format" as the contract; this hook
    # only handles what it can derive mechanically (tokens / idempotency / hours).

    # 3. atomic write back
    cp["last_updated"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_json(cp_path, cp)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — Stop hook must never break the session
        print(f"[durable_loop_checkpoint] fail-open: {exc}", file=sys.stderr)
        sys.exit(0)
