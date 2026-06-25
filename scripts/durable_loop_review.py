#!/usr/bin/env python3
#
# durable_loop_review.py — the human-review-feedback reflow layer (Gap2).
#
# A human review of a loop's draft / PR / diff is otherwise a one-shot chat
# message: the agent nods, applies the change, and the *why* of the approval /
# rejection / modification is lost. This script turns that review verdict into a
# durable, searchable learning so the NEXT reflect-in picks it up — approvals and
# modifications become reusable patterns, rejections become pitfalls to avoid,
# and the loop stops re-litigating a decision a human already made.
#
# It does NOT touch pending_approval.json or any HITL-approval state: review only
# reads the human's `--reason` and (optional) `--draft` path and reflows them into
# the same `.scratch/<feature>/learnings.jsonl` the agent already searches, so the
# approval-flow and the learning-flow stay decoupled (mirrors durable_loop_checkpoint.py
# reading learnings.jsonl directly instead of importing/shelling learn.py — except
# here the coupling is the other way: review imports learn's pure functions).
#
# Decision → learning mapping:
#   approve → type=pattern   (this direction is endorsed, reuse it)  confidence=7
#   reject  → type=pitfall   (this direction is vetoed, avoid it)    confidence=8
#   modify  → BOTH: a pattern ([修改自审核], the kept direction, conf 6)
#                   AND a pitfall (the part a human required changed, conf 8)
#             modify = approve + local-reject, so we double-log: the surviving
#             idea becomes a pattern while the rejected bits become a pitfall.
#
# key construction: f"review-{decision}-{slug}" where slug is kebab-cased from
# the --draft basename (if given) else feature + a short time hash. A stable key
# for the same (feature, decision, draft) means a second identical review MERGES
# (seen+1, confidence max) rather than piling up duplicate lines — same dedup the
# agent's own reflect-out logs enjoy.
#
# source: the --draft path when given (so learn.prune can later mark it stale if
# the draft disappears), else the literal "human-review".
#
# CLI:
#   python durable_loop_review.py --feature F [--project-dir DIR] \
#       --decision approve|reject|modify --reason "human rationale" [--draft <path>]
#
# Exit codes: 0 on a well-formed invocation (fail-open — any runtime error is
# swallowed and still exits 0, matching durable_loop_learn.py); 2 only on a usage
# error (bad feature name / missing project_dir / empty reason), matching
# learn.py / verify_done.py / replay_trace.py. argparse usage errors also exit 2.

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

# learn.py is a pure-stdlib module whose functions are side-effect-free outside
# their explicit file writes and are unit-tested in isolation, so importing it
# directly (rather than shelling out) keeps review.py testable and avoids a
# subprocess round-trip per decision. This is the documented Gap2 coupling
# decision; checkpoint.py by contrast reads learnings.jsonl directly because it
# is a hook that must stay import-light.
import durable_loop_learn as learn

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
DECISIONS = ("approve", "reject", "modify")

# Confidence per decision (rationale in the module docstring).
_CONFIDENCE = {"approve": 7, "reject": 8, "modify": 6}


