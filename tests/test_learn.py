"""Tests for the experience-distillation (learnings) layer — durable_loop_learn.py.

Covers the shared learnings contract:
  log    — new entry creation; same-(type,key) merge (confidence=max, seen+1,
           insight updated, id preserved, no duplicate line); run_id read from
           checkpoint.json (missing => "").
  search — keyword scoring/ordering; --type filter; --cross-feature scan; friendly
           no-result message.
  prune  — dry-run report vs --apply deletion; stale detection on missing source file.
  compile — confidence threshold + non-stale + pattern-only filter, confidence-desc order.
  robustness — malformed lines skipped (non-fatal); fail-open with no feature dir;
           usage error (bad feature / missing project_dir) exits 2.
"""
import json
import subprocess
import sys
from pathlib import Path

import durable_loop_learn as learn

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PY = sys.executable


def _run(*cli_args):
    proc = subprocess.run(
        [PY, str(SCRIPTS / "durable_loop_learn.py"), *[str(a) for a in cli_args]],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _learnings_file(tmp_path, feature="f"):
    return tmp_path / ".scratch" / feature / "learnings.jsonl"


def _read_records(tmp_path, feature="f"):
    p = _learnings_file(tmp_path, feature)
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _mk_feature(tmp_path, feature="f", checkpoint=None):
    d = tmp_path / ".scratch" / feature
    d.mkdir(parents=True, exist_ok=True)
    if checkpoint is not None:
        (d / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    return d


# --- log: new entry --------------------------------------------------------
def test_log_creates_new_entry(tmp_path):
    _mk_feature(tmp_path, "f", checkpoint={"run_id": "rid-1", "iteration": 3})
    rc, out, err = _run("log", "f", tmp_path, "--type", "pattern",
                        "--key", "use-tmp-replace", "--insight", "atomic writes via tmp+replace",
                        "--confidence", "7", "--source", "x.py", "--iteration", "3")
    assert rc == 0, err
    recs = _read_records(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["type"] == "pattern"
    assert r["key"] == "use-tmp-replace"
    assert r["confidence"] == 7
    assert r["seen"] == 1
    assert r["stale"] is False
    assert r["run_id"] == "rid-1"
    assert r["iteration"] == 3
    assert len(r["id"]) == 8


def test_log_run_id_empty_when_no_checkpoint(tmp_path):
    _mk_feature(tmp_path, "f")  # no checkpoint.json
    rc, _, err = _run("log", "f", tmp_path, "--type", "pitfall",
                      "--key", "k", "--insight", "i", "--confidence", "4")
    assert rc == 0, err
    assert _read_records(tmp_path)[0]["run_id"] == ""


def test_log_confidence_clamped(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "k",
         "--insight", "i", "--confidence", "99")
    assert _read_records(tmp_path)[0]["confidence"] == 10


# --- log: same-key merge ---------------------------------------------------
def test_log_merge_same_type_key(tmp_path):
    _mk_feature(tmp_path, "f", checkpoint={"run_id": "rid-2"})
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "k",
         "--insight", "first insight", "--confidence", "5", "--source", "a.py")
    id1 = _read_records(tmp_path)[0]["id"]
    # second log: same (type,key), lower confidence, new insight
    rc, out, err = _run("log", "f", tmp_path, "--type", "pattern", "--key", "k",
                        "--insight", "second insight", "--confidence", "3")
    assert rc == 0, err
    recs = _read_records(tmp_path)
    assert len(recs) == 1, "merge must not append a duplicate line"
    r = recs[0]
    assert r["confidence"] == 5          # max(5,3)
    assert r["insight"] == "second insight"  # newest insight wins
    assert r["seen"] == 2                # +1
    assert r["id"] == id1                # id preserved


def test_log_merge_takes_higher_confidence(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "k",
         "--insight", "i", "--confidence", "4")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "k",
         "--insight", "i2", "--confidence", "9")
    assert _read_records(tmp_path)[0]["confidence"] == 9


def test_log_different_type_same_key_is_distinct(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "k",
         "--insight", "i", "--confidence", "5")
    _run("log", "f", tmp_path, "--type", "pitfall", "--key", "k",
         "--insight", "j", "--confidence", "5")
    recs = _read_records(tmp_path)
    assert len(recs) == 2  # (pattern,k) and (pitfall,k) are different learnings


# --- search ----------------------------------------------------------------
def _seed_search(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "atomic-write",
         "--insight", "use tmp file then replace for atomic write", "--confidence", "8")
    _run("log", "f", tmp_path, "--type", "pitfall", "--key", "wsl-bash",
         "--insight", "bare bash resolves to wsl launcher on windows", "--confidence", "6")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "fail-open",
         "--insight", "hooks must never raise into the caller", "--confidence", "9")


