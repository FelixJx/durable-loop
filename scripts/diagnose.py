#!/usr/bin/env python3
#
# diagnose.py — three-axis FAIL attribution reporter for durable loops.
#
# The diagnostic companion to verify_done.py. verify_done only records
# {iteration, result, timestamp} per run — it never stores WHY a run failed.
# diagnose closes that gap with a STATISTICAL attribution: from the *pattern* of
# recent FAILs in checkpoint.verify_history (frequency, spacing, which iteration
# band they cluster in, whether PASSes interleave), infer which of three layers
# the failures most plausibly live in, and point the human there instead of
# reflexively dumping more rules into the learnings library.
#
#   agent   layer (model capability / driver prompt)  — task understood but
#           executed unstably: late-iteration FAILs with intermittent PASSes.
#   harness layer (hook wiring / scheduling / dirs)   — FAILs vanish after one
#           and nothing re-verifies: history too sparse or has gaps.
#   skill   layer (done.criteria / learnings)         — convergence condition
#           itself too strict or wrong-headed: early-iteration dense FAILs.
#
# THIS IS A READ-ONLY REPORTER, NOT A GATE. Like verify_done.py it never blocks
# any call and never writes a single byte — it only reads checkpoint.json and
# (optionally) learnings.jsonl to flavour the skill-layer advice. Every read/
# parse path is fail-open: a missing/unreadable checkpoint, an empty history,
# an all-PASS history, or any unexpected exception degrades to a friendly
# no-op message and exit 0, never a raise into the caller.
#
# Anti-bloat rationale: today a FAIL tends to reflexively `learn log` a pitfall
# and dump rules into SKILL.md — so the experience library grows even when the
# real fix is "swap the model" or "wire the Stop hook". diagnose TRIAGES first:
# only the skill axis earns a "consider logging a learning" recommendation; the
# agent/harness axes redirect to config/model changes instead of piling on rules.
#
# Usage:
#   python diagnose.py <feature> [project_dir] [--limit N]
#     --limit N   inspect the N most recent verify_history entries (default 5).
#                 All of those N (not just the FAILs) are used for pass-rate /
#                 trend math; FAIL-band heuristics run on the FAILs among them.
#
# Exit codes: 0 on a well-formed invocation (even with no data — fail-open),
# 2 only on a usage error (bad feature name / missing project_dir). argparse
# usage errors also exit 2. Matches verify_done.py / durable_loop_learn.py.

import argparse
import json
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
DEFAULT_LIMIT = 5

# Iteration-band thresholds for the heuristics. Tuned for the default 25-iter
# budget but interpreted relatively so they degrade gracefully on short loops.
EARLY_ITER_MAX = 5     # FAILs at iter < 5 -> early-convergence-struggle band.
LATE_ITER_MIN = 10     # FAILs at iter > 10 -> late-iteration-instability band.
SPARSE_HISTORY = 3     # < this many recent entries -> harness-suspicion band.


