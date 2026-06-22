"""Category 4: durable_loop_checkpoint.py — pricing, side-effect key derivation,
parse_transcript determinism, and that main() actually writes dollars."""
import json

import durable_loop_checkpoint as m


# --- pricing ---------------------------------------------------------------
def test_price_for_model_matches_family():
    assert m.price_for_model("claude-opus-4-8")["output"] == 75.0
    assert m.price_for_model("CLAUDE-SONNET-4-6")["output"] == 15.0
    assert m.price_for_model("claude-haiku-4-5")["input"] == 1.0
    assert m.price_for_model("glm-5.2")["input"] == 0.60
    assert m.price_for_model("unknown-model") == m.DEFAULT_PRICING


def test_cost_for_usage_opus():
    # 1M input + 1M output, no cache
    cost = m.cost_for_usage("claude-opus-4-8", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert abs(cost - 90.0) < 0.01  # $15 in + $75 out


def test_cost_for_usage_zero():
    assert m.cost_for_usage("claude-opus-4-8", {}) == 0.0


# --- side-effect key derivation --------------------------------------------
def test_write_key_disambiguates_same_basename():
    # regression: basename-only keys collided for src/config.py vs tests/config.py
    a = m._derive_side_effect_key("Write", {"file_path": "src/config.py"})
    b = m._derive_side_effect_key("Write", {"file_path": "tests/config.py"})
    assert a.startswith("write-config.py-")
    assert a != b


def test_write_key_stable_for_same_path():
    assert (m._derive_side_effect_key("Write", {"file_path": "a/b/c.py"})
            == m._derive_side_effect_key("Write", {"file_path": "a/b/c.py"}))


def test_bash_side_effects_detected():
    for cmd in ("git commit -m x", "git push origin main", "npm publish",
                "alembic upgrade head", "docker push img:tag",
                "terraform apply", "kubectl apply -f x.yaml", "gh release create v1"):
        assert m._derive_side_effect_key("Bash", {"command": cmd}).startswith("exec-"), cmd


def test_bash_curl_post_forms_detected():
    # regression: old code only matched the malformed literal 'curl -xput'
    for cmd in ("curl -X POST https://api/charge", "curl --request PUT https://x",
                "curl -d k=v https://x", "curl --data-raw k=v https://x"):
        assert m._derive_side_effect_key("Bash", {"command": cmd}).startswith("exec-"), cmd


def test_bash_non_side_effects_empty():
    for cmd in ("ls -la", "grep foo bar", "echo hi", "cat f.txt", "cd /tmp"):
        assert m._derive_side_effect_key("Bash", {"command": cmd}) == "", cmd


def test_powershell_mcp_tool_detected():
    assert m._derive_side_effect_key("mcp__windows-mcp__PowerShell", {"command": "Set-Item x"}).startswith("exec-")
    assert m._derive_side_effect_key("mcp__windows-mcp__PowerShell", {"command": ""}) == ""


def test_read_is_not_a_side_effect():
    assert m._derive_side_effect_key("Read", {"file_path": "x.py"}) == ""


# --- parse_transcript ------------------------------------------------------
def _entry(model, usage, blocks=None):
    return {"type": "assistant", "model": model,
            "message": {"role": "assistant", "model": model, "usage": usage,
                        "content": blocks or []}}


def test_parse_transcript_tokens_dollars_keys(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [
        _entry("claude-opus-4-8",
               {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 100},
               [{"type": "tool_use", "name": "Write", "input": {"file_path": "src/a.py"}}]),
        _entry("glm-5.2", {"input_tokens": 2000, "output_tokens": 100},
               [{"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m x"}}]),
    ]) + "\n", encoding="utf-8")
    tokens, dollars, keys = m.parse_transcript(str(t))
    assert tokens == 3900  # 1800 + 2100
    assert dollars > 0.05 and dollars < 0.06  # ~0.0561
    assert any(k.startswith("write-a.py-") for k in keys)
    assert any(k.startswith("exec-") for k in keys)