def test_search_matches_and_orders(tmp_path):
    _seed_search(tmp_path)
    rc, out, err = _run("search", "f", tmp_path, "--query", "atomic write")
    assert rc == 0, err
    assert "atomic-write" in out
    assert "Prior learning applied:" in out


def test_search_score_then_confidence_order(tmp_path):
    _mk_feature(tmp_path, "f")
    # both match one token "write"; "two" matches two tokens -> ranks first
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "one",
         "--insight", "write something", "--confidence", "10")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "two",
         "--insight", "write atomic always", "--confidence", "1")
    rc, out, err = _run("search", "f", tmp_path, "--query", "write atomic")
    assert rc == 0, err
    assert out.index("[two]") < out.index("[one]"), "higher match score ranks first"


def test_search_type_filter(tmp_path):
    _seed_search(tmp_path)
    rc, out, err = _run("search", "f", tmp_path, "--query", "write bash raise",
                        "--type", "pitfall")
    assert rc == 0, err
    assert "wsl-bash" in out
    assert "atomic-write" not in out
    assert "fail-open" not in out


def test_search_limit(tmp_path):
    _seed_search(tmp_path)
    rc, out, err = _run("search", "f", tmp_path, "--query", "write bash raise atomic",
                        "--limit", "1")
    assert rc == 0, err
    assert out.count("Prior learning applied:") == 1


def test_search_no_match_friendly(tmp_path):
    _seed_search(tmp_path)
    rc, out, err = _run("search", "f", tmp_path, "--query", "zzzznomatch")
    assert rc == 0, err
    assert "no prior learnings matched" in out


def test_search_cross_feature(tmp_path):
    _mk_feature(tmp_path, "f")
    _mk_feature(tmp_path, "g")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "k-in-f",
         "--insight", "concurrency lesson", "--confidence", "7")
    _run("log", "g", tmp_path, "--type", "pattern", "--key", "k-in-g",
         "--insight", "concurrency wisdom", "--confidence", "7")
    # without cross-feature, searching f only sees f's entry
    rc, out, _ = _run("search", "f", tmp_path, "--query", "concurrency")
    assert "k-in-g" not in out
    # with cross-feature, both surface
    rc, out, err = _run("search", "f", tmp_path, "--query", "concurrency", "--cross-feature")
    assert rc == 0, err
    assert "k-in-f" in out and "k-in-g" in out


def test_search_prefers_non_stale(tmp_path):
    _mk_feature(tmp_path, "f")
    p = _learnings_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": "aaaaaaaa", "type": "pattern", "key": "stale-one",
         "insight": "concurrency note", "confidence": 10, "source": "observed",
         "iteration": 1, "run_id": "", "timestamp": "t", "seen": 1, "stale": True},
        {"id": "bbbbbbbb", "type": "pattern", "key": "fresh-one",
         "insight": "concurrency note", "confidence": 5, "source": "observed",
         "iteration": 1, "run_id": "", "timestamp": "t", "seen": 1, "stale": False},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    rc, out, err = _run("search", "f", tmp_path, "--query", "concurrency")
    assert rc == 0, err
    assert out.index("[fresh-one]") < out.index("[stale-one]")


