"""durable_loop_guard.py — PreToolUse idempotency gate + strict dangerous-op block.

Covers:
  - idempotency key already recorded  -> DENY
  - key not recorded                  -> ALLOW
  - strict ON + dangerous command     -> DENY (+ pending_approval.json written)
  - strict OFF + dangerous command    -> ALLOW
  - no checkpoint / no .scratch       -> ALLOW (fail open)
  - inactive (paused) loop            -> ALLOW (scope guard)
  - ambiguous (>1 feature)            -> ALLOW (fail open)

The deny decision is emitted as PreToolUse JSON on stdout; tests call evaluate()
in-process (capturing stdout) so they assert the official permissionDecision shape,
not just an exit code.
"""
import io
import json
import os
from contextlib import redirect_stdout

import pytest

import durable_loop_guard as g


def _run(ev, env=None):
    """Invoke evaluate() in-process under an optional env patch.
    Returns (rc, decision_or_None) where decision is the parsed stdout JSON."""
    saved = {}
    if env is not None:
        for k, v in env.items():
            saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = g.evaluate(ev)
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
    out = buf.getvalue().strip()
    decision = json.loads(out) if out else None
    return rc, decision


def _is_deny(decision) -> bool:
    return (
        isinstance(decision, dict)
        and decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


def _ev(tmp_path, tool_name, tool_input):
    return {
        "hook_event_name": "PreToolUse",
        "cwd": str(tmp_path),
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


# Ensure no inherited strict env leaks into the "off by default" tests.
@pytest.fixture(autouse=True)
def _clear_strict_env():
    saved = os.environ.pop("DURABLE_LOOP_STRICT", None)
    saved_feat = os.environ.pop("DURABLE_LOOP_FEATURE", None)
    yield
    if saved is not None:
        os.environ["DURABLE_LOOP_STRICT"] = saved
    if saved_feat is not None:
        os.environ["DURABLE_LOOP_FEATURE"] = saved_feat


# --- idempotency gate ------------------------------------------------------
def test_idempotency_key_hit_denies(tmp_path, make_checkpoint):
    key = g._derive_side_effect_key("Bash", {"command": "git push origin main"})
    assert key  # sanity: this is a side-effect command
    make_checkpoint("f", status="running", idempotency_keys=[key])
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "git push origin main"}))
    assert rc == g.EXIT_ALLOW  # deny is expressed via JSON, exit stays 0
    assert _is_deny(decision)
    assert "幂等门" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_idempotency_key_miss_allows(tmp_path, make_checkpoint):
    make_checkpoint("f", status="running", idempotency_keys=["exec-deadbeef"])
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "git push origin main"}))
    assert rc == g.EXIT_ALLOW
    assert decision is None  # allow == no decision emitted


def test_non_side_effect_call_allows(tmp_path, make_checkpoint):
    # a read-only command derives no key, so the gate never fires even if keys exist
    make_checkpoint("f", status="running", idempotency_keys=["exec-anything"])
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "ls -la"}))
    assert rc == g.EXIT_ALLOW
    assert decision is None


def test_idempotency_write_key_hit_denies(tmp_path, make_checkpoint):
    key = g._derive_side_effect_key("Write", {"file_path": "src/config.py"})
    make_checkpoint("f", status="running", idempotency_keys=[key])
    rc, decision = _run(_ev(tmp_path, "Write", {"file_path": "src/config.py"}))
    assert _is_deny(decision)


# --- strict dangerous-op block --------------------------------------------
def test_strict_on_blocks_dangerous_via_env(tmp_path, make_checkpoint):
    cp = make_checkpoint("f", status="running")
    rc, decision = _run(
        _ev(tmp_path, "Bash", {"command": "git push --force origin main"}),
        env={"DURABLE_LOOP_STRICT": "1"},
    )
    assert _is_deny(decision)
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pending_approval.json" in reason
    # the blocked op is recorded for operator review
    pa = cp.parent / "pending_approval.json"
    assert pa.exists()
    data = json.loads(pa.read_text(encoding="utf-8"))
    assert data["requests"][0]["status"] == "pending"
    assert "--force" in data["requests"][0]["command"]


