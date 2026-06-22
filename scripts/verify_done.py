#!/usr/bin/env python3
#
# verify_done.py — cross-platform Python port of verify_done.sh.
#
# The EVALUATOR half of generator/evaluator separation: independently checks
# whether the machine-verifiable criteria in done.criteria.md pass. The actor
# MUST NOT self-declare done. Mirrors verify_done.sh but uses subprocess for
# timeouts (no GNU `timeout` dependency) and runs natively on Windows.
#
# Usage:
#   python verify_done.py <feature> [project_dir] [--timeout <secs>]
#
# Commands come ONLY from `<!-- cmd: ... -->` HTML comments on a checkbox line
# (inline backticks are prose, never commands). Multiple <!-- cmd: --> on one
# line are each run and ALL must pass (non-greedy parse — unlike the .sh's
# greedy regex, this correctly handles >1 cmd per line).
#
# Output per criterion: [PASS] / [FAIL] — <reason> / [MANUAL]
# Final: VERDICT: DONE (all PASS, none FAIL, none MANUAL-pending) / NOT DONE.
# Exit 0 DONE, 1 NOT DONE, 2 usage error / file missing.
#
# Anti flip-flop (improvement #8): a single machine PASS is NOT enough to declare
# convergence. When a checkpoint.json exists alongside done.criteria.md, every run
# appends its overall verdict to checkpoint.verify_history, and only K CONSECUTIVE
# passes (K default 2; override via env DURABLE_LOOP_CONVERGE_K or checkpoint field
# converge_k) emit "已收敛 (converged)" with exit 0. A single PASS that has not yet
# reached K prints "PASS (k/K, 尚未收敛)" and exits 1 (NOT done). Any FAIL resets the
# consecutive-PASS streak to zero. With NO checkpoint this degrades to the original
# single-shot judgment (fully backward compatible). Reading/writing the checkpoint is
# fail-open: a missing/unreadable/unparseable checkpoint never changes the verdict and
# never raises — verify_done stays the sole gate and the actor still never self-declares.

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT = 120
DEFAULT_CONVERGE_K = 2
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# A list item that starts a line: optional indent, bullet, [ ]/[x]/[X], space.
CHECKBOX_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[[ xX]\][ \t]+(.*)$")
# Non-greedy: each <!-- cmd: ... --> is one command (handles >1 per line).
CMD_RE = re.compile(r"<!--\s*cmd:(.*?)-->", re.DOTALL)

NOT_FOUND_MARKERS = (
    "command not found",
    "not recognized as an internal or external command",
    "is not recognized as",  # Windows: 'foo' is not recognized as ...
    "no such file or directory",
    "cannot find the file",
    "the system cannot find the file specified",
    "no command not found",  # sentinel to avoid false substring; harmless
)