# --- prune -----------------------------------------------------------------
def test_prune_dry_run_reports_without_deleting(tmp_path):
    _mk_feature(tmp_path, "f")
    # source points to a missing file -> stale
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "gone",
         "--insight", "i", "--confidence", "7", "--source", "subdir/missing.py")
    rc, out, err = _run("prune", "f", tmp_path)
    assert rc == 0, err
    assert "dry-run" in out
    assert "stale" in out
    # dry-run only reports: the line is still present and NOT deleted.
    recs = _read_records(tmp_path)
    assert len(recs) == 1
    assert recs[0]["key"] == "gone"


def test_prune_keeps_existing_source(tmp_path):
    _mk_feature(tmp_path, "f")
    real = tmp_path / "real.py"
    real.write_text("x = 1\n", encoding="utf-8")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "alive",
         "--insight", "i", "--confidence", "7", "--source", "real.py")
    rc, out, err = _run("prune", "f", tmp_path)
    assert rc == 0, err
    assert _read_records(tmp_path)[0]["stale"] is False


def test_prune_observed_source_never_stale(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "obs",
         "--insight", "i", "--confidence", "7")  # source defaults to "observed"
    rc, out, err = _run("prune", "f", tmp_path)
    assert rc == 0, err
    assert _read_records(tmp_path)[0]["stale"] is False


def test_prune_apply_deletes_stale(tmp_path):
    _mk_feature(tmp_path, "f")
    real = tmp_path / "real.py"
    real.write_text("x\n", encoding="utf-8")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "gone",
         "--insight", "i", "--confidence", "7", "--source", "missing.py")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "alive",
         "--insight", "i", "--confidence", "7", "--source", "real.py")
    rc, out, err = _run("prune", "f", tmp_path, "--apply")
    assert rc == 0, err
    keys = {r["key"] for r in _read_records(tmp_path)}
    assert keys == {"alive"}


def test_prune_no_learnings_friendly(tmp_path):
    _mk_feature(tmp_path, "f")
    rc, out, err = _run("prune", "f", tmp_path)
    assert rc == 0, err
    assert "nothing to prune" in out


# --- compile ---------------------------------------------------------------
def test_compile_threshold_and_pattern_only(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "high",
         "--insight", "high conf pattern", "--confidence", "8")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "low",
         "--insight", "low conf pattern", "--confidence", "2")
    _run("log", "f", tmp_path, "--type", "pitfall", "--key", "pit",
         "--insight", "a pitfall", "--confidence", "9")
    rc, out, err = _run("compile", "f", tmp_path)
    assert rc == 0, err
    assert "已验证经验" in out
    assert "[high]" in out
    assert "[low]" not in out   # below default threshold 6
    assert "[pit]" not in out   # pitfalls excluded


def test_compile_orders_by_confidence_desc(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "mid",
         "--insight", "i", "--confidence", "7")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "top",
         "--insight", "i", "--confidence", "10")
    rc, out, err = _run("compile", "f", tmp_path)
    assert rc == 0, err
    assert out.index("[top]") < out.index("[mid]")


def test_compile_min_confidence_override(tmp_path):
    _mk_feature(tmp_path, "f")
    _run("log", "f", tmp_path, "--type", "pattern", "--key", "k3",
         "--insight", "i", "--confidence", "3")
    rc, out, err = _run("compile", "f", tmp_path, "--min-confidence", "3")
    assert rc == 0, err
    assert "[k3]" in out


def test_compile_excludes_stale(tmp_path):
    _mk_feature(tmp_path, "f")
    p = _learnings_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "id": "aaaaaaaa", "type": "pattern", "key": "stale-pat",
        "insight": "i", "confidence": 9, "source": "observed",
        "iteration": 1, "run_id": "", "timestamp": "t", "seen": 1, "stale": True,
    }) + "\n", encoding="utf-8")
    rc, out, err = _run("compile", "f", tmp_path)
    assert rc == 0, err
    assert "[stale-pat]" not in out
    assert "none yet" in out


def test_compile_empty_block_when_nothing(tmp_path):
    _mk_feature(tmp_path, "f")
    rc, out, err = _run("compile", "f", tmp_path)
    assert rc == 0, err
    assert "已验证经验" in out
    assert "none yet" in out


