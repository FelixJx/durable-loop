"""Category 3: diagnose.py — three-axis FAIL attribution (agent / harness / skill).

diagnose is a read-only reporter (like verify_done's reporting half, not its
gate half). These tests exercise it two ways: (1) import the module and call
the pure functions (analyze_history / detect_trend / attribute) directly for
deterministic pattern->axis mapping, and (2) drive the CLI end-to-end via
subprocess to prove fail-open exits, the read-only contract, and arg validation.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import diagnose as m  # noqa: E402  (scripts/ is on sys.path via conftest)

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"
PY = sys.executable


# --- helpers -----------------------------------------------------------------

def _cp_path(tmp_path, feature="f"):
    return tmp_path / ".scratch" / feature / "checkpoint.json"


def _entry(iteration, result, ts="2026-06-23T00:00:00"):
    return {"iteration": iteration, "result": result, "timestamp": ts}


def _write_checkpoint(tmp_path, feature="f", verify_history=None, **extra):
    p = _cp_path(tmp_path, feature)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {"feature": feature, "iteration": 0, "status": "running"}
    if verify_history is not None:
        d["verify_history"] = verify_history
    d.update(extra)
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def _run_cli(feature, project_dir, *extra, env=None):
    cmd = [PY, "-X", "utf8", str(SCRIPTS / "diagnose.py"), feature, str(project_dir)] + list(extra)
    # Force UTF-8 on both ends: the report contains CJK; on Windows the default
    # GBK codepage would otherwise raise UnicodeDecodeError in the reader thread
    # and leave stdout=None (which would mask the real rc/output).
    full_env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if env:
        full_env.update(env)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=full_env)
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


# --- pure-function unit tests ------------------------------------------------

def test_analyze_history_counts_and_streaks():
    hist = [
        _entry(1, "FAIL"), _entry(2, "FAIL"), _entry(3, "PASS"),
        _entry(4, "PASS"), _entry(5, "FAIL"),
    ]
    s = m.analyze_history(hist)
    assert s["total"] == 5
    assert s["pass_n"] == 2 and s["fail_n"] == 3
    assert s["pass_rate"] == pytest.approx(2 / 5)
    assert s["longest_pass_streak"] == 2
    assert s["longest_fail_streak"] == 2


def test_analyze_history_empty_is_safe():
    s = m.analyze_history([])
    assert s["total"] == 0 and s["pass_rate"] == 0.0
    assert s["longest_pass_streak"] == 0 and s["longest_fail_streak"] == 0


def test_detect_trend_improving_stagnating_regressing():
    assert m.detect_trend(["FAIL", "FAIL", "PASS", "PASS"]) == "improving"
    assert m.detect_trend(["PASS", "PASS", "FAIL", "FAIL"]) == "regressing"
    # too few points or balanced -> stagnating
    assert m.detect_trend(["PASS", "FAIL"]) == "stagnating"
    assert m.detect_trend(["PASS", "FAIL", "PASS", "FAIL"]) == "stagnating"


# --- (c) early dense FAIL -> skill axis --------------------------------------

def test_early_dense_fail_attributes_skill():
    # First 5 rounds all FAIL at low iterations.
    hist = [_entry(i, "FAIL") for i in range(1, 6)]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert axes["skill"] is True
    assert primary == "skill"


def test_early_majority_fail_attributes_skill():
    # 3 early FAILs + 1 later FAIL out of 4 -> majority early still skill.
    hist = [_entry(1, "FAIL"), _entry(2, "FAIL"), _entry(3, "FAIL"), _entry(20, "FAIL")]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert axes["skill"] is True
    assert primary == "skill"


# --- (d) late intermittent FAIL -> agent axis --------------------------------

def test_late_intermittent_fail_attributes_agent():
    # A PASS somewhere, and a FAIL at iter > 10.
    hist = [_entry(1, "PASS"), _entry(12, "FAIL"), _entry(13, "PASS"), _entry(14, "FAIL")]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert axes["agent"] is True
    # No early-dense (FAILs are late) and history is not sparse -> agent primary.
    assert primary == "agent"


# --- (e) sparse history -> harness axis --------------------------------------

def test_sparse_history_attributes_harness():
    # < SPARSE_HISTORY entries.
    hist = [_entry(1, "FAIL"), _entry(2, "FAIL")]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert axes["harness"] is True
    assert primary == "harness"  # harness wins ties


def test_timing_gap_attributes_harness():
    # Not sparse (4 entries) but one adjacent interval is far larger than the
    # median step -> scheduling gap (hook stalled / interval blown).
    hist = [
        _entry(1, "PASS", "2026-06-23T01:00:00"),
        _entry(2, "PASS", "2026-06-23T01:05:00"),
        _entry(3, "FAIL", "2026-06-23T01:10:00"),
        _entry(4, "PASS", "2026-06-24T08:00:00"),  # ~30h later vs ~5min median
    ]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert axes["harness"] is True
    assert primary == "harness"


def test_no_timing_gap_does_not_trip_harness():
    # Same FAIL-ending history but evenly spaced -> no gap, so harness must NOT
    # fire (proves the gap signal is about timing, not the trailing FAIL).
    hist = [
        _entry(1, "PASS", "2026-06-23T01:00:00"),
        _entry(2, "PASS", "2026-06-23T02:00:00"),
        _entry(3, "FAIL", "2026-06-23T03:00:00"),
        _entry(4, "FAIL", "2026-06-23T04:00:00"),
    ]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert axes["harness"] is False


def test_harness_wins_tie_over_skill_and_agent():
    # Sparse AND early-dense AND late: harness must win the priority order.
    hist = [_entry(1, "FAIL"), _entry(15, "PASS")]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert axes["harness"] is True
    assert primary == "harness"


def test_no_fail_triggers_no_axis():
    hist = [_entry(1, "PASS"), _entry(2, "PASS"), _entry(3, "PASS"), _entry(4, "PASS")]
    stats = m.analyze_history(hist)
    axes, primary = m.attribute(stats, hist)
    assert primary is None
    assert not any(axes.values())


# --- (a) no checkpoint -> fail-open exit 0 -----------------------------------

def test_no_checkpoint_failopen_exit0(tmp_path):
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "无 checkpoint" in out or "无需归因" in out


def test_corrupt_checkpoint_failopen_exit0(tmp_path):
    p = _cp_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not valid json", encoding="utf-8")
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "无 checkpoint" in out or "无需归因" in out


def test_no_verify_history_failopen_exit0(tmp_path):
    # Checkpoint exists but has no verify_history field.
    _write_checkpoint(tmp_path, verify_history=None)
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "无 verify_history" in out or "无足够数据" in out


# --- (b) all-PASS history -> "no recent FAIL" --------------------------------

def test_no_fail_history_says_no_fail(tmp_path):
    _write_checkpoint(tmp_path, verify_history=[_entry(i, "PASS") for i in range(1, 6)])
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "无近期 FAIL" in out


# --- (h) summary includes pass-rate and streak -------------------------------

def test_summary_includes_pass_rate_and_streak(tmp_path):
    _write_checkpoint(tmp_path, verify_history=[
        _entry(1, "FAIL"), _entry(2, "FAIL"), _entry(3, "PASS"),
        _entry(4, "PASS"), _entry(5, "FAIL"),
    ])
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "pass-rate" in out
    assert "longest PASS streak" in out
    assert "longest FAIL streak" in out
    assert "trend" in out


# --- (f) invalid feature -> exit 2 -------------------------------------------

def test_invalid_feature_exits_2(tmp_path):
    rc, out = _run_cli("a/b", tmp_path)
    assert rc == 2
    assert "invalid feature name" in out


def test_missing_project_dir_exits_2(tmp_path):
    bogus = tmp_path / "does-not-exist"
    rc, out = _run_cli("f", bogus)
    assert rc == 2
    assert "project_dir does not exist" in out


def test_zero_limit_exits_2(tmp_path):
    _write_checkpoint(tmp_path, verify_history=[_entry(1, "FAIL")])
    rc, out = _run_cli("f", tmp_path, "--limit", "0")
    assert rc == 2
    assert "--limit" in out


# --- (g) reads only, never writes --------------------------------------------

def test_reads_only_never_writes(tmp_path):
    hist = [
        _entry(1, "FAIL"), _entry(2, "FAIL"), _entry(3, "FAIL"),
        _entry(4, "FAIL"), _entry(5, "FAIL"),
    ]
    p = _write_checkpoint(tmp_path, verify_history=hist)
    before = p.read_bytes()
    rc, out = _run_cli("f", tmp_path)
    after = p.read_bytes()
    assert rc == 0
    assert before == after  # byte-for-byte unchanged
    # And it really did produce a skill-axis report (proving it read the data).
    assert "skill" in out


# --- full CLI rendering: each axis shows up in real output -------------------

def test_cli_early_dense_renders_skill_recommendation(tmp_path):
    _write_checkpoint(tmp_path, verify_history=[_entry(i, "FAIL") for i in range(1, 6)])
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "skill" in out
    assert "推荐优先改" in out


def test_cli_late_intermittent_renders_agent_recommendation(tmp_path):
    _write_checkpoint(tmp_path, verify_history=[
        _entry(1, "PASS"), _entry(11, "FAIL"), _entry(12, "PASS"), _entry(13, "FAIL"),
        _entry(14, "PASS"),
    ])
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "agent" in out
    assert "推荐优先改" in out


def test_cli_sparse_renders_harness_recommendation(tmp_path):
    _write_checkpoint(tmp_path, verify_history=[_entry(1, "FAIL"), _entry(2, "FAIL")])
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "harness" in out
    assert "推荐优先改" in out


def test_cli_limit_truncates_window(tmp_path):
    # 10 entries; --limit 4 -> summary total should reflect the window of 4.
    _write_checkpoint(tmp_path, verify_history=[
        _entry(i, "FAIL" if i % 2 else "PASS") for i in range(1, 11)
    ])
    rc, out = _run_cli("f", tmp_path, "--limit", "4")
    assert rc == 0
    assert "recent 4 run" in out


def test_cli_warns_against_blindly_logging(tmp_path):
    _write_checkpoint(tmp_path, verify_history=[_entry(i, "FAIL") for i in range(1, 6)])
    rc, out = _run_cli("f", tmp_path)
    assert rc == 0
    assert "learn log" in out or "learnings.jsonl" in out or "SKILL.md" in out
