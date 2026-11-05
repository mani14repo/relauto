"""Local LLM client: Ollama (default, CPU) or any OpenAI-compatible server (vLLM, llama.cpp server).

Settings (env / .env):
    LLM_PROVIDER   ollama | openai_compat            (default ollama)
    LLM_BASE_URL   http://localhost:11434 (ollama) | http://vllm:8000/v1
    LLM_MODEL      qwen3.5:4b                        (laptop default; cluster candidate gemma4:26b - choose by eval)
    LLM_THINK      false | true                      (default false: reasoning tokens are too slow on CPU)
    LLM_TIMEOUT    seconds per call                  (default 300 - CPU inference is slow)
    LLM_NUM_CTX    context window for ollama          (default 8192)

All calls are temperature 0 with a JSON schema for structured output. Nothing leaves the cluster.
Current Qwen 3.5 / Gemma 4 models have a thinking mode; it is turned off unless LLM_THINK=true, and any
<think>...</think> text a model emits anyway is stripped before parsing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import setting

DEFAULT_MODEL = "qwen3.5:4b"
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def describe(exc: Exception, timeout: float) -> str:
    """A short, plain description of an HTTP failure (httpx messages are long and link to MDN)."""
    if isinstance(exc, httpx.TimeoutException):
        return f"no answer within {timeout:.0f}s (LLM_TIMEOUT); the model may be overloaded or the prompt too long"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect; is the model server running?"
    if isinstance(exc, httpx.HTTPStatusError):
        body = ""
        try:
            j = exc.response.json()
            body = j.get("error") if isinstance(j.get("error"), str) else (j.get("error") or {}).get("message", "")
        except ValueError:
            body = exc.response.text[:200]
        return f"HTTP {exc.response.status_code}" + (f": {body}" if body else "")
    return f"{type(exc).__name__}: {exc}"


class LLMError(RuntimeError):
    pass


class ChatClient(Protocol):
    model: str

    def json_chat(self, system: str, user: str, schema: dict) -> dict: ...

    def chat(self, system: str, user: str) -> str: ...


def strip_thinking(text: str) -> str:
    text = THINK_BLOCK.sub("", text or "")
    if "</think>" in text.lower():  # unterminated opening tag: keep what follows the close
        text = re.split(r"</think>", text, flags=re.I)[-1]
    return text.strip()


@dataclass
class OllamaClient:
    base_url: str = "http://localhost:11434"
    model: str = DEFAULT_MODEL
    timeout: float = 300
    num_ctx: int = 8192
    think: bool = False

    def _post(self, payload: dict) -> dict:
        try:
            r = httpx.post(f"{self.base_url.rstrip('/')}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama call failed: {describe(exc, self.timeout)}") from exc

    def _payload(self, system: str, user: str, temperature: float, schema: dict | None = None) -> dict:
        p = {
            "model": self.model, "stream": False, "think": self.think,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]
        }
        if schema is not None:
            p["format"] = schema
        return p

    def json_chat(self, system: str, user: str, schema: dict) -> dict:
        data = self._post(self._payload(system, user, 0, schema))
        return _parse_json(data.get("message", {}).get("content", ""))

    def chat(self, system: str, user: str) -> str:
        data = self._post(self._payload(system, user, 0.1))
        return strip_thinking(data.get("message", {}).get("content", ""))

    def health(self) -> bool:
        """True when the server is up and this exact model (tag) is pulled."""
        try:
            r = httpx.get(f"{self.base_url.rstrip('/')}/api/tags", timeout=5)
            if r.status_code != 200:
                return False
            want = _norm(self.model)
            return any(_norm(m.get("name", "")) == want or _norm(m.get("model", "")) == want
                       for m in r.json().get("models", []))
        except (httpx.HTTPError, ValueError):
            return False


def _norm(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


@dataclass
class OpenAICompatClient:
    base_url: str = "http://localhost:8000/v1"
    model: str = "Qwen/Qwen3.5-4B"
    timeout: float = 300
    api_key: str = "not-needed"
    think: bool = False

    def _post(self, payload: dict) -> dict:
        try:
            r = httpx.post(f"{self.base_url.rstrip('/')}/chat/completions", json=payload, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"openai-compatible call failed: {describe(exc, self.timeout)}") from exc

    def _payload(self, system: str, user: str, temperature: float) -> dict:
        # vLLM / llama.cpp pass chat_template_kwargs to the template; Qwen and Gemma templates read enable_thinking.
        return {"model": self.model, "temperature": temperature,
                "chat_template_kwargs": {"enable_thinking": self.think},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}

    def json_chat(self, system: str, user: str, schema: dict) -> dict:
        p = self._payload(system, user, 0)
        p["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}
        data = self._post(p)
        return _parse_json(data["choices"][0]["message"]["content"])

    def chat(self, system: str, user: str) -> str:
        data = self._post(self._payload(system, user, 0.1))
        return strip_thinking(data["choices"][0]["message"]["content"])

    def health(self) -> bool:
        try:
            return httpx.get(f"{self.base_url.rstrip('/')}/models", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False


def _parse_json(text: str) -> dict:
    text = strip_thinking(text)
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"model did not return JSON: {text[:200]!r}")
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned malformed JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError("model returned non-object JSON")
    return obj


def from_settings() -> ChatClient:
    provider = setting("LLM_PROVIDER", "ollama")
    timeout = float(setting("LLM_TIMEOUT", "300"))
    think = str(setting("LLM_THINK", "false")).strip().lower() in ("1", "true", "yes")
    if provider == "openai_compat":
        return OpenAICompatClient(base_url=setting("LLM_BASE_URL", "http://localhost:8000/v1"),
                                  model=setting("LLM_MODEL", "Qwen/Qwen3.5-4B"), timeout=timeout,
                                  api_key=setting("LLM_API_KEY", "not-needed"), think=think)
    return OllamaClient(base_url=setting("LLM_BASE_URL", "http://localhost:11434"),
                        model=setting("LLM_MODEL", DEFAULT_MODEL), timeout=timeout,
                        num_ctx=int(setting("LLM_NUM_CTX", "8192")), think=think)
