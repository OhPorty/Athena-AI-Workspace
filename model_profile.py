"""What an inference endpoint can tell us about a model, and the harness tier derived from it.

No standard exists for this: Ollama reports size/quantization/capabilities,
llama-server and Strata report context window and vision, LM Studio reports
type and max context, vLLM reports only max_model_len, and a plain
OpenAI-compatible server reports an id. So each engine gets a small adapter
that fills an all-optional facts record, and anything unknown stays None --
unknown is never treated as "no".

Facts are a *prior*. Parameter counts mislead (an MoE `35B-A3B` reports 34.7B
total, a 1-bit quant of a big model behaves small), so the tier only sets the
starting gate thresholds; progress-aware gating (see main._next_gate_counts)
does the real work, and a manual per-model override always wins.

Probing is best-effort and never raises: short timeouts, an unreachable host
stops the chain after the first probe, and results (including failures) are
cached so a chat request pays the cost at most once per model.
"""
import os
import re
import threading
import time

import httpx

import settings

TIERS = ("small", "standard", "large")

# Checkpoint thresholds per tier. "small" is exactly the behavior Athena had
# before profiles existed (tuned on small quantized local models); bigger
# models get more room before a checkpoint fires. Starting values -- turn_start
# trace records carry the tier, so tune these from real sessions.
GATE_THRESHOLDS = {
    "small": {"plan_checkin": 4, "self_investigation": 6, "workspace_search": 6},
    "standard": {"plan_checkin": 5, "self_investigation": 8, "workspace_search": 8},
    "large": {"plan_checkin": 8, "self_investigation": 12, "workspace_search": 12},
}

HOSTED_PROVIDERS = {"openai", "openrouter", "anthropic", "google"}

PROBING_ENABLED = os.environ.get("ATHENA_MODEL_PROBING", "1") != "0"
_PROBE_TIMEOUT = 2.0
_TTL_OK = 600
_TTL_FAIL = 120

_cache = {}
_cache_lock = threading.Lock()


def new_facts():
    return {
        "engine": None, "params_b": None, "active_params_b": None, "quant": None, "bits_per_weight": None,
        "context_window": None, "context_train": None, "vision": None, "tools": None, "thinking": None,
        "sources": [],
    }


# ---------- name hints ----------

_BITS = [("iq1", 1.6), ("iq2", 2.4), ("q2", 2.8), ("iq3", 3.3), ("q3", 3.7), ("iq4", 4.3), ("q4", 4.8),
         ("q5", 5.7), ("q6", 6.6), ("q8", 8.5), ("bf16", 16.0), ("f16", 16.0), ("fp16", 16.0)]
_QUANT_RE = re.compile(r"(?<![a-z0-9])(iq[1-4]_[a-z0-9]+|q[2-8](?:_[a-z0-9]+)*|bf16|f16|fp16)(?![a-z0-9])", re.I)
_SIZE_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", re.I)
_ACTIVE_RE = re.compile(r"(?<![a-z0-9])a(\d+(?:\.\d+)?)b(?![a-z0-9])", re.I)


def bits_for_quant(quant):
    q = (quant or "").lower()
    for prefix, bits in _BITS:
        if q.startswith(prefix):
            return bits
    return None


def parse_size(text):
    """'4.33B' -> 4.33, '494.03M' -> 0.49403, '' -> None."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([bmkBMK])", str(text or ""))
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    return value * {"b": 1.0, "m": 1e-3, "k": 1e-6}[unit]


def hints_from_name(name):
    """Best-effort size/active/quant from a model id or file name. Weak by nature
    (many ids carry none of these) -- only ever fills fields that are still None."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    out = {}
    quant = _QUANT_RE.search(name)
    if quant:
        out["quant"] = quant.group(1).upper()
    active = _ACTIVE_RE.search(name)
    if active:
        out["active_params_b"] = float(active.group(1))
    sizes = [float(s) for s in _SIZE_RE.findall(name)]
    if sizes:
        out["params_b"] = max(sizes)
    return out


def _apply_name_hints(facts, *names):
    for name in names:
        for key, value in hints_from_name(name).items():
            if facts.get(key) is None:
                facts[key] = value
                facts["sources"].append("name")
    if facts["bits_per_weight"] is None:
        facts["bits_per_weight"] = bits_for_quant(facts["quant"])
    return facts


# ---------- adapters ----------

def _root(url):
    url = (url or "").rstrip("/")
    for suffix in ("/api/chat", "/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")


