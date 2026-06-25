"""Gap1: 进化度 metrics — compute_evolution / append_evolution_trend / handoff
section. Mirrors the import style of test_checkpoint_replay.py and uses the shared
conftest run_hook / make_checkpoint fixtures."""
import json

import durable_loop_checkpoint as m


# --- (a) basic counting ----------------------------------------------------
def test_compute_evolution_basic():
    # [P, P, F, P] at iteration 4 -> 3 pass / 1 fail
    hist = [
        {"iteration": 1, "result": "PASS", "timestamp": "t1"},
        {"iteration": 2, "result": "PASS", "timestamp": "t2"},
        {"iteration": 3, "result": "FAIL", "timestamp": "t3"},
        {"iteration": 4, "result": "PASS", "timestamp": "t4"},
    ]
    snap = m.compute_evolution(hist, 4)
    assert snap["pass_count"] == 3
    assert snap["fail_count"] == 1
    assert snap["window_size"] == 4
    assert snap["pass_rate"] == 0.75
    assert snap["iteration"] == 4
    # tail entry is PASS -> converged True
    assert snap["converged"] is True


def test_compute_evolution_tail_fail_not_converged():
    hist = [
        {"iteration": 1, "result": "PASS"},
        {"iteration": 2, "result": "FAIL"},
    ]
    snap = m.compute_evolution(hist, 2)
    assert snap["converged"] is False
    assert snap["pass_count"] == 1
    assert snap["fail_count"] == 1


# --- (b) fail-open on garbage ----------------------------------------------
def test_compute_evolution_failopen_on_garbage():
    # None
    assert m.compute_evolution(None, 1) == {}
    # a string instead of a list
    assert m.compute_evolution("not-a-list", 1) == {}
    # empty list
    assert m.compute_evolution([], 1) == {}
    # rows whose result is not PASS/FAIL are ignored, not fatal
    snap = m.compute_evolution(
        [{"iteration": 1, "result": "MAYBE"},
         {"iteration": 2, "result": 123},
         {"iteration": 3, "result": None},
         {"iteration": 4, "result": "PASS"}],
        4,
    )
    # only the PASS counts; total usable = 1
    assert snap["pass_count"] == 1
    assert snap["fail_count"] == 0
    assert snap["window_size"] == 4  # all 4 entries are in-window


def test_compute_evolution_non_dict_rows_ignored():
    # junk non-dict rows don't crash; they're skipped during iteration
    snap = m.compute_evolution(
        ["garbage", 42, None, {"iteration": 5, "result": "PASS"}],
        5,
    )
    assert snap["pass_count"] == 1


def test_compute_evolution_future_iteration_filtered():
    # entries beyond the current iteration are excluded from the window
    hist = [
        {"iteration": 1, "result": "PASS"},
        {"iteration": 5, "result": "FAIL"},  # future, relative to iteration=3
        {"iteration": 2, "result": "PASS"},
    ]
    snap = m.compute_evolution(hist, 3)
    assert snap["pass_count"] == 2
    assert snap["fail_count"] == 0


def test_compute_evolution_small_window_no_direction():
    # window < 4 -> improving/prev_pass_rate are None (no false positive)
    snap = m.compute_evolution(
        [{"iteration": 1, "result": "PASS"}, {"iteration": 2, "result": "FAIL"}],
        2,
    )
    assert snap["improving"] is None
    assert snap["prev_pass_rate"] is None


def test_compute_evolution_improving_signal():
    # older half all FAIL (rate 0), recent half all PASS -> improving True
    hist = [
        {"iteration": 1, "result": "FAIL"},
        {"iteration": 2, "result": "FAIL"},
        {"iteration": 3, "result": "PASS"},
        {"iteration": 4, "result": "PASS"},
    ]
    snap = m.compute_evolution(hist, 4)
    assert snap["improving"] is True
    assert snap["prev_pass_rate"] == 0.0


# --- (c) appended on Stop --------------------------------------------------
def _entry(model, usage, blocks=None):
    return {"type": "assistant", "model": model,
            "message": {"role": "assistant", "model": model, "usage": usage,
                        "content": blocks or []}}


def _write_cp(tmp_path, cp, feature="f"):
    cp_dir = tmp_path / ".scratch" / feature
    cp_dir.mkdir(parents=True, exist_ok=True)
    p = cp_dir / "checkpoint.json"
    p.write_text(json.dumps(cp), encoding="utf-8")
    return p


