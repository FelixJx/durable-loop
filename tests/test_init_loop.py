"""Category 1: init_loop.py — scaffolding, placeholder replacement, idempotency,
--force overwrite, and feature-name validation."""
import json
from pathlib import Path

import pytest


EXPECTED_FILES = ["checkpoint.json", "done.criteria.md", "handoff.md",
                  "tasks.jsonl", "decisions.log", "session.log"]
EXPECTED_DIRS = ["intermediate", "dead_letter", "traces"]


def test_creates_full_scaffold(tmp_path, run_init):
    rc, out, _ = run_init("myfeat", tmp_path)
    assert rc == 0
    d = tmp_path / ".scratch" / "myfeat"
    for f in EXPECTED_FILES:
        assert (d / f).is_file(), f
    for sub in EXPECTED_DIRS:
        assert (d / sub).is_dir(), sub


def test_checkpoint_placeholder_replaced(tmp_path, run_init):
    rc, out, _ = run_init("myfeat", tmp_path)
    assert rc == 0
    cp = json.loads((tmp_path / ".scratch/myfeat/checkpoint.json").read_text(encoding="utf-8"))
    assert cp["feature"] == "myfeat"  # NOT literal "<FEATURE>"
    assert cp["status"] == "fresh"


def test_idempotent_preserves_edits(tmp_path, run_init):
    run_init("myfeat", tmp_path)
    slog = tmp_path / ".scratch/myfeat/session.log"
    slog.write_text("USER EDIT\n", encoding="utf-8")
    cp = tmp_path / ".scratch/myfeat/checkpoint.json"
    cp.write_text(json.dumps({"status": "running", "feature": "myfeat"}), encoding="utf-8")
    # re-run WITHOUT --force
    rc, _, _ = run_init("myfeat", tmp_path)
    assert rc == 0
    assert slog.read_text(encoding="utf-8") == "USER EDIT\n"          # preserved
    assert json.loads(cp.read_text(encoding="utf-8"))["status"] == "running"  # preserved


def test_force_overwrites(tmp_path, run_init):
    run_init("myfeat", tmp_path)
    cp = tmp_path / ".scratch/myfeat/checkpoint.json"
    cp.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    rc, _, _ = run_init("myfeat", tmp_path, extra=["--force"])
    assert rc == 0
    assert json.loads(cp.read_text(encoding="utf-8"))["status"] == "fresh"  # reverted to template


@pytest.mark.parametrize("bad", ["r&d", "a/b", "a b", "a\\b", ""])
def test_rejects_invalid_feature_name(tmp_path, run_init, bad):
    rc, _, err = run_init(bad, tmp_path)
    assert rc == 1
    assert "invalid feature name" in err


def test_missing_project_dir(tmp_path, run_init):
    rc, _, err = run_init("myfeat", tmp_path / "does_not_exist")
    assert rc == 1
    assert "does not exist" in err


def test_feature_hint_printed(tmp_path, run_init):
    rc, out, _ = run_init("myfeat", tmp_path)
    assert rc == 0
    assert "DURABLE_LOOP_FEATURE" in out
    assert "myfeat" in out
