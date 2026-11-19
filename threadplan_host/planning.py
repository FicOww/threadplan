"""The single autonomous model/read/decision loop; execution stays host-authorized."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import time

from threadplan.codec import canonical_json, _aware_time, ModelCallError
from threadplan.planning import AGENT_PROMPT
from threadplan.protocol import decode, model_schema, check_result
from .agent import AgentSession
from .limits import PlanningLimits
from .model_errors import _failure


class RetryLater(RuntimeError):
    def __init__(self, not_before):
        self.not_before = not_before
        super().__init__("model_retry_wait")


async def recorded_model_call(llm, captured, **kwargs):
    """Capture task-local transport diagnostics inside the actual model task."""
    try:
        value = await llm(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        captured["raw"] = getattr(error, "rejected_output", None)
        captured["metadata"] = deepcopy(getattr(llm, "last_record", None))
        raise
    captured["raw"] = value
    captured["metadata"] = deepcopy(getattr(llm, "last_record", None))
    return value


class ConversationPlanner:
    def __init__(self, store, *, context, calendar_reader, history_reader=None, limits=None):
        self.store, self.context = store, context
        self.limits = limits or PlanningLimits()
        self.session = AgentSession(store, context=context, calendar_reader=calendar_reader,
                                    history_reader=history_reader, limits=self.limits)

    def _remaining(self, row):
        return (_aware_time(row["deadline_at"]) - self.store.now()).total_seconds()

    async def _model(self, row, token, llm, payload, *, allow_recovery=True):
        if len(canonical_json(payload).encode("utf-8")) > self.limits.max_input_bytes:
            raise ValueError("model_input_too_large")
        prompt = AGENT_PROMPT
        timeout = min(self._remaining(row), self.limits.model_timeout_seconds)
        if timeout <= 0:
            raise ValueError("request_deadline_exceeded")
        number = self.store.reserve(row["id"], token, "MODEL", {"stage": "agent", "payload": payload,
            "system_prompt": prompt, "response_schema": model_schema()}, self.limits.max_model_calls)
        raw, metadata, captured = None, None, {}
        started = time.perf_counter()
        def measured(record):
            return dict(record or {}, elapsed_seconds=time.perf_counter() - started,
                        input_bytes=len(canonical_json(payload).encode("utf-8")),
                        prompt_bytes=len(prompt.encode("utf-8")))
        try:
            raw = await asyncio.wait_for(recorded_model_call(llm, captured, system_prompt=prompt, input_json=canonical_json(payload),
                                              response_schema=model_schema()), timeout)
            metadata = captured.get("metadata")
            result = decode(raw)
        except BaseException as error:
            # An outer wait timeout may leave a worker running. No automatic
            # resubmission, guessed usage or provider fallback is attempted.
            if not isinstance(error, (asyncio.CancelledError, TimeoutError)):
                raw = raw if raw is not None else captured.get("raw")
                metadata = captured.get("metadata")
            self.store.trace_result(row["id"], "MODEL", number, output=raw,
                error={"type": type(error).__name__, "status": getattr(error, "status", None)}, metadata=measured(metadata))
            if allow_recovery and isinstance(error, Exception) and not isinstance(error, TimeoutError):
                wrapped = ModelCallError("planning_model_error")
                wrapped.__cause__ = error
                failure = _failure(wrapped)
                if failure.get("correction_available") or (isinstance(error, ValueError) and raw is not None):
                    return await self._model(row, token, llm, dict(payload, correction={
                        "previous_response": raw, "errors": failure.get("validation_errors", ["invalid_json"])}), allow_recovery=False)
                if failure.get("retryable"):
                    delay = failure.get("retry_after_seconds")
                    delay = 1 if delay is None else delay
                    if delay >= self._remaining(row):
                        raise ValueError("retry_exceeds_deadline") from error
                    if delay > 2:
                        raise RetryLater((self.store.now()+timedelta(seconds=delay)).isoformat()) from error
                    if delay:
                        await asyncio.sleep(delay)
                    return await self._model(row, token, llm, payload, allow_recovery=False)
            raise
        self.store.trace_result(row["id"], "MODEL", number, output=raw, metadata=measured(metadata))
        self.store.current(row["id"], token=token)
        return result

    @staticmethod
    def _stop_reads(candidate, reason, *, technical=False):
        result = deepcopy(candidate)
        for item in result["items"]:
            if type(item) is dict and item.get("decision") == "NEED_MORE":
                if not technical and item.get("clarification_question") and item.get("missing_facts"):
                    item["decision"] = "WAIT"
                    item["reason"] += "；" + reason
                    item.pop("next_read", None)
                else:
                    # Technical stopping is never mislabeled as a guessed business answer.
                    item["decision"] = "STOPPED"
                    item["reason"] = reason
        return result

    async def analyze(self, request, llm, *, retry=False):
        opened = self.session.open(request, retry=retry)
        row = self.store.get(opened["request_key"])
        if not opened["claimed"]:
            return dict(row, reused=True)
        token, candidate, corrected = row["token"], None, False
        feedback = None
        try:
            while True:
                payload = self.session.model_input(row["id"])
                if feedback:
                    payload["correction"] = feedback
                response = await self._model(row, token, llm, payload)
                errors = check_result(response, self.store.get(row["id"])["work"]["messages"])
                has_envelope = type(response) is dict and type(response.get("items")) is list and bool(response["items"])
                if has_envelope:
                    candidate = response
                # One bounded format correction, preserving the original raw trace.
                if errors and not corrected:
                    feedback, corrected = {"errors": errors, "previous_response": response}, True
                    continue
                if not has_envelope:
                    raise ValueError("invalid_decision_envelope")
                requests = [(index, item) for index, item in enumerate(candidate["items"])
                            if type(item) is dict and item.get("decision") == "NEED_MORE"]
                if not requests:
                    self.session.propose(row["id"], candidate)
                    return self.store.get(row["id"])
                fresh, read_errors = False, []
                for index, item in requests:
                    try:
                        read = await self.session.read(row["id"], item.get("next_read"),
                                                       activity_ref=item.get("activity_ref", "a" + str(index + 1)))
                        fresh |= not read["cached"]
                    except ValueError as error:
                        read_errors.append({"item_index": index, "code": str(error)})
                if read_errors:
                    if corrected:
                        candidate = self._stop_reads(candidate, "invalid_read_or_budget_exhausted", technical=True)
                        self.session.propose(row["id"], candidate)
                        return self.store.get(row["id"])
                    feedback, corrected = {"errors": read_errors}, True
                    continue
                if not fresh:
                    self.session.propose(row["id"], self._stop_reads(candidate, "重复读取没有新增事实"))
                    return self.store.get(row["id"])
                attempts = sum(trace["kind"] == "MODEL" for trace in self.store.traces(row["id"]))
                if attempts >= self.limits.max_model_calls:
                    self.session.propose(row["id"], self._stop_reads(candidate, "model_budget_exhausted", technical=True))
                    return self.store.get(row["id"])
                feedback = None
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            wrapped = ModelCallError("planning_error")
            wrapped.__cause__ = error
            failure = _failure(wrapped)
            details = {"code": str(error)[:500], "type": type(error).__name__,
                       "retryable": not isinstance(error, (TimeoutError, asyncio.CancelledError)) and
                       (isinstance(error, RetryLater) or failure.get("retryable", False))}
            if getattr(error, "http_status", None) == 400:
                details.update(code="model_request_rejected", manual_retry_allowed=True)
            if isinstance(error, RetryLater):
                details["not_before"] = error.not_before
            if type(candidate) is dict and candidate.get("items") and not isinstance(error, asyncio.CancelledError):
                try:
                    self.session.propose(row["id"], self._stop_reads(candidate, details["code"], technical=True))
                    return self.store.get(row["id"])
                except ValueError:
                    pass
            try:
                result = self.store.finish(row["id"], token, error=details)
            except ValueError:
                result = self.store.get(row["id"])
            if isinstance(error, asyncio.CancelledError):
                raise
            return result
