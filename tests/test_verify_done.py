"""Category 2: verify_done.py — command extraction (cmd-comment only, prose
backticks never run), PASS/FAIL/MANUAL classification, VERDICT matrix."""
import pytest


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
