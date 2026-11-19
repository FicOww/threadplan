"""Single-request OpenRouter transport; no CLI, retries, or paid fallbacks.

Official OpenRouter documentation for these request fields:
https://openrouter.ai/docs/guides/routing/provider-selection
https://openrouter.ai/docs/guides/features/structured-outputs
https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
https://openrouter.ai/docs/api_reference/overview
https://openrouter.ai/docs/cookbook/administration/usage-accounting
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
from email.utils import parsedate_to_datetime
from contextvars import ContextVar
import threading

from .codec import _schema_errors

# Fixed free candidate. Its published
# endpoint retirement is 2026-12-31; do not treat it as a permanent dependency.
DEFAULT_MODEL = "dots-studio/dots-3-note-preview:free"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_NO_CONTEXT_RECORD = object()


class OpenRouterError(RuntimeError):
    """Explicit failed request, with sanitized structured API diagnostics."""

    def __init__(self, status: str, *, http_status: int | None = None, api_error: Any = None,
                 retry_after_seconds: float | None = None, rejected_output: Any = None):
        self.status = status
        self.http_status = http_status
        self.api_error = api_error
        self.retry_after_seconds = retry_after_seconds
        self.rejected_output = rejected_output
        super().__init__(f"OpenRouter request failed: {status}" +
                         (f" (HTTP {http_status})" if http_status is not None else ""))


TransportError = OpenRouterError


def _retry_after(value):
    if not isinstance(value, str):
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            due = parsedate_to_datetime(value)
            if due.utcoffset() is None:
                return None
            delay = (due - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) and delay <= 86400 else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward bearer credentials to a redirected origin.
        return None


def _open_request(request: Request, timeout: float):
    """Real HTTP by default. Tests patch this boundary with explicit fake HTTP."""
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _loads(value: str) -> Any:
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = item
        return result

    def reject(value):
        raise ValueError("non_json_constant")

    return json.loads(value, object_pairs_hook=unique, parse_constant=reject)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact(value: Any, key: str | None) -> Any:
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]") if key else value
    if isinstance(value, dict):
        return {_redact(name, key): _redact(item, key) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def wire_schema(value: Any) -> Any:
    """Use a portable grammar; domain validation still enforces uniqueness.

    The initial complex request was rejected upstream while a minimal schema
    succeeded. String/array refinements are enforced locally, not entrusted to
    inconsistent native wire grammars. Never mutate the domain schema.
    """
    if isinstance(value, dict):
        return {key: wire_schema(item) for key, item in value.items()
                if key not in {"uniqueItems", "minLength", "minItems", "pattern"}}
    if isinstance(value, list):
        return [wire_schema(item) for item in value]
    return value


def _validate_free_policy(model, allow_paid, prices, max_price) -> None:
    """Fail before credentials/HTTP; no runtime configuration can opt into paid."""
    if allow_paid is not False:
        raise ValueError("free-only transport: allow_paid must be False")
    if (type(model) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*:free", model) is None
            or model.split("/", 1)[0].lower() == "openrouter"):
        raise ValueError("free-only transport requires an explicit organization/model:free slug; routers are forbidden")
    if type(max_price) is not dict or set(max_price) != {"prompt", "completion", "request"}:
        raise ValueError("free-only transport requires fixed zero price caps")
    for price in (*prices, *max_price.values()):
        if type(price) not in (int, float) or not math.isfinite(price) or price != 0:
            raise ValueError("free-only transport: all price caps must be zero")


class OpenRouterTransport:
    """Provider-neutral async callable backed by one bounded HTTPS POST.

    Construction never reads credentials. A call uses api_key, or reads only
    OPENROUTER_API_KEY if api_key was not supplied. Price caps are USD per
    million tokens. All caps, including per-request charges, are always zero.
    allow_paid is retained only for caller compatibility and accepts only False.
    An explicit :free model is mandatory; routers and paid fallbacks are banned.
    """

    def __init__(self, *, runs_dir: Path, model: str = DEFAULT_MODEL,
                 api_key: str | None = None, allow_paid: bool = False,
                 max_tokens: int = 1024, timeout_seconds: float = 60,
                 max_prompt_price: float = 0, max_completion_price: float = 0,
                 max_request_price: float = 0, output_mode: str = "json_object"):
        zero_caps = {"prompt": 0, "completion": 0, "request": 0}
        _validate_free_policy(model, allow_paid,
                              (max_prompt_price, max_completion_price, max_request_price), zero_caps)
        if type(max_tokens) is not int or not 1 <= max_tokens <= 1024:
            raise ValueError("max_tokens must be an integer from 1 to 1024")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if api_key is not None and (type(api_key) is not str or not api_key or any(c.isspace() for c in api_key)):
            raise ValueError("api_key must be a nonempty token without whitespace")
        self.model = model
        self.runs_dir = Path(runs_dir)
        self._api_key = api_key
        self.allow_paid = allow_paid
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.max_prompt_price = max_prompt_price
        self.max_completion_price = max_completion_price
        self.max_request_price = max_request_price
        self.max_price = zero_caps
        self._record_context = ContextVar("openrouter_record_" + uuid.uuid4().hex, default=_NO_CONTEXT_RECORD)
        self._thread_records = threading.local()
        self._latest_record = None
        if output_mode not in {"json_object", "json_schema"}:
            raise ValueError("Unsupported output_mode")
        self.output_mode = output_mode

    def _assert_free_only(self) -> None:
        _validate_free_policy(self.model, self.allow_paid,
                              (self.max_prompt_price, self.max_completion_price, self.max_request_price),
                              self.max_price)

    async def __call__(self, *, system_prompt: str, input_json: str, response_schema: dict) -> dict:
        self._record_context.set(None)
        def observed():
            self._thread_records.value = None
            try:
                value = self._run(system_prompt, input_json, response_schema)
                return value, None, self._thread_records.value
            except Exception as error:
                return None, error, self._thread_records.value
        value, error, record = await asyncio.to_thread(observed)
        self._record_context.set(record)
        if error is not None:
            raise error
        return value

    @property
    def last_record(self):
        record = self._record_context.get()
        return self._latest_record if record is _NO_CONTEXT_RECORD else record

    @last_record.setter
    def last_record(self, value):
        # Retain a latest diagnostic for synchronous callers; async attribution
        # uses its own task context, never another thread's most recent result.
        self._latest_record = value

    def _run(self, system_prompt: str, input_json: str, response_schema: dict) -> dict:
        self.last_record = None
        self._assert_free_only()
        if type(system_prompt) is not str or type(input_json) is not str or type(response_schema) is not dict:
            raise ValueError("system_prompt/input_json must be strings and response_schema an object")
        schema_text = _json(response_schema)
        provider_schema = wire_schema(response_schema)
        # Some free endpoints declare structured outputs but reject complex
        # native schemas. JSON-object mode supplies the FULL domain schema in
        # the prompt and enforces it locally; no output repair or fee fallback.
        effective_prompt = (system_prompt + "\nReturn JSON matching this complete domain schema:\n" + schema_text
                            if self.output_mode == "json_object" else system_prompt)
        response_format = ({"type": "json_object"} if self.output_mode == "json_object" else {
            "type": "json_schema", "json_schema": {"name": "threadplan_response", "strict": True, "schema": provider_schema}})
        payload = {
            "model": self.model, "messages": [
                {"role": "system", "content": effective_prompt},
                {"role": "user", "content": input_json},
            ],
            "response_format": response_format,
            "reasoning": {"enabled": False}, "max_tokens": self.max_tokens,
            "stream": False,
            "provider": {"require_parameters": True, "data_collection": "deny", "allow_fallbacks": False,
                         "max_price": {"prompt": 0, "completion": 0, "request": 0}},
        }
        payload_bytes = _json(payload).encode("utf-8")
        run_dir = self.runs_dir / (datetime.now(timezone.utc).strftime("openrouter-%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8])
        run_dir.mkdir(parents=True, exist_ok=False)
        started = time.perf_counter()
        record: dict[str, Any] = {
            "transport": "openrouter", "endpoint": ENDPOINT,
            "requested_model": self.model, "resolved_model": None, "actual_model": None,
            "provider": None, "requested_reasoning_enabled": False,
            "max_tokens": self.max_tokens, "allow_paid": False, "free_only": True,
            "cost_guard": "upstream zero price caps prevent paid routing; returned cost is a post-request check only",
            "provider_preferences": payload["provider"], "price_cap_unit": "USD per million tokens",
            "prompt_sha256": _hash(effective_prompt), "input_sha256": _hash(input_json),
            "schema_sha256": _hash(_json(provider_schema)), "domain_schema_sha256": _hash(schema_text),
            "wire_schema_omissions_enforced_locally": ["uniqueItems", "minLength", "minItems", "pattern"],
            "output_mode": self.output_mode,
            "domain_schema_enforced_locally": True,
            "provider_native_domain_schema_requested": self.output_mode == "json_schema",
            "reported_usage": [], "usage": None, "cost": None, "cost_unit": "OpenRouter credits",
            "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            "run_directory": str(run_dir), "retries": 0, "http_attempts": 0,
            "http_status": None, "finish_reason": None, "status": "pending",
            "latency_scope": "whole HTTPS request including HTTP and response validation; not per-case latency",
        }
        key = None
        response_body = None
        try:
            key = self._api_key if self._api_key is not None else os.environ.get("OPENROUTER_API_KEY")
            if type(key) is not str or not key or any(c.isspace() for c in key):
                raise OpenRouterError("missing_or_invalid_credentials")
            request = Request(ENDPOINT, data=payload_bytes, method="POST", headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json",
            })
            record["http_attempts"] = 1
            try:
                # Recheck after request construction too. The serialized body
                # uses fixed caps and no tools, plugins or alternate models.
                self._assert_free_only()
                with _open_request(request, timeout=self.timeout_seconds) as response:
                    record["http_status"] = response.status
                    body = response.read(MAX_RESPONSE_BYTES + 1)
            except HTTPError as error:
                record["http_status"] = error.code
                try:
                    error_body = error.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")
                    response_body = _loads(error_body)
                except (OSError, ValueError, RecursionError):
                    response_body = {"unparsed_error_body": "HTTP error body was not valid bounded JSON"}
                delay = _retry_after(error.headers.get("Retry-After")) if error.headers else None
                record["retry_after_seconds"] = delay
                raise OpenRouterError("http_error", http_status=error.code,
                                      api_error=_redact(response_body, key), retry_after_seconds=delay) from None
            if len(body) > MAX_RESPONSE_BYTES:
                raise OpenRouterError("response_too_large")
            try:
                response_body = _loads(body.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                raise OpenRouterError("invalid_response_json") from None
            if type(response_body) is not dict:
                raise OpenRouterError("invalid_response_envelope")
            record["resolved_model"] = record["actual_model"] = response_body.get("model")
            record["provider"] = response_body.get("provider")
            record["response_id"] = response_body.get("id")
            usage = response_body.get("usage")
            if usage is not None and type(usage) is not dict:
                raise OpenRouterError("invalid_usage")
            record["usage"] = usage
            if usage is not None:
                record["reported_usage"] = [usage]
                for field in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
                    record[field] = usage.get(field)
            if "error" in response_body:
                raise OpenRouterError("api_error", api_error=_redact(response_body["error"], key))
            if record["http_status"] != 200:
                raise OpenRouterError("http_error", http_status=record["http_status"])
            if record["cost"] is not None and type(record["cost"]) not in (int, float):
                raise OpenRouterError("invalid_cost")
            # This detects a violation after the response; it cannot undo a
            # charge. The mandatory zero routing caps are the preventive guard.
            if record["cost"] is not None and record["cost"] > 0:
                raise OpenRouterError("unexpected_nonzero_cost")
            choices = response_body.get("choices")
            if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
                raise OpenRouterError("invalid_choices")
            choice = choices[0]
            record["finish_reason"] = choice.get("finish_reason")
            if "error" in choice:
                raise OpenRouterError("api_error", api_error=_redact(choice["error"], key))
            if choice.get("finish_reason") != "stop":
                raise OpenRouterError("incomplete_or_failed_finish")
            message = choice.get("message")
            if type(message) is not dict:
                raise OpenRouterError("invalid_message")
            if message.get("refusal"):
                raise OpenRouterError("refusal")
            if message.get("tool_calls") or message.get("function_call"):
                raise OpenRouterError("unexpected_tool_call")
            content = message.get("content")
            if type(content) is not str or not content.strip():
                raise OpenRouterError("missing_json_content")
            try:
                result = _loads(content)
            except (ValueError, RecursionError):
                raise OpenRouterError("invalid_content_json", rejected_output=_redact(content, key)) from None
            schema_errors = _schema_errors(result, response_schema, "response")
            if schema_errors:
                raise OpenRouterError("response_schema_error", api_error={"schema_errors": schema_errors},
                                      rejected_output=_redact(result, key))
            record["status"] = "completed"
            return result
        except OpenRouterError as error:
            record.update(status=error.status, error=error.api_error)
            raise
        except (TimeoutError, socket.timeout):
            record["status"] = "timeout"
            raise OpenRouterError("timeout") from None
        except URLError as error:
            status = "timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "network_error"
            record["status"] = status
            raise OpenRouterError(status) from None
        except OSError:
            record["status"] = "network_error"
            raise OpenRouterError("network_error") from None
        finally:
            record["elapsed_seconds"] = round(time.perf_counter() - started, 6)
            safe_record = _redact(record, key)
            if response_body is not None:
                (run_dir / "response.json").write_text(_json(_redact(response_body, key)), encoding="utf-8")
            (run_dir / "record.json").write_text(json.dumps(safe_record, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
            self.last_record = safe_record
            self._thread_records.value = safe_record
