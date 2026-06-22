#!/usr/bin/env python3
#
# durable_loop_guard.py — PreToolUse hook: execution-time idempotency gate +
# optional strict dangerous-operation hard-block.
#
# Two responsibilities, both BEFORE the tool actually runs:
#   1. IDEMPOTENCY GATE (always on when a loop is active): derive the side-effect
#      key for the tool call about to run (same logic as durable_loop_checkpoint.py
#      _derive_side_effect_key). If that key is already in
#      checkpoint.idempotency_keys, the side effect has ALREADY happened in a prior
#      iteration — deny the call so a crash-resume doesn't double-push / double-POST
#      / re-run a migration. (durable_loop_checkpoint.py records keys AFTER the fact
#      on Stop; this hook reads them back to prevent the replay.)
#   2. STRICT DANGEROUS-OP BLOCK (opt-in: env DURABLE_LOOP_STRICT=1 OR checkpoint
#      field strict_guard=true): destructive commands (git push --force, reset
#      --hard, clean -fd, branch -D, rm -rf, DROP TABLE, TRUNCATE, ...) are denied
#      and routed to pending_approval.json. OFF by default — durable-loop stays a
#      pure quality-convergence loop unless the operator opts in.
#
# Hook: PreToolUse (fires before every tool call)
# stdin (Claude Code PreToolUse protocol):
#   {"session_id","transcript_path","cwd","hook_event_name":"PreToolUse",
#    "tool_name","tool_input":{...}}
#
# Block protocol (Claude Code PreToolUse): we emit JSON on stdout (exit 0) with
#   {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#                          "permissionDecision":"deny",
#                          "permissionDecisionReason":"..."}}
# permissionDecision="deny" blocks the call and feeds the reason back to the model.
# Allow = no decision block (exit 0, empty stdout). This is the structured official
# protocol; exit-2+stderr is the legacy equivalent.
#
# FAIL-OPEN everywhere: no .scratch/<feature>/, unreadable/unparseable checkpoint,
# inactive loop, ambiguous (>1) feature, or any unexpected error => ALLOW (no-op).
# A guard hook must never break unrelated sessions.

import json
import os
import re
import sys
import hashlib
from pathlib import Path


# Loop statuses meaning "not actively iterating". When the discovered checkpoint
# is in one of these states the guard no-ops (allow), so a stranded paused/finished
# loop in a parent dir does not block unrelated sessions. Unknown/missing => ACTIVE.
INACTIVE_STATUSES = frozenset({
    "paused", "paused_for_approval", "completed", "done",
    "stopped", "aborted", "succeeded", "failed",
})

# PreToolUse exit codes (legacy block path). We primarily use the JSON decision
# protocol, but keep these for the crash-handler fail-open contract.
EXIT_ALLOW = 0


# --- side-effect key derivation -------------------------------------------
# Reuse durable_loop_checkpoint.py's logic so the keys this guard CHECKS are
# byte-identical to the keys that hook RECORDS. Import is preferred; if it fails
# (path/packaging quirk) we fall back to the in-file replica below, which is kept
# in lockstep with the source of truth.
try:
    import durable_loop_checkpoint as _ckpt  # type: ignore
    _derive_side_effect_key = _ckpt._derive_side_effect_key
