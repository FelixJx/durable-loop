"""Tests for the human-review-feedback reflow layer — durable_loop_review.py (Gap2).

Covers the review → learning mapping contract:
  approve → one pattern row (confidence 7, insight carries the reason).
  reject  → one pitfall row (confidence 8).
  modify  → BOTH a pattern ([修改自审核], conf 6) AND a pitfall (the changed bits, conf 8).
  dedup   → same (feature, decision, draft) re-review MERGES (seen+1, no new row),
            because the key is derived from the draft basename and learn.py merges
            on (type,key).
  source  → --draft path becomes the learning `source` (so learn.prune can later
            mark it stale); absent draft → literal "human-review".
  session.log → one JSON line per review action (tool=durable_loop_review).
  robustness → missing reason / invalid feature name / missing project_dir exit 2;
            no .scratch dir at all still exits 0 (fail-open, mirroring learn.py).

All tests invoke the script via subprocess (same pattern as test_learn.py) so the
argparse + fail-open top-level guard are exercised end-to-end.
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PY = sys.executable


def _run(*cli_args):
    proc = subprocess.run(
        [PY, str(SCRIPTS / "durable_loop_review.py"), *[str(a) for a in cli_args]],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _learnings_file(tmp_path, feature="f"):
    return tmp_path / ".scratch" / feature / "learnings.jsonl"


def _read_records(tmp_path, feature="f"):
    p = _learnings_file(tmp_path, feature)
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _read_session(tmp_path, feature="f"):
    p = tmp_path / ".scratch" / feature / "session.log"
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _mk_feature(tmp_path, feature="f", checkpoint=None):
    d = tmp_path / ".scratch" / feature
    d.mkdir(parents=True, exist_ok=True)
    if checkpoint is not None:
        (d / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    return d


# --- approve → pattern -----------------------------------------------------
def test_review_approve_logs_pattern(tmp_path):
    _mk_feature(tmp_path, "f", checkpoint={"run_id": "rid-1", "iteration": 3})
    rc, out, err = _run("--feature", "f", "--project-dir", tmp_path,
                        "--decision", "approve", "--reason", "this direction is sound")
    assert rc == 0, err
    recs = _read_records(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["type"] == "pattern"
    assert r["confidence"] == 7
    assert "this direction is sound" in r["insight"]
    assert r["key"].startswith("review-approve-")
    assert r["source"] == "human-review"   # no --draft given
    assert "decision=approve" in out


# --- reject → pitfall ------------------------------------------------------
def test_review_reject_logs_pitfall(tmp_path):
    _mk_feature(tmp_path, "f")
    rc, out, err = _run("--feature", "f", "--project-dir", tmp_path,
                        "--decision", "reject", "--reason", "wrong abstraction")
    assert rc == 0, err
    recs = _read_records(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["type"] == "pitfall"
    assert r["confidence"] == 8
    assert "wrong abstraction" in r["insight"]
    assert r["key"].startswith("review-reject-")


# --- modify → pattern + pitfall -------------------------------------------
def test_review_modify_logs_both(tmp_path):
    _mk_feature(tmp_path, "f")
    rc, out, err = _run("--feature", "f", "--project-dir", tmp_path,
                        "--decision", "modify",
                        "--reason", "keep the API but rename the helper")
    assert rc == 0, err
    recs = _read_records(tmp_path)
    assert len(recs) == 2
    by_type = {r["type"]: r for r in recs}
    assert set(by_type) == {"pattern", "pitfall"}
    pat = by_type["pattern"]
    pit = by_type["pitfall"]
    assert pat["confidence"] == 6
    assert "[修改自审核]" in pat["insight"]
    assert "审核要求修改的部分" in pit["insight"]
    assert pit["confidence"] == 8
    # session.log still records ONE row for the whole review action
    sess = _read_session(tmp_path)
    assert len(sess) == 1
    assert sess[0]["decision"] == "modify"


# --- dedup: same decision + draft merges ----------------------------------
def test_review_dedup_same_decision_draft(tmp_path):
    _mk_feature(tmp_path, "f")
    draft = tmp_path / "pr-42.diff"
    draft.write_text("diff\n", encoding="utf-8")
    # First review of this draft
    rc1, _, err1 = _run("--feature", "f", "--project-dir", tmp_path, "--decision", "approve",
                        "--reason", "good", "--draft", str(draft))
    assert rc1 == 0, err1
    after_first = _read_records(tmp_path)
    assert len(after_first) == 1
    assert after_first[0]["seen"] == 1
    key1 = after_first[0]["key"]
    # Second IDENTICAL review (same feature / decision / draft basename)
    rc2, _, err2 = _run("--feature", "f", "--project-dir", tmp_path, "--decision", "approve",
                        "--reason", "still good", "--draft", str(draft))
    assert rc2 == 0, err2
    after_second = _read_records(tmp_path)
    assert len(after_second) == 1, "same (type,key) must merge, not append"
    assert after_second[0]["seen"] == 2
    assert after_second[0]["key"] == key1   # key stable across re-reviews
    # key is derived from the draft stem
    assert "pr-42" in key1


# --- missing reason exits 2 ------------------------------------------------
def test_review_missing_reason_exits_2(tmp_path):
    _mk_feature(tmp_path, "f")
    # argparse `required=True` rejects a fully absent --reason with exit 2
    rc, out, err = _run("--feature", "f", "--project-dir", tmp_path, "--decision", "approve")
    assert rc == 2
    # empty-string reason is also a usage error (caught in main(), not argparse)
    rc2, out2, err2 = _run("--feature", "f", "--project-dir", tmp_path, "--decision", "approve",
                           "--reason", "   ")
    assert rc2 == 2
    assert "reason" in err2.lower()


# --- invalid feature name exits 2 -----------------------------------------
def test_review_invalid_feature_exits_2(tmp_path):
    rc, out, err = _run("--feature", "bad name", "--project-dir", tmp_path,
                        "--decision", "approve", "--reason", "x")
    assert rc == 2
    assert "invalid feature name" in err


def test_review_missing_project_dir_exits_2(tmp_path):
    missing = tmp_path / "does-not-exist"
    rc, out, err = _run("--feature", "f", "--project-dir", missing,
                        "--decision", "approve", "--reason", "x")
    assert rc == 2
    assert "does not exist" in err


def test_review_bad_decision_exits_2(tmp_path):
    _mk_feature(tmp_path, "f")
    rc, out, err = _run("--feature", "f", "--project-dir", tmp_path,
                        "--decision", "bogus", "--reason", "x")
    assert rc == 2  # argparse choices= rejection


# --- fail-open: no .scratch dir at all still rc 0 --------------------------
def test_review_failopen_no_scratch(tmp_path):
    # tmp_path has NO .scratch/ — learn.cmd_log creates learnings.jsonl lazily,
    # and the whole thing is wrapped in a fail-open guard, so rc must be 0.
    rc, out, err = _run("--feature", "f", "--project-dir", tmp_path,
                        "--decision", "approve", "--reason", "no scratch yet")
    assert rc == 0, err
    # a learning was still written (learn.py creates the dir)
    recs = _read_records(tmp_path)
    assert len(recs) == 1
    assert recs[0]["run_id"] == ""   # no checkpoint => empty run_id


# --- source is the --draft path when given --------------------------------
def test_review_source_is_draft_path(tmp_path):
    _mk_feature(tmp_path, "f")
    draft = tmp_path / "subdir" / "draft-pr.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("# pr\n", encoding="utf-8")
    rc, out, err = _run("--feature", "f", "--project-dir", tmp_path, "--decision", "reject",
                        "--reason", "nope", "--draft", str(draft))
    assert rc == 0, err
    r = _read_records(tmp_path)[0]
    assert r["source"] == str(draft)


# --- session.log records the review action --------------------------------
def test_review_appends_session_log(tmp_path):
    _mk_feature(tmp_path, "f", checkpoint={"run_id": "rid-9"})
    rc, _, err = _run("--feature", "f", "--project-dir", tmp_path, "--decision", "reject",
                      "--reason", "vetoed approach")
    assert rc == 0, err
    sess = _read_session(tmp_path)
    assert len(sess) == 1
    e = sess[0]
    assert e["tool"] == "durable_loop_review"
    assert e["decision"] == "reject"
    assert e["run_id"] == "rid-9"
    assert e["key"].startswith("review-reject-")
    assert "vetoed approach" in e["resp"]


# --- the logged learning is searchable (the whole point of Gap2) ----------
def test_review_result_is_searchable(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("--feature", "f", "--project-dir", tmp_path, "--decision", "reject",
         "--reason", "do not roll your own crypto")
    proc = subprocess.run(
        [PY, str(SCRIPTS / "durable_loop_learn.py"), "search", "f", tmp_path,
         "--query", "crypto"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Prior learning applied:" in proc.stdout
    assert "do not roll your own crypto" in proc.stdout
