#!/usr/bin/env python3
#
# emit_schedule.py — cross-session / scheduling scaffold generator for durable-loop.
#
# Implements the SKILL.md scheduling-selection table (step 1 "选调度模式") as a
# CLI: given a feature + a time horizon, it emits a ready-to-edit config skeleton
# for the right scheduler. The skill's decision table maps horizon -> mechanism:
#
#   horizon  | mechanism                         | notes
#   ---------|-----------------------------------|--------------------------------
#   min      | /loop <interval> (in-session)     | z.ai/GLM: use 240s, NOT 300s
#   hours    | Desktop Scheduled Tasks           | survives across sessions
#   days     | Cloud Routines                    | Anthropic-hosted, 1h min interval
#   long     | GitHub Actions (cron + claude CLI)| >7 days, unattended / CI
#
# Usage:
#   python emit_schedule.py <feature> <horizon> [project_dir]
#                           [--interval <spec>] [--command <cmd>] [--stdout]
#
#   <horizon>  one of: min | hours | days | long
#   --interval scheduler-specific cadence (e.g. 240s, 30m, 6h, "0 */6 * * *").
#              Defaulted per horizon when omitted.
#   --command  the command the schedule should run (default: the durable-loop
#              driver invocation for <feature>).
#   --stdout   print the skeleton to stdout instead of writing a file.
#
# By default the skeleton is written under
#   <project_dir>/.scratch/<feature>/schedules/<horizon>.<ext>
# (and the path is printed). The min horizon is advisory-only: it always prints
# /loop usage to stdout (there is no file to schedule) and additionally drops a
# notes file unless --stdout.
#
# FAIL-OPEN: this is a scaffold generator, never a hook — it does NOT need an
# existing .scratch/<feature>/ to run (it will create schedules/ on demand). But
# it mirrors the durable-loop convention: if <project_dir> is missing it errors
# with a clear usage message (exit 2) rather than guessing. It NEVER blocks or
# touches another loop's state; multiple .scratch/<feature>/ are irrelevant here
# because the feature is always passed explicitly.
#
# Exit codes: 0 ok, 2 usage / argument error (mirrors verify_done.py).

import argparse
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

HORIZONS = ("min", "hours", "days", "long")

# Default cadence per horizon. min uses 240s (NOT 300s) — see z.ai/GLM note.
DEFAULT_INTERVAL = {
    "min": "240s",
    "hours": "6h",
    "days": "24h",
    "long": "0 6 * * *",   # daily 06:00 UTC cron
}

# z.ai/GLM cache inference, surfaced wherever a min-level cadence is chosen.
ZAI_240S_NOTE = (
    "z.ai/GLM note: third-party providers have a fixed 5min cache TTL. A 300s "
    "interval lands ON the cache boundary, so EVERY iteration cache-misses and "
    "cost roughly doubles. Use 240s, NOT 300s. For monitoring/alert/CI-polling "
    "tasks prefer event-driven (watch / MCP stream) over cron to skip polling cost."
)


