#!/usr/bin/env python3
#
# durable_loop_learn.py — the experience-distillation (learnings) layer.
#
# Stores reusable, cross-iteration / cross-session learnings as JSONL under
# .scratch/<feature>/learnings.jsonl so a long-running durable loop (and future
# sessions on the same feature) can carry forward what worked (patterns) and what
# bit (pitfalls) instead of re-discovering them every iteration. This is a QUALITY
# ENHANCER, on by default, but NEVER a brake: it never blocks a tool call, and every
# read/parse path is fail-open — a missing .scratch/<feature>/, missing learnings
# file, or malformed lines degrade to a friendly no-op / empty result, never a raise
# into the caller.
#
# Schema (one JSON object per line):
#   {
#     "id":        "<8-hex>",          # stable reference / id, kept across merges
#     "type":      "pattern"|"pitfall",
#     "key":       "<kebab-case>",     # dedup key: same (type,key) == same learning
#     "insight":   "<reusable one/two-liner>",
#     "confidence": <int 0-10>,
#     "source":    "<file path / commit / 'observed'>",
#     "iteration": <int|null>,
#     "run_id":    "<from checkpoint.json, '' if absent>",
#     "timestamp": "<ISO8601>",
#     "seen":      <int, default 1>,   # +1 each time the same key is re-logged
#     "stale":     <bool, default false>  # set by prune when source path vanished
#   }
#
# CLI:
#   python durable_loop_learn.py log    <feature> [project_dir] --type T --key K \
#                                       --insight TXT --confidence N [--source S --iteration I]
#   python durable_loop_learn.py search <feature> [project_dir] --query "kw kw" \
#                                       [--limit N] [--type T] [--cross-feature]
#   python durable_loop_learn.py prune  <feature> [project_dir] [--apply]
#   python durable_loop_learn.py compile <feature> [project_dir] [--min-confidence N] [--limit K]
#
# Exit codes: 0 on a well-formed invocation (even with no data — fail-open), 2 only
# on a usage error (bad feature name / missing project_dir), matching verify_done.py
# and replay_trace.py. argparse usage errors also exit 2.

import argparse
import datetime
import json
import re
import sys
import uuid
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
VALID_TYPES = ("pattern", "pitfall")

# Words ignored when scoring a search query — short connective noise only.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]+")  # ASCII runs | CJK runs — bare [A-Za-z0-9]+ silently dropped ALL Chinese, breaking search/reflect-in on CJK-pattern learnings (审核理由/中文 pattern 全检索不到)


