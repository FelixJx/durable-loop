#!/usr/bin/env bash
#
# init_loop.sh — Initialize a durable-loop .scratch/<feature>/ state directory.
#
# Usage:
#   init_loop.sh <feature> [project_dir] [--force]
#
# Creates the full durable-loop state scaffold inside <project_dir>/.scratch/<feature>/:
#   checkpoint.json      loop state (copied from skill assets, <FEATURE> placeholder replaced)
#   done.criteria.md     machine-verifiable convergence criteria (quantity + quality)
#   handoff.md           minimal context for full-context resets
#   tasks.jsonl          append-only task stream (empty)
#   decisions.log        ADR-style decision log (empty)
#   session.log          append-only observability log (empty)
#   intermediate/        filesystem offload for large outputs
#   dead_letter/         DLQ for permanently-failed idempotent ops
#   traces/              per-iteration execution traces
#
# Idempotent: existing files are NOT overwritten unless --force is passed.
#
# The skill assets live at ~/.claude/skills/durable-loop/assets/ ; this script
# locates them relative to its own location (resolves symlinks).

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: init_loop.sh <feature> [project_dir] [--force]

  feature      name of the loop/feature (used as .scratch/<feature>/ subdir)
  project_dir  project root containing .scratch/ (default: current dir)
  --force      overwrite existing files (default: keep existing)

Examples:
  init_loop.sh red-dragonfly-activation ~/projects/red-dragonfly
  init_loop.sh quant-backtest . --force
EOF
}

die() { echo "init_loop.sh: ERROR: $*" >&2; exit 1; }

# --- locate skill root (resolve symlinks so this works under any cwd) ---
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
# Resolve symlinks (macOS coreutils readlink lacks -f; do it manually)
while [ -h "$SCRIPT_PATH" ]; do
  DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" >/dev/null 2>&1 && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  case "$SCRIPT_PATH" in
    /*) ;;
    *)  SCRIPT_PATH="$DIR/$SCRIPT_PATH" ;;
  esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" >/dev/null 2>&1 && pwd)"
SKILL_DIR="$(cd -P "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
ASSETS_DIR="$SKILL_DIR/assets"

[ -d "$ASSETS_DIR" ] || die "skill assets dir not found: $ASSETS_DIR"

# --- parse args ---
FORCE=0
FEATURE=""
PROJECT_DIR=""

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --force)   FORCE=1 ;;
    -*)        die "unknown option: $arg (try --help)" ;;
    *)
      if [ -z "$FEATURE" ]; then
        FEATURE="$arg"
      elif [ -z "$PROJECT_DIR" ]; then
        PROJECT_DIR="$arg"
      else
        die "unexpected extra argument: $arg"
      fi
      ;;
  esac
done

[ -n "$FEATURE" ] || { usage; exit 1; }
[ -n "$PROJECT_DIR" ] || PROJECT_DIR="."

# validate feature name with a whitelist: [A-Za-z0-9_-], must start alphanumeric.
# This blocks path traversal (/, ..), spaces, AND sed-replacement metacharacters
# (& = matched text, \ = escape) that would corrupt the <FEATURE> substitution
# in write_file() below. A positive whitelist is safer than enumerating bad chars.
if ! printf '%s' "$FEATURE" | grep -qE '^[A-Za-z0-9][A-Za-z0-9_-]*$'; then
  die "invalid feature name '$FEATURE' (allowed: letters, digits, - and _ ; must start alphanumeric)"
fi

[ -d "$PROJECT_DIR" ] || die "project_dir does not exist: $PROJECT_DIR"
PROJECT_DIR="$(cd -P "$PROJECT_DIR" >/dev/null 2>&1 && pwd)"

SCRATCH_DIR="$PROJECT_DIR/.scratch"
FEATURE_DIR="$SCRATCH_DIR/$FEATURE"

mkdir -p "$FEATURE_DIR/intermediate" "$FEATURE_DIR/dead_letter" "$FEATURE_DIR/traces"

write_file() {
  # write_file <dest> <src_asset> [extra sed-replace]
  local dest="$1" src="$2"
  if [ -f "$dest" ] && [ "$FORCE" -eq 0 ]; then
    echo "  keep  (exists): $(basename "$dest")"
    return 0
  fi
  if [ ! -f "$src" ]; then
    echo "  WARN  missing asset, skipping: $(basename "$src")" >&2
    return 0
  fi
  sed "s/<FEATURE>/$FEATURE/g" "$src" > "$dest"
  echo "  wrote:           $(basename "$dest")"
}

touch_file() {
  # touch_file <dest> — create empty append-only file if absent
  local dest="$1"
  if [ -f "$dest" ] && [ "$FORCE" -eq 0 ]; then
    echo "  keep  (exists): $(basename "$dest")"
    return 0
  fi
  : > "$dest"
  echo "  wrote:           $(basename "$dest")"
}

echo "init_loop: feature='$FEATURE' dir='$FEATURE_DIR'"
echo "  assets from:      $ASSETS_DIR"

# checkpoint.json — the one file we never keep stale on --force because it IS the state;
# but by default we never clobber existing state (that's the whole point of durability).
if [ -f "$FEATURE_DIR/checkpoint.json" ] && [ "$FORCE" -eq 0 ]; then
  echo "  keep  (exists):  checkpoint.json  (existing loop state preserved)"
else
  write_file "$FEATURE_DIR/checkpoint.json" "$ASSETS_DIR/checkpoint.json"
fi

write_file "$FEATURE_DIR/done.criteria.md" "$ASSETS_DIR/done.criteria.md"
write_file "$FEATURE_DIR/handoff.md"       "$ASSETS_DIR/handoff.md"
touch_file "$FEATURE_DIR/tasks.jsonl"
touch_file "$FEATURE_DIR/decisions.log"
touch_file "$FEATURE_DIR/session.log"

echo "  dirs:             intermediate/ dead_letter/ traces/"
echo "init_loop: done. Next: edit $FEATURE_DIR/done.criteria.md with task-specific criteria,"
echo "           then have your loop read $FEATURE_DIR/checkpoint.json each iteration."
echo ""
echo "IMPORTANT — bind the hooks to THIS feature (required if >1 loop shares this .scratch/):"
echo "  The PreToolUse/PostToolUse/Stop hooks auto-discover a single .scratch/<feature>/."
echo "  When several loops run in parallel, set DURABLE_LOOP_FEATURE so each attaches to one."
# Detect the parent shell so we print the right env-set syntax. Git Bash on
# Windows reports a MINGW*/MSYS*/CYGWIN* uname; native Windows Claude Code runs
# PowerShell, whose syntax is \$env:NAME (printed literally here for the user).
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "  PowerShell : \$env:DURABLE_LOOP_FEATURE='$FEATURE'; \$env:DURABLE_LOOP_PROJECT_DIR='$PROJECT_DIR'"
    ;;
  *)
    echo "  bash/zsh   : export DURABLE_LOOP_FEATURE='$FEATURE'; export DURABLE_LOOP_PROJECT_DIR='$PROJECT_DIR'"
    ;;
esac
echo "  (set this in the SAME session/shell that runs the loop; a child-process export"
echo "   from this script does NOT propagate back to the Claude Code harness.)"