def _json(client, method, url, **kw):
    """-> (status_code, parsed_json_or_None). Raises httpx.TransportError for an unreachable host."""
    r = client.request(method, url, **kw)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def _probe_ollama(client, root, model):
    status, d = _json(client, "POST", root + "/api/show", json={"model": model})
    if status != 200 or not isinstance(d, dict):
        return None
    facts = new_facts()
    facts["engine"] = "ollama"
    facts["sources"].append("ollama:/api/show")
    details = d.get("details") or {}
    facts["params_b"] = parse_size(details.get("parameter_size"))
    facts["quant"] = details.get("quantization_level") or None
    info = d.get("model_info") or {}
    arch = info.get("general.architecture")
    ctx = info.get(f"{arch}.context_length") if arch else None
    if ctx is None:
        ctx = next((v for k, v in info.items() if k.endswith(".context_length")), None)
    facts["context_train"] = ctx
    caps = d.get("capabilities")
    if isinstance(caps, list):  # absent on old Ollama -> stay unknown rather than claim "no"
        facts["vision"] = "vision" in caps
        facts["tools"] = "tools" in caps
        facts["thinking"] = "thinking" in caps
    return facts


def _probe_llamacpp(client, root, model, headers):
    """llama-server and Strata (which deliberately mirrors its /props and /v1/models)."""
    status, props = _json(client, "GET", root + "/props", headers=headers)
    if status != 200 or not isinstance(props, dict):
        return None
    facts = new_facts()
    facts["engine"] = "llamacpp"
    facts["sources"].append("llamacpp:/props")
    gen = props.get("default_generation_settings") or {}
    facts["context_window"] = gen.get("n_ctx")
    modalities = props.get("modalities")
    if isinstance(modalities, dict) and "vision" in modalities:
        facts["vision"] = bool(modalities["vision"])
    caps = props.get("chat_template_caps")
    if isinstance(caps, dict):
        for key in ("supports_tool_calls", "supports_tools"):
            if key in caps:
                facts["tools"] = bool(caps[key])
                break
    status, health = _json(client, "GET", root + "/health", headers=headers)
    if status == 200 and isinstance(health, dict) and health.get("service") == "strata":
        facts["engine"] = "strata"
        facts["sources"].append("strata:/health")
    status, models = _json(client, "GET", root + "/v1/models", headers=headers)
    if status == 200 and isinstance(models, dict):
        entry = next((m for m in models.get("data", []) if isinstance(m, dict) and m.get("id") == model), None) \
            or next(iter(models.get("data") or []), None)
        if isinstance(entry, dict):
            meta = entry.get("meta") or {}
            facts["context_window"] = facts["context_window"] or meta.get("n_ctx")
            facts["context_train"] = meta.get("n_ctx_train")
            if meta.get("n_params"):
                facts["params_b"] = float(meta["n_params"]) / 1e9
            arch = entry.get("architecture") or {}
            if facts["vision"] is None and isinstance(arch.get("input_modalities"), list):
                facts["vision"] = "image" in arch["input_modalities"]
    _apply_name_hints(facts, props.get("model_path"), props.get("model_alias"), model)
    return facts


def _probe_lmstudio(client, root, model, headers):
    status, d = _json(client, "GET", root + "/api/v0/models", headers=headers)
    if status != 200 or not isinstance(d, dict):
        return None
    entry = next((m for m in d.get("data", []) if isinstance(m, dict) and m.get("id") == model), None)
    if not entry:
        return None
    facts = new_facts()
    facts["engine"] = "lmstudio"
    facts["sources"].append("lmstudio:/api/v0/models")
    facts["context_train"] = entry.get("max_context_length")
    facts["quant"] = entry.get("quantization") or None
    facts["vision"] = entry.get("type") == "vlm"
    caps = entry.get("capabilities")
    if isinstance(caps, list):
        facts["tools"] = "tool_use" in caps
    return _apply_name_hints(facts, model)


def _probe_openai_models(client, base, model, headers):
    """Plain /v1/models: usually just an id; vLLM adds max_model_len."""
    status, d = _json(client, "GET", base.rstrip("/") + "/models", headers=headers)
    if status != 200 or not isinstance(d, dict):
        return None
    entry = next((m for m in d.get("data", []) if isinstance(m, dict) and m.get("id") == model), None) or {}
    facts = new_facts()
    facts["engine"] = "openai-compatible"
    facts["sources"].append("openai:/models")
    facts["context_window"] = entry.get("max_model_len") or (entry.get("meta") or {}).get("n_ctx")
    return _apply_name_hints(facts, model)


