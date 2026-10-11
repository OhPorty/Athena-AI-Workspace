import httpx
import pytest

import model_profile as mp
import settings


def transport(routes):
    """routes: {(METHOD, path): (status, json)}; unknown routes 404; 'raise' simulates an unreachable host."""
    calls = []

    def handler(request):
        key = (request.method, request.url.path)
        calls.append(key)
        if routes == "raise":
            raise httpx.ConnectError("down")
        status, body = routes.get(key, (404, {"error": "not found"}))
        return httpx.Response(status, json=body)

    t = httpx.MockTransport(handler)
    t.calls = calls
    return t


@pytest.fixture(autouse=True)
def probing_on(monkeypatch):
    monkeypatch.setattr(mp, "PROBING_ENABLED", True)
    mp.clear_cache()


# ---------- name hints ----------

@pytest.mark.parametrize("name,expected", [
    ("qwen3.5:4b", {"params_b": 4.0}),
    ("hf.co/mudler/Qwen3.6-35B-A3B-APEX-GGUF:Qwen3.6-35B-A3B-APEX-I-Balanced.gguf", {"params_b": 35.0, "active_params_b": 3.0}),
    ("hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF:IQ2_XS", {"params_b": 27.0, "quant": "IQ2_XS"}),
    ("qwen3.8-flash-next-coder-iq1_m", {"quant": "IQ1_M"}),
    ("/models/Llama-3.1-8B-Instruct-Q4_K_M.gguf", {"params_b": 8.0, "quant": "Q4_K_M"}),
    ("gpt-4o", {}),
])
def test_hints_from_name(name, expected):
    assert mp.hints_from_name(name) == expected


def test_parse_size_units():
    assert mp.parse_size("4.33B") == pytest.approx(4.33)
    assert mp.parse_size("494.03M") == pytest.approx(0.49403)
    assert mp.parse_size("") is None


# ---------- adapters ----------

OLLAMA_SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "details": {"parameter_size": "4.33B", "quantization_level": "Q4_K_M", "family": "qwen35"},
    "model_info": {"general.architecture": "qwen35", "qwen35.context_length": 262144},
}


def test_ollama_adapter_reads_size_quant_context_and_capabilities():
    t = transport({("POST", "/api/show"): (200, OLLAMA_SHOW)})
    f = mp.get_facts("", "http://h:11434", "", "m", transport=t)
    assert (f["engine"], f["params_b"], f["quant"], f["context_train"]) == ("ollama", pytest.approx(4.33), "Q4_K_M", 262144)
    assert (f["tools"], f["thinking"], f["vision"]) == (True, True, False)
    assert f["bits_per_weight"] == pytest.approx(4.8)


def test_ollama_without_capabilities_field_leaves_them_unknown_not_false():
    old = {**OLLAMA_SHOW}
    del old["capabilities"]
    f = mp.get_facts("", "http://h:11434", "", "m", transport=transport({("POST", "/api/show"): (200, old)}))
    assert (f["tools"], f["thinking"], f["vision"]) == (None, None, None)


STRATA_PROPS = {
    "default_generation_settings": {"n_ctx": 32768, "params": {}},
    "modalities": {"vision": False},
    "chat_template_caps": {"supports_tools": True, "supports_tool_calls": True, "supports_preserve_reasoning": True},
    "model_alias": "qwen3.8-flash-next-coder-iq1_m",
    "model_path": "/models/Qwen3.8-Flash-Next-Coder-IQ1_M.gguf",
}


def test_strata_is_recognized_from_health_and_reads_window_vision_tools():
    t = transport({
        ("GET", "/props"): (200, STRATA_PROPS),
        ("GET", "/health"): (200, {"status": "ok", "service": "strata", "max_context": 32768}),
        ("GET", "/v1/models"): (200, {"data": [{"id": "qwen3.8-flash-next-coder-iq1_m", "meta": {"n_ctx": 32768},
                                               "architecture": {"input_modalities": ["text"]}}]}),
    })
    f = mp.get_facts("custom", "http://192.168.1.131:8080/v1", "k", "qwen3.8-flash-next-coder-iq1_m", transport=t)
    assert f["engine"] == "strata"
    assert (f["context_window"], f["vision"], f["tools"]) == (32768, False, True)
    assert f["quant"] == "IQ1_M" and f["bits_per_weight"] == pytest.approx(1.6)
    assert ("GET", "/props") in t.calls  # /v1 suffix stripped to reach the server root


def test_plain_llamacpp_without_health_service_is_llamacpp():
    t = transport({("GET", "/props"): (200, STRATA_PROPS), ("GET", "/health"): (200, {"status": "ok"})})
    assert mp.get_facts("custom", "http://h:8080/v1", "", "m", transport=t)["engine"] == "llamacpp"


def test_lmstudio_adapter():
    t = transport({("GET", "/api/v0/models"): (200, {"data": [
        {"id": "qwen2-vl-7b", "type": "vlm", "quantization": "Q4_K_M", "max_context_length": 32768, "capabilities": ["tool_use"]}]})})
    f = mp.get_facts("custom", "http://h:1234/v1", "", "qwen2-vl-7b", transport=t)
    assert (f["engine"], f["vision"], f["tools"], f["context_train"]) == ("lmstudio", True, True, 32768)


def test_generic_openai_server_reports_vllm_max_model_len_or_nothing():
    t = transport({("GET", "/v1/models"): (200, {"data": [{"id": "m", "max_model_len": 16384}]})})
    f = mp.get_facts("custom", "http://h:8000/v1", "", "m", transport=t)
    assert (f["engine"], f["context_window"]) == ("openai-compatible", 16384)


