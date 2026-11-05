"""Minimal client for an OpenAI-compatible Microsoft Foundry endpoint (used only to draft synthetic scenarios).

Settings (from the environment or .env):
    FOUNDRY_ENDPOINT   e.g. https://<resource>.services.ai.azure.com/openai/v1/
    FOUNDRY_API_KEY    the resource key
    APP_MODEL          deployment name, e.g. gpt-6-luna
    APP_EFFORT         reasoning effort: low | medium | high (optional)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from sra.config import setting  # noqa: E402


class FoundryError(RuntimeError):
    pass


def chat_json(system: str, user: str, schema: dict, name: str = "result", timeout: float = 300) -> dict:
    """One chat completion constrained to `schema`; returns the parsed JSON object."""
    endpoint, key, model = setting("FOUNDRY_ENDPOINT", ""), setting("FOUNDRY_API_KEY", ""), setting("APP_MODEL", "")
    missing = [n for n, v in (("FOUNDRY_ENDPOINT", endpoint), ("FOUNDRY_API_KEY", key), ("APP_MODEL", model)) if not v]
    if missing:
        raise FoundryError(f"set {', '.join(missing)} in .env")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
    }
    if effort := setting("APP_EFFORT", ""):
        body["reasoning_effort"] = effort
    url = endpoint.rstrip("/") + "/chat/completions"
    r = httpx.post(url, json=body, headers={"api-key": key}, timeout=timeout)
    if r.status_code >= 400:
        raise FoundryError(f"Foundry returned HTTP {r.status_code}: {r.text[:500]}")
    content = r.json()["choices"][0]["message"].get("content") or ""
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise FoundryError(f"model did not return JSON: {content[:300]}") from e
