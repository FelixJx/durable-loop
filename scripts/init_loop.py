#!/usr/bin/env python3
#
# init_loop.py — cross-platform Python port of init_loop.sh.
#
# Initializes a durable-loop .scratch/<feature>/ state directory. Mirrors
# init_loop.sh but uses only the Python stdlib, so it runs natively on Windows
# (PowerShell/cmd) without Git Bash. The .sh remains for Unix; this .py is the
# canonical cross-platform entry point.
#
# Usage:
#   python init_loop.py <feature> [project_dir] [--force]
#
# Creates, under <project_dir>/.scratch/<feature>/:
#   checkpoint.json   loop state (from assets, <FEATURE> replaced)
#   done.criteria.md  machine-verifiable convergence criteria
#   handoff.md        minimal context for full-context resets
#   tasks.jsonl       append-only task stream (empty)
#   decisions.log     ADR-style decision log (empty)
#   session.log       append-only observability log (empty)
#   intermediate/     filesystem offload for large outputs
#   dead_letter/      DLQ for permanently-failed idempotent ops
#   traces/           per-iteration execution traces
#
# Idempotent: existing files are NOT overwritten unless --force. Existing
# checkpoint.json state is ALWAYS preserved (durability) unless --force.

import argparse
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def die(msg: str) -> "NoReturn":
    print(f"init_loop.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def locate_assets() -> Path:
    # scripts/ -> skill root -> assets/
    return Path(__file__).resolve().parent.parent / "assets"


def write_file(dest: Path, src_asset: Path, feature: str, force: bool) -> str:
    if dest.is_file() and not force:
        return f"  keep  (exists): {dest.name}"
    if not src_asset.is_file():
        print(f"  WARN  missing asset, skipping: {src_asset.name}", file=sys.stderr)
        return ""
    text = src_asset.read_text(encoding="utf-8").replace("<FEATURE>", feature)
    dest.write_text(text, encoding="utf-8")
    return f"  wrote:           {dest.name}"


def write_checkpoint(dest: Path, src_asset: Path, feature: str, force: bool) -> str:
    """Like write_file, but for checkpoint.json: also injects a fresh run_id so
    each fresh start gets a unique trace identifier (session.log trace-ification).

    run_id is uuid4 hex. Injected only on a fresh start (this code path runs only
    when the checkpoint does not yet exist or --force is given); resume preserves
    the existing checkpoint untouched, so its run_id is never rewritten. If the
    asset is unparseable JSON we fall back to a plain placeholder replace so init
    never hard-fails on a malformed template (backward compatible)."""
    if dest.is_file() and not force:
        return f"  keep  (exists): {dest.name}"
    if not src_asset.is_file():
        print(f"  WARN  missing asset, skipping: {src_asset.name}", file=sys.stderr)
        return ""
    text = src_asset.read_text(encoding="utf-8").replace("<FEATURE>", feature)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        dest.write_text(text, encoding="utf-8")
        return f"  wrote:           {dest.name}"
    if not data.get("run_id"):
        data["run_id"] = uuid.uuid4().hex
    dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"  wrote:           {dest.name}"


def touch_file(dest: Path, force: bool) -> str:
    if dest.is_file() and not force:
        return f"  keep  (exists): {dest.name}"
    dest.write_text("", encoding="utf-8")
    return f"  wrote:           {dest.name}"


def feature_hint(feature: str, project_dir: Path) -> str:
    # On Windows the parent shell is PowerShell; on Unix it's bash/zsh. We can't
    # export back into the harness from this child process, so we just print the
    # exact line the user must run in the session that drives the loop.
    if sys.platform.startswith("win"):
        return (f"  PowerShell : $env:DURABLE_LOOP_FEATURE='{feature}'; "
                f"$env:DURABLE_LOOP_PROJECT_DIR='{project_dir}'")
    return (f"  bash/zsh   : export DURABLE_LOOP_FEATURE='{feature}'; "
            f"export DURABLE_LOOP_PROJECT_DIR='{project_dir}'")


def main() -> int:
    ap = argparse.ArgumentParser(description="Initialize a durable-loop state dir.")
    ap.add_argument("feature", help="loop/feature name (used as .scratch/<feature>/)")
    ap.add_argument("project_dir", nargs="?", default=".", help="project root (default: cwd)")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args()

    feature = args.feature
    if not NAME_RE.match(feature):
        die(f"invalid feature name '{feature}' (allowed: letters, digits, - and _ ; "
            "must start alphanumeric)")

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project_dir does not exist: {project_dir}")

    assets = locate_assets()
    if not assets.is_dir():
        die(f"skill assets dir not found: {assets}")

    feature_dir = project_dir / ".scratch" / feature
    for sub in ("intermediate", "dead_letter", "traces"):
        (feature_dir / sub).mkdir(parents=True, exist_ok=True)

    print(f"init_loop: feature='{feature}' dir='{feature_dir}'")
    print(f"  assets from:      {assets}")

    # checkpoint.json is the loop's state — never clobber an existing one without --force.
    cp = feature_dir / "checkpoint.json"
    if cp.is_file() and not args.force:
        print("  keep  (exists):  checkpoint.json  (existing loop state preserved)")
    else:
        print(write_checkpoint(cp, assets / "checkpoint.json", feature, args.force))

    print(write_file(feature_dir / "done.criteria.md", assets / "done.criteria.md", feature, args.force))
    print(write_file(feature_dir / "handoff.md", assets / "handoff.md", feature, args.force))
    print(touch_file(feature_dir / "tasks.jsonl", args.force))
    print(touch_file(feature_dir / "decisions.log", args.force))
    print(touch_file(feature_dir / "session.log", args.force))

    print("  dirs:             intermediate/ dead_letter/ traces/")
    print(f"init_loop: done. Next: edit {feature_dir}/done.criteria.md with task-specific criteria,")
    print(f"           then have your loop read {feature_dir}/checkpoint.json each iteration.")
    print("")
    print("IMPORTANT — bind the hooks to THIS feature (required if >1 loop shares this .scratch/):")
    print("  The PreToolUse/PostToolUse/Stop hooks auto-discover a single .scratch/<feature>/.")
    print("  When several loops run in parallel, set DURABLE_LOOP_FEATURE so each attaches to one.")
    print(feature_hint(feature, project_dir))
    print("  (set this in the SAME session/shell that runs the loop; a child-process export")
    print("   from this script does NOT propagate back to the Claude Code harness.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
