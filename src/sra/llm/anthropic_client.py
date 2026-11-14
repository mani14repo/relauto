"""Claude through the Anthropic Messages API, normally by way of the CVS LMS gateway that OIP uses
(hyperion-lms-api -> AWS Bedrock -> Claude; OIP docs: GATEWAY_COMPATIBILITY.md, CAP_DEPLOYMENT_ARCHITECTURE.md).

Selected with LLM_PROVIDER=anthropic (alias: lms_gateway); on CAP this is the default in the Helm chart. Assessment
evidence (redacted of secrets, but otherwise as uploaded) is then sent to the hosted model, under the same approval as
OIP's AI checks.

How a structured answer is asked for (LLM_OUTPUT_MODE), as OIP does it:
  auto (default)  native for https://api.anthropic.com (or no base URL); tool for any other address (the gateway)
  native          output_config.format: structured outputs, which guarantee the schema. The gateway rejects this
                  field (HTTP 400 "output_config.format: Extra inputs are not permitted", confirmed by OIP 2026-09-30).
  tool            one forced tool call, submit_answer, whose input schema is the answer format. Not strict (the gateway
                  rejects strict: true). Every answer is validated; a malformed one gets one corrective retry that sends
                  Claude's tool call back with an error tool_result listing what was wrong (confirmed to work through
                  the gateway: OIP recovered 7 of 7). A reply with no tool call is sent again once.
  prompt          the schema is described in the prompt and the reply text parsed (the old LLM_STRUCTURED_OUTPUT=false).

Configuration, the same variables OIP's image and runtime use:
  ANTHROPIC_BASE_URL   the gateway URL (Helm values); unset = api.anthropic.com
  ANTHROPIC_API_KEY    supplied at runtime from the approved secret store; never in the image or the repo
  LLM_MODEL            default claude-opus-5-5; the gateway publishes its own names (OIP confirmed claude-opus-5)
  LLM_EFFORT           low | medium | high | xhigh | max; empty = not sent. Unset: medium in native mode, not sent
                       otherwise (OIP sends no output_config through the gateway)
  LLM_MAX_TOKENS       default 16000
  LLM_REFUSAL_FALLBACK empty (default) | default: native mode only; not available through Amazon Bedrock.
"""
from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import load_dotenv, setting
from ..progress import JobCancelled
from .client import LLMError, _parse_json

DEFAULT_MODEL = "claude-opus-5-5"
PUBLIC_API = "https://api.anthropic.com"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TOOL_NAME = "submit_answer"
MODES = ("auto", "native", "tool", "prompt")
# JSON-schema keywords the structured-output feature does not accept; the reply is checked by llm/verify.py anyway
UNSUPPORTED = ("minLength", "maxLength", "minimum", "maximum", "multipleOf", "minItems", "maxItems")


def strict_schema(schema: dict) -> dict:
    """The schema in the form structured outputs accept: no length/number/array-size limits, every object closed
    (additionalProperties: false), and a list of types written as anyOf."""
    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items() if k not in UNSUPPORTED}
        if isinstance(out.get("type"), list):
            types = out.pop("type")
            out = {"anyOf": [{**out, "type": t} if t != "null" else {"type": "null"} for t in types]}
        if out.get("type") == "object":
            out["additionalProperties"] = False
        return out
    return walk(copy.deepcopy(schema))