except Exception:  # noqa: BLE001 — never let an import failure break the guard
    _ckpt = None

    # --- replicated from durable_loop_checkpoint.py (keep in sync) ---------
    _SIDE_EFFECT_PATTERNS = [
        # --- VCS ---
        r"\bgit\s+(commit|push|tag\s+-a|amend|rebase)\b",
        r"\bgh\s+(pr\s+create|release\s+create|api\s+.+(post|put|patch|delete))\b",
        # --- HTTP mutations (any client, with or without explicit -X) ---
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
    _POWERSHELL_TOOLS = ("mcp__windows-mcp__PowerShell", "mcp__windows-mcp__App")

    def _is_side_effect_command(cmd: str) -> bool:
        if not cmd:
            return False
        return bool(_SIDE_EFFECT_RE.search(cmd))

    def _derive_side_effect_key(tool_name: str, tool_input: dict) -> str:
        if tool_name in ("Write", "Edit", "NotebookEdit"):
            fp = str(tool_input.get("file_path") or tool_input.get("path") or "unknown")
            return f"write-{Path(fp).name}-{hashlib.sha256(fp.encode('utf-8')).hexdigest()[:8]}"
        if tool_name in _POWERSHELL_TOOLS:
            cmd = str(tool_input.get("command", ""))
            if cmd.strip():
                return f"exec-{hashlib.sha256(cmd.encode('utf-8')).hexdigest()[:8]}"
            return ""
        if tool_name == "Bash":
            cmd = str(tool_input.get("command", ""))
            if _is_side_effect_command(cmd):
                return f"exec-{hashlib.sha256(cmd.encode('utf-8')).hexdigest()[:8]}"
        return ""


# --- strict dangerous-operation patterns ----------------------------------
# Destructive/irreversible commands. Only consulted when strict mode is ON. These
# are NOT in the idempotency set (idempotency tracks "already done" mutations;
# strict tracks "should a human approve this at all"). Matched IGNORECASE on the
# raw command string of Bash / PowerShell-class tools.
_DANGEROUS_PATTERNS = [
    # --- destructive git ---
    r"\bgit\s+push\b[^\n]*(--force\b|--force-with-lease\b|(^|\s)-f(\s|$))",
    r"\bgit\s+reset\b[^\n]*--hard\b",
    r"\bgit\s+clean\b[^\n]*-[a-z]*f[a-z]*d|-[a-z]*d[a-z]*f|--force\b",
    r"\bgit\s+branch\b[^\n]*\s-D\b",
    r"\bgit\s+checkout\b[^\n]*\s(-f|--force)\b",
    # --- filesystem wipes ---
    r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-rf|-fr)\b",
    r"\brmdir\s+/s\b", r"\bdel\s+/[a-z]*\b",
    r"\bremove-item\b[^\n]*(-recurse\b[^\n]*-force\b|-force\b[^\n]*-recurse\b)",
    r"\b(mkfs|dd\s+if=)\b",
    # --- destructive SQL ---
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\bdelete\s+from\b(?![^\n]*\bwhere\b)",  # unscoped DELETE (no WHERE)
    # --- destructive infra ---
    r"\b(terraform|tf)\s+destroy\b",
    r"\bkubectl\s+delete\b[^\n]*(--all\b|namespace\b)",
    r"\bdocker\s+system\s+prune\b",
    r"\bhelm\s+uninstall\b",
]
_DANGEROUS_RE = re.compile("|".join(_DANGEROUS_PATTERNS), re.IGNORECASE)

# Tools that carry a shell command we should scan for dangerous patterns.
_COMMAND_TOOLS = ("Bash", "mcp__windows-mcp__PowerShell", "mcp__windows-mcp__App")


def read_stdin() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def discover_checkpoint(cwd: str):
    """Return checkpoint_path or None (fail open when no single active loop).
    Mirrors durable_loop_checkpoint.discover_checkpoint / observe discover."""
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
            print("[durable_loop_guard] WARN: >1 feature under .scratch/ found ("
                  + ", ".join(c.parent.name for c in cps)
                  + ") — guard is NO-OP. Set DURABLE_LOOP_FEATURE=<name> to "
                  + "re-enable for one loop.", file=sys.stderr)
            return None  # ambiguous — fail open
    return None


