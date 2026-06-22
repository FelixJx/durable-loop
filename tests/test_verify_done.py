"""Category 2: verify_done.py — command extraction (cmd-comment only, prose
backticks never run), PASS/FAIL/MANUAL classification, VERDICT matrix, and the
anti flip-flop K-consecutive-PASS convergence gate (improvement #8)."""
import json

import pytest


def _cp_path(tmp_path, feature="f"):
    return tmp_path / ".scratch" / feature / "checkpoint.json"


def _write_checkpoint(tmp_path, feature="f", **fields):
    """Write a checkpoint.json next to done.criteria.md so verify_done switches from
    single-shot mode into the K-consecutive-PASS convergence gate. The .scratch dir is
    created lazily by run_verify_done, so we mkdir defensively here too."""
    p = _cp_path(tmp_path, feature)
    p.parent.mkdir(parents=True, exist_ok=True)
    base = {"feature": feature, "iteration": 0, "status": "running"}
    base.update(fields)
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


def _history(tmp_path, feature="f"):
    return json.loads(_cp_path(tmp_path, feature).read_text(encoding="utf-8")).get("verify_history", [])


def test_cmd_pass(run_verify_done):
    rc, out = run_verify_done("f", "- [ ] ok <!-- cmd: true -->\n")
    assert rc == 0 and "[PASS] ok" in out and "VERDICT: DONE" in out


def test_cmd_fail(run_verify_done):
    rc, out = run_verify_done("f", "- [ ] bad <!-- cmd: false -->\n")
    assert rc == 1 and "[FAIL]" in out and "VERDICT: NOT DONE" in out


def test_manual_no_command(run_verify_done):
    rc, out = run_verify_done("f", "- [ ] needs human judgment\n")
    assert rc == 1 and "[MANUAL]" in out and "require manual" in out


def test_prose_backtick_not_run(run_verify_done):
    # regression: `` `idempotency_keys` `` used to be executed as a command -> false FAIL
    rc, out = run_verify_done("f", "- [ ] check the `idempotency_keys` list\n")
    assert "[MANUAL]" in out and "[FAIL]" not in out


def test_todo_inverted_grep(run_verify_done):
    # `! false` -> PASS ; `! true` -> FAIL (proves the ! inversion works)
    rc_pass, _ = run_verify_done("f", "- [ ] a <!-- cmd: ! false -->\n")
    rc_fail, _ = run_verify_done("f", "- [ ] a <!-- cmd: ! true -->\n")
    assert rc_pass == 0 and rc_fail == 1


def test_multiple_cmds_per_line_all_must_pass(run_verify_done):
    # Python port uses non-greedy parse -> two cmds run independently
    rc_ok, _ = run_verify_done("f", "- [ ] m <!-- cmd: true --> <!-- cmd: true -->\n")
    rc_bad, out = run_verify_done("f", "- [ ] m <!-- cmd: true --> <!-- cmd: false -->\n")
    assert rc_ok == 0
    assert rc_bad == 1 and "[FAIL]" in out


def test_verdict_done_all_pass(run_verify_done):
    rc, out = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n- [ ] b <!-- cmd: true -->\n")
    assert rc == 0 and "VERDICT: DONE" in out


def test_verdict_mixed_pass_and_manual_not_done(run_verify_done):
    rc, out = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n- [ ] b manual\n")
    assert rc == 1 and "VERDICT: NOT DONE" in out


def test_no_checkboxes(run_verify_done):
    rc, out = run_verify_done("f", "# title\nsome prose\n")
    assert rc == 1 and "no checkbox criteria found" in out


def test_command_not_found_is_fail(run_verify_done):
    rc, out = run_verify_done("f", "- [ ] x <!-- cmd: this_cmd_does_not_exist_xyz -->\n")
    assert rc == 1 and "command not found" in out


def test_timeout_is_fail(run_verify_done):
    rc, out = run_verify_done("f", "- [ ] x <!-- cmd: sleep 5 -->\n", timeout=1)
    assert rc == 1 and "timed out" in out


def test_invalid_feature_name_rejected(run_verify_done):
    rc, out = run_verify_done("a/b", "- [ ] x <!-- cmd: true -->\n")
    assert rc == 2 and "invalid feature name" in out