def test_parse_transcript_missing_file(tmp_path):
    assert m.parse_transcript(str(tmp_path / "nope.jsonl")) == (0, 0.0, [])


def test_parse_transcript_deterministic(tmp_path):
    t = tmp_path / "t.jsonl"
    blocks = [{"type": "tool_use", "name": "Bash", "input": {"command": "git push"}}]
    t.write_text(json.dumps(_entry("claude-sonnet-4-6", {"input_tokens": 10, "output_tokens": 5}, blocks)) + "\n",
                 encoding="utf-8")
    a = m.parse_transcript(str(t))
    b = m.parse_transcript(str(t))
    assert a == b  # full-rebuild is idempotent


# --- main() writes dollars + idempotency union -----------------------------
def test_main_writes_dollars_and_keys(tmp_path, run_hook):
    cp_dir = tmp_path / ".scratch" / "f"
    cp_dir.mkdir(parents=True)
    cp = cp_dir / "checkpoint.json"
    base = {
        "feature": "f", "budget_used": {"tokens": 0, "dollars": 0.0, "iterations": 0, "hours": 0},
        "started_at": "", "idempotency_keys": [],
    }
    cp.write_text(json.dumps(base), encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(_entry("claude-opus-4-8", {"input_tokens": 100, "output_tokens": 50},
                              [{"type": "tool_use", "name": "Bash", "input": {"command": "npm publish"}}])) + "\n",
                          encoding="utf-8")
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": str(transcript)})
    assert rc == 0
    out = json.loads(cp.read_text(encoding="utf-8"))
    assert out["budget_used"]["dollars"] > 0  # dollars rail is now live
    assert out["budget_used"]["tokens"] == 150
    assert any(k.startswith("exec-") for k in out["idempotency_keys"])


def test_main_failopen_no_scratch(tmp_path, run_hook):
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0  # no .scratch -> fail open, exit 0


# --- context reset / handoff (improvement #2) ------------------------------
def _reset_checkpoint(iteration, reset_every_n=None, status="running"):
    cp = {
        "feature": "f", "iteration": iteration, "status": status,
        "budget_used": {"tokens": 10, "dollars": 0.1, "hours": 0.5, "iterations": iteration},
        "started_at": "", "idempotency_keys": [],
        "resume_from": "run pytest tests/test_charge.py -x",
        "cumulative_state": {
            "goal": "make charge idempotent",
            "artifacts_produced": ["src/charge.py", "tests/test_charge.py"],
            "decisions_made": ["use stripe idempotency key"],
            "facts_discovered": ["replay returns first 200, not a new charge"],
            "rejected_approaches": ["in-memory dedupe — lost on restart"],
        },
    }
    if reset_every_n is not None:
        cp["reset_every_n"] = reset_every_n
    return cp


def _write_cp(tmp_path, cp, feature="f"):
    cp_dir = tmp_path / ".scratch" / feature
    cp_dir.mkdir(parents=True, exist_ok=True)
    p = cp_dir / "checkpoint.json"
    p.write_text(json.dumps(cp), encoding="utf-8")
    return p


def test_handoff_fires_at_nth_iteration(tmp_path, run_hook):
    # default reset_every_n=5; iteration 5 -> 5 % 5 == 0 -> reset due
    cp_path = _write_cp(tmp_path, _reset_checkpoint(iteration=5))
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert out.get("reset_due") is True
    handoff = tmp_path / ".scratch" / "f" / "handoff.md"
    assert handoff.exists()
    body = handoff.read_text(encoding="utf-8")
    # constraint-class facts must survive the reset
    assert "make charge idempotent" in body
    assert "in-memory dedupe" in body          # rejected approach
    assert "run pytest tests/test_charge.py -x" in body  # next action from resume_from