def die(msg: str) -> "NoReturn":
    print(f"emit_schedule.py: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def default_command(feature: str) -> str:
    """The canonical durable-loop driver invocation for a feature."""
    return (
        "claude -p \"Resume the durable-loop for feature '"
        + feature
        + "'. Read .scratch/"
        + feature
        + "/checkpoint.json, run ONE iteration per the loop-driver-prompt, then "
        "run scripts/verify_done.py "
        + feature
        + " and atomically write the checkpoint.\""
    )


# --- skeleton builders ----------------------------------------------------
# Each returns (text, file_ext). min returns ext "" (no schedulable file).

def build_min(feature: str, interval: str, command: str) -> tuple:
    text = (
        f"# min-level schedule for feature '{feature}'\n"
        f"# Mechanism: /loop <interval> — runs IN-SESSION (no cross-session persistence).\n"
        f"# Suited to: iterative optimization / refactor / research at <5min cadence.\n"
        f"#\n"
        f"# Drive it by pasting this into the Claude Code session that owns the loop:\n"
        f"#\n"
        f"#   /loop {interval} \"<loop-driver-prompt for {feature}>\"\n"
        f"#\n"
        f"# (Fill <loop-driver-prompt ...> from assets/loop-driver-prompt.md with the\n"
        f"#  <FEATURE>={feature} placeholder + the other placeholders replaced.)\n"
        f"#\n"
        f"# Equivalent default command this would wake each tick:\n"
        f"#   {command}\n"
        f"#\n"
        f"# {ZAI_240S_NOTE}\n"
    )
    return text, "txt"


def build_hours(feature: str, interval: str, command: str, project_dir: Path) -> tuple:
    # Desktop Scheduled Task config fragment (~/.claude/scheduled_tasks/).
    text = (
        "# Desktop Scheduled Task fragment for durable-loop feature '"
        + feature
        + "'.\n"
        "# Mechanism: Desktop Scheduled Tasks (~/.claude/scheduled_tasks/) — survives\n"
        "# across sessions; use for hours-long tasks with local-file dependencies.\n"
        "# Drop this under ~/.claude/scheduled_tasks/ and edit the schedule/command.\n"
        "{\n"
        f'  "name": "durable-loop-{feature}",\n'
        f'  "feature": "{feature}",\n'
        f'  "schedule": "{interval}",\n'
        f'  "working_dir": "{project_dir.as_posix()}",\n'
        '  "env": {\n'
        f'    "DURABLE_LOOP_FEATURE": "{feature}",\n'
        f'    "DURABLE_LOOP_PROJECT_DIR": "{project_dir.as_posix()}"\n'
        "  },\n"
        f'  "command": {_json_str(command)},\n'
        '  "enabled": true,\n'
        '  "notes": "Cross-session persistent. Each tick wakes one loop iteration; '
        'convergence is gated ONLY by scripts/verify_done.py, not by this scheduler."\n'
        "}\n"
    )
    return text, "json"


def build_days(feature: str, interval: str, command: str, project_dir: Path) -> tuple:
    # Cloud Routine config fragment (Anthropic-hosted, 1h minimum interval).
    text = (
        "# Cloud Routine fragment for durable-loop feature '"
        + feature
        + "'.\n"
        "# Mechanism: Cloud Routines (Anthropic-hosted) — for days-long tasks with NO\n"
        "# local-file dependency. Minimum interval is 1h; intervals below that are\n"
        "# clamped up to 1h. Each run starts a fresh session, so the loop MUST resume\n"
        "# from .scratch/" + feature + "/checkpoint.json rather than in-memory state.\n"
        "{\n"
        f'  "name": "durable-loop-{feature}",\n'
        f'  "feature": "{feature}",\n'
        f'  "type": "cloud_routine",\n'
        f'  "interval": "{interval}",\n'
        '  "min_interval": "1h",\n'
        f'  "prompt": {_json_str(command)},\n'
        '  "enabled": true,\n'
        '  "notes": "No local files. Resume-from-checkpoint each run. verify_done is '
        'the only convergence gate."\n'
        "}\n"
    )
    return text, "json"


def build_long(feature: str, interval: str, command: str) -> tuple:
    # GitHub Actions workflow YAML skeleton: cron schedule + claude CLI trigger.
    cron = interval if _looks_like_cron(interval) else DEFAULT_INTERVAL["long"]
    text = (
        "# GitHub Actions workflow for durable-loop feature '" + feature + "'.\n"
        "# Mechanism: >7-day unattended / CI-integrated. Each run starts a NEW session,\n"
        "# so the job resumes from the committed checkpoint. Commit .scratch/" + feature + "/\n"
        "# (or restore it from an artifact/cache) so state survives across runs.\n"
        f"name: durable-loop-{feature}\n"
        "\n"
        "on:\n"
        "  schedule:\n"
        f"    - cron: \"{cron}\"   # edit cadence; >7-day horizons typically run daily\n"
        "  workflow_dispatch: {}   # allow manual kick\n"
        "\n"
        "permissions:\n"
        "  contents: write   # to commit updated .scratch/ checkpoint state\n"
        "\n"
        "jobs:\n"
        "  iterate:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: \"3.x\"\n"
        "      - name: Install Claude CLI\n"
        "        run: npm install -g @anthropic-ai/claude-code\n"
        "      - name: Run one durable-loop iteration\n"
        "        env:\n"
        "          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n"
        f"          DURABLE_LOOP_FEATURE: {feature}\n"
        "        run: |\n"
        f"          {command}\n"
        f"          python scripts/verify_done.py {feature} || echo \"not converged yet\"\n"
        "      - name: Persist checkpoint state\n"
        "        run: |\n"
        "          git config user.name  \"durable-loop-bot\"\n"
        "          git config user.email \"durable-loop-bot@users.noreply.github.com\"\n"
        f"          git add .scratch/{feature}/ || true\n"
        "          git commit -m \"durable-loop: checkpoint after scheduled iteration\" || echo \"nothing to commit\"\n"
        "          git push || echo \"push skipped\"\n"
    )
    return text, "yml"


def _json_str(s: str) -> str:
    """Minimal JSON string escaping for embedding a command in a JSON fragment."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + out + '"'


def _looks_like_cron(spec: str) -> bool:
    # A 5-field cron expression (space-separated). Anything else (e.g. "30m",
    # "6h") is treated as a friendly cadence and we fall back to the cron default.
    return len(spec.split()) == 5


def build_skeleton(horizon: str, feature: str, interval: str, command: str,
                   project_dir: Path) -> tuple:
    if horizon == "min":
        return build_min(feature, interval, command)
    if horizon == "hours":
        return build_hours(feature, interval, command, project_dir)
    if horizon == "days":
        return build_days(feature, interval, command, project_dir)
    return build_long(feature, interval, command)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Emit a scheduling scaffold for a durable-loop feature per the "
                    "SKILL.md horizon -> scheduler selection table.")
    ap.add_argument("feature", help="loop/feature name (matches .scratch/<feature>/)")
    ap.add_argument("horizon", help="time horizon: min | hours | days | long")
    ap.add_argument("project_dir", nargs="?", default=".",
                    help="project root (default: cwd)")
    ap.add_argument("--interval", default=None,
                    help="scheduler cadence (e.g. 240s, 6h, '0 6 * * *'); "
                         "defaulted per horizon when omitted")
    ap.add_argument("--command", default=None,
                    help="command the schedule runs (default: the durable-loop "
                         "driver invocation for <feature>)")
    ap.add_argument("--stdout", action="store_true",
                    help="print the skeleton to stdout instead of writing a file")
    args = ap.parse_args()

    feature = args.feature
    if not NAME_RE.match(feature):
        die(f"invalid feature name '{feature}' (allowed: letters, digits, - and _ ; "
            "must start alphanumeric)")

    horizon = args.horizon.strip().lower()
    if horizon not in HORIZONS:
        die(f"invalid horizon '{args.horizon}' — choose one of: {' | '.join(HORIZONS)}")

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project_dir does not exist: {project_dir}")

    interval = args.interval or DEFAULT_INTERVAL[horizon]
    command = args.command or default_command(feature)

    text, ext = build_skeleton(horizon, feature, interval, command, project_dir)

    # min is advisory-only: there's no schedulable file to hand to a scheduler, so
    # always print /loop usage. (Still drop a notes file unless --stdout, for the
    # record.) Other horizons write the config skeleton unless --stdout.
    if horizon == "min":
        print(text)
        if not args.stdout:
            sched_dir = project_dir / ".scratch" / feature / "schedules"
            sched_dir.mkdir(parents=True, exist_ok=True)
            dest = sched_dir / f"{horizon}.{ext}"
            dest.write_text(text, encoding="utf-8")
            print(f"emit_schedule: wrote notes to {dest}", file=sys.stderr)
        return 0

    if args.stdout:
        print(text)
        return 0

    sched_dir = project_dir / ".scratch" / feature / "schedules"
    sched_dir.mkdir(parents=True, exist_ok=True)
    dest = sched_dir / f"{horizon}.{ext}"
    dest.write_text(text, encoding="utf-8")
    print(f"emit_schedule: feature='{feature}' horizon='{horizon}' "
          f"interval='{interval}'")
    print(f"  wrote: {dest}")
    print(f"  edit the cadence/command, then register it with the {horizon}-level "
          f"scheduler per SKILL.md.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — scaffold tool, fail loud-but-clean
        die(f"unexpected error: {exc}")