def die(msg: str) -> "NoReturn":
    print(f"durable_loop_learn.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def learnings_path(project_dir: Path, feature: str) -> Path:
    return project_dir / ".scratch" / feature / "learnings.jsonl"


def read_run_id(project_dir: Path, feature: str) -> str:
    """Read run_id from .scratch/<feature>/checkpoint.json. Fail-open: missing /
    unreadable / unparseable / absent field => '' (matches the observe hook)."""
    cp = project_dir / ".scratch" / feature / "checkpoint.json"
    if not cp.is_file():
        return ""
    try:
        d = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    if isinstance(d, dict):
        rid = d.get("run_id", "")
        return rid if isinstance(rid, str) else ""
    return ""


def load_learnings(path: Path):
    """Return (records, malformed_count). Blank lines skipped; non-JSON / non-dict
    lines counted as malformed and skipped (fail-soft). Missing file => ([], 0)."""
    records = []
    malformed = 0
    if not path.is_file():
        return records, malformed
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records, malformed
    for line in text.splitlines():
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


def atomic_write_jsonl(path: Path, records) -> None:
    """Write all records back as JSONL via tmp+replace (file-level atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def _clamp_confidence(n) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return 0
    if v < 0:
        return 0
    if v > 10:
        return 10
    return v


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------
def cmd_log(args, project_dir: Path) -> int:
    path = learnings_path(project_dir, args.feature)
    records, malformed = load_learnings(path)
    run_id = read_run_id(project_dir, args.feature)
    confidence = _clamp_confidence(args.confidence)
    now = _now_iso()
    source = args.source if args.source is not None else "observed"
    iteration = args.iteration  # may be None

    # Dedup on (type, key): merge into the existing entry rather than appending.
    existing = None
    for r in records:
        if r.get("type") == args.type and r.get("key") == args.key:
            existing = r
            break

    if existing is not None:
        prev_conf = _clamp_confidence(existing.get("confidence", 0))
        existing["confidence"] = max(prev_conf, confidence)
        existing["insight"] = args.insight
        existing["source"] = source
        if iteration is not None:
            existing["iteration"] = iteration
        existing["run_id"] = run_id
        existing["timestamp"] = now
        try:
            seen = int(existing.get("seen", 1))
        except (TypeError, ValueError):
            seen = 1
        existing["seen"] = seen + 1
        # id is preserved (never regenerated on merge).
        existing.setdefault("id", _new_id())
        existing.setdefault("stale", False)
        atomic_write_jsonl(path, records)
        print(f"learn: merged [{args.key}] ({args.type}) — seen={existing['seen']}, "
              f"confidence={existing['confidence']}/10, id={existing['id']}")
        return 0

    entry = {
        "id": _new_id(),
        "type": args.type,
        "key": args.key,
        "insight": args.insight,
        "confidence": confidence,
        "source": source,
        "iteration": iteration,
        "run_id": run_id,
        "timestamp": now,
        "seen": 1,
        "stale": False,
    }
    records.append(entry)
    atomic_write_jsonl(path, records)
    print(f"learn: logged [{args.key}] ({args.type}) — confidence={confidence}/10, id={entry['id']}")
    return 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def _tokens(text: str):
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def score_record(record: dict, query_tokens) -> int:
    """Keyword match count across key + insight + source. Each query token that
    appears (as a substring of any haystack token) contributes 1."""
    hay = " ".join([
        str(record.get("key", "")),
        str(record.get("insight", "")),
        str(record.get("source", "")),
    ]).lower()
    haytokens = set(_tokens(hay))
    score = 0
    for qt in query_tokens:
        if qt in haytokens or qt in hay:
            score += 1
    return score


def _iter_label(it):
    if it is None:
        return "?"
    return str(it)


def format_hit(record: dict) -> str:
    key = record.get("key", "?")
    typ = record.get("type", "?")
    conf = _clamp_confidence(record.get("confidence", 0))
    it = _iter_label(record.get("iteration"))
    insight = record.get("insight", "")
    return (f"Prior learning applied: [{key}] ({typ}, confidence {conf}/10, "
            f"iter {it}) — {insight}")


def gather_cross_feature(project_dir: Path):
    """Yield all learnings across sibling .scratch/*/learnings.jsonl files."""
    scratch = project_dir / ".scratch"
    out = []
    if not scratch.is_dir():
        return out
    for jf in sorted(scratch.glob("*/learnings.jsonl")):
        recs, _ = load_learnings(jf)
        out.extend(recs)
    return out


def cmd_search(args, project_dir: Path) -> int:
    if args.cross_feature:
        records = gather_cross_feature(project_dir)
    else:
        records, _ = load_learnings(learnings_path(project_dir, args.feature))

    if args.type:
        records = [r for r in records if r.get("type") == args.type]

    query_tokens = _tokens(args.query)
    scored = []
    for r in records:
        s = score_record(r, query_tokens)
        if s <= 0:
            continue
        scored.append((s, r))

    # Sort: non-stale first, then match score desc, then confidence desc.
    scored.sort(key=lambda sr: (
        1 if sr[1].get("stale") else 0,          # non-stale (0) before stale (1)
        -sr[0],                                   # higher match score first
        -_clamp_confidence(sr[1].get("confidence", 0)),
    ))

    limit = args.limit if args.limit and args.limit > 0 else 5
    top = scored[:limit]

    if not top:
        print(f"learn: no prior learnings matched '{args.query}'"
              + (" (cross-feature)" if args.cross_feature else "")
              + " — nothing to apply yet.")
        return 0

    print(f"== learn search: feature='{args.feature}' query='{args.query}'"
          + (" cross-feature" if args.cross_feature else "")
          + f" — {len(top)} hit(s) ==")
    for _, r in top:
        print(format_hit(r))
    return 0


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------
def _source_is_missing_file(source, project_dir: Path) -> bool:
    """True only when source looks like a file path that no longer exists. Non-path
    sources ('observed', commit hashes, URLs) are never treated as missing."""
    if not isinstance(source, str) or not source.strip():
        return False
    s = source.strip()
    if s == "observed" or s.lower().startswith(("http://", "https://", "commit:")):
        return False
    # Heuristic: treat as a path only if it contains a path separator or a dot-ext,
    # so a bare commit-ish token isn't mistaken for a path.
    looks_pathy = ("/" in s) or ("\\" in s) or ("." in Path(s).name)
    if not looks_pathy:
        return False
    p = Path(s)
    if not p.is_absolute():
        p = project_dir / s
    return not p.exists()


def cmd_prune(args, project_dir: Path) -> int:
    path = learnings_path(project_dir, args.feature)
    records, malformed = load_learnings(path)

    if not records:
        print(f"learn prune: no learnings under .scratch/{args.feature}/ — nothing to prune."
              + (f" ({malformed} malformed line(s) skipped)" if malformed else ""))
        return 0

    # Detect which records are stale: already-flagged stale OR a source file that
    # has since vanished. Dry-run does NOT persist anything (仅报告); only --apply
    # mutates the file (deleting the stale rows).
    newly_stale = 0
    is_stale = []
    for r in records:
        already = bool(r.get("stale"))
        missing = _source_is_missing_file(r.get("source"), project_dir)
        stale = already or missing
        if missing and not already:
            newly_stale += 1
        is_stale.append(stale)

    stale_records = [r for r, s in zip(records, is_stale) if s]

    if not args.apply:
        print(f"== learn prune (dry-run): feature='{args.feature}' ==")
        print(f"  {len(records)} learning(s), {len(stale_records)} stale "
              f"({newly_stale} newly detected this run).")
        for r in stale_records:
            print(f"  [stale] [{r.get('key','?')}] ({r.get('type','?')}) "
                  f"source={r.get('source','')}")
        if stale_records:
            print("  Re-run with --apply to delete the stale line(s).")
        else:
            print("  Nothing stale. (No --apply needed.)")
        return 0

    # --apply: drop stale rows and persist.
    kept = [r for r, s in zip(records, is_stale) if not s]
    removed = len(records) - len(kept)
    atomic_write_jsonl(path, kept)
    print(f"== learn prune (--apply): feature='{args.feature}' ==")
    print(f"  removed {removed} stale learning(s); {len(kept)} remain.")
    return 0


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------
def cmd_compile(args, project_dir: Path) -> int:
    records, _ = load_learnings(learnings_path(project_dir, args.feature))
    min_conf = args.min_confidence if args.min_confidence is not None else 6

    eligible = [
        r for r in records
        if r.get("type") == "pattern"
        and not r.get("stale")
        and _clamp_confidence(r.get("confidence", 0)) >= min_conf
    ]
    eligible.sort(key=lambda r: -_clamp_confidence(r.get("confidence", 0)))

    limit = args.limit if args.limit and args.limit > 0 else 10
    eligible = eligible[:limit]

    if not eligible:
        # Emit a friendly empty block so handoff injection has something stable.
        print("## 已验证经验 (verified learnings)")
        print()
        print(f"_(none yet — no non-stale pattern with confidence >= {min_conf})_")
        return 0

    print("## 已验证经验 (verified learnings)")
    print()
    print(f"_Patterns confirmed across iterations (confidence >= {min_conf}). "
          "Apply these before re-deriving them._")
    print()
    for r in eligible:
        key = r.get("key", "?")
        conf = _clamp_confidence(r.get("confidence", 0))
        insight = r.get("insight", "")
        src = r.get("source", "")
        seen = r.get("seen", 1)
        line = f"- **[{key}]** (confidence {conf}/10, seen {seen}x) — {insight}"
        if src and src != "observed":
            line += f"  _(src: {src})_"
        print(line)
    return 0


# ---------------------------------------------------------------------------
# argparse / dispatch
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="durable_loop_learn.py",
        description="Durable-loop experience-distillation (learnings) layer.",
    )
    sub = ap.add_subparsers(dest="subcommand", required=True)

    def add_common(p):
        p.add_argument("feature", help="name matching .scratch/<feature>/")
        p.add_argument("project_dir", nargs="?", default=".", help="project root (default: cwd)")

    p_log = sub.add_parser("log", help="record/merge a learning")
    add_common(p_log)
    p_log.add_argument("--type", required=True, choices=VALID_TYPES)
    p_log.add_argument("--key", required=True, help="kebab-case dedup key")
    p_log.add_argument("--insight", required=True, help="one/two-line reusable insight")
    p_log.add_argument("--confidence", required=True, type=int, help="0-10")
    p_log.add_argument("--source", default=None, help="file path / commit / 'observed'")
    p_log.add_argument("--iteration", type=int, default=None)

    p_search = sub.add_parser("search", help="find relevant prior learnings")
    add_common(p_search)
    p_search.add_argument("--query", required=True, help="space-separated keywords")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--type", choices=VALID_TYPES, default=None)
    p_search.add_argument("--cross-feature", action="store_true",
                          help="scan sibling .scratch/*/learnings.jsonl too")

    p_prune = sub.add_parser("prune", help="detect/remove stale learnings")
    add_common(p_prune)
    p_prune.add_argument("--apply", action="store_true",
                         help="actually delete stale lines (default: dry-run)")

    p_compile = sub.add_parser("compile", help="emit a 已验证经验 markdown block")
    add_common(p_compile)
    p_compile.add_argument("--min-confidence", type=int, default=6)
    p_compile.add_argument("--limit", type=int, default=10)

    return ap


_DISPATCH = {
    "log": cmd_log,
    "search": cmd_search,
    "prune": cmd_prune,
    "compile": cmd_compile,
}


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()  # argparse exits 2 on usage error

    if not NAME_RE.match(args.feature):
        die(f"invalid feature name '{args.feature}'")

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project_dir does not exist: {project_dir}")

    return _DISPATCH[args.subcommand](args, project_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — learnings layer must never raise into the caller
        print(f"durable_loop_learn.py: fail-open: {exc}", file=sys.stderr)
        sys.exit(0)