def _hosted_facts(provider, model):
    facts = new_facts()
    facts["engine"] = "hosted"
    facts["sources"].append("provider:" + provider)
    facts["tools"] = True
    if provider in ("anthropic", "google"):
        facts["vision"] = True
    return facts


# ---------- resolution ----------

def _probe(provider, endpoint_url, api_key, model, transport=None):
    if provider in HOSTED_PROVIDERS:
        return _hosted_facts(provider, model)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT, transport=transport) as client:
            if provider == "":
                root = _root(endpoint_url) or _root(_default_ollama_url())
                facts = _probe_ollama(client, root, model)
            else:
                root = _root(endpoint_url)
                facts = (_probe_llamacpp(client, root, model, headers)
                         or _probe_lmstudio(client, root, model, headers)
                         or _probe_openai_models(client, (endpoint_url or "").rstrip("/") or root + "/v1", model, headers))
    except httpx.HTTPError:
        facts = None  # unreachable: stop here instead of timing out on every remaining probe
    if facts is None:
        facts = new_facts()
        facts["sources"].append("unavailable")
    return _apply_name_hints(facts, model)


def _default_ollama_url():
    from host_locks import OLLAMA_URL
    return OLLAMA_URL


def get_facts(provider, endpoint_url, api_key, model, transport=None):
    """Cached facts for a model; never raises, never blocks longer than one probe timeout."""
    provider = provider or ""
    key = (provider, _root(endpoint_url), model)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    if PROBING_ENABLED:
        try:
            facts = _probe(provider, endpoint_url, api_key, model, transport)
        except Exception:
            facts = _apply_name_hints(new_facts(), model)
    else:
        facts = _apply_name_hints(new_facts(), model)
        if provider in HOSTED_PROVIDERS:
            facts = _hosted_facts(provider, model)
    ok = facts["engine"] is not None
    with _cache_lock:
        _cache[key] = (now + (_TTL_OK if ok else _TTL_FAIL), facts)
    return facts


def clear_cache():
    with _cache_lock:
        _cache.clear()


def derive_tier(facts, override=None):
    if override in TIERS:
        return override
    if facts.get("engine") == "hosted":
        return "large"
    effective = facts.get("active_params_b") or facts.get("params_b")
    if effective is None:
        tier = 1
    else:
        tier = 0 if effective < 12 else (1 if effective < 60 else 2)
    bits = facts.get("bits_per_weight")
    if bits is not None and bits < 3.0:  # IQ1/IQ2/Q2 land well below their parameter count
        tier = max(0, tier - 1)
    return TIERS[tier]


def get_override(model):
    entry = (settings.load_settings().get("model_profiles") or {}).get(model)
    return entry if isinstance(entry, dict) else {}


def resolve(provider, endpoint_url, api_key, model):
    """The profile a request should run under: facts, effective tier, vision, thresholds."""
    facts = get_facts(provider, endpoint_url, api_key, model)
    override = get_override(model)
    tier = derive_tier(facts, override.get("tier"))
    vision = override["vision"] if isinstance(override.get("vision"), bool) else bool(facts.get("vision"))
    return {
        "tier": tier,
        "tier_source": "override" if override.get("tier") in TIERS else "auto",
        "vision": vision,
        "facts": facts,
        "thresholds": dict(GATE_THRESHOLDS[tier]),
    }


def max_context(facts):
    """The largest context this model can actually be run with, or None when the endpoint
    didn't say. A server that loads the model at a fixed window (llama.cpp, Strata, vLLM)
    is limited by that window; Ollama and LM Studio size it per request, so the trained
    length is the limit."""
    window, trained = facts.get("context_window"), facts.get("context_train")
    fixed_window = facts.get("engine") in ("llamacpp", "strata", "openai-compatible")
    value = (window or trained) if fixed_window else (trained or window)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def thinking_supported(provider, endpoint_url, api_key, model):
    """True/False when the endpoint said so, None when it didn't (unknown is not 'no')."""
    return get_facts(provider, endpoint_url, api_key, model).get("thinking")


def summarize(facts):
    parts = []
    if facts.get("params_b"):
        size = f"{facts['params_b']:.1f}B"
        if facts.get("active_params_b"):
            size += f" ({facts['active_params_b']:g}B active)"
        parts.append(size)
    if facts.get("quant"):
        parts.append(facts["quant"])
    ctx = facts.get("context_window") or facts.get("context_train")
    if ctx:
        parts.append(f"{int(ctx) // 1024}k ctx")
    for key, label in (("vision", "vision"), ("tools", "tools"), ("thinking", "thinking")):
        if facts.get(key):
            parts.append(label)
    return " · ".join(parts)
