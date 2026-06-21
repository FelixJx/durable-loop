"""Category 3: check_budget.py — THRASHING guard + fail-open discovery.

NOTE (2026-06-19): budget enforcement was REMOVED per user request ("only quality,
no budget guardrail"). The module is thrashing-only now. These tests confirm (a)
budget exhaustion no longer blocks, (b) the thrashing guard still works, (c)
fail-open discovery still behaves."""
import json

import check_budget as cb


# --- budget no longer blocks (characterization of the removal) -------------
def test_budget_exhausted_does_not_block(tmp_path, run_hook):
    # a checkpoint blown WAY past every cap must NOT block anymore
    d = tmp_path / ".scratch" / "f"
    d.mkdir(parents=True)
    (d / "checkpoint.json").write_text(json.dumps({
        "budget_used": {"tokens": 999_000_000, "dollars": 9999.0, "iterations": 999, "hours": 999},
        "max_budget": {"tokens": 5_000_000, "dollars": 10.0, "iterations": 25, "hours": 6},
        "feature": "f", "iteration": 1,
    }), encoding="utf-8")
    rc, _ = run_hook("check_budget.py", {"tool_input": {}, "cwd": str(tmp_path)})
    assert rc == cb.EXIT_ALLOW  # thrashing no-op (no session.log) -> allow, budget ignored


# --- thrashing (iteration-aware) ------------------------------------------
def test_thrash_block_3_identical_iters(write_session_log):
    p = write_session_log("f", [
        {"iter": 1, "action": "git push origin main"},
        {"iter": 2, "action": "git push origin main"},
        {"iter": 3, "action": "git push origin main"},
    ])
    assert cb.check_thrashing(p) == cb.EXIT_BLOCK


def test_thrash_allow_3_distinct_iters(write_session_log):
    p = write_session_log("f", [
        {"iter": 1, "action": "git pull"},
        {"iter": 2, "action": "npm install"},
        {"iter": 3, "action": "pytest run"},
    ])
    assert cb.check_thrashing(p) == cb.EXIT_ALLOW


def test_thrash_allow_under_window(write_session_log):
    p = write_session_log("f", [{"iter": 1, "action": "x"}, {"iter": 2, "action": "x"}])
    assert cb.check_thrashing(p) == cb.EXIT_ALLOW  # needs 3 iterations


def test_thrash_iteration_not_toolcall(write_session_log):
    # 4 tool calls across only 2 distinct iterations must NOT block
    p = write_session_log("f", [
        {"iter": 1, "action": "Edit src/foo.py"},
        {"iter": 1, "action": "Edit src/foo.py"},
        {"iter": 2, "action": "Edit src/foo.py"},
    ])
    assert cb.check_thrashing(p) == cb.EXIT_ALLOW


def test_thrash_ignores_nonjson_legacy(write_session_log):
    p = write_session_log("f", ["restart loop", "restart loop", "restart loop"])
    assert cb.check_thrashing(p) == cb.EXIT_BLOCK


# --- discovery fail-open ---------------------------------------------------
def test_main_failopen_no_scratch(tmp_path, run_hook):
    rc, _ = run_hook("check_budget.py", {"tool_input": {}, "cwd": str(tmp_path)})
    assert rc == cb.EXIT_ALLOW


def test_main_warns_on_ambiguous_two_features(tmp_path, run_hook):
    for f in ("a", "b"):
        d = tmp_path / ".scratch" / f
        d.mkdir(parents=True)
        (d / "checkpoint.json").write_text(json.dumps({
            "budget_used": {"tokens": 9_000_000, "dollars": 0, "iterations": 0, "hours": 0},
            "max_budget": {"tokens": 5_000_000, "dollars": 0, "iterations": 25, "hours": 6},
            "feature": f, "iteration": 1,
        }), encoding="utf-8")
    rc, err = run_hook("check_budget.py", {"tool_input": {}, "cwd": str(tmp_path)})
    assert rc == cb.EXIT_ALLOW  # ambiguous -> fail open
    assert ">1 feature" in err and "DURABLE_LOOP_FEATURE" in err  # but no longer silent