def test_unreachable_host_stops_after_one_probe_and_degrades_to_name_hints():
    t = transport("raise")
    f = mp.get_facts("custom", "http://down:8080/v1", "", "x-7B-Q4_K_M", transport=t)
    assert f["engine"] is None and "unavailable" in f["sources"]
    assert len(t.calls) == 1
    assert f["params_b"] == 7.0 and f["quant"] == "Q4_K_M"


def test_hosted_providers_need_no_network():
    f = mp.get_facts("anthropic", "", "k", "claude-x", transport=transport("raise"))
    assert (f["engine"], f["vision"], f["tools"]) == ("hosted", True, True)


def test_results_are_cached_including_failures():
    t = transport({("POST", "/api/show"): (200, OLLAMA_SHOW)})
    mp.get_facts("", "http://h:11434", "", "m", transport=t)
    mp.get_facts("", "http://h:11434", "", "m", transport=t)
    assert t.calls.count(("POST", "/api/show")) == 1
    down = transport("raise")
    mp.get_facts("custom", "http://dead:1/v1", "", "m", transport=down)
    mp.get_facts("custom", "http://dead:1/v1", "", "m", transport=down)
    assert len(down.calls) == 1


# ---------- tiers (against the models actually in use) ----------

def facts_for(name, **known):
    f = mp.new_facts()
    f.update(known)
    return mp._apply_name_hints(f, name)


@pytest.mark.parametrize("name,known,tier", [
    ("qwen3.5:4b", {"engine": "ollama", "params_b": 4.7}, "small"),
    ("Qwen3.6-35B-A3B-APEX", {"engine": "ollama", "params_b": 34.7}, "small"),            # MoE: 3B active, not 35B
    ("Qwen3.8-27B-IQ2_XS", {"engine": "ollama", "params_b": 26.9, "quant": "IQ2_XXS"}, "small"),  # 2-bit drops a tier
    ("qwen3.8-flash-next-coder-iq1_m", {"engine": "strata"}, "small"),                    # size unknown, 1-bit quant
    ("some-unknown-model", {"engine": "openai-compatible"}, "standard"),
    ("llama-32B-Q4_K_M", {"engine": "ollama", "params_b": 32.0, "quant": "Q4_K_M"}, "standard"),
    ("llama-70B-Q4_K_M", {"engine": "ollama", "params_b": 70.0, "quant": "Q4_K_M"}, "large"),
    ("claude-x", {"engine": "hosted"}, "large"),
])
def test_tier_derivation(name, known, tier):
    assert mp.derive_tier(facts_for(name, **known)) == tier


def test_override_beats_derivation_and_bad_values_are_ignored():
    f = facts_for("qwen3.5:4b", engine="ollama", params_b=4.7)
    assert mp.derive_tier(f, "large") == "large"
    assert mp.derive_tier(f, "enormous") == "small"


def test_resolve_applies_settings_override_and_vision_override(monkeypatch):
    monkeypatch.setattr(mp, "PROBING_ENABLED", False)
    settings.save_settings({"model_profiles": {"m": {"tier": "large", "vision": True}}})
    p = mp.resolve("custom", "http://h:8080/v1", "", "m")
    assert (p["tier"], p["tier_source"], p["vision"]) == ("large", "override", True)
    assert p["thresholds"] == mp.GATE_THRESHOLDS["large"]
    other = mp.resolve("custom", "http://h:8080/v1", "", "other-model")
    assert other["tier_source"] == "auto"


def test_small_tier_thresholds_equal_the_pre_profile_constants():
    import main
    t = mp.GATE_THRESHOLDS["small"]
    assert (t["plan_required"], t["plan_checkin"]) == (main._PLAN_REQUIRED_THRESHOLD, main._PLAN_CHECKIN_THRESHOLD)
    assert t["self_investigation"] == main._SELF_INVESTIGATION_TOOL_THRESHOLD


def test_thinking_supported_is_tri_state(monkeypatch):
    t = transport({("POST", "/api/show"): (200, {**OLLAMA_SHOW, "capabilities": ["completion", "tools"]})})
    assert mp.get_facts("", "http://h:11434", "", "m", transport=t)["thinking"] is False
    assert mp.thinking_supported("", "http://h:11434", "", "m") is False  # served from cache
    mp.clear_cache()
    monkeypatch.setattr(mp, "PROBING_ENABLED", False)
    assert mp.thinking_supported("", "http://h:11434", "", "m") is None   # nothing known -> unknown, not False


def test_summarize_is_compact_and_skips_unknowns():
    f = facts_for("x", engine="ollama", params_b=4.33, quant="Q4_K_M", context_train=262144, tools=True, thinking=True, vision=False)
    assert mp.summarize(f) == "4.3B · Q4_K_M · 256k ctx · tools · thinking"


def test_max_context_follows_the_engine_that_limits_it():
    facts = mp.new_facts
    ollama = {**facts(), "engine": "ollama", "context_train": 262144, "context_window": None}
    assert mp.max_context(ollama) == 262144
    # llama.cpp / Strata run at the window the server was started with, not the trained length
    llama = {**facts(), "engine": "llamacpp", "context_train": 262144, "context_window": 32768}
    assert mp.max_context(llama) == 32768
    lmstudio = {**facts(), "engine": "lmstudio", "context_train": 131072}
    assert mp.max_context(lmstudio) == 131072
    assert mp.max_context({**facts(), "engine": "hosted"}) is None
    assert mp.max_context({**facts(), "engine": "ollama", "context_train": 0}) is None
