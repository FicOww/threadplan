"""Provider-neutral model failure categories; no retry loop or fallback."""

import math
from threadplan.codec import ModelCallError

def _failure(error):
    terminal = isinstance(error, ModelCallError)
    cause = error.__cause__ if terminal and error.__cause__ else error
    status, http = getattr(cause, "status", None), getattr(cause, "http_status", None)
    status = status if type(status) is str else None
    details = getattr(cause, "api_error", None)
    if http in (None, 200) and isinstance(details, dict):
        nested = details.get("error", details)
        http = nested.get("code") if isinstance(nested, dict) else http
    http = http if type(http) is int else None
    if isinstance(error, TimeoutError) and not terminal:
        return {"code": "model_wait_timeout", "retryable": False}
    if terminal and (isinstance(cause, TimeoutError) or status in {"timeout", "network_error"}
                     or http in {429, 500, 502, 503, 504}):
        delay = getattr(cause, "retry_after_seconds", None)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 86400:
            delay = None
        return {"code": "model_temporarily_unavailable", "retryable": True,
                "retry_after_seconds": delay, "exception_type": type(cause).__name__}
    correctable = status in {"response_schema_error", "invalid_content_json"} and getattr(cause, "rejected_output", None) is not None
    schema_errors = details.get("schema_errors") if isinstance(details, dict) else None
    validation_errors = ([item[:300] for item in schema_errors[:32] if isinstance(item, str)]
                         if isinstance(schema_errors, list) else [])
    return {"code": "invalid_model_output" if correctable or isinstance(error, ValueError) else "model_call_failed",
            "retryable": bool(correctable), "correction_available": bool(correctable),
            "validation_errors": validation_errors or ["invalid_model_output"],
            "exception_type": type(cause).__name__}
