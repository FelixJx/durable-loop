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
