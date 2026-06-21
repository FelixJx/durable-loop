#!/usr/bin/env bash
#
# verify_done.sh — Machine-verifiable convergence evaluator for durable-loop.
#
# This is the EVALUATOR half of generator/evaluator separation. The loop actor
# (generator) writes code; this script independently checks whether the
# machine-verifiable criteria in done.criteria.md actually pass. The actor MUST
# NOT self-declare "done" — Huang判据: pure intrinsic self-correction makes
# models declare done at ~30% completion.
#
# Usage:
#   verify_done.sh <feature> [project_dir] [--timeout <secs>]
#
# Reads <project_dir>/.scratch/<feature>/done.criteria.md, extracts each
# checkbox's verification command, runs it, and prints:
#   [PASS] criteria text
#   [FAIL] criteria text — <reason>
#   [MANUAL] criteria text — no machine command, requires human/judge judgment
# Then prints a final:
#   VERDICT: DONE       (all executable criteria PASS, no FAIL)
#   VERDICT: NOT DONE   (any FAIL, or any MANUAL left unjudged)
# Exit 0 on DONE, exit 1 on NOT DONE.
#
# Command extraction — supports ONE convention:
#   1. HTML comment on the checkbox line:
#        - [ ] some criterion <!-- cmd: shell --command here -->
#      The cmd may span the rest of the line. Multiple <!-- cmd: ... --> on one
#      line are each run; ALL must pass for that checkbox to PASS.
#   A checkbox with no <!-- cmd: ... --> is [MANUAL] (needs human/judge sign-off).
#
#   NOTE: inline backtick text is intentionally NOT treated as a command. The
#   criteria template uses prose backticks (paths, words like `idempotency_keys`,
#   `# TODO`) that must never run — only <!-- cmd: --> runs. (Earlier versions
#   grabbed the first backtick string and executed it, which produced false
#   FAILs on paths and a false PASS on `# TODO`, a bash comment.)
#
# Each command runs under `timeout` (default 120s, override with --timeout) in
# the project_dir. Non-zero exit = FAIL; timeout = FAIL with a timeout reason.
# Commands run with `set +e` semantics inside this script; one FAIL does not
# abort the rest of the evaluation.

set -uo pipefail
# NOTE: deliberately NOT `set -e` — we collect per-criterion pass/fail.

DEFAULT_TIMEOUT=120
TIMEOUT=$DEFAULT_TIMEOUT

usage() {
  cat <<'EOF'
Usage: verify_done.sh <feature> [project_dir] [--timeout <secs>]

  feature      name matching .scratch/<feature>/
  project_dir  project root (default: current dir)
  --timeout    per-command timeout in seconds (default: 120)

Exit codes:
  0  VERDICT: DONE   (all executable criteria PASS, none FAIL, none MANUAL-pending)
  1  VERDICT: NOT DONE
  2  usage error / file missing
EOF
}

die() { echo "verify_done.sh: ERROR: $*" >&2; exit 2; }

# --- parse args ---
FEATURE=""
PROJECT_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --timeout) shift; [ $# -gt 0 ] || die "--timeout needs a value"; TIMEOUT="$1" ;;
    --timeout=*) TIMEOUT="${1#--timeout=}" ;;
    -*) die "unknown option: $1 (try --help)" ;;
    *)
      if [ -z "$FEATURE" ]; then FEATURE="$1"
      elif [ -z "$PROJECT_DIR" ]; then PROJECT_DIR="$1"
      else die "unexpected extra argument: $1"
      fi
      ;;
  esac
  shift
done

[ -n "$FEATURE" ] || { usage; exit 2; }
[ -n "$PROJECT_DIR" ] || PROJECT_DIR="."
case "$FEATURE" in */*|*" "*) die "invalid feature name '$FEATURE'" ;; esac

# validate timeout is a positive integer
case "$TIMEOUT" in
  ''|*[!0-9]*) die "--timeout must be a positive integer, got '$TIMEOUT'" ;;
esac
[ "$TIMEOUT" -gt 0 ] || die "--timeout must be > 0"

[ -d "$PROJECT_DIR" ] || die "project_dir does not exist: $PROJECT_DIR"
PROJECT_DIR="$(cd -P "$PROJECT_DIR" >/dev/null 2>&1 && pwd)"
CRITERIA="$PROJECT_DIR/.scratch/$FEATURE/done.criteria.md"
[ -f "$CRITERIA" ] || die "criteria file not found: $CRITERIA
  Run init_loop.sh '$FEATURE' '$PROJECT_DIR' first, then edit done.criteria.md."

PASS_COUNT=0
FAIL_COUNT=0
MANUAL_COUNT=0
FAIL_REASONS=()

# Emit a short label for a checkbox line (strip markdown checkbox + cmd markers).
label_of() {
  # strip leading "- [ ]"/"- [x]" and surrounding markdown
  sed -E 's/^[[:space:]]*- \[[ xX]\][[:space:]]*//; s/<!-- cmd:.*-->//g; s/`[^`]*`//g' \
    | sed -E 's/\*\*//g; s/[[:space:]]+$//' | cut -c1-90
}