def test_evolution_appended_on_stop(tmp_path, run_hook):
    cp_path = _write_cp(tmp_path, {
        "feature": "f", "iteration": 1, "status": "running",
        "budget_used": {"tokens": 0, "dollars": 0.0, "iterations": 0, "hours": 0},
        "started_at": "", "idempotency_keys": [],
        "verify_history": [
            {"iteration": 1, "result": "PASS", "timestamp": "t1"},
        ],
    })
    rc, _ = run_hook("durable_loop_checkpoint.py",
                     {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert "evolution_trend" in out
    assert isinstance(out["evolution_trend"], list)
    assert len(out["evolution_trend"]) == 1
    snap = out["evolution_trend"][0]
    assert "improving" in snap
    assert "converged" in snap
    assert snap["pass_count"] == 1


def test_evolution_verify_history_unchanged(tmp_path, run_hook):
    # Gap1 must not mutate verify_history (no write conflict with verify_done.py)
    base = [{"iteration": 1, "result": "PASS", "timestamp": "t1"}]
    cp_path = _write_cp(tmp_path, {
        "feature": "f", "iteration": 1, "status": "running",
        "budget_used": {"tokens": 0, "dollars": 0.0, "iterations": 0, "hours": 0},
        "started_at": "", "idempotency_keys": [],
        "verify_history": list(base),
    })
    run_hook("durable_loop_checkpoint.py",
             {"cwd": str(tmp_path), "transcript_path": ""})
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert out["verify_history"] == base


# --- (d) trims to window ---------------------------------------------------
def test_evolution_trims_to_window(tmp_path, run_hook):
    # 12 verify entries; running the hook once appends one trend entry, but the
    # field should never exceed EVOLUTION_WINDOW (10). Simulate a cp that already
    # has an evolution_trend plus repeated appends by calling the function directly,
    # then confirm the subprocess path also respects the cap.
    hist = [{"iteration": i, "result": "PASS"} for i in range(1, 13)]
    cp_path = _write_cp(tmp_path, {
        "feature": "f", "iteration": 12, "status": "running",
        "budget_used": {"tokens": 0, "dollars": 0.0, "iterations": 0, "hours": 0},
        "started_at": "", "idempotency_keys": [],
        "verify_history": hist,
    })
    rc, _ = run_hook("durable_loop_checkpoint.py",
                     {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert len(out["evolution_trend"]) <= m.EVOLUTION_WINDOW

    # Unit-level: append 15 times in-memory and confirm the cap holds.
    cp = {"verify_history": hist, "iteration": 12, "evolution_trend": []}
    for _ in range(15):
        m.append_evolution_trend(cp)
    assert len(cp["evolution_trend"]) <= m.EVOLUTION_WINDOW


# --- (e) fail-open does not break main ------------------------------------
def test_evolution_failopen_does_not_break_main(tmp_path, run_hook):
    # verify_history is an illegal type — must not crash; budget still written
    cp_path = _write_cp(tmp_path, {
        "feature": "f", "iteration": 1, "status": "running",
        "budget_used": {"tokens": 0, "dollars": 0.0, "iterations": 0, "hours": 0},
        "started_at": "", "idempotency_keys": [],
        "verify_history": "this-should-be-a-list",
    })
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(_entry("claude-sonnet-4-6", {"input_tokens": 10, "output_tokens": 5})) + "\n",
        encoding="utf-8",
    )
    rc, _ = run_hook("durable_loop_checkpoint.py",
                     {"cwd": str(tmp_path), "transcript_path": str(transcript)})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    # budget rail still live despite bad verify_history
    assert out["budget_used"]["tokens"] == 15
    # evolution_trend must not be created from garbage
    assert "evolution_trend" not in out or out["evolution_trend"] == []


def test_append_evolution_trend_failopen_non_dict():
    # calling on a non-dict must be a no-op, never raise
    m.append_evolution_trend(None)
    m.append_evolution_trend("nope")
    m.append_evolution_trend(123)


# --- (f) handoff includes evolution section --------------------------------
def test_build_handoff_includes_evolution_section():
    cp = {
        "feature": "f", "iteration": 4,
        "evolution_trend": [{
            "iteration": 4, "window_size": 4, "pass_count": 3, "fail_count": 1,
            "pass_rate": 0.75, "converged": True, "prev_pass_rate": 0.5,
            "improving": True,
        }],
    }
    body = m.build_handoff(cp, 4)
    assert "进化趋势" in body
    assert "75%" in body
    assert "改善中" in body
    assert "收敛" in body


def test_build_handoff_evolution_placeholder_when_absent():
    cp = {"feature": "f", "iteration": 1}
    body = m.build_handoff(cp, 1)
    assert "进化趋势" in body
    assert "(暂无)" in body
