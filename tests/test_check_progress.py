"""Improvement #4: check_progress.py — no-progress detector (DEFAULT OFF).

Covers the four required cases:
  1. N adjacent rounds with no progress -> pause for approval
  2. progress between rounds -> no pause
  3. default OFF (no env / no checkpoint field) -> pure no-op
  4. no checkpoint / ambiguous -> fail open (no-op)

Tests drive evaluate() directly for the iteration-by-iteration state machine, and
exercise the CLI / hook entry points via subprocess for discovery + fail-open.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import check_progress as cp

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PY = sys.executable


def _set_metrics(make_checkpoint, feature, iteration, score, status="running", **extra):
    """(Re)write the checkpoint to simulate one iteration's state, then return path."""
    return make_checkpoint(
        feature,
        iteration=iteration,
        status=status,
        no_progress_limit=extra.pop("no_progress_limit", 0),
        last_result=extra.pop("last_result", f"score={score}"),
        cumulative_state={"metrics_snapshot": {"score": score}},
        **extra,
    )


def _rewrite(cp_path: Path, **fields):
    d = json.loads(cp_path.read_text(encoding="utf-8"))
    d.update(fields)
    cp_path.write_text(json.dumps(d), encoding="utf-8")


# --- 1. N adjacent no-progress rounds -> pause -----------------------------
def test_no_progress_triggers_pause(make_checkpoint, monkeypatch):
    monkeypatch.delenv("DURABLE_LOOP_NOPROGRESS_N", raising=False)
    cp_path = _set_metrics(make_checkpoint, "f", 1, 0.5,
                           no_progress_limit=3, last_result="stuck")
    # Three iterations, identical signal each time.
    assert cp.evaluate(cp_path) == "insufficient-history"
    _rewrite(cp_path, iteration=2)
    assert cp.evaluate(cp_path) == "insufficient-history"
    _rewrite(cp_path, iteration=3)
    assert cp.evaluate(cp_path) == "paused"

    # Side effects: pending_approval.json written, status flipped.
    pending = cp_path.parent / "pending_approval.json"
    assert pending.is_file()
    pj = json.loads(pending.read_text(encoding="utf-8"))
    assert pj["detector"] == "check_progress"
    assert pj["no_progress_limit"] == 3
    cpj = json.loads(cp_path.read_text(encoding="utf-8"))
    assert cpj["status"] == cp.PAUSED_STATUS
    assert cpj["status_before_pause"] == "running"


def test_paused_loop_not_reprocessed(make_checkpoint, monkeypatch):
    # Once paused (INACTIVE status), further runs are inactive no-ops.
    monkeypatch.delenv("DURABLE_LOOP_NOPROGRESS_N", raising=False)
    cp_path = _set_metrics(make_checkpoint, "f", 1, 0.5, no_progress_limit=2)
    cp.evaluate(cp_path)
    _rewrite(cp_path, iteration=2)
    assert cp.evaluate(cp_path) == "paused"
    # Now it is paused_for_approval; another invocation must be a no-op.
    assert cp.evaluate(cp_path) == "inactive"


# --- 2. progress between rounds -> no pause --------------------------------
def test_progress_does_not_trigger(make_checkpoint, monkeypatch):
    monkeypatch.delenv("DURABLE_LOOP_NOPROGRESS_N", raising=False)
    cp_path = _set_metrics(make_checkpoint, "f", 1, 0.5, no_progress_limit=3)
    assert cp.evaluate(cp_path) == "insufficient-history"
    _rewrite(cp_path, iteration=2,
             cumulative_state={"metrics_snapshot": {"score": 0.7}}, last_result="better")
    assert cp.evaluate(cp_path) == "insufficient-history"
    _rewrite(cp_path, iteration=3,
             cumulative_state={"metrics_snapshot": {"score": 0.9}}, last_result="best")
    assert cp.evaluate(cp_path) == "recorded"
    assert not (cp_path.parent / "pending_approval.json").exists()


def test_progress_in_last_round_resets(make_checkpoint, monkeypatch):
    # Two identical rounds then a change in the 3rd => window not all-identical.
    monkeypatch.delenv("DURABLE_LOOP_NOPROGRESS_N", raising=False)
    cp_path = _set_metrics(make_checkpoint, "f", 1, 0.5, no_progress_limit=3)
    cp.evaluate(cp_path)
    _rewrite(cp_path, iteration=2)  # identical signal
    cp.evaluate(cp_path)
    _rewrite(cp_path, iteration=3,
             cumulative_state={"metrics_snapshot": {"score": 0.6}}, last_result="moved")
    assert cp.evaluate(cp_path) == "recorded"
    assert not (cp_path.parent / "pending_approval.json").exists()


def test_env_var_enables_when_field_absent(make_checkpoint, monkeypatch):
    # no_progress_limit absent from checkpoint; env var turns it on.
    monkeypatch.setenv("DURABLE_LOOP_NOPROGRESS_N", "2")
    cp_path = make_checkpoint("f", iteration=1, status="running",
                              last_result="x",
                              cumulative_state={"metrics_snapshot": {"score": 1}})
    assert cp.evaluate(cp_path) == "insufficient-history"
    _rewrite(cp_path, iteration=2)
    assert cp.evaluate(cp_path) == "paused"