def die(msg: str) -> "NoReturn":
    print(f"verify_done.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def label_of(line: str) -> str:
    m = CHECKBOX_RE.match(line)
    rest = m.group(1) if m else line
    rest = CMD_RE.sub("", rest)            # drop cmd comments
    rest = re.sub(r"`[^`]*`", "", rest)    # drop backtick spans
    rest = rest.replace("**", "")
    return rest.strip()[:90]


def run_cmd(cmd: str, project_dir: Path, timeout: int, use_bash: bool) -> str:
    """Run one criterion command; return 'PASS' or 'FAIL: <reason>'."""
    try:
        if use_bash:
            # Use the resolved bash path, NOT the bare name "bash": on Windows,
            # subprocess CreateProcess resolves bare "bash" to System32\bash.exe
            # (the WSL launcher) ahead of PATH, which breaks /c/... msys paths.
            proc = subprocess.run([use_bash, "-c", cmd], cwd=str(project_dir),
                                  timeout=timeout, capture_output=True, text=True)
        else:
            proc = subprocess.run(cmd, cwd=str(project_dir), timeout=timeout,
                                  shell=True, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return f"FAIL: timed out after {timeout}s"
    combined = (proc.stdout or "") + (proc.stderr or "")
    low = combined.lower()
    if any(marker in low for marker in NOT_FOUND_MARKERS if marker != "no command not found"):
        return f"FAIL: command not found / missing dependency — {cmd}"
    if proc.returncode != 0:
        lines = [ln for ln in combined.splitlines() if ln.strip()]
        first = lines[0][:160] if lines else ""
        return f"FAIL: exit {proc.returncode} — {first}" if first else f"FAIL: exit {proc.returncode}"
    return "PASS"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_checkpoint(cp_path: Path):
    """Return the parsed checkpoint dict, or None if absent/unreadable (fail-open)."""
    if not cp_path.is_file():
        return None
    try:
        data = json.loads(cp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def converge_k_for(cp) -> int:
    """Resolve K (consecutive PASSes required). Precedence: env > checkpoint > default.
    Any unparseable / non-positive value falls back to the next source / default."""
    raw = os.environ.get("DURABLE_LOOP_CONVERGE_K")
    if raw is not None and str(raw).strip() != "":
        try:
            k = int(str(raw).strip())
            if k > 0:
                return k
        except (TypeError, ValueError):
            pass
    if isinstance(cp, dict) and "converge_k" in cp:
        try:
            k = int(cp.get("converge_k"))
            if k > 0:
                return k
        except (TypeError, ValueError):
            pass
    return DEFAULT_CONVERGE_K


def consecutive_pass_streak(history) -> int:
    """Count trailing consecutive PASS verdicts in verify_history (inclusive of the
    last appended entry). A FAIL/anything-else breaks the streak — so one FAIL resets
    the count to zero on the next pass. Tolerant of malformed entries (treated as
    non-PASS, i.e. they break the streak)."""
    if not isinstance(history, list):
        return 0
    streak = 0
    for entry in reversed(history):
        result = entry.get("result") if isinstance(entry, dict) else None
        if str(result).upper() == "PASS":
            streak += 1
        else:
            break
    return streak


def append_verify_history(cp, cp_path: Path, iteration, passed: bool) -> None:
    """Append this run's overall verdict to checkpoint.verify_history and atomically
    write it back. Fail-open: any error is swallowed so verify_done never breaks just
    because the checkpoint could not be updated."""
    try:
        history = cp.get("verify_history")
        if not isinstance(history, list):
            history = []
        history.append({
            "iteration": iteration,
            "result": "PASS" if passed else "FAIL",
            "timestamp": _now_iso(),
        })
        cp["verify_history"] = history
        cp["last_updated"] = _now_iso()
        tmp = cp_path.with_suffix(cp_path.suffix + ".tmp")
        tmp.write_text(json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cp_path)
    except (OSError, TypeError, ValueError):
        pass  # fail-open — observability loss only, verdict is unaffected


def main() -> int:
    ap = argparse.ArgumentParser(description="Machine-verifiable convergence evaluator.")
    ap.add_argument("feature", help="name matching .scratch/<feature>/")
    ap.add_argument("project_dir", nargs="?", default=".", help="project root (default: cwd)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="per-command timeout seconds")
    args = ap.parse_args()

    if not NAME_RE.match(args.feature):
        die(f"invalid feature name '{args.feature}'")
    if args.timeout <= 0:
        die(f"--timeout must be > 0, got {args.timeout}")

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project_dir does not exist: {project_dir}")
    criteria = project_dir / ".scratch" / args.feature / "done.criteria.md"
    if not criteria.is_file():
        die(f"criteria file not found: {criteria}\n  Run init_loop first, then edit done.criteria.md.")

    # Prefer bash for command semantics (the criteria cmds are bash-flavored:
    # `! grep`, `[ ... ]`, `test`). Falls back to the platform shell if absent.
    use_bash = shutil.which("bash")  # resolved path (truthy) or None; passed to run_cmd

    print(f"== verify_done: feature='{args.feature}' criteria='{criteria}' timeout={args.timeout}s ==")
    print()

    pass_count = fail_count = manual_count = checked_any = 0
    for rawline in criteria.read_text(encoding="utf-8").splitlines():
        if not CHECKBOX_RE.match(rawline):
            continue
        checked_any = 1
        label = label_of(rawline) or "(unnamed criterion)"
        cmds = [c.strip() for c in CMD_RE.findall(rawline) if c.strip()]
        if not cmds:
            manual_count += 1
            print(f"[MANUAL] {label} — no machine command; needs human/judge judgment")
            continue
        # ALL commands on the line must pass.
        result = "PASS"
        for c in cmds:
            result = run_cmd(c, project_dir, args.timeout, use_bash)
            if not result.startswith("PASS"):
                break
        if result.startswith("PASS"):
            pass_count += 1
            print(f"[PASS] {label}")
        else:
            fail_count += 1
            print(f"[FAIL] {label} — {result[len('FAIL: '):] if result.startswith('FAIL: ') else result}")

    print()
    print("-------------------------------------------")
    print(f"PASS: {pass_count}  FAIL: {fail_count}  MANUAL(pending): {manual_count}")

    # Overall machine verdict for THIS run: True only if every executable criterion
    # passed and nothing is FAIL / MANUAL-pending. This is the per-run signal that the
    # anti flip-flop K-streak is built on.
    if checked_any == 0:
        print("VERDICT: NOT DONE — no checkbox criteria found in done.criteria.md")
        this_run_pass = False
    elif fail_count > 0:
        print(f"VERDICT: NOT DONE — {fail_count} criterion/criteria FAILED")
        this_run_pass = False
    elif manual_count > 0:
        print(f"VERDICT: NOT DONE — {manual_count} criterion/criteria require manual/judge sign-off")
        this_run_pass = False
    else:
        print("VERDICT: DONE — all executable criteria PASS")
        this_run_pass = True

    # Anti flip-flop gate. With a checkpoint present we record this run's verdict and
    # require K CONSECUTIVE passes before declaring convergence; without one we keep the
    # original single-shot behavior (backward compatible). Reading/writing the checkpoint
    # is fail-open — it can change the message but never breaks the run.
    cp_path = criteria.with_name("checkpoint.json")
    cp = load_checkpoint(cp_path)

    if cp is None:
        # No (usable) checkpoint -> single-shot judgment, exactly as before.
        if not this_run_pass:
            return 1
        print("  NOTE: this is the machine-verifiable gate only. For full done, an")
        print("  independent evaluator (non-actor model or human) must still confirm the")
        print("  QUALITY anti-gaming criteria and write 'JUDGE: PASS'.")
        return 0

    k = converge_k_for(cp)
    iteration = cp.get("iteration")
    append_verify_history(cp, cp_path, iteration, this_run_pass)
    streak = consecutive_pass_streak(cp.get("verify_history"))

    if not this_run_pass:
        # A FAIL just reset the consecutive-PASS streak to zero.
        print(f"CONVERGENCE: streak reset (0/{k} consecutive PASS) — keep iterating.")
        return 1
    if streak < k:
        print(f"CONVERGENCE: PASS ({streak}/{k}, 尚未收敛) — need {k - streak} more "
              f"consecutive PASS before declaring done; run again next iteration.")
        return 1
    print(f"CONVERGENCE: 已收敛 (converged) — {streak}/{k} consecutive machine PASS.")
    print("  NOTE: this is the machine-verifiable gate only. For full done, an")
    print("  independent evaluator (non-actor model or human) must still confirm the")
    print("  QUALITY anti-gaming criteria and write 'JUDGE: PASS'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
