"""The optional Claude provider (LMS gateway / Anthropic Messages API) with a stand-in SDK client: no network, no key."""
import json
from types import SimpleNamespace as NS

import pytest

from sra.llm import anthropic_client as ac
from sra.llm.client import LLMError, OllamaClient, clients_from_settings, from_settings
from sra.llm.prompts import SCHEMA
from sra.progress import JobCancelled

ANSWER = {"result": "Met", "rationale": "ok", "cited": ["EV-1#0"], "gaps": [], "recommendation": "", "risk": "Low", "confidence": 0.9}


class FakeStream:
    def __init__(self, message, events=3):
        self.message, self.events = message, events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(range(self.events))

    def get_final_message(self):
        return self.message


def message(text=None, stop="end_turn", **kw):
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=json.dumps(ANSWER) if text is None else text)],
              stop_reason=stop, stop_details=kw.get("details"), model=kw.get("model", "claude-opus-5-5"),
              usage=NS(input_tokens=1200, output_tokens=90, cache_read_input_tokens=300, cache_creation_input_tokens=0))


class FakeSDK:
    def __init__(self, reply=None, error=None):
        self.calls, self.beta_calls = [], []
        outer = self

        def stream(**kw):
            outer.calls.append(kw)
            if error:
                raise error
            return FakeStream(reply or message())

        def beta_stream(**kw):
            outer.beta_calls.append(kw)
            return FakeStream(reply or message())
        self.messages = NS(stream=stream)
        self.beta = NS(messages=NS(stream=beta_stream))


def test_off_by_default_and_selected_by_provider(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert isinstance(from_settings(), OllamaClient)
    for name in ("anthropic", "lms_gateway"):
        monkeypatch.setenv("LLM_PROVIDER", name)
        monkeypatch.setenv("LLM_MODEL", "claude-opus-5-5")
        monkeypatch.setenv("LLM_CONCURRENCY", "3")
        monkeypatch.setenv("LLM_BASE_URL", "http://ollama-0:11434,http://ollama-1:11434")     # ignored for this provider
        pool = clients_from_settings()
        assert len(pool) == 3 and all(isinstance(c, ac.AnthropicClient) for c in pool)


def test_health_needs_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(ac, "load_dotenv", lambda: None)
    assert ac.AnthropicClient().health() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert ac.AnthropicClient().health() is True


def test_schema_is_made_acceptable_for_structured_outputs():
    s = ac.strict_schema(SCHEMA)
    text = json.dumps(s)
    assert not any(k in text for k in ac.UNSUPPORTED) and s["additionalProperties"] is False
    assert s["properties"]["maturity_level"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert s["required"] == SCHEMA["required"] and "maxLength" in json.dumps(SCHEMA)       # the original is untouched


def test_json_call_request_shape_and_usage():
    sdk = FakeSDK()
    c = ac.AnthropicClient(client=sdk)
    assert c.json_chat("SYSTEM", "USER", SCHEMA) == ANSWER
    kw = sdk.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 16000 and kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert kw["output_config"]["effort"] == "medium" and kw["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in kw and "temperature" not in kw and not sdk.beta_calls
    assert c.last_usage["prompt_tokens"] == 1500 and c.last_usage["reply_tokens"] == 90 and c.last_usage["cached_tokens"] == 300
    assert c.last_usage["stopped_at_limit"] is False


def test_gateway_without_output_config_support():
    sdk = FakeSDK(message("Here it is:\n```json\n" + json.dumps(ANSWER) + "\n```"))
    c = ac.AnthropicClient(client=sdk, structured=False, effort="", model="gateway-model")
    assert c.json_chat("SYSTEM", "USER", SCHEMA) == ANSWER
    kw = sdk.calls[0]
    assert "output_config" not in kw and kw["model"] == "gateway-model"
    assert "JSON schema" in kw["system"][0]["text"] and '"result"' in kw["system"][0]["text"]


def test_chat_returns_text():
    assert ac.AnthropicClient(client=FakeSDK(message("  The runbook covers it. [EV-1#0]  "))).chat("S", "U") == "The runbook covers it. [EV-1#0]"


def test_refusal_and_truncation():
    refused = message("", stop="refusal", details=NS(category="cyber", explanation="x"))
    with pytest.raises(LLMError, match="declined.*cyber"):
        ac.AnthropicClient(client=FakeSDK(refused)).json_chat("S", "U", SCHEMA)
    c = ac.AnthropicClient(client=FakeSDK(message('{"result": "Met", "rat', stop="max_tokens")))
    with pytest.raises(LLMError):
        c.json_chat("S", "U", SCHEMA)
    assert c.last_usage["stopped_at_limit"] is True


def test_cancel_stops_the_call():
    c = ac.AnthropicClient(client=FakeSDK(), cancel_check=lambda: True)
    with pytest.raises(JobCancelled):
        c.json_chat("S", "U", SCHEMA)


def test_api_errors_become_clear_llm_errors():
    import anthropic
    import httpx2
    resp = lambda code: httpx2.Response(code, request=httpx2.Request("POST", "https://gateway.example/v1/messages"))  # noqa: E731
    bad = anthropic.BadRequestError("output_config: Extra inputs are not permitted", response=resp(400), body=None)
    with pytest.raises(LLMError, match="LLM_STRUCTURED_OUTPUT=false"):
        ac.AnthropicClient(client=FakeSDK(error=bad)).json_chat("S", "U", SCHEMA)
    auth = anthropic.AuthenticationError("invalid x-api-key", response=resp(401), body=None)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY") as e:
        ac.AnthropicClient(client=FakeSDK(error=auth)).json_chat("S", "U", SCHEMA)
    assert "invalid x-api-key" not in str(e.value)
    down = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://gateway.example/v1/messages"))
    with pytest.raises(LLMError, match="could not reach"):
        ac.AnthropicClient(client=FakeSDK(error=down)).json_chat("S", "U", SCHEMA)


def test_refusal_fallback_is_opt_in():
    sdk = FakeSDK()
    ac.AnthropicClient(client=sdk, refusal_fallback="default").json_chat("S", "U", SCHEMA)
    assert not sdk.calls and sdk.beta_calls[0]["fallbacks"] == "default" and sdk.beta_calls[0]["betas"] == [ac.FALLBACK_BETA]


def test_evaluator_runs_criteria_through_the_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")          # the health check only looks for a configured key
    from sra.catalog.models import Result
    from sra.llm.evaluate import Evaluator
    from test_llm import assessment, bundle, first_chunk_id

    class Echo(FakeSDK):
        def __init__(self):
            super().__init__()
            self.messages = NS(stream=self.stream)

        def stream(self, **kw):
            self.calls.append(kw)
            cited = first_chunk_id(kw["messages"][0]["content"])
            return FakeStream(message(json.dumps({**ANSWER, "cited": [cited]})))
    sdk = Echo()
    ev = Evaluator(ac.AnthropicClient(client=sdk))
    a = assessment()
    ev.evaluate_pending(a, bundle())
    assert a.results["SRE-AT-03-C01"].result == Result.MET and ev.stats["verified"] >= 3 and ev.stats["rejected"] == 0
    assert ev.stats["prompt_tokens"] > 0 and len(sdk.calls) == ev.stats["calls"]