# --- robustness: malformed lines, fail-open, usage errors ------------------
def test_malformed_lines_skipped(tmp_path):
    _mk_feature(tmp_path, "f")
    p = _learnings_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"id": "aaaaaaaa", "type": "pattern", "key": "ok",
                       "insight": "atomic concept", "confidence": 8, "source": "observed",
                       "iteration": 1, "run_id": "", "timestamp": "t", "seen": 1, "stale": False})
    p.write_text("not json at all\n" + good + "\n[1,2,3]\n", encoding="utf-8")
    rc, out, err = _run("search", "f", tmp_path, "--query", "atomic")
    assert rc == 0, err
    assert "[ok]" in out  # good line survives, bad lines skipped non-fatally


def test_log_into_corrupt_file_does_not_lose_good_lines(tmp_path):
    _mk_feature(tmp_path, "f")
    p = _learnings_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"id": "aaaaaaaa", "type": "pattern", "key": "ok",
                       "insight": "i", "confidence": 8, "source": "observed",
                       "iteration": 1, "run_id": "", "timestamp": "t", "seen": 1, "stale": False})
    p.write_text("garbage\n" + good + "\n", encoding="utf-8")
    rc, _, err = _run("log", "f", tmp_path, "--type", "pattern", "--key", "new",
                      "--insight", "i", "--confidence", "5")
    assert rc == 0, err
    keys = {r["key"] for r in _read_records(tmp_path)}
    assert keys == {"ok", "new"}  # surviving good line + the new one


def test_fail_open_no_feature_dir_search(tmp_path):
    # no .scratch/<feature>/ at all
    rc, out, err = _run("search", "nope", tmp_path, "--query", "anything")
    assert rc == 0, err
    assert "no prior learnings matched" in out


def test_fail_open_no_feature_dir_compile(tmp_path):
    rc, out, err = _run("compile", "nope", tmp_path)
    assert rc == 0, err
    assert "已验证经验" in out


def test_fail_open_no_feature_dir_prune(tmp_path):
    rc, out, err = _run("prune", "nope", tmp_path)
    assert rc == 0, err
    assert "nothing to prune" in out


def test_usage_error_bad_feature_name(tmp_path):
    rc, out, err = _run("search", "bad name", tmp_path, "--query", "x")
    assert rc == 2
    assert "invalid feature name" in err


def test_usage_error_missing_project_dir(tmp_path):
    missing = tmp_path / "does-not-exist"
    rc, out, err = _run("search", "f", missing, "--query", "x")
    assert rc == 2
    assert "does not exist" in err


def test_usage_error_unknown_subcommand(tmp_path):
    rc, out, err = _run("bogus", "f", tmp_path)
    assert rc == 2  # argparse usage error


def test_usage_error_missing_required_arg(tmp_path):
    _mk_feature(tmp_path, "f")
    rc, out, err = _run("log", "f", tmp_path, "--type", "pattern", "--key", "k",
                        "--insight", "i")  # missing --confidence
    assert rc == 2


# --- module-level unit checks ----------------------------------------------
def test_score_record_counts_distinct_tokens():
    r = {"key": "atomic-write", "insight": "use replace", "source": "x.py"}
    assert learn.score_record(r, ["atomic", "replace"]) == 2
    assert learn.score_record(r, ["nope"]) == 0


def test_format_hit_shape():
    r = {"key": "k", "type": "pattern", "confidence": 7, "iteration": 2, "insight": "do it"}
    s = learn.format_hit(r)
    assert s == "Prior learning applied: [k] (pattern, confidence 7/10, iter 2) — do it"


def test_source_missing_detection(tmp_path):
    assert learn._source_is_missing_file("nope/missing.py", tmp_path) is True
    assert learn._source_is_missing_file("observed", tmp_path) is False
    assert learn._source_is_missing_file("abc123commit", tmp_path) is False
    real = tmp_path / "real.py"
    real.write_text("x\n", encoding="utf-8")
    assert learn._source_is_missing_file("real.py", tmp_path) is False
