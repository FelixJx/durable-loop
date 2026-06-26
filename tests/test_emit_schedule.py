"""Category 6: emit_schedule.py — horizon -> scheduler scaffold generation,
default cadence (240s not 300s for min), file vs stdout output, and argument
validation (bad horizon / bad feature name / missing project_dir)."""
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"
PY = sys.executable
EMIT = str(SCRIPTS / "emit_schedule.py")


@pytest.fixture
def run_emit(tmp_path):
    def _run(feature, horizon, project_dir=None, extra=None):
        pd = str(project_dir) if project_dir is not None else str(tmp_path)
        cmd = [PY, EMIT, feature, horizon, pd]
        if extra:
            cmd += extra
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    return _run


def _sched_dir(tmp_path, feature):
    return tmp_path / ".scratch" / feature / "schedules"


# --- min horizon: advisory /loop usage + 240s note -----------------------

def test_min_prints_loop_usage(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "min")
    assert rc == 0
    assert "/loop" in out
    # default cadence for min must be 240s, NOT 300s.
    assert "240s" in out
    assert "300s" in out  # appears in the cautionary note text...
    # ...but only as the thing to AVOID, never as the chosen interval.
    assert "/loop 300s" not in out


def test_min_240s_zai_note_present(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "min")
    assert rc == 0
    low = out.lower()
    assert "z.ai" in low or "glm" in low
    assert "cache" in low
    assert "240s" in out


def test_min_writes_notes_file(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "min")
    assert rc == 0
    note = _sched_dir(tmp_path, "feat") / "min.txt"
    assert note.is_file()
    assert "240s" in note.read_text(encoding="utf-8")


def test_min_stdout_only_writes_no_file(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "min", extra=["--stdout"])
    assert rc == 0
    assert not _sched_dir(tmp_path, "feat").exists()
    assert "240s" in out


# --- hours horizon: Desktop Scheduled Task JSON --------------------------

def test_hours_emits_scheduled_task(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "hours")
    assert rc == 0
    f = _sched_dir(tmp_path, "feat") / "hours.json"
    assert f.is_file()
    text = f.read_text(encoding="utf-8")
    assert "Scheduled Task" in text
    assert "DURABLE_LOOP_FEATURE" in text
    assert "durable-loop-feat" in text
    # must be valid-ish JSON once comment lines are stripped.
    import json
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    json.loads(body)


# --- days horizon: Cloud Routine JSON ------------------------------------

def test_days_emits_cloud_routine(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "days")
    assert rc == 0
    f = _sched_dir(tmp_path, "feat") / "days.json"
    assert f.is_file()
    text = f.read_text(encoding="utf-8")
    assert "Cloud Routine" in text
    assert "1h" in text  # minimum interval note
    import json
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    json.loads(body)


# --- long horizon: GitHub Actions YAML -----------------------------------

def test_long_emits_github_actions_yaml(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "long")
    assert rc == 0
    f = _sched_dir(tmp_path, "feat") / "long.yml"
    assert f.is_file()
    text = f.read_text(encoding="utf-8")
    assert "schedule:" in text
    assert "cron:" in text
    assert "uses: actions/checkout" in text
    assert "claude" in text  # triggers the CLI


def test_long_custom_cron_interval(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "long", extra=["--interval", "0 */6 * * *"])
    assert rc == 0
    text = (_sched_dir(tmp_path, "feat") / "long.yml").read_text(encoding="utf-8")
    assert "0 */6 * * *" in text


def test_long_non_cron_interval_falls_back(tmp_path, run_emit):
    # a friendly cadence like "6h" is not a 5-field cron -> default cron used.
    rc, out, _ = run_emit("feat", "long", extra=["--interval", "6h"])
    assert rc == 0
    text = (_sched_dir(tmp_path, "feat") / "long.yml").read_text(encoding="utf-8")
    assert "0 6 * * *" in text


# --- custom command threads through ---------------------------------------

def test_custom_command_threads_through(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "hours", extra=["--command", "echo MYCMD"])
    assert rc == 0
    text = (_sched_dir(tmp_path, "feat") / "hours.json").read_text(encoding="utf-8")
    assert "MYCMD" in text


def test_default_command_references_verify_done(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "long")
    assert rc == 0
    text = (_sched_dir(tmp_path, "feat") / "long.yml").read_text(encoding="utf-8")
    assert "verify_done.py" in text


# --- argument validation --------------------------------------------------

@pytest.mark.parametrize("bad", ["weeks", "5min", "minute", "", "cron"])
def test_rejects_invalid_horizon(tmp_path, run_emit, bad):
    rc, _, err = run_emit("feat", bad)
    assert rc == 2
    assert "invalid horizon" in err


def test_horizon_case_insensitive(tmp_path, run_emit):
    rc, out, _ = run_emit("feat", "MIN")
    assert rc == 0
    assert "240s" in out


@pytest.mark.parametrize("bad", ["r&d", "a/b", "a b", ""])
def test_rejects_invalid_feature_name(tmp_path, run_emit, bad):
    rc, _, err = run_emit(bad, "hours")
    assert rc == 2
    assert "invalid feature name" in err


def test_missing_project_dir(tmp_path, run_emit):
    rc, _, err = run_emit("feat", "hours", project_dir=tmp_path / "nope")
    assert rc == 2
    assert "does not exist" in err
