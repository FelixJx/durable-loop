"""Category: improvement #5 — session.log trace-ification + replay_trace.py.

Covers:
  (a) init_loop.py injects a run_id into a fresh checkpoint; resume preserves it.
  (b) durable_loop_observe.py stamps each session.log line with the checkpoint run_id.
  (c) replay_trace.py renders a timeline grouped by run_id / iter, with phase
      transitions and a cost/timing rollup when those fields are present.
  (d) replay_trace.py is fail-open & friendly on missing dir / missing log / empty
      log / malformed lines (exit 0, no traceback).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import replay_trace as rt

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PY = sys.executable


def _run_replay(feature, project_dir):
    proc = subprocess.run(
        [PY, str(SCRIPTS / "replay_trace.py"), feature, str(project_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# --- (a) run_id injection in init_loop.py ---------------------------------
def test_init_injects_run_id(tmp_path, run_init):
    rc, _, _ = run_init("myfeat", tmp_path)
    assert rc == 0
    cp = json.loads((tmp_path / ".scratch/myfeat/checkpoint.json").read_text(encoding="utf-8"))
    assert cp.get("run_id"), "fresh checkpoint must carry a run_id"
    assert isinstance(cp["run_id"], str) and len(cp["run_id"]) >= 8


def test_init_run_id_unique_per_feature(tmp_path, run_init):
    run_init("a", tmp_path)
    run_init("b", tmp_path)
    rid_a = json.loads((tmp_path / ".scratch/a/checkpoint.json").read_text(encoding="utf-8"))["run_id"]
    rid_b = json.loads((tmp_path / ".scratch/b/checkpoint.json").read_text(encoding="utf-8"))["run_id"]
    assert rid_a != rid_b


def test_init_resume_preserves_run_id(tmp_path, run_init):
    run_init("myfeat", tmp_path)
    cp_path = tmp_path / ".scratch/myfeat/checkpoint.json"
    rid1 = json.loads(cp_path.read_text(encoding="utf-8"))["run_id"]
    # re-run WITHOUT --force => existing checkpoint (incl. run_id) preserved
    run_init("myfeat", tmp_path)
    rid2 = json.loads(cp_path.read_text(encoding="utf-8"))["run_id"]
    assert rid1 == rid2


def test_init_force_regenerates_run_id(tmp_path, run_init):
    run_init("myfeat", tmp_path)
    cp_path = tmp_path / ".scratch/myfeat/checkpoint.json"
    rid1 = json.loads(cp_path.read_text(encoding="utf-8"))["run_id"]
    run_init("myfeat", tmp_path, extra=["--force"])
    rid2 = json.loads(cp_path.read_text(encoding="utf-8"))["run_id"]
    assert rid1 != rid2  # fresh start => new run_id


# --- (b) observe hook stamps run_id ---------------------------------------
def test_observe_writes_run_id(tmp_path, make_checkpoint, run_hook):
    make_checkpoint("f", run_id="abc123", iteration=2, phase="building")
    rc, _ = run_hook("durable_loop_observe.py", {
        "tool_name": "Bash", "tool_input": {"command": "ls"},
        "tool_response": {"output": "ok"}, "cwd": str(tmp_path),
    })
    assert rc == 0
    log = (tmp_path / ".scratch/f/session.log").read_text(encoding="utf-8").strip()
    entry = json.loads(log.splitlines()[-1])
    assert entry["run_id"] == "abc123"
    assert entry["iter"] == 2 and entry["phase"] == "building"
    assert entry["tool"] == "Bash"


def test_observe_run_id_defaults_empty_for_legacy_checkpoint(tmp_path, make_checkpoint, run_hook):
    make_checkpoint("f", iteration=1)  # no run_id field at all
    rc, _ = run_hook("durable_loop_observe.py", {
        "tool_name": "Read", "tool_input": {"file_path": "x.py"},
        "tool_response": {"output": "ok"}, "cwd": str(tmp_path),
    })
    assert rc == 0
    entry = json.loads((tmp_path / ".scratch/f/session.log").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["run_id"] == ""  # backward compatible default


# --- (c) replay rendering --------------------------------------------------
def test_replay_groups_by_run_and_iter(tmp_path, write_session_log):
    write_session_log("f", [
        {"run_id": "r1", "iter": 1, "tool": "Bash", "phase": "planning", "action": "ls"},
        {"run_id": "r1", "iter": 1, "tool": "Read", "phase": "planning", "action": "read"},
        {"run_id": "r1", "iter": 2, "tool": "Edit", "phase": "building", "action": "edit"},
        {"run_id": "r2", "iter": 1, "tool": "Bash", "phase": "building", "action": "ls"},
    ])
    rc, out, err = _run_replay("f", tmp_path)
    assert rc == 0, err
    assert "run 1/2" in out and "run 2/2" in out
    assert "run_id=r1" in out and "run_id=r2" in out
    assert "iter 1: 2 call(s)" in out
    assert "iter 2: 1 call(s)" in out
    assert "planning -> building" in out  # phase transition for r1


def test_replay_cost_rollup(tmp_path, write_session_log):
    write_session_log("f", [
        {"run_id": "r1", "iter": 1, "tool": "Bash", "cost": 0.5, "tokens": 100},
        {"run_id": "r1", "iter": 2, "tool": "Bash", "cost": 0.25, "tokens": 50},
    ])
    rc, out, err = _run_replay("f", tmp_path)
    assert rc == 0, err
    assert "cost/timing" in out
    assert "TOTAL cost/timing" in out
    assert "tokens=150" in out
    assert "cost=0.75" in out


def test_replay_legacy_no_run_id_bucket(tmp_path, write_session_log):
    write_session_log("f", [
        {"iter": 1, "tool": "Bash", "action": "ls"},
        {"iter": 1, "tool": "Read", "action": "r"},
    ])
    rc, out, err = _run_replay("f", tmp_path)
    assert rc == 0, err
    assert "(no run_id)" in out
    assert "iter 1: 2 call(s)" in out


def test_replay_tolerates_malformed_lines(tmp_path):
    d = tmp_path / ".scratch" / "f"
    d.mkdir(parents=True)
    (d / "session.log").write_text(
        "not json at all\n"
        + json.dumps({"run_id": "r1", "iter": 1, "tool": "Bash"}) + "\n",
        encoding="utf-8",
    )
    rc, out, err = _run_replay("f", tmp_path)
    assert rc == 0, err
    assert "malformed" in out
    assert "run_id=r1" in out


# --- (d) fail-open / friendly ----------------------------------------------
def test_replay_no_scratch_dir(tmp_path):
    rc, out, _ = _run_replay("nope", tmp_path)
    assert rc == 0
    assert "nothing to replay" in out


def test_replay_no_session_log(tmp_path):
    (tmp_path / ".scratch" / "f").mkdir(parents=True)
    rc, out, _ = _run_replay("f", tmp_path)
    assert rc == 0
    assert "no session.log" in out


def test_replay_empty_log(tmp_path):
    d = tmp_path / ".scratch" / "f"
    d.mkdir(parents=True)
    (d / "session.log").write_text("", encoding="utf-8")
    rc, out, _ = _run_replay("f", tmp_path)
    assert rc == 0
    assert "empty" in out


def test_replay_bad_feature_name(tmp_path):
    rc, _, err = _run_replay("a b", tmp_path)
    assert rc == 2
    assert "invalid feature name" in err


# --- unit-level checks on the module internals -----------------------------
def test_group_runs_preserves_order():
    recs = [{"run_id": "b"}, {"run_id": "a"}, {"run_id": "b"}]
    groups = rt.group_runs(recs)
    assert [g[0] for g in groups] == ["b", "a"]
    assert len(groups[0][1]) == 2


def test_phase_transitions_collapses_repeats():
    recs = [{"phase": "p"}, {"phase": "p"}, {"phase": "?"}, {"phase": "b"}, {"phase": "b"}]
    assert rt.phase_transitions(recs) == ["p", "b"]


def test_cost_rollup_ignores_bools_and_missing():
    recs = [{"cost": 1}, {"cost": 2, "flag": True}, {"nope": 9}]
    assert rt.cost_rollup(recs) == {"cost": 3}
