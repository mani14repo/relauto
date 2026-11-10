"""Local LLM client: Ollama (default, CPU) or any OpenAI-compatible server (vLLM, llama.cpp server).

Settings (env / .env):
    LLM_PROVIDER   ollama | openai_compat            (default ollama)
    LLM_BASE_URL   http://localhost:11434 (ollama) | http://vllm:8000/v1; several servers comma-separated
    LLM_CONCURRENCY  calls in flight per server      (default: OLLAMA_NUM_PARALLEL if this process sees it, else 1)
    LLM_MODEL      qwen3.5:4b                        (laptop default; cluster candidate gemma4:26b - choose by eval)
    LLM_THINK      false | true                      (default false: reasoning tokens are too slow on CPU)
    LLM_TIMEOUT    seconds per call                  (default 300 - CPU inference is slow)
    LLM_NUM_CTX    context window for ollama          (default 8192)
    LLM_NUM_PREDICT  most tokens a reply may have     (default 768; the JSON schema also caps field lengths)
    LLM_KEEP_ALIVE how long Ollama keeps the model loaded after a call (default 30m; -1 = forever)
    LLM_NUM_THREAD CPU threads for Ollama            (default: Ollama's choice; set to the physical core count)

All calls are temperature 0 with a JSON schema for structured output. Nothing leaves the cluster.
After each call `last_usage` holds what the server reported: prompt tokens it had to read (Ollama excludes the
part of the prompt it reused from its cache), reply tokens, and the seconds spent on each.
Responses are streamed so a run can be cancelled mid-call: when `cancel_check()` turns true the caller is
released within a second, and the connection is closed at the next streamed token, which makes the model
server stop generating.
Current Qwen 3.5 / Gemma 4 models have a thinking mode; it is turned off unless LLM_THINK=true, and any
<think>...</think> text a model emits anyway is stripped before parsing.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from ..config import setting
from ..progress import JobCancelled

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


def stream_call(url: str, payload: dict, *, timeout: float, headers: dict | None = None,
                cancel_check: Callable[[], bool] | None = None, piece: Callable[[str], str | None]) -> str:
    """POST with a streamed response and return the concatenated text.

    `piece(line)` turns one streamed line into text (or None to skip). Servers that ignore streaming and send one
    JSON body still work: unparseable lines are kept and the whole body is parsed at the end.
    The request runs in a worker thread so a cancel releases the caller at once, even while the model is still
    reading the prompt and has sent nothing yet.
    """
    check = cancel_check or (lambda: False)
    out: dict = {}
    finished = threading.Event()

    def run() -> None:
        try:
            with httpx.stream("POST", url, json=payload, headers=headers, timeout=timeout) as r:
                if r.status_code >= 400:
                    r.read()
                    r.raise_for_status()
                parts, raw = [], []
                for line in r.iter_lines():
                    if check():
                        out["cancelled"] = True
                        return            # leaving the context closes the connection; the server stops generating
                    if not line.strip():
                        continue
                    raw.append(line)
                    try:
                        text = piece(line)
                    except (ValueError, KeyError, IndexError, TypeError):
                        out["fallback"] = True
                        continue
                    if text:
                        parts.append(text)
                out["text"] = "".join(parts)
                out["raw"] = "\n".join(raw)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller's thread
            out["error"] = exc
        finally:
            finished.set()

    threading.Thread(target=run, daemon=True, name="llm-call").start()
    while not finished.wait(0.5):
        if check():
            raise JobCancelled("cancelled during a model call")
    if out.get("cancelled"):
        raise JobCancelled("cancelled during a model call")
    if "error" in out:
        raise out["error"]
    if out.get("fallback") and not out.get("text"):
        return out.get("raw", "")
    return out.get("text", "")


def _ollama_piece(line: str, usage: dict | None = None) -> str | None:
    obj = json.loads(line)
    if obj.get("error"):
        raise LLMError(str(obj["error"]))
    if obj.get("done") and usage is not None:
        ns = 1e9                                    # Ollama reports durations in nanoseconds
        usage.update({"prompt_tokens": obj.get("prompt_eval_count", 0), "reply_tokens": obj.get("eval_count", 0),
                      "prompt_s": round(obj.get("prompt_eval_duration", 0) / ns, 2),
                      "reply_s": round(obj.get("eval_duration", 0) / ns, 2),
                      "load_s": round(obj.get("load_duration", 0) / ns, 2),
                      "total_s": round(obj.get("total_duration", 0) / ns, 2),
                      "stopped_at_limit": obj.get("done_reason") == "length"})
    return (obj.get("message") or {}).get("content") or None


def _openai_piece(line: str, usage: dict | None = None) -> str | None:
    if line.startswith("data:"):
        data = line[5:].strip()
        if data == "[DONE]":
            return None
        obj = json.loads(data)
        if obj.get("error"):
            raise LLMError(str(obj["error"]))
        _openai_usage(obj, usage)
        choice = (obj.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length" and usage is not None:
            usage["stopped_at_limit"] = True
        return (choice.get("delta") or choice.get("message") or {}).get("content") or None
    obj = json.loads(line)                          # non-streaming server: one JSON body
    _openai_usage(obj, usage)
    return obj["choices"][0]["message"]["content"]


def _openai_usage(obj: dict, usage: dict | None) -> None:
    u = obj.get("usage")
    if u and usage is not None:
        usage.update({"prompt_tokens": u.get("prompt_tokens", 0), "reply_tokens": u.get("completion_tokens", 0)})


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
    num_predict: int = 768
    keep_alive: str = "30m"
    num_thread: int | None = None
    cancel_check: Callable[[], bool] | None = None   # set by the evaluator for cancellable runs
    last_usage: dict = field(default_factory=dict)

    def _post(self, payload: dict) -> dict:
        usage: dict = {}
        self.last_usage = usage
        try:
            text = stream_call(f"{self.base_url.rstrip('/')}/api/chat", {**payload, "stream": True},
                               timeout=self.timeout, cancel_check=self.cancel_check,
                               piece=lambda line: _ollama_piece(line, usage))
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama call failed: {describe(exc, self.timeout)}") from exc
        except LLMError as exc:
            raise LLMError(f"ollama call failed: {exc}") from exc
        return {"message": {"content": text}}

    def _payload(self, system: str, user: str, temperature: float, schema: dict | None = None) -> dict:
        options = {"temperature": temperature, "num_ctx": self.num_ctx, "num_predict": self.num_predict}
        if self.num_thread:
            options["num_thread"] = self.num_thread
        p = {
            "model": self.model, "stream": True, "think": self.think, "keep_alive": self.keep_alive,
            "options": options,
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
    num_predict: int = 768
    cancel_check: Callable[[], bool] | None = None
    last_usage: dict = field(default_factory=dict)

    def _post(self, payload: dict) -> dict:
        usage: dict = {}
        self.last_usage = usage
        try:
            text = stream_call(f"{self.base_url.rstrip('/')}/chat/completions",
                               {**payload, "stream": True, "stream_options": {"include_usage": True}},
                               timeout=self.timeout, headers={"Authorization": f"Bearer {self.api_key}"},
                               cancel_check=self.cancel_check, piece=lambda line: _openai_piece(line, usage))
        except httpx.HTTPError as exc:
            raise LLMError(f"openai-compatible call failed: {describe(exc, self.timeout)}") from exc
        except LLMError as exc:
            raise LLMError(f"openai-compatible call failed: {exc}") from exc
        return {"choices": [{"message": {"content": text}}]}

    def _payload(self, system: str, user: str, temperature: float) -> dict:
        # vLLM / llama.cpp pass chat_template_kwargs to the template; Qwen and Gemma templates read enable_thinking.
        return {"model": self.model, "temperature": temperature, "max_tokens": self.num_predict,
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


def base_urls() -> list[str]:
    """LLM_BASE_URL may list several servers, comma-separated (e.g. one per Ollama replica)."""
    default = "http://localhost:8000/v1" if setting("LLM_PROVIDER", "ollama") == "openai_compat" else "http://localhost:11434"
    return [u.strip() for u in str(setting("LLM_BASE_URL", default)).split(",") if u.strip()] or [default]


def concurrency() -> str:
    """LLM_CONCURRENCY if set; otherwise, for Ollama, OLLAMA_NUM_PARALLEL when this process can see it (e.g. a Windows
    user variable shared with a local Ollama); otherwise 1. Ollama never reads this app's .env, so setting only
    OLLAMA_NUM_PARALLEL in .env changes the app, not the server."""
    own = str(setting("LLM_CONCURRENCY", "") or "").strip()
    if own:
        return own
    if setting("LLM_PROVIDER", "ollama") == "ollama":
        server = str(setting("OLLAMA_NUM_PARALLEL", "") or "").strip()
        if server.isdigit():
            return server
    return "1"


def clients_from_settings() -> list[ChatClient]:
    """One client per call that may be in flight: LLM_CONCURRENCY per server, interleaved across servers.

    Each client is used by one call at a time, so its `last_usage` and `cancel_check` are never shared.
    """
    per_server = max(1, int(concurrency()))
    urls = base_urls()
    return [from_settings(url) for _ in range(per_server) for url in urls]


def from_settings(base_url: str | None = None) -> ChatClient:
    provider = setting("LLM_PROVIDER", "ollama")
    timeout = float(setting("LLM_TIMEOUT", "300"))
    think = str(setting("LLM_THINK", "false")).strip().lower() in ("1", "true", "yes")
    num_predict = int(setting("LLM_NUM_PREDICT", "768"))
    base_url = base_url or base_urls()[0]
    if provider == "openai_compat":
        return OpenAICompatClient(base_url=base_url,
                                  model=setting("LLM_MODEL", "Qwen/Qwen3.5-4B"), timeout=timeout,
                                  api_key=setting("LLM_API_KEY", "not-needed"), think=think, num_predict=num_predict)
    threads = str(setting("LLM_NUM_THREAD", "")).strip()
    return OllamaClient(base_url=base_url,
                        model=setting("LLM_MODEL", DEFAULT_MODEL), timeout=timeout,
                        num_ctx=int(setting("LLM_NUM_CTX", "8192")), think=think, num_predict=num_predict,
                        keep_alive=str(setting("LLM_KEEP_ALIVE", "30m")), num_thread=int(threads) if threads else None)
