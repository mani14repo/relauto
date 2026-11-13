"""Claude through the Anthropic Messages API, e.g. by way of the company LMS gateway that COG uses
(hyperion-lms-api -> AWS Bedrock -> Claude; see docs/cog_cap_deployment.svg).

OFF BY DEFAULT. It is only used when LLM_PROVIDER=anthropic (alias: lms_gateway). Assessment evidence (redacted of
secrets, but otherwise as uploaded) is then sent to the hosted model, so it needs the same approval COG has.

Configuration, the same variables COG's image and runtime use:
  ANTHROPIC_BASE_URL   the gateway URL (set in the image / Helm values); unset = api.anthropic.com
  ANTHROPIC_API_KEY    supplied at runtime from the approved secret store; never in the image or the repo
  LLM_MODEL            default claude-opus-5-5; a gateway may publish its own model names
  LLM_EFFORT           low | medium | high | xhigh | max; empty = do not send (for models that reject the field)
  LLM_STRUCTURED_OUTPUT  true (default): the reply is constrained to the JSON schema by the API; false: the schema
                       is only described in the prompt and the reply is parsed (for gateways that reject the field)
  LLM_MAX_TOKENS       default 16000 (includes the model's thinking)
  LLM_REFUSAL_FALLBACK empty (default) | default: on a safety decline, let the API retry on a fallback model.
                       Not available through Amazon Bedrock, so leave it empty for the LMS gateway.
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
FALLBACK_BETA = "server-side-fallback-2026-07-01"
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


@dataclass
class AnthropicClient:
    model: str = DEFAULT_MODEL
    timeout: float = 300
    max_tokens: int = 16000
    effort: str = "medium"
    structured: bool = True
    refusal_fallback: str = ""
    max_retries: int = 2
    cancel_check: Callable[[], bool] | None = None   # set by the evaluator for cancellable runs
    last_usage: dict = field(default_factory=dict)
    client: object | None = None                     # an anthropic.Anthropic; built on first use (tests pass a stand-in)

    @property
    def base_url(self) -> str:
        return os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"

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

    # ------------------------------------------------------------------------------------------------ one call
    def _call(self, system: str, user: str, schema: dict | None) -> str:
        import anthropic
        usage: dict = {}
        self.last_usage = usage
        output_config: dict = {}
        if self.effort:
            output_config["effort"] = self.effort
        if schema is not None and self.structured:
            output_config["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
        elif schema is not None:
            system = (f"{system}\n\nReply with one JSON object only, no other text, matching this JSON schema:\n"
                      f"{json.dumps(schema, separators=(',', ':'))}")
        request = dict(
            model=self.model, max_tokens=self.max_tokens,
            # the system prompt is the same for every criterion: mark it cacheable
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        if output_config:
            request["output_config"] = output_config
        sdk = self._sdk()
        def open_stream():
            if self.refusal_fallback:
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
            raise LLMError(f"the model API rejected the request ({exc.message}). If the gateway does not accept "
                           "output_config, set LLM_STRUCTURED_OUTPUT=false and/or LLM_EFFORT= (empty)") from exc
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
        usage.update(prompt_tokens=(u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                     + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                     reply_tokens=u.output_tokens or 0, reply_s=time.monotonic() - started,
                     cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                     stopped_at_limit=message.stop_reason == "max_tokens", served_by=message.model)
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            raise LLMError("the model declined this request for safety reasons"
                           + (f" (category: {category})" if category else "") + "; the assessor must evaluate it")
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise LLMError(f"the model returned no text (stop reason: {message.stop_reason}); raise LLM_MAX_TOKENS if it "
                           "was cut off")
        return text

    def json_chat(self, system: str, user: str, schema: dict) -> dict:
        return _parse_json(self._call(system, user, schema))

    def chat(self, system: str, user: str) -> str:
        return self._call(system, user, None).strip()


def from_settings() -> AnthropicClient:
    return AnthropicClient(
        model=setting("LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        timeout=float(setting("LLM_TIMEOUT", "300")),
        max_tokens=int(setting("LLM_MAX_TOKENS", "16000")),
        effort=str(setting("LLM_EFFORT", "medium") or "").strip().lower(),
        structured=str(setting("LLM_STRUCTURED_OUTPUT", "true")).strip().lower() not in ("0", "false", "no", "off"),
        refusal_fallback=str(setting("LLM_REFUSAL_FALLBACK", "") or "").strip().lower(),
    )
