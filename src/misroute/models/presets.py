import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {"provider": "anthropic", "key": "ANTHROPIC_API_KEY"},
    "foundry": {"provider": "anthropic-foundry"},
    "bedrock": {"provider": "bedrock"},
    "openai": {"provider": "openai-compatible", "base_url": "https://api.openai.com/v1", "key": "OPENAI_API_KEY"},
    "openrouter": {
        "provider": "openai-compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "key": "OPENROUTER_API_KEY",
        "options": {"extra": {"usage": {"include": True}}},
    },
    "azure": {"provider": "azure-openai", "base_env": "AZURE_OPENAI_ENDPOINT", "key": "AZURE_OPENAI_API_KEY"},
    "ollama": {"provider": "openai-compatible", "base_url": "http://localhost:11434/v1"},
    "lmstudio": {"provider": "openai-compatible", "base_url": "http://localhost:1234/v1"},
    "vllm": {"provider": "openai-compatible", "base_env": "VLLM_BASE_URL", "base_url": "http://localhost:8000/v1"},
    "mock": {"provider": "mock"},
}

DETECT = [
    ("ANTHROPIC_API_KEY", "anthropic:claude-sonnet-5-5"),
    ("ANTHROPIC_FOUNDRY_API_KEY", "foundry:claude-sonnet-5-5"),
    ("OPENROUTER_API_KEY", "openrouter:anthropic/claude-sonnet-5.5"),
    ("AZURE_OPENAI_API_KEY", None),
    ("OPENAI_API_KEY", None),
]

CATALOG = "https://openrouter.ai/api/v1/models"
CACHE = Path.home() / ".cache" / "misroute" / "prices.json"
MAX_AGE = 7 * 24 * 3600


def expand(spec: str) -> dict[str, Any]:
    """Turn provider:model shorthand into a model config block, for example anthropic:claude-opus-5-5."""
    prefix, sep, model = spec.partition(":")
    if not sep or prefix not in PROVIDERS:
        raise ValueError(f"model shorthand must look like provider:model, providers are {', '.join(PROVIDERS)}")
    p = PROVIDERS[prefix]
    out: dict[str, Any] = {
        "name": re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or prefix,
        "provider": p["provider"],
    }
    out["model"] = model
    base = os.environ.get(p.get("base_env", ""), "") or p.get("base_url")
    if base:
        out["base_url"] = base
    if p.get("key"):
        out["api_key_env"] = p["key"]
    if p.get("options"):
        out["options"] = json.loads(json.dumps(p["options"]))
    if prefix == "azure" and os.environ.get("AZURE_OPENAI_API_VERSION"):
        out.setdefault("options", {})["api_version"] = os.environ["AZURE_OPENAI_API_VERSION"]
    return out


def ollama_model() -> str | None:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1) as r:
            models = json.load(r).get("models", [])
    except (OSError, ValueError):
        return None
    return models[0]["name"] if models else None


def detect() -> tuple[str | None, str]:
    """Pick a model from the environment. Returns the shorthand, or None with the reason."""
    for var, spec in DETECT:
        if os.environ.get(var):
            if spec:
                return spec, f"found {var}"
            return None, f"found {var}, pass --model with your model or deployment name"
    if local := ollama_model():
        return f"ollama:{local}", "found a local Ollama server"
    return None, "no model key found in the environment, pass --model provider:model"


def _catalog() -> dict[str, tuple[float, float]]:
    if CACHE.exists() and time.time() - CACHE.stat().st_mtime < MAX_AGE:
        return {k: tuple(v) for k, v in json.loads(CACHE.read_text()).items()}
    if os.environ.get("MISROUTE_OFFLINE"):
        return {}
    try:
        with urllib.request.urlopen(CATALOG, timeout=5) as r:
            rows = json.load(r)["data"]
    except (OSError, ValueError, KeyError):
        return {}
    prices = {}
    for m in rows:
        try:
            prices[m["id"]] = (float(m["pricing"]["prompt"]) * 1e6, float(m["pricing"]["completion"]) * 1e6)
        except (KeyError, TypeError, ValueError):
            continue
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(prices))
    return prices


def candidates(provider: str, model: str) -> list[str]:
    base = model.split("/")[-1]
    dotted = re.sub(r"(\d)-(\d)", r"\1.\2", base)
    names = [model, base, dotted]
    if provider.startswith("anthropic") or provider == "bedrock" or base.startswith("claude"):
        names += [f"anthropic/{dotted}", f"anthropic/{base}"]
    return names


def price(provider: str, model: str) -> tuple[float, float] | None:
    """List price per million input and output tokens, from the public OpenRouter catalog, cached for a week."""
    if provider == "mock" or not model:
        return None
    table = _catalog()
    for name in candidates(provider, model):
        if name in table:
            return table[name]
    return None