def die(msg: str) -> "NoReturn":
    print(f"durable_loop_review.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _kebab(text: str) -> str:
    """Lowercase kebab-case: non-alphanumerics → '-', trim, collapse runs.
    Empty input → '' so callers can fall back to a hash."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def build_slug(feature: str, draft: str) -> str:
    """Stable kebab slug for the dedup key.

    Preference order: --draft basename (the review is about a specific artifact,
    so naming the key after it makes re-reviews of the same draft merge) → else
    feature + an 8-hex time hash (so two different ad-hoc reviews of the same
    feature don't collide but re-running the identical one within the same
    second does)."""
    if draft:
        stem = _kebab(Path(draft).stem)
        if stem:
            return stem
    h = hashlib.sha256(f"{feature}|{_now_iso()}".encode("utf-8")).hexdigest()[:8]
    base = _kebab(feature) or "feature"
    return f"{base}-{h}"


def build_key(decision: str, slug: str) -> str:
    return f"review-{decision}-{slug}"


def _log_learning(project_dir: Path, feature: str, decision_for_type: str,
                  key: str, insight: str, confidence: int, source: str) -> int:
    """Construct an argparse.Namespace exactly the way durable_loop_learn.py's
    `log` subparser would, and feed it to learn.cmd_log. Reuses learn.py's
    (type,key) merge / atomic-write / run_id-read machinery verbatim so review
    and reflect-out share one dedup contract."""
    ns = argparse.Namespace(
        feature=feature,
        project_dir=str(project_dir),
        type=decision_for_type,   # "pattern" or "pitfall"
        key=key,
        insight=insight,
        confidence=confidence,
        source=source,
        iteration=None,           # cmd_log reads run_id from checkpoint itself
    )
    return learn.cmd_log(ns, project_dir)


def append_review_to_session_log(project_dir: Path, feature: str, decision: str,
                                 reason: str, draft: str, key: str) -> None:
    """Append one JSON line describing this review action to
    .scratch/<feature>/session.log, mirroring the observe hook's append-only
    shape so replay_trace.py / 24h-reconstruction still works. Best-effort:
    a missing .scratch dir is created here; any IO failure is swallowed by the
    top-level fail-open guard."""
    sess = project_dir / ".scratch" / feature / "session.log"
    sess.parent.mkdir(parents=True, exist_ok=True)
    run_id = learn.read_run_id(project_dir, feature)
    entry = {
        "ts": _now_iso(),
        "run_id": run_id,
        "iter": "?",
        "tool": "durable_loop_review",
        "action": f"review {decision} [{key}]",
        "resp": (reason or "")[:140],
        "phase": "human-review",
        "decision": decision,
        "draft": draft or "",
        "key": key,
    }
    # POSIX O_APPEND makes a small write atomic against concurrent PostToolUse
    # appends from the observe hook.
    with open(sess, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_review(args, project_dir: Path) -> int:
    decision = args.decision
    feature = args.feature
    reason = args.reason
    draft = args.draft or ""
    source = draft if draft else "human-review"

    slug = build_slug(feature, draft)
    confidence = _CONFIDENCE[decision]

    logged_types = []
    if decision == "approve":
        key = build_key("approve", slug)
        insight = f"审核反馈(approve): {reason}"
        _log_learning(project_dir, feature, "pattern", key, insight, confidence, source)
        logged_types.append(("pattern", key, confidence))
    elif decision == "reject":
        key = build_key("reject", slug)
        insight = f"审核反馈(reject): {reason}"
        _log_learning(project_dir, feature, "pitfall", key, insight, confidence, source)
        logged_types.append(("pitfall", key, confidence))
    else:  # modify — double-log: kept direction as pattern, changed bits as pitfall
        pat_key = build_key("modify", slug)
        pit_key = f"review-modify-reject-{slug}"
        pat_insight = f"[修改自审核] 审核反馈(modify): {reason}"
        pit_insight = f"审核要求修改的部分: {reason}"
        _log_learning(project_dir, feature, "pattern", pat_key, pat_insight,
                      confidence, source)
        # The pitfall half carries the higher reject-confidence so the vetoed
        # bits are remembered at least as firmly as a plain reject.
        _log_learning(project_dir, feature, "pitfall", pit_key, pit_insight,
                      _CONFIDENCE["reject"], source)
        logged_types.append(("pattern", pat_key, confidence))
        logged_types.append(("pitfall", pit_key, _CONFIDENCE["reject"]))

    # Record the review action in session.log (one line; modify logs both keys
    # inside the single action string so the log stays one-row-per-review).
    first_key = logged_types[0][1]
    append_review_to_session_log(project_dir, feature, decision, reason,
                                 draft, first_key)

    # Human-facing summary: tell the user their verdict became a searchable
    # learning and how to recall it next reflect-in.
    types_str = " + ".join(f"{t}/{k} (confidence={c}/10)" for t, k, c in logged_types)
    print(f"review: logged {types_str} — decision={decision}")
    print(f"  next reflect-in: durable_loop_learn.py search {feature} "
          f"--query '{(reason or decision)[:40]}' 即可复用/规避")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="durable_loop_review.py",
        description="Reflow a human review verdict (approve/reject/modify) into a "
                    "durable, searchable learning under .scratch/<feature>/learnings.jsonl.",
    )
    ap.add_argument("--feature", required=True,
                    help="feature name matching .scratch/<feature>/")
    ap.add_argument("--project-dir", default=".", dest="project_dir",
                    help="project root (default: cwd)")
    ap.add_argument("--decision", required=True, choices=DECISIONS,
                    help="approve→pattern, reject→pitfall, modify→pattern+pitfall")
    ap.add_argument("--reason", required=True,
                    help="human rationale (required — a verdict without a why is not reusable)")
    ap.add_argument("--draft", default=None,
                    help="path to the reviewed draft / PR / diff (becomes the learning source)")
    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()  # argparse exits 2 on usage error

    if not NAME_RE.match(args.feature):
        die(f"invalid feature name '{args.feature}'")

    # An empty reason is a usage error: a verdict with no rationale carries no
    # reusable signal and would pollute learnings with content-free rows.
    if not args.reason or not args.reason.strip():
        die("reason must not be empty — a review verdict without a rationale is not reusable")

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project_dir does not exist: {project_dir}")

    return run_review(args, project_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — review reflow must never raise into the caller
        print(f"durable_loop_review.py: fail-open: {exc}", file=sys.stderr)
        sys.exit(0)
