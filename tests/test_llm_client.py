"""LLM client payloads and parsing (httpx mocked; no server needed)."""
import httpx
import pytest

from sra.llm import client as mod
from sra.llm.client import LLMError, OllamaClient, OpenAICompatClient, _parse_json, from_settings, strip_thinking


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Model calls stream through httpx.stream; never let a test reach a real server."""
    def blocked(*a, **k):
        raise AssertionError("test tried to reach a real model server")
    monkeypatch.setattr(mod.httpx, "stream", blocked)
    monkeypatch.setattr(mod.httpx, "post", blocked)


@pytest.fixture()
def capture(monkeypatch):
    sent = {}

    def fake_stream_call(url, payload, *, timeout, headers=None, cancel_check=None, piece):
        sent["url"], sent["json"] = url, payload
        if url.endswith("/api/chat"):     # Ollama streams one JSON object per line
            lines = ['{"message": {"content": "<think>hmm</think>"}, "done": false}',
                     '{"message": {"content": "{\\"result\\": \\"Met\\"}"}, "done": true}']
        else:                             # OpenAI-compatible servers stream SSE
            lines = ['data: {"choices": [{"delta": {"content": "{\\"result\\": "}}]}',
                     'data: {"choices": [{"delta": {"content": "\\"Gap\\"}"}}]}', "data: [DONE]"]
        return "".join(t for t in (piece(x) for x in lines) if t)

    monkeypatch.setattr(mod, "stream_call", fake_stream_call)
    return sent


def test_ollama_disables_thinking_and_sends_schema(capture):
    c = OllamaClient()
    assert c.model == "qwen3.5:4b"
    assert c.json_chat("sys", "user", {"type": "object"}) == {"result": "Met"}
    p = capture["json"]
    assert p["think"] is False and p["format"] == {"type": "object"} and p["options"]["temperature"] == 0
    assert p["stream"] is True
    c.chat("sys", "q")
    assert capture["json"]["think"] is False and "format" not in capture["json"]


def test_openai_compat_disables_thinking(capture):
    out = OpenAICompatClient().json_chat("s", "u", {"type": "object"})
    assert out == {"result": "Gap"}
    p = capture["json"]
    assert p["chat_template_kwargs"] == {"enable_thinking": False}
    assert p["response_format"]["type"] == "json_schema"


def test_think_setting(monkeypatch):
    monkeypatch.setenv("LLM_THINK", "true")
    monkeypatch.setenv("LLM_MODEL", "gemma4:26b")
    c = from_settings()
    assert c.think is True and c.model == "gemma4:26b"


def test_health_requires_exact_tag(monkeypatch):
    tags = {"models": [{"name": "qwen3.5:9b", "model": "qwen3.5:9b"}, {"name": "gemma4:latest", "model": "gemma4:latest"}]}
    monkeypatch.setattr(mod.httpx, "get", lambda url, timeout=None: Resp(tags))
    assert not OllamaClient(model="qwen3.5:4b").health()       # a different size is not enough
    assert OllamaClient(model="qwen3.5:9b").health()
    assert OllamaClient(model="gemma4").health()                # bare name means :latest


def test_strip_thinking_and_parse():
    assert strip_thinking("<think>a\nb</think>\nanswer") == "answer"
    assert strip_thinking("reasoning without open tag</think>answer") == "answer"
    assert _parse_json('<think>x</think>```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        _parse_json("{not json}")