_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def problems(schema: dict, value, path: str = "answer") -> list[str]:
    """What is wrong with `value` against the answer schema: types, required properties, enums, nested items.

    Length and size limits are not checked: they only keep replies short, and a long but well-formed answer is
    better kept than retried. llm/verify.py checks the citations afterwards.
    """
    out: list[str] = []
    if "anyOf" in schema:
        if all(problems(s, value, path) for s in schema["anyOf"]):
            out.append(f"{path} matches none of the allowed forms")
        return out
    types = schema.get("type")
    types = types if isinstance(types, list) else [types] if types else []

    def is_type(t: str) -> bool:
        if t == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if t == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, _TYPES.get(t, object))
    if types and not any(is_type(t) for t in types):
        return [f"{path} must be {' or '.join(types)}, not {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path} must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        out += [f"{path}.{k} is required" for k in schema.get("required", []) if k not in value]
        for k, v in value.items():
            if k in props:
                out += problems(props[k], v, f"{path}.{k}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            out += problems(schema["items"], v, f"{path}[{i}]")
    return out


@dataclass
class AnthropicClient:
    model: str = DEFAULT_MODEL
    timeout: float = 300
    max_tokens: int = 16000
    effort: str | None = None          # None: medium in native mode, not sent otherwise; "" never sent
    structured: bool = True            # False = prompt mode (kept for LLM_STRUCTURED_OUTPUT=false)
    output_mode: str = "auto"          # auto | native | tool | prompt
    refusal_fallback: str = ""
    max_retries: int = 2
    cancel_check: Callable[[], bool] | None = None   # set by the evaluator for cancellable runs
    last_usage: dict = field(default_factory=dict)
    client: object | None = None                     # an anthropic.Anthropic; built on first use (tests pass a stand-in)

    @property
    def base_url(self) -> str:
        return (os.environ.get("ANTHROPIC_BASE_URL") or PUBLIC_API).rstrip("/")

    @property
    def mode(self) -> str:
        """The output mode in use: auto resolves to native for the public API and tool for anything else."""
        if not self.structured:
            return "prompt"
        if self.output_mode in ("native", "tool", "prompt"):
            return self.output_mode
        return "native" if self.base_url == PUBLIC_API else "tool"

    @property
    def effort_sent(self) -> str:
        if self.effort is None:
            return "medium" if self.mode == "native" else ""
        return self.effort

    def _sdk(self):
        if self.client is None:
            import anthropic
            load_dotenv()                 # .env values (local development) become visible to the SDK
            # Zero-argument client: the SDK reads ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL itself.
            self.client = anthropic.Anthropic(timeout=self.timeout, max_retries=self.max_retries)
        return self.client

    def health(self) -> bool:
        """True when credentials are configured. A wrong key or model shows up as a clear error on the first call."""
        load_dotenv()
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    # ------------------------------------------------------------------------------------------------ one request
    def _send(self, request: dict, usage: dict, fallback: bool):
        """Stream one request and return the final message; usage is added to `usage`."""
        import anthropic
        sdk = self._sdk()

        def open_stream():
            if fallback:
                return sdk.beta.messages.stream(**request, betas=[FALLBACK_BETA], fallbacks=self.refusal_fallback)
            return sdk.messages.stream(**request)
        started = time.monotonic()
        try:
            # Streaming keeps long requests clear of HTTP timeouts and lets a cancel stop the call between events.
            with open_stream() as stream:
                for _ in stream:
                    if self.cancel_check and self.cancel_check():
                        raise JobCancelled("cancelled during a model call")
                message = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            hint = ("set LLM_OUTPUT_MODE=tool and LLM_EFFORT= (empty) for the LMS gateway" if self.mode == "native"
                    else "the endpoint rejected a request field; check LLM_EFFORT= (empty) and LLM_MODEL")
            raise LLMError(f"the model API rejected the request ({exc.message}); {hint}. "
                           "(LLM_STRUCTURED_OUTPUT=false is the older switch for the same thing)") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("the model API rejected the credentials (check ANTHROPIC_API_KEY)") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"the key is not allowed to use this model or endpoint ({exc.message})") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"model '{self.model}' or the endpoint was not found at {self.base_url} (check LLM_MODEL "
                           "and ANTHROPIC_BASE_URL)") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited by the model API after {self.max_retries} retries") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"model API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:      # includes timeouts
            raise LLMError(f"could not reach the model API at {self.base_url} ({type(exc).__name__})") from exc

        u = message.usage
        add = {"prompt_tokens": (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                                + (getattr(u, "cache_creation_input_tokens", 0) or 0),
               "reply_tokens": u.output_tokens or 0, "reply_s": time.monotonic() - started,
               "cached_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        for k, v in add.items():
            usage[k] = usage.get(k, 0) + v
        usage.update(stopped_at_limit=message.stop_reason == "max_tokens", served_by=message.model,
                     answer_format=self.mode)
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            raise LLMError("the model declined this request for safety reasons"
                           + (f" (category: {category})" if category else "") + "; the assessor must evaluate it")
        return message

    def _base_request(self, system: str, user: str) -> dict:
        return dict(
            model=self.model, max_tokens=self.max_tokens,
            # the system prompt is the same for every criterion: mark it cacheable (accepted by the gateway)
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )

    @staticmethod
    def _text(message) -> str:
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise LLMError(f"the model returned no text (stop reason: {message.stop_reason}); raise LLM_MAX_TOKENS if it "
                           "was cut off")
        return text

    def _call(self, system: str, user: str, schema: dict | None) -> str:
        """Native or prompt mode, or plain chat: one request, text back."""
        usage: dict = {}
        self.last_usage = usage
        mode = self.mode
        if schema is not None and mode == "prompt":
            system = (f"{system}\n\nReply with one JSON object only, no other text, matching this JSON schema:\n"
                      f"{json.dumps(schema, separators=(',', ':'))}")
        request = self._base_request(system, user)
        output_config: dict = {}
        if self.effort_sent:
            output_config["effort"] = self.effort_sent
        if schema is not None and mode == "native":
            output_config["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
        if output_config:
            request["output_config"] = output_config
        message = self._send(request, usage, fallback=bool(self.refusal_fallback) and mode == "native")
        return self._text(message)

    # ------------------------------------------------------------------------------------------------ tool mode
    def _tool_answer(self, system: str, user: str, schema: dict) -> dict:
        usage: dict = {"malformed": 0, "recovered": 0}
        self.last_usage = usage
        tool = {"name": TOOL_NAME, "description": "Submit the evaluation. Call this exactly once with the full answer.",
                "input_schema": schema}
        request = self._base_request(system, user)
        request.update(tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME})
        if self.effort_sent:
            request["output_config"] = {"effort": self.effort_sent}
        message = self._send(request, usage, fallback=False)
        call = next((b for b in message.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        found = problems(schema, call.input) if call is not None else ["no submit_answer call"]
        if not found:
            return dict(call.input)
        usage["malformed"] = 1
        if call is None:
            # nothing to correct: send the same request again
            retry = request
        else:
            # send Claude's tool call back with an error result naming what was wrong, and ask for the corrected answer
            retry = {**request, "messages": request["messages"] + [
                {"role": "assistant", "content": [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                              "content": "The answer does not match the required format: "
                                                         + "; ".join(found[:12])
                                                         + ". Call submit_answer again with the complete, corrected answer."}]},
            ]}
        message = self._send(retry, usage, fallback=False)
        call = next((b for b in message.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        again = problems(schema, call.input) if call is not None else ["no submit_answer call"]
        if again:
            raise LLMError("the model's answer did not match the required format twice ("
                           + "; ".join(again[:5]) + "); the assessor must evaluate it")
        usage["recovered"] = 1
        return dict(call.input)

    def json_chat(self, system: str, user: str, schema: dict) -> dict:
        if self.mode == "tool":
            return self._tool_answer(system, user, schema)
        return _parse_json(self._call(system, user, schema))

    def chat(self, system: str, user: str) -> str:
        return self._call(system, user, None).strip()


def from_settings() -> AnthropicClient:
    mode = str(setting("LLM_OUTPUT_MODE", "auto") or "auto").strip().lower()
    if mode not in MODES:
        raise ValueError(f"LLM_OUTPUT_MODE must be one of {', '.join(MODES)}, not {mode!r}")
    effort = setting("LLM_EFFORT")          # unset = mode default; set but empty = never sent
    return AnthropicClient(
        model=setting("LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        timeout=float(setting("LLM_TIMEOUT", "300")),
        max_tokens=int(setting("LLM_MAX_TOKENS", "16000")),
        effort=None if effort is None else effort.strip().lower(),
        structured=str(setting("LLM_STRUCTURED_OUTPUT", "true")).strip().lower() not in ("0", "false", "no", "off"),
        output_mode=mode,
        refusal_fallback=str(setting("LLM_REFUSAL_FALLBACK", "") or "").strip().lower(),
    )