def test_handoff_archives_previous_version(tmp_path, run_hook):
    cp_path = _write_cp(tmp_path, _reset_checkpoint(iteration=10))
    # a stale handoff already exists -> must be archived under iter_<N>.md
    handoff = tmp_path / ".scratch" / "f" / "handoff.md"
    handoff.write_text("OLD HANDOFF CONTENT", encoding="utf-8")
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    archived = tmp_path / ".scratch" / "f" / "handoff_archive" / "iter_10.md"
    assert archived.exists()
    assert archived.read_text(encoding="utf-8") == "OLD HANDOFF CONTENT"
    # new handoff overwrote the old one
    assert "OLD HANDOFF CONTENT" not in handoff.read_text(encoding="utf-8")
    assert json.loads(cp_path.read_text(encoding="utf-8")).get("reset_due") is True


def test_handoff_not_triggered_before_nth(tmp_path, run_hook):
    # iteration 3 with default N=5 -> 3 % 5 != 0 -> no reset
    cp_path = _write_cp(tmp_path, _reset_checkpoint(iteration=3))
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert "reset_due" not in out  # flag is only ever set, never spuriously written
    assert not (tmp_path / ".scratch" / "f" / "handoff.md").exists()


def test_handoff_iteration_zero_never_resets(tmp_path, run_hook):
    # iteration 0: 0 % 5 == 0 but we explicitly require iteration > 0
    cp_path = _write_cp(tmp_path, _reset_checkpoint(iteration=0))
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert "reset_due" not in out
    assert not (tmp_path / ".scratch" / "f" / "handoff.md").exists()


def test_handoff_disabled_when_n_zero(tmp_path, run_hook):
    # reset_every_n=0 disables the feature even on a multiple-of-anything iteration
    cp_path = _write_cp(tmp_path, _reset_checkpoint(iteration=5, reset_every_n=0))
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert "reset_due" not in out
    assert not (tmp_path / ".scratch" / "f" / "handoff.md").exists()


def test_handoff_custom_cadence(tmp_path, run_hook):
    # reset_every_n=3, iteration 6 -> 6 % 3 == 0 -> fires
    cp_path = _write_cp(tmp_path, _reset_checkpoint(iteration=6, reset_every_n=3))
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    assert json.loads(cp_path.read_text(encoding="utf-8")).get("reset_due") is True
    assert (tmp_path / ".scratch" / "f" / "handoff.md").exists()


def test_handoff_skipped_for_inactive_status(tmp_path, run_hook):
    # a paused/finished loop must not be touched even if cadence would fire
    cp_path = _write_cp(tmp_path, _reset_checkpoint(iteration=5, status="completed"))
    rc, _ = run_hook("durable_loop_checkpoint.py", {"cwd": str(tmp_path), "transcript_path": ""})
    assert rc == 0
    out = json.loads(cp_path.read_text(encoding="utf-8"))
    assert "reset_due" not in out
    assert not (tmp_path / ".scratch" / "f" / "handoff.md").exists()


# --- unit-level: build_handoff / maybe_write_handoff -----------------------
def test_build_handoff_falls_back_when_state_missing():
    cp = {"feature": "myfeat", "iteration": 5}
    body = m.build_handoff(cp, 5)
    assert "myfeat" in body                 # goal falls back to feature name
    assert "(none recorded)" in body        # empty sections render a placeholder
    assert "iteration 5" in body or "iterations: 5" in body


def test_maybe_write_handoff_default_n_when_field_absent(tmp_path):
    # field absent -> DEFAULT_RESET_EVERY_N (5) used (back-compat)
    cp = _reset_checkpoint(iteration=5)
    del cp["cumulative_state"]  # also exercises missing cumulative_state
    feature_dir = tmp_path / ".scratch" / "f"
    feature_dir.mkdir(parents=True)
    fired = m.maybe_write_handoff(cp, feature_dir)
    assert fired is True
    assert cp.get("reset_due") is True
    assert (feature_dir / "handoff.md").exists()
