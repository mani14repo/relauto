"""Local LLM client: Ollama (default, CPU) or any OpenAI-compatible server (vLLM, llama.cpp server).

Settings (env / .env):
    LLM_PROVIDER   ollama | openai_compat            (default ollama)
    LLM_BASE_URL   http://localhost:11434 (ollama) | http://vllm:8000/v1
    LLM_MODEL      qwen2.5:7b-instruct               (choose by eval; any GGUF instruct model)
    LLM_TIMEOUT    seconds per call                  (default 300 - CPU inference is slow)
    LLM_NUM_CTX    context window for ollama          (default 8192)

All calls are temperature 0 with a JSON schema for structured output. Nothing leaves the cluster.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import setting


class LLMError(RuntimeError):
    pass


class ChatClient(Protocol):
    model: str

    def json_chat(self, system: str, user: str, schema: dict) -> dict: ...

    def chat(self, system: str, user: str) -> str: ...


@dataclass
class OllamaClient:
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b-instruct"
    timeout: float = 300
    num_ctx: int = 8192

    def _post(self, payload: dict) -> dict:
        try:
            r = httpx.post(f"{self.base_url.rstrip('/')}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama call failed: {exc}") from exc

    def json_chat(self, system: str, user: str, schema: dict) -> dict:
        data = self._post({"model": self.model, "stream": False, "format": schema,
                           "options": {"temperature": 0, "num_ctx": self.num_ctx},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return _parse_json(data.get("message", {}).get("content", ""))

    def chat(self, system: str, user: str) -> str:
        data = self._post({"model": self.model, "stream": False, "options": {"temperature": 0.1, "num_ctx": self.num_ctx},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return data.get("message", {}).get("content", "")

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url.rstrip('/')}/api/tags", timeout=5)
            return r.status_code == 200 and any(m.get("name", "").startswith(self.model.split(":")[0])
                                                for m in r.json().get("models", []))
        except httpx.HTTPError:
            return False


@dataclass
class OpenAICompatClient:
    base_url: str = "http://localhost:8000/v1"
    model: str = "qwen2.5-7b-instruct"
    timeout: float = 300
    api_key: str = "not-needed"

    def _post(self, payload: dict) -> dict:
        try:
            r = httpx.post(f"{self.base_url.rstrip('/')}/chat/completions", json=payload, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"openai-compatible call failed: {exc}") from exc

    def json_chat(self, system: str, user: str, schema: dict) -> dict:
        data = self._post({"model": self.model, "temperature": 0,
                           "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return _parse_json(data["choices"][0]["message"]["content"])

    def chat(self, system: str, user: str) -> str:
        data = self._post({"model": self.model, "temperature": 0.1,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return data["choices"][0]["message"]["content"]

    def health(self) -> bool:
        try:
            return httpx.get(f"{self.base_url.rstrip('/')}/models", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"model did not return JSON: {text[:200]!r}")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise LLMError("model returned non-object JSON")
    return obj


def from_settings() -> ChatClient:
    provider = setting("LLM_PROVIDER", "ollama")
    timeout = float(setting("LLM_TIMEOUT", "300"))
    if provider == "openai_compat":
        return OpenAICompatClient(base_url=setting("LLM_BASE_URL", "http://localhost:8000/v1"),
                                  model=setting("LLM_MODEL", "qwen2.5-7b-instruct"), timeout=timeout,
                                  api_key=setting("LLM_API_KEY", "not-needed"))
    return OllamaClient(base_url=setting("LLM_BASE_URL", "http://localhost:11434"),
                        model=setting("LLM_MODEL", "qwen2.5:7b-instruct"), timeout=timeout,
                        num_ctx=int(setting("LLM_NUM_CTX", "8192")))
