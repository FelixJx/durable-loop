"""Shared fixtures + helpers for the durable-loop test suite.

Tests import the hook modules directly (pure Python, cross-platform) and invoke
the init_loop.py / verify_done.py entry points via subprocess. No bash/GNU-timeout
dependency — the suite runs under Windows python.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"
PY = sys.executable

# Put scripts/ on sys.path at collection time so test modules can
# `import check_budget` / `import durable_loop_checkpoint` at top level.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def skill_root():
    return SKILL_ROOT


@pytest.fixture(autouse=True)
def _scripts_on_path():
    sys.path.insert(0, str(SCRIPTS))
    yield
    sys.path.pop(0)


def _base_checkpoint(feature="f"):
    return {
        "feature": feature, "iteration": 0, "phase": "planning",
        "started_at": "", "last_updated": "", "last_action": "", "last_result": "",
        "cumulative_state": {},
        "budget_used": {"tokens": 0, "dollars": 0.0, "iterations": 0, "hours": 0},
        "max_budget": {"tokens": 5000000, "dollars": 10.0, "iterations": 25, "hours": 6},
        "status": "fresh", "resume_from": "", "idempotency_keys": [], "thrashing_counter": 0,
    }


@pytest.fixture
def make_checkpoint(tmp_path):
    """Returns cp_path after writing checkpoint.json under tmp/.scratch/<feature>/."""
    def _make(feature="f", **overrides):
        d = _base_checkpoint(feature)
        d.update(overrides)
        cp_dir = tmp_path / ".scratch" / feature
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp = cp_dir / "checkpoint.json"
        cp.write_text(json.dumps(d), encoding="utf-8")
        return cp
    return _make


@pytest.fixture
def write_session_log(tmp_path):
    def _write(feature, actions):
        p = tmp_path / ".scratch" / feature / "session.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [a if isinstance(a, str) else json.dumps(a) for a in actions]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p
    return _write


@pytest.fixture
def run_hook():
    """Invoke a hook script via subprocess feeding stdin JSON. Returns (rc, stderr)."""
    def _run(script_name, stdin_obj, env=None):
        proc = subprocess.run(
            [PY, str(SCRIPTS / script_name)],
            input=json.dumps(stdin_obj), text=True, capture_output=True,
            env=env, cwd=stdin_obj.get("cwd"),
        )
        return proc.returncode, proc.stderr
    return _run


@pytest.fixture
def run_verify_done(tmp_path):
    def _run(feature, criteria_text, timeout=8):
        cdir = tmp_path / ".scratch" / feature
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "done.criteria.md").write_text(criteria_text, encoding="utf-8")
        proc = subprocess.run(
            [PY, str(SCRIPTS / "verify_done.py"), feature, str(tmp_path), "--timeout", str(timeout)],
            capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout + "\n" + proc.stderr
    return _run


@pytest.fixture
def run_init(tmp_path):
    def _run(feature, project_dir=None, extra=None):
        pd = str(project_dir) if project_dir else str(tmp_path)
        cmd = [PY, str(SCRIPTS / "init_loop.py"), feature, pd]
        if extra:
            cmd += extra
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr
    return _run