def test_env_var_overrides_field(make_checkpoint, monkeypatch):
    # field says 99 (effectively never), env says 2 => env wins, pauses at 2.
    monkeypatch.setenv("DURABLE_LOOP_NOPROGRESS_N", "2")
    cp_path = _set_metrics(make_checkpoint, "f", 1, 0.5, no_progress_limit=99)
    cp.evaluate(cp_path)
    _rewrite(cp_path, iteration=2)
    assert cp.evaluate(cp_path) == "paused"


# --- 3. default OFF -> no-op ------------------------------------------------
def test_default_off_is_noop(make_checkpoint, monkeypatch):
    monkeypatch.delenv("DURABLE_LOOP_NOPROGRESS_N", raising=False)
    # no_progress_limit defaults to 0 in _set_metrics
    cp_path = _set_metrics(make_checkpoint, "f", 1, 0.5)
    for it in (1, 2, 3, 4):
        _rewrite(cp_path, iteration=it)
        assert cp.evaluate(cp_path) == "disabled"
    # Never writes a history file or pending approval when disabled.
    assert not (cp_path.parent / "progress_history.json").exists()
    assert not (cp_path.parent / "pending_approval.json").exists()


def test_env_zero_or_garbage_is_off(make_checkpoint, monkeypatch):
    cp_path = _set_metrics(make_checkpoint, "f", 1, 0.5, no_progress_limit=3)
    for val in ("0", "-1", "abc", ""):
        if val == "":
            monkeypatch.delenv("DURABLE_LOOP_NOPROGRESS_N", raising=False)
        else:
            monkeypatch.setenv("DURABLE_LOOP_NOPROGRESS_N", val)
        # "" falls through to the field (3) -> enabled; the non-empty garbage/0/-1
        # values explicitly disable. Verify the explicit-disable cases are off.
        if val in ("0", "-1", "abc"):
            assert cp.evaluate(cp_path) == "disabled"


# --- 4. no checkpoint / ambiguous -> fail open -----------------------------
def test_no_scratch_failopen_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("DURABLE_LOOP_FEATURE", raising=False)
    monkeypatch.delenv("DURABLE_LOOP_NOPROGRESS_N", raising=False)
    rc = cp.run("nonexistent", str(tmp_path), str(tmp_path))
    assert rc == cp.EXIT_OK


def test_missing_checkpoint_failopen(tmp_path):
    # feature dir absent entirely
    assert cp.discover_checkpoint("ghost", str(tmp_path), str(tmp_path)) is None


def test_ambiguous_two_features_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("DURABLE_LOOP_FEATURE", raising=False)
    for f in ("a", "b"):
        d = tmp_path / ".scratch" / f
        d.mkdir(parents=True)
        (d / "checkpoint.json").write_text(json.dumps({"feature": f}), encoding="utf-8")
    # discovery is ambiguous -> None -> run() is a no-op (exit 0)
    assert cp.discover_checkpoint(None, None, str(tmp_path)) is None
    assert cp.run(None, None, str(tmp_path)) == cp.EXIT_OK


def test_unreadable_checkpoint_failopen(tmp_path, monkeypatch):
    monkeypatch.setenv("DURABLE_LOOP_NOPROGRESS_N", "2")
    d = tmp_path / ".scratch" / "f"
    d.mkdir(parents=True)
    bad = d / "checkpoint.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert cp.evaluate(bad) == "disabled"  # fail open on parse error


# --- entry points via subprocess (Stop hook + CLI) -------------------------
def test_stop_hook_failopen_no_loop(tmp_path):
    proc = subprocess.run(
        [PY, str(SCRIPTS / "check_progress.py")],
        input=json.dumps({"cwd": str(tmp_path), "hook_event_name": "Stop"}),
        text=True, capture_output=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0  # always exit 0 for Stop hook


def test_stop_hook_pauses_via_env(tmp_path):
    feature = "f"
    d = tmp_path / ".scratch" / feature
    d.mkdir(parents=True)
    cp_path = d / "checkpoint.json"

    def write(it):
        cp_path.write_text(json.dumps({
            "feature": feature, "iteration": it, "status": "running",
            "last_result": "stuck",
            "cumulative_state": {"metrics_snapshot": {"score": 1}},
        }), encoding="utf-8")

    env = dict(os.environ)
    env["DURABLE_LOOP_FEATURE"] = feature
    env["DURABLE_LOOP_PROJECT_DIR"] = str(tmp_path)
    env["DURABLE_LOOP_NOPROGRESS_N"] = "2"

    for it in (1, 2):
        write(it)
        proc = subprocess.run(
            [PY, str(SCRIPTS / "check_progress.py")],
            input=json.dumps({"cwd": str(tmp_path), "hook_event_name": "Stop"}),
            text=True, capture_output=True, encoding="utf-8", errors="replace", env=env,
        )
        assert proc.returncode == 0

    assert (d / "pending_approval.json").is_file()
    final = json.loads(cp_path.read_text(encoding="utf-8"))
    assert final["status"] == cp.PAUSED_STATUS


def test_cli_bad_feature_name_usage_error(tmp_path):
    proc = subprocess.run(
        [PY, str(SCRIPTS / "check_progress.py"), "bad/name"],
        text=True, capture_output=True, encoding="utf-8", errors="replace", cwd=str(tmp_path),
    )
    assert proc.returncode == cp.EXIT_USAGE