def load_checkpoint(cp_path: Path):
    """Return the parsed checkpoint dict, or None on any read/parse failure."""
    try:
        return json.loads(cp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def strict_enabled(cp: dict) -> bool:
    """Strict dangerous-op block is opt-in: env DURABLE_LOOP_STRICT=1 (truthy) OR
    checkpoint.strict_guard == true. Default OFF (backward compatible — field may
    be absent)."""
    env = str(os.environ.get("DURABLE_LOOP_STRICT", "")).strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    return bool(cp.get("strict_guard", False))


def command_of(tool_name: str, tool_input: dict) -> str:
    """Return the shell command for command-carrying tools, else ''."""
    if tool_name in _COMMAND_TOOLS:
        return str(tool_input.get("command", "") or "")
    return ""


def is_dangerous_command(cmd: str) -> bool:
    if not cmd:
        return False
    return bool(_DANGEROUS_RE.search(cmd))


def allow() -> int:
    """No-op allow: emit nothing, exit 0."""
    return EXIT_ALLOW


def deny(reason: str) -> int:
    """Emit the PreToolUse deny decision on stdout and exit 0 (block protocol)."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return EXIT_ALLOW


def record_pending_approval(feature_dir: Path, tool_name: str, cmd: str) -> None:
    """Best-effort append the blocked dangerous op to pending_approval.json so the
    operator can review/approve. Never raises into the hook (fail-open)."""
    try:
        import datetime
        path = feature_dir / "pending_approval.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        items = data.get("requests")
        if not isinstance(items, list):
            items = []
        items.append({
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": tool_name,
            "command": cmd[:500],
            "reason": "strict_guard blocked dangerous operation",
            "status": "pending",
        })
        data["requests"] = items
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — recording is best-effort
        pass


def evaluate(ev: dict) -> int:
    """Core decision logic. Returns an exit code; emits a deny JSON when blocking."""
    cwd = ev.get("cwd") or os.getcwd()
    cp_path = discover_checkpoint(cwd)
    if cp_path is None:
        return allow()  # no single active loop — fail open

    cp = load_checkpoint(cp_path)
    if cp is None:
        return allow()  # unreadable/unparseable — fail open

    # Scope guard: never block for a paused/finished loop stranded in a parent dir.
    if str(cp.get("status", "")).strip() in INACTIVE_STATUSES:
        return allow()

    tool_name = ev.get("tool_name", "") or ""
    tool_input = ev.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        return allow()

    # (2) strict dangerous-op hard block (opt-in). Checked BEFORE the idempotency
    # gate: a destructive op should be routed to approval even if it isn't yet in
    # the idempotency set.
    if strict_enabled(cp):
        cmd = command_of(tool_name, tool_input)
        if is_dangerous_command(cmd):
            record_pending_approval(cp_path.parent, tool_name, cmd)
            return deny(
                "strict_guard: dangerous/irreversible operation blocked — "
                f"{cmd[:160]!r}. This call was NOT executed. It has been recorded "
                "to pending_approval.json; obtain explicit operator approval (set "
                "the request status to approved, or run it manually) before retrying. "
                "Disable by unsetting DURABLE_LOOP_STRICT and strict_guard."
            )

    # (1) idempotency gate (always on for an active loop). If the side-effect key
    # for this exact call already exists in the checkpoint, the side effect already
    # happened — deny the replay.
    key = ""
    try:
        key = _derive_side_effect_key(tool_name, tool_input)
    except Exception:  # noqa: BLE001 — derivation must never break the guard
        key = ""
    if key:
        existing = cp.get("idempotency_keys", [])
        if isinstance(existing, list) and key in existing:
            return deny(
                "重复副作用已被幂等门拦截 (idempotency gate): the side-effect key "
                f"'{key}' for this {tool_name} call is already recorded in "
                "checkpoint.idempotency_keys, meaning this exact mutation already "
                "ran in a prior iteration. Re-running it would double-apply the "
                "side effect (e.g. duplicate push / POST / migration). Skip it and "
                "advance to the next un-applied step; if you truly need to re-run, "
                "remove the key from checkpoint.json first."
            )

    return allow()


def main() -> int:
    ev = read_stdin()
    return evaluate(ev)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — guard hook must never break the session
        print(f"[durable_loop_guard] fail-open: {exc}", file=sys.stderr)
        sys.exit(EXIT_ALLOW)