def die(msg: str) -> "NoReturn":
    print(f"diagnose.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def load_checkpoint(cp_path: Path):
    """Return the parsed checkpoint dict, or None if absent/unreadable (fail-open).
    Mirrors verify_done.load_checkpoint exactly."""
    if not cp_path.is_file():
        return None
    try:
        data = json.loads(cp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iter_of(entry) -> int:
    """Coerce an entry's iteration to int; -1 sentinel if absent/unparseable so
    it never trips the < EARLY / > LATE band tests spuriously."""
    try:
        return int(entry.get("iteration"))
    except (TypeError, ValueError):
        return -1


def analyze_history(history):
    """Compute summary stats over a verify_history list (already truncated to the
    window the caller wants). Returns a dict; all fields are defensive. The
    window is the caller's responsibility so pass-rate reflects exactly the
    entries shown, while streaks are computed over the WHOLE (untruncated)
    history when available so they aren't capped by --limit."""
    entries = []
    if isinstance(history, list):
        for e in history:
            if isinstance(e, dict):
                entries.append(e)

    results = [str(e.get("result", "")).upper() for e in entries]
    total = len(results)
    pass_n = sum(1 for r in results if r == "PASS")
    fail_n = sum(1 for r in results if r == "FAIL")
    pass_rate = (pass_n / total) if total else 0.0

    # Longest PASS streak and longest FAIL streak across the full history.
    longest_pass = longest_fail = cur_pass = cur_fail = 0
    for r in results:
        if r == "PASS":
            cur_pass += 1
            cur_fail = 0
            longest_pass = max(longest_pass, cur_pass)
        elif r == "FAIL":
            cur_fail += 1
            cur_pass = 0
            longest_fail = max(longest_fail, cur_fail)
        else:
            cur_pass = 0
            cur_fail = 0

    return {
        "total": total,
        "pass_n": pass_n,
        "fail_n": fail_n,
        "pass_rate": pass_rate,
        "longest_pass_streak": longest_pass,
        "longest_fail_streak": longest_fail,
        "results": results,
        "entries": entries,
    }


def detect_trend(results):
    """improving / stagnating / regressing, computed locally (no
    evolution_trend field dependency — that field does not exist). Compares the
    first half PASS-rate to the second half; ties or too-few points stagnate."""
    n = len(results)
    if n < 4:
        return "stagnating"
    mid = n // 2
    first = results[:mid]
    second = results[mid:]
    r1 = sum(1 for r in first if r == "PASS") / len(first)
    r2 = sum(1 for r in second if r == "PASS") / len(second)
    if r2 - r1 > 0.15:
        return "improving"
    if r1 - r2 > 0.15:
        return "regressing"
    return "stagnating"


def _parse_ts(entry):
    """Best-effort ISO-ish timestamp -> epoch seconds; None if absent/unparseable."""
    ts = entry.get("timestamp") if isinstance(entry, dict) else None
    if not isinstance(ts, str) or not ts.strip():
        return None
    # datetime.fromisoformat handles the 'YYYY-MM-DDTHH:MM:SS[+HH:MM]' shape
    # verify_done writes. Tolerate a trailing Z.
    import datetime
    s = ts.strip().rstrip("Z")
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def has_timing_gap(history, gap_multiplier=3.0):
    """Detect a SCHEDULING gap: some adjacent pair of entries whose timestamp
    delta is much larger than the median step. This is the principled 'FAIL
    then silence' signal — it fires when verification clearly went quiet for an
    unusually long stretch (hook unwired / interval blown), NOT merely because
    the history happens to end on a FAIL (a continuous run ending in FAIL is
    normal and must NOT trip the harness axis). Needs >=3 timestamps to form a
    median; fewer can't establish a baseline step so we defer to sparsity."""
    if not isinstance(history, list) or len(history) < 3:
        return False
    epochs = [_parse_ts(e) for e in history]
    deltas = [b - a for a, b in zip(epochs, epochs[1:])
              if a is not None and b is not None and b > a]
    if len(deltas) < 2:
        return False
    deltas.sort()
    median = deltas[len(deltas) // 2]
    if median <= 0:
        return False
    return any(d > median * gap_multiplier for d in deltas)


def attribute(stats, full_history):
    """Run the three-axis heuristics. `stats` is the analyze_history() summary
    over the (--limit) window; `full_history` is the untruncated list (may equal
    stats['entries'] when --limit covered everything) for gap detection.

    Returns (axes, primary) where axes is {axis: bool} of who triggered and
    primary is the single recommended-priority axis ('agent'/'harness'/'skill'
    or None when nothing triggered).

    Priority order when multiple fire: harness > skill > agent. Rationale: a
    sparse/gapped history makes the other two axes unreliable (you can't trust
    late-vs-early FAIL patterns if verification barely ran), so harness wins
    the tie and the human should fix wiring first."""
    entries = stats["entries"]
    fails = [e for e in entries if str(e.get("result", "")).upper() == "FAIL"]
    passes = [e for e in entries if str(e.get("result", "")).upper() == "PASS"]

    axes = {"agent": False, "harness": False, "skill": False}

    # --- harness axis: history too sparse, OR a real scheduling gap ---
    # Sparse  -> verification barely running (hook unwired / interval too long).
    # Gap     -> an unusually long silence between adjacent runs vs the median
    #            step (hook stalled / interval blown). NOTE: a continuous history
    #            that merely ends on a FAIL does NOT trip this — that is normal.
    sparse = stats["total"] < SPARSE_HISTORY
    timing_gap = has_timing_gap(full_history)
    if sparse or timing_gap:
        axes["harness"] = True

    # --- skill axis: early-iteration dense FAILs ---
    # FAILs clustered at iter < EARLY_ITER_MAX, especially all-FAIL across the
    # first few rounds -> the convergence condition itself is the problem.
    if fails:
        early_fails = [f for f in fails if 0 <= _iter_of(f) < EARLY_ITER_MAX]
        # All-fail-in-early-band, OR majority of FAILs are early.
        if (len(fails) >= 2 and len(early_fails) == len(fails)
                and all(0 <= _iter_of(f) < EARLY_ITER_MAX for f in fails)):
            axes["skill"] = True
        elif len(early_fails) >= max(2, len(fails) // 2 + 1):
            axes["skill"] = True

    # --- agent axis: late-iteration intermittent FAILs with interleaved PASS ---
    # FAILs at iter > LATE_ITER_MIN while PASSes still happen -> task understood
    # but execution is flaky (model stepping over a constraint / prompt too loose).
    if fails:
        late_fails = [f for f in fails if _iter_of(f) > LATE_ITER_MIN]
        if len(late_fails) >= 1 and len(passes) >= 1:
            axes["agent"] = True

    # Primary recommendation (tie-break order: harness > skill > agent).
    primary = None
    for ax in ("harness", "skill", "agent"):
        if axes[ax]:
            primary = ax
            break
    return axes, primary


def render_report(feature, cp_path, window_stats, axes, primary, trend, limit):
    """Build the markdown attribution report as a single string."""
    L = []
    L.append(f"## diagnose: feature=`{feature}`  (recent {window_stats['total']} run(s), "
             f"--limit {limit})")
    L.append("")
    L.append(f"_checkpoint: `{cp_path}`  — read-only reporter, never writes, never gates._")
    L.append("")

    s = window_stats
    L.append("### 摘要 (summary)")
    pr = f"{s['pass_rate'] * 100:.0f}%" if s["total"] else "n/a"
    L.append(f"- runs: **{s['total']}**  (PASS {s['pass_n']} / FAIL {s['fail_n']})  "
             f"overall pass-rate: **{pr}**")
    L.append(f"- longest PASS streak: **{s['longest_pass_streak']}**  "
             f"longest FAIL streak: **{s['longest_fail_streak']}**")
    L.append(f"- trend: **{trend}**  (locally computed, no evolution_trend dependency)")
    L.append("")

    if s["fail_n"] == 0:
        L.append("### 无近期 FAIL，无需归因")
        L.append("")
        L.append("最近窗口全 PASS — 没有可归因的失败。如果尚未收敛，跑 `verify_done` 推进 "
                 "K-连续-PASS streak 即可。")
        return "\n".join(L)

    L.append("### 三维度归因 (three-axis attribution)")
    L.append("")
    L.append("| axis | triggered | 信号 (signal) |")
    L.append("|---|---|---|")
    L.append(f"| **agent** (model / driver prompt) | {'yes' if axes['agent'] else 'no'} | "
             "late-iter FAIL (>{0}) 且间歇出现 PASS → 任务理解对但执行不稳 |".format(LATE_ITER_MIN))
    L.append(f"| **harness** (hooks / scheduling / dirs) | {'yes' if axes['harness'] else 'no'} | "
             "history <{0} 条 或相邻运行时间间隔远大于中位步长 → 调度/hook 没在跑或间隔太长 |".format(SPARSE_HISTORY))
    L.append(f"| **skill** (done.criteria / learnings) | {'yes' if axes['skill'] else 'no'} | "
             "早期 iter (<{0}) FAIL 密集/全 FAIL → 收敛条件本身过严或方向错 |".format(EARLY_ITER_MAX))
    L.append("")

    L.append("### 建议 (recommendations)")
    L.append("")
    if primary is None:
        L.append("未触发任一轴的强信号——FAIL 模式不够典型。建议人工核对最近一次 FAIL 的"
                 "`done.criteria.md` 输出后再决定改哪层。")
    elif primary == "harness":
        L.append("**推荐优先改 harness 层。**")
        L.append("")
        L.append("- 检查 Stop hook `durable_loop_checkpoint.py` 是否真的接线（`settings.json` 的 "
                 "`Stop` 事件）。")
        L.append("- 检查 `reset_every_n` 节律与 `.scratch/<feature>/` 目录结构是否齐全。")
        L.append("- 检查调度间隔：z.ai 侧 `/loop` 用 **240s**（非 300s），间隔过长会让 "
                 "verify_history 长期稀疏。")
        L.append("- **不要**把 harness 层问题写进 learnings/SKILL.md——那是配置/接线问题，"
                 "堆规则只会让经验库膨胀。")
    elif primary == "skill":
        L.append("**推荐优先改 skill 层。**")
        L.append("")
        L.append("- 检查 `done.criteria.md` 的 `<!-- cmd: -->` 命令是否**可达且非 gaming**："
                 "阈值是否过严、命令是否依赖尚未生成的文件、是否被字面满足而非真正达标。")
        L.append("- 用 `python scripts/durable_loop_learn.py search <feature> --query \"<关键词>\"` "
                 "看是否已有对应 pitfall；**确认是 skill 层后再 `log`**，避免无脑堆规则。")
        L.append("- 必要时放宽或重设 criteria（仍须人工 judge 兜底，不能只靠机器）。")
    elif primary == "agent":
        L.append("**推荐优先改 agent 层。**")
        L.append("")
        L.append("- 换更强 / 更稳的模型，或在 `assets/loop-driver-prompt.md` 里强化**逐步约束**"
                 "（每步必须做什么、不许跳到哪）。")
        L.append("- 把任务拆成更小的可验证子任务，降低单轮执行的方差。")
        L.append("- 检查最近几轮是否在同一类步骤上反复翻车——若是，是 prompt 约束缺失，"
                 "而非经验库缺规则。**不要**把执行不稳当 pitfall 堆进 learnings。")
    L.append("")
    L.append("> 警告：FAIL 不是自动往 `learnings.jsonl` / `SKILL.md` 堆规则的理由。"
             "diagnose 先分流——只有 skill 层才建议 `learn log`，agent/harness 层去改配置/换模型。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="diagnose.py",
        description="Three-axis FAIL attribution reporter (agent / harness / skill). "
                    "Read-only, fail-open, never gates.",
    )
    # feature is positional (`diagnose.py <feature> [project_dir]`) but ALSO
    # accepted as --feature, so both the documented form and the common
    # `--feature X` invocation work. argparse can't merge the two natively, so
    # feature_optional carries the --feature value and we resolve below.
    ap.add_argument("feature", nargs="?", default=None,
                    help="name matching .scratch/<feature>/ (or use --feature)")
    ap.add_argument("--feature", dest="feature_optional", default=None,
                    help="alias for the positional feature")
    ap.add_argument("project_dir", nargs="?", default=".",
                    help="project root (default: cwd)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"inspect the N most recent verify_history entries (default {DEFAULT_LIMIT})")
    args = ap.parse_args()

    args.feature = args.feature_optional if args.feature_optional is not None else args.feature
    if not args.feature:
        die("feature is required: `diagnose.py <feature> [project_dir] [--limit N]` "
            "(positional or --feature)")
    if not NAME_RE.match(args.feature):
        die(f"invalid feature name '{args.feature}'")
    if args.limit <= 0:
        die(f"--limit must be > 0, got {args.limit}")

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project_dir does not exist: {project_dir}")

    cp_path = project_dir / ".scratch" / args.feature / "checkpoint.json"
    cp = load_checkpoint(cp_path)

    print(f"== diagnose: feature='{args.feature}' project='{project_dir}' limit={args.limit} ==")
    print()

    if cp is None:
        print("无 checkpoint（或不可读）—— fail-open，无需归因。")
        print("  提示：先 `python scripts/init_loop.py <feature>` 初始化，再跑几轮 "
              "verify_done 让 verify_history 积累。")
        return 0

    full_history = cp.get("verify_history")
    if not isinstance(full_history, list) or not full_history:
        print("checkpoint 无 verify_history —— 无足够数据归因。")
        print("  提示：verify_done 在有 checkpoint 时才会写 verify_history；"
              "多跑几轮 `python scripts/verify_done.py <feature>` 让历史积累。")
        return 0

    # Window for display + pass-rate math = the N most recent entries.
    limit = min(args.limit, len(full_history))
    window = full_history[-limit:] if limit > 0 else full_history
    window_stats = analyze_history(window)
    trend = detect_trend([str(e.get("result", "")).upper()
                          for e in window_stats["entries"]])
    axes, primary = attribute(window_stats, full_history)

    print(render_report(args.feature, cp_path, window_stats, axes, primary, trend, limit))
    if primary:
        print()
        print(f"== 总判：推荐优先改 **{primary}** 层 ==")
    elif window_stats["fail_n"] > 0:
        print()
        print("== 总判：FAIL 模式不典型，建议人工核对最近一次 verify_done 输出 ==")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — reporter must never raise into the caller
        print(f"diagnose.py: fail-open: {exc}", file=sys.stderr)
        sys.exit(0)