# Run one command; echo "PASS" or "FAIL: <reason>". Runs in project_dir.
# timeout exit codes: 124 (timed out), 137 (SIGKILL), 143 (SIGTERM, e.g. from
# --preserve-status or OOM-adjacent kills) — all treated as timeout/kill FAIL.
run_cmd() {
  local cmd="$1"
  local out rc
  out="$(cd "$PROJECT_DIR" && timeout "$TIMEOUT" bash -c "$cmd" 2>&1 </dev/null)" && rc=0 || rc=$?
  case "$rc" in
    124|137|143)
      echo "FAIL: timed out after ${TIMEOUT}s (rc=$rc)"
      return
      ;;
  esac
  # Detect "command not found" / "No such file" in output — these indicate
  # the tool/project isn't installed, which should FAIL rather than silently pass.
  case "$out" in
    *"command not found"*|*"No such file or directory"*|*"not found"*)
      echo "FAIL: command not found / missing dependency — $cmd"
      return
      ;;
  esac
  if [ $rc -ne 0 ]; then
    # keep first line of stderr/out as the reason
    local firstline
    firstline="$(printf '%s' "$out" | head -1 | cut -c1-160)"
    [ -n "$firstline" ] || firstline="exit code $rc"
    echo "FAIL: exit $rc — $firstline"
    return
  fi
  echo "PASS"
}

# Process one checkbox line: extract commands, run them, emit verdict.
process_checkbox() {
  local line="$1"
  local label
  label="$(printf '%s\n' "$line" | label_of)"
  [ -n "$label" ] || label="(unnamed criterion)"

  # Collect commands: ONLY HTML <!-- cmd: ... --> comments. Inline backticks are
  # prose, never commands (see header docstring).
  local cmds=()
  # Extract every <!-- cmd: ... --> on the line (grep -o, portable).
  local html_cmds
  html_cmds="$(printf '%s\n' "$line" | grep -oE '<!-- cmd: .*-->' || true)"
  if [ -n "$html_cmds" ]; then
    while IFS= read -r h; do
      [ -n "$h" ] || continue
      # strip the wrapper
      c="$(printf '%s\n' "$h" | sed -E 's/<!-- cmd:[[:space:]]*//; s/[[:space:]]*-->$//')"
      [ -n "$c" ] && cmds+=("$c")
    done <<<"$html_cmds"
  fi

  if [ "${#cmds[@]}" -eq 0 ]; then
    MANUAL_COUNT=$((MANUAL_COUNT + 1))
    printf '[MANUAL] %s — no machine command; needs human/judge judgment\n' "$label"
    return
  fi

  local all_pass=1
  local reason=""
  for c in "${cmds[@]}"; do
    local res
    res="$(run_cmd "$c")"
    case "$res" in
      PASS*) ;;
      *)
        all_pass=0
        reason="$res"
        break
        ;;
    esac
  done

  if [ "$all_pass" -eq 1 ]; then
    PASS_COUNT=$((PASS_COUNT + 1))
    printf '[PASS] %s\n' "$label"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAIL_REASONS+=("$label :: $reason")
    printf '[FAIL] %s — %s\n' "$label" "${reason#FAIL: }"
  fi
}

# --- main: walk criteria file ---
echo "== verify_done: feature='$FEATURE' criteria='$CRITERIA' timeout=${TIMEOUT}s =="
echo

checked_any=0
while IFS= read -r rawline || [ -n "$rawline" ]; do
  # Match a markdown task checkbox that STARTS a list item: optional leading
  # indent, a bullet (-, *, +), then [ ]/[x]/[X]. Anchoring to the line start
  # avoids matching prose that merely mentions "- [ ]" mid-sentence (the old
  # nested-case matcher was a no-op and matched any line containing the marker).
  if printf '%s\n' "$rawline" | grep -qE '^[[:space:]]*[-*+][[:space:]]+\[[ xX]\][[:space:]]'; then
    process_checkbox "$rawline"
    checked_any=1
  fi
done <"$CRITERIA"

echo
echo "-------------------------------------------"
echo "PASS: $PASS_COUNT  FAIL: $FAIL_COUNT  MANUAL(pending): $MANUAL_COUNT"

if [ "$checked_any" -eq 0 ]; then
  echo "VERDICT: NOT DONE — no checkbox criteria found in done.criteria.md"
  exit 1
fi

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "VERDICT: NOT DONE — $FAIL_COUNT criterion/criteria FAILED"
  exit 1
fi

if [ "$MANUAL_COUNT" -gt 0 ]; then
  echo "VERDICT: NOT DONE — $MANUAL_COUNT criterion/criteria require manual/judge sign-off"
  echo "  (Have an independent evaluator confirm them, then re-run after removing"
  echo "   the MANUAL items or attaching <!-- cmd: ... --> commands.)"
  exit 1
fi

echo "VERDICT: DONE — all executable criteria PASS"
echo "  NOTE: this is the machine-verifiable gate only. For full done, an"
echo "  independent evaluator (non-actor model or human) must still confirm the"
echo "  QUALITY anti-gaming criteria and write 'JUDGE: PASS'."
exit 0