def test_strict_on_via_checkpoint_field(tmp_path, make_checkpoint):
    make_checkpoint("f", status="running", strict_guard=True)
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "rm -rf /tmp/x"}))
    assert _is_deny(decision)


def test_strict_off_allows_dangerous(tmp_path, make_checkpoint):
    # strict NOT enabled (default) -> dangerous command passes the strict stage.
    # It also derives no idempotency key, so the whole guard allows it.
    make_checkpoint("f", status="running")
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "git reset --hard HEAD~1"}))
    assert rc == g.EXIT_ALLOW
    assert decision is None
    assert not (tmp_path / ".scratch" / "f" / "pending_approval.json").exists()


def test_strict_env_falsey_stays_off(tmp_path, make_checkpoint):
    make_checkpoint("f", status="running")
    rc, decision = _run(
        _ev(tmp_path, "Bash", {"command": "rm -rf /tmp/x"}),
        env={"DURABLE_LOOP_STRICT": "0"},
    )
    assert decision is None


# --- fail-open paths -------------------------------------------------------
def test_no_checkpoint_allows(tmp_path):
    # Explicit feature pointing at a non-existent checkpoint -> discover returns
    # None -> fail open. (Using DURABLE_LOOP_FEATURE avoids the upward parent walk
    # picking up a sibling pytest tmp dir's .scratch.)
    rc, decision = _run(
        _ev(tmp_path, "Bash", {"command": "git push --force origin main"}),
        env={"DURABLE_LOOP_STRICT": "1", "DURABLE_LOOP_FEATURE": "missing",
             "DURABLE_LOOP_PROJECT_DIR": str(tmp_path)},
    )
    assert rc == g.EXIT_ALLOW
    assert decision is None


def test_unreadable_checkpoint_allows(tmp_path):
    cp_dir = tmp_path / ".scratch" / "f"
    cp_dir.mkdir(parents=True)
    (cp_dir / "checkpoint.json").write_text("{ not json", encoding="utf-8")
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "git push origin main"}))
    assert rc == g.EXIT_ALLOW
    assert decision is None


def test_inactive_loop_allows(tmp_path, make_checkpoint):
    key = g._derive_side_effect_key("Bash", {"command": "git push origin main"})
    # even with a key hit AND strict on, a paused loop must not block
    make_checkpoint("f", status="paused", idempotency_keys=[key])
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "git push origin main"}),
                        env={"DURABLE_LOOP_STRICT": "1"})
    assert rc == g.EXIT_ALLOW
    assert decision is None


def test_ambiguous_two_features_allows(tmp_path):
    for f in ("a", "b"):
        d = tmp_path / ".scratch" / f
        d.mkdir(parents=True)
        key = g._derive_side_effect_key("Bash", {"command": "git push origin main"})
        d.joinpath("checkpoint.json").write_text(
            json.dumps({"feature": f, "status": "running", "idempotency_keys": [key]}),
            encoding="utf-8",
        )
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "git push origin main"}))
    assert rc == g.EXIT_ALLOW
    assert decision is None


def test_missing_idempotency_keys_field_allows(tmp_path, make_checkpoint):
    # backward-compat: checkpoint without idempotency_keys must not crash/deny
    cp_dir = tmp_path / ".scratch" / "f"
    cp_dir.mkdir(parents=True)
    cp_dir.joinpath("checkpoint.json").write_text(
        json.dumps({"feature": "f", "status": "running"}), encoding="utf-8")
    rc, decision = _run(_ev(tmp_path, "Bash", {"command": "git push origin main"}))
    assert rc == g.EXIT_ALLOW
    assert decision is None
