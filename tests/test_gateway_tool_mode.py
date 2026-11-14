"""Tool mode for the LMS gateway (OIP GATEWAY_COMPATIBILITY.md): forced submit_answer call, no output_config.format,
no strict, validation and one corrective retry. Stand-in SDK: no network, no key."""
import json
from types import SimpleNamespace as NS

import pytest

from sra.llm import anthropic_client as ac
from sra.llm.client import LLMError
from sra.llm.prompts import SCHEMA
from test_anthropic_client import ANSWER, FakeStream

GATEWAY = "https://hyperion-lms-api.prod.cvshealth.com"


def tool_message(answer, *, call=True, tid="toolu_1"):
    content = [NS(type="tool_use", id=tid, name=ac.TOOL_NAME, input=answer)] if call else [NS(type="text", text="Sure.")]
    return NS(content=content, stop_reason="tool_use" if call else "end_turn", stop_details=None, model="claude-opus-5",
              usage=NS(input_tokens=1000, output_tokens=80, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class Scripted:
    """Returns the given messages in order and records each request."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
        self.messages = NS(stream=self.stream)
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        return FakeStream(self.replies.pop(0))


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY)


def test_auto_mode_follows_the_base_url(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert ac.AnthropicClient().mode == "native" and ac.AnthropicClient().effort_sent == "medium"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY + "/")
    c = ac.AnthropicClient()
    assert c.mode == "tool" and c.effort_sent == ""          # OIP sends no output_config through the gateway
    assert ac.AnthropicClient(effort="low").effort_sent == "low"
    assert ac.AnthropicClient(structured=False).mode == "prompt"
    assert ac.AnthropicClient(output_mode="native").mode == "native"


def test_tool_request_shape(gateway):
    sdk = Scripted(tool_message(ANSWER))
    c = ac.AnthropicClient(client=sdk, model="claude-opus-5")
    assert c.json_chat("SYSTEM", "USER", SCHEMA) == ANSWER
    kw = sdk.calls[0]
    assert kw["tool_choice"] == {"type": "tool", "name": "submit_answer"}
    assert kw["tools"][0]["name"] == "submit_answer" and kw["tools"][0]["input_schema"]["required"] == SCHEMA["required"]
    assert "strict" not in kw["tools"][0] and "output_config" not in kw and "betas" not in kw
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert c.last_usage["answer_format"] == "tool" and c.last_usage["malformed"] == 0 and len(sdk.calls) == 1


def test_malformed_answer_gets_one_corrective_retry(gateway):
    flattened = {**ANSWER, "gaps": False, "risk": "Severe"}
    del flattened["confidence"]
    sdk = Scripted(tool_message(flattened, tid="toolu_bad"), tool_message(ANSWER, tid="toolu_ok"))
    c = ac.AnthropicClient(client=sdk)
    assert c.json_chat("S", "U", SCHEMA) == ANSWER
    retry = sdk.calls[1]["messages"]
    assert retry[0] == {"role": "user", "content": "U"}
    assert retry[1]["role"] == "assistant" and retry[1]["content"][0]["id"] == "toolu_bad"
    result = retry[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_bad" and result["is_error"] is True
    assert "answer.confidence is required" in result["content"] and "answer.gaps must be array" in result["content"]
    assert "answer.risk must be one of" in result["content"]
    assert c.last_usage["malformed"] == 1 and c.last_usage["recovered"] == 1 and c.last_usage["prompt_tokens"] == 2000


def test_no_tool_call_is_sent_again(gateway):
    sdk = Scripted(tool_message(None, call=False), tool_message(ANSWER))
    assert ac.AnthropicClient(client=sdk).json_chat("S", "U", SCHEMA) == ANSWER
    assert sdk.calls[1]["messages"] == sdk.calls[0]["messages"]


def test_malformed_twice_fails_only_that_criterion(gateway):
    sdk = Scripted(tool_message({"result": "Met"}), tool_message({"result": "Met"}))
    c = ac.AnthropicClient(client=sdk)
    with pytest.raises(LLMError, match="did not match the required format twice"):
        c.json_chat("S", "U", SCHEMA)
    assert c.last_usage["malformed"] == 1 and c.last_usage["recovered"] == 0


def test_chat_in_tool_mode_sends_no_tools(gateway):
    sdk = Scripted(NS(content=[NS(type="text", text=" See [EV-1#0]. ")], stop_reason="end_turn", stop_details=None,
                      model="m", usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0)))
    assert ac.AnthropicClient(client=sdk).chat("S", "U") == "See [EV-1#0]."
    assert "tools" not in sdk.calls[0] and "output_config" not in sdk.calls[0]


def test_problems_accepts_well_formed_answers():
    assert ac.problems(SCHEMA, ANSWER) == []
    assert ac.problems(SCHEMA, {**ANSWER, "maturity_level": None, "measured_value": "3/4"}) == []
    assert ac.problems(SCHEMA, {**ANSWER, "rationale": "x" * 2000}) == []       # length limits are not a retry reason
    assert ac.problems(SCHEMA, {**ANSWER, "confidence": True}) == ["answer.confidence must be number, not bool"]
    assert ac.problems(SCHEMA, {**ANSWER, "cited": ["ok", 3]}) == ["answer.cited[1] must be string, not int"]


def test_settings(monkeypatch):
    monkeypatch.setattr(ac, "setting", lambda name, default=None: {"LLM_OUTPUT_MODE": "TOOL", "LLM_EFFORT": ""}.get(name, default))
    c = ac.from_settings()
    assert c.output_mode == "tool" and c.effort == "" and c.mode == "tool"
    monkeypatch.setattr(ac, "setting", lambda name, default=None: {"LLM_OUTPUT_MODE": "json"}.get(name, default))
    with pytest.raises(ValueError, match="LLM_OUTPUT_MODE"):
        ac.from_settings()