def test_prose_mentioning_checkbox_not_matched(run_verify_done):
    # anchored matcher must not treat a prose sentence mentioning "- [ ]" as a criterion
    rc, out = run_verify_done("f", "Note: this line mentions - [ ] in prose only.\n- [ ] real <!-- cmd: true -->\n")
    assert rc == 0 and "[PASS] real" in out
    assert "prose only" not in out.replace("Note: this line mentions", "")  # prose line not a criterion


# --- improvement #8: anti flip-flop K-consecutive-PASS convergence gate -------------

def test_no_checkpoint_degrades_to_single_shot(run_verify_done):
    # Backward compat: with NO checkpoint a single machine PASS = DONE, no streak gate.
    rc, out = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert rc == 0 and "VERDICT: DONE" in out
    assert "CONVERGENCE" not in out  # single-shot path never prints the streak line


def test_single_pass_not_yet_converged(run_verify_done, tmp_path):
    # With a checkpoint present, ONE PASS is not enough (K=2 default): exit 1, "尚未收敛".
    _write_checkpoint(tmp_path)
    rc, out = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert rc == 1
    assert "VERDICT: DONE" in out          # machine criteria did pass this run
    assert "尚未收敛" in out and "1/2" in out  # but streak gate holds it back
    hist = _history(tmp_path)
    assert len(hist) == 1 and hist[0]["result"] == "PASS"
    assert "iteration" in hist[0] and "timestamp" in hist[0]


def test_two_consecutive_pass_converges(run_verify_done, tmp_path):
    _write_checkpoint(tmp_path)
    rc1, out1 = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert rc1 == 1 and "尚未收敛" in out1
    rc2, out2 = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert rc2 == 0 and "已收敛" in out2 and "2/2" in out2
    hist = _history(tmp_path)
    assert [h["result"] for h in hist] == ["PASS", "PASS"]


def test_fail_resets_streak(run_verify_done, tmp_path):
    # PASS, then FAIL must drop the consecutive count back to zero, so the next PASS is
    # only 1/2 again (not 2/2) — proves flip-flop cannot sneak through to convergence.
    _write_checkpoint(tmp_path)
    run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")          # streak -> 1
    rc_fail, out_fail = run_verify_done("f", "- [ ] a <!-- cmd: false -->\n")  # streak reset
    assert rc_fail == 1 and "streak reset" in out_fail
    rc_pass, out_pass = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")   # back to 1/2
    assert rc_pass == 1 and "1/2" in out_pass and "尚未收敛" in out_pass
    assert [h["result"] for h in _history(tmp_path)] == ["PASS", "FAIL", "PASS"]


def test_converge_k_override_via_checkpoint_field(run_verify_done, tmp_path):
    # converge_k=3 in the checkpoint requires three consecutive passes.
    _write_checkpoint(tmp_path, converge_k=3)
    r1, o1 = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    r2, o2 = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    r3, o3 = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert r1 == 1 and "1/3" in o1
    assert r2 == 1 and "2/3" in o2
    assert r3 == 0 and "已收敛" in o3 and "3/3" in o3


def test_converge_k_override_via_env(run_verify_done, tmp_path, monkeypatch):
    # DURABLE_LOOP_CONVERGE_K=1 collapses the gate to single-shot-with-history.
    monkeypatch.setenv("DURABLE_LOOP_CONVERGE_K", "1")
    _write_checkpoint(tmp_path)
    rc, out = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert rc == 0 and "已收敛" in out and "1/1" in out


def test_env_k_overrides_checkpoint_field(run_verify_done, tmp_path, monkeypatch):
    # Precedence: env wins over the checkpoint field.
    monkeypatch.setenv("DURABLE_LOOP_CONVERGE_K", "1")
    _write_checkpoint(tmp_path, converge_k=5)
    rc, out = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert rc == 0 and "已收敛" in out


def test_unreadable_checkpoint_degrades_to_single_shot(run_verify_done, tmp_path):
    # Fail-open: a corrupt checkpoint.json must not break or block — fall back to
    # single-shot judgment exactly as if no checkpoint existed.
    p = _cp_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not valid json", encoding="utf-8")
    rc, out = run_verify_done("f", "- [ ] a <!-- cmd: true -->\n")
    assert rc == 0 and "VERDICT: DONE" in out and "CONVERGENCE" not in out
