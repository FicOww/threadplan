"""Explicit fake-HTTP contract tests; no live service, real key, or inference."""

from io import BytesIO
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from threadplan.openrouter_transport import DEFAULT_MODEL, ENDPOINT, OpenRouterError, OpenRouterTransport, _NoRedirect, wire_schema


FAKE_KEY = "constructed-fake-api-key-not-a-credential"
FAKE_RESULT = {"answer": "constructed transport result"}


def response_schema():
    """Application-neutral fixture: this checks transport, not retired APIs."""
    return {"type": "object", "properties": {"answer": {"type": "string"}},
            "required": ["answer"], "additionalProperties": False}


def fake_completion(**overrides):
    value = {
        "id": "constructed-response-id", "model": "constructed-actual-model", "provider": "constructed-provider",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(FAKE_RESULT)}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19, "cost": 0,
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    }
    value.update(overrides)
    return value


class FakeHTTPResponse:
    """Constructed HTTP response for boundary tests, never a production fallback."""
    def __init__(self, value, status=200):
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


class ConstructedOpenRouterHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="threadplan-fake-http-")
        self.addCleanup(self.temp.cleanup)
        self.runs = Path(self.temp.name) / "runs"

    def transport(self, **kwargs):
        return OpenRouterTransport(runs_dir=self.runs, api_key=FAKE_KEY, **kwargs)

    async def call(self, transport):
        return await transport(system_prompt="Constructed JSON-only rules.", input_json='{"messages":[]}',
                               response_schema=response_schema())

    async def test_fake_http_single_request_strict_schema_and_zero_price_privacy_constraints(self):
        transport = self.transport()
        schema = response_schema()
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(fake_completion())) as opened:
            result = await transport(system_prompt="Constructed rules.", input_json='{"untrusted":"text"}', response_schema=schema)
        self.assertEqual(result, FAKE_RESULT)
        self.assertEqual(opened.call_count, 1)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(payload["provider"], {"require_parameters": True, "data_collection": "deny",
                                               "allow_fallbacks": False,
                                               "max_price": {"prompt": 0, "completion": 0, "request": 0}})
        self.assertEqual(payload["reasoning"], {"enabled": False})
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIn(json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(",", ":")), payload["messages"][0]["content"])
        self.assertEqual([item["role"] for item in payload["messages"]], ["system", "user"])
        self.assertEqual(payload["messages"][1]["content"], '{"untrusted":"text"}')
        self.assertNotIn("models", payload)
        self.assertNotIn("plugins", payload)
        self.assertEqual(opened.call_args.kwargs["timeout"], 60)
        self.assertEqual(transport.last_record["http_attempts"], 1)
        self.assertEqual(transport.last_record["retries"], 0)
        self.assertIs(transport.last_record["free_only"], True)

    async def test_actual_usage_cost_and_models_recorded_without_invented_values_or_key(self):
        transport = self.transport()
        response = fake_completion()
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)):
            await self.call(transport)
        record = transport.last_record
        self.assertEqual(record["requested_model"], DEFAULT_MODEL)
        self.assertEqual(record["resolved_model"], response["model"])
        self.assertEqual(record["provider"], response["provider"])
        self.assertEqual(record["reported_usage"], [response["usage"]])
        self.assertEqual(record["total_tokens"], 19)
        self.assertEqual(record["cost"], 0)
        self.assertEqual(record["cost_unit"], "OpenRouter credits")
        self.assertGreaterEqual(record["elapsed_seconds"], 0)
        self.assertEqual(len(record["prompt_sha256"]), 64)
        self.assertEqual(len(record["input_sha256"]), 64)
        run_dir = Path(record["run_directory"])
        self.assertEqual(run_dir.parent, self.runs)
        self.assertEqual(json.loads((run_dir / "record.json").read_text(encoding="utf-8")), record)
        for file in run_dir.iterdir():
            text = file.read_text(encoding="utf-8")
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("Authorization", text)

    async def test_missing_usage_provider_model_remain_null_not_estimated_free(self):
        response = fake_completion()
        for key in ("usage", "model", "provider"):
            response.pop(key)
        transport = self.transport()
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)):
            await self.call(transport)
        for key in ("usage", "cost", "prompt_tokens", "completion_tokens", "total_tokens", "resolved_model", "provider"):
            self.assertIsNone(transport.last_record[key])
        self.assertEqual(transport.last_record["reported_usage"], [])

    async def test_environment_key_read_only_when_called_and_missing_key_never_posts(self):
        fake_environment = Mock()
        fake_environment.get.return_value = None
        with patch("threadplan.openrouter_transport.os.environ", fake_environment):
            transport = OpenRouterTransport(runs_dir=self.runs)
            fake_environment.get.assert_not_called()
            with patch("threadplan.openrouter_transport._open_request") as opened:
                with self.assertRaises(OpenRouterError) as caught:
                    await self.call(transport)
                opened.assert_not_called()
        self.assertEqual(caught.exception.status, "missing_or_invalid_credentials")
        self.assertEqual(transport.last_record["http_attempts"], 0)
        fake_environment.get.assert_called_once_with("OPENROUTER_API_KEY")

    async def test_explicit_fake_environment_key_used_when_no_parameter_given(self):
        with patch("threadplan.openrouter_transport.os.environ", {"OPENROUTER_API_KEY": FAKE_KEY}):
            transport = OpenRouterTransport(runs_dir=self.runs)
            with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(fake_completion())) as opened:
                await self.call(transport)
        self.assertEqual(opened.call_args.args[0].get_header("Authorization"), "Bearer " + FAKE_KEY)

    async def test_failure_finish_reasons_are_not_success_and_never_retried(self):
        for reason in ("length", "content_filter", "error", "tool_calls", None):
            with self.subTest(reason=reason):
                response = fake_completion()
                response["choices"][0]["finish_reason"] = reason
                transport = self.transport()
                with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)) as opened:
                    with self.assertRaises(OpenRouterError):
                        await self.call(transport)
                self.assertEqual(opened.call_count, 1)
                self.assertNotEqual(transport.last_record["status"], "completed")
                self.assertEqual(transport.last_record["reported_usage"], [response["usage"]])

    async def test_refusal_tools_empty_choices_and_malformed_content_fail_explicitly(self):
        for changes in ({"refusal": "Constructed refusal."}, {"tool_calls": [{}]},
                        {"content": None}, {"content": []}, {"content": "```json\n{}\n```"},
                        {"content": '{"proposals":NaN}'},
                        {"content": '{"proposals":[],"proposals":[],"questions":[],"observations":[]}'}):
            with self.subTest(changes=changes):
                response = fake_completion()
                response["choices"][0]["message"].update(changes)
                with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)):
                    with self.assertRaises(OpenRouterError):
                        await self.call(self.transport())
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(fake_completion(choices=[]))):
            with self.assertRaises(OpenRouterError):
                await self.call(self.transport())

    async def test_schema_violation_is_not_repaired_or_marked_success(self):
        response = fake_completion()
        response["choices"][0]["message"]["content"] = json.dumps(dict(FAKE_RESULT, approved=True))
        transport = self.transport()
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)):
            with self.assertRaises(OpenRouterError) as caught:
                await self.call(transport)
        self.assertEqual(caught.exception.status, "response_schema_error")
        self.assertEqual(transport.last_record["status"], "response_schema_error")

    async def test_concurrent_calls_keep_usage_and_model_metadata_in_their_own_task(self):
        transport = self.transport()
        ready = asyncio.Event()
        completed = []
        async def worker(label):
            await transport(system_prompt=label, input_json="{}", response_schema={"type": "object", "properties": {},
                            "required": [], "additionalProperties": True})
            completed.append(label)
            if len(completed) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=5)
            return label, transport.last_record["actual_model"]
        # Use a response matching the requested tiny object schema.
        def schema_response(request, timeout):
            payload = json.loads(request.data)
            label = payload["messages"][0]["content"].split("\n")[0]
            value = fake_completion(model=label)
            value["choices"][0]["message"]["content"] = "{}"
            return FakeHTTPResponse(value)
        with patch("threadplan.openrouter_transport._open_request", side_effect=schema_response):
            results = await asyncio.gather(worker("constructed-model-A"), worker("constructed-model-B"))
        self.assertEqual(results, [("constructed-model-A", "constructed-model-A"),
                                   ("constructed-model-B", "constructed-model-B")])

    async def test_http_error_preserves_structured_diagnostics_redacts_key_no_fallback(self):
        body = {"error": {"code": 429, "message": "Constructed rate limit " + FAKE_KEY}}
        failure = HTTPError(ENDPOINT, 429, "constructed error", {}, BytesIO(json.dumps(body).encode()))
        transport = self.transport()
        with patch("threadplan.openrouter_transport._open_request", side_effect=failure) as opened:
            with self.assertRaises(OpenRouterError) as caught:
                await self.call(transport)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.api_error["error"]["code"], 429)
        self.assertNotIn(FAKE_KEY, json.dumps(caught.exception.api_error))
        for file in Path(transport.last_record["run_directory"]).iterdir():
            self.assertNotIn(FAKE_KEY, file.read_text(encoding="utf-8"))

    async def test_api_error_inside_http_success_is_failure(self):
        for response in (fake_completion(error={"code": 503, "message": "Constructed unavailable"}),
                         fake_completion(choices=[{"error": {"code": 502}, "finish_reason": "error"}])):
            transport = self.transport()
            with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)):
                with self.assertRaises(OpenRouterError) as caught:
                    await self.call(transport)
            self.assertEqual(caught.exception.status, "api_error")

    async def test_timeouts_and_network_errors_explicit_single_attempt(self):
        for failure, expected in ((TimeoutError("constructed timeout"), "timeout"),
                                  (URLError(TimeoutError()), "timeout"),
                                  (URLError("constructed offline"), "network_error")):
            transport = self.transport(timeout_seconds=2)
            with patch("threadplan.openrouter_transport._open_request", side_effect=failure) as opened:
                with self.assertRaises(OpenRouterError) as caught:
                    await self.call(transport)
            self.assertEqual(caught.exception.status, expected)
            self.assertEqual(transport.last_record["status"], expected)
            self.assertEqual(opened.call_count, 1)

    async def test_invalid_response_envelope_json_and_unexpected_charge_fail(self):
        for response in (b"not JSON", b"[]", fake_completion(usage={"cost": 0.01})):
            with self.subTest(response=response):
                transport = self.transport()
                with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)):
                    with self.assertRaises(OpenRouterError):
                        await self.call(transport)
                self.assertNotEqual(transport.last_record["status"], "completed")

    def test_paid_models_and_nonzero_caps_unconditionally_forbidden(self):
        for kwargs in ({"model": "constructed/paid"}, {"max_prompt_price": 0.01},
                       {"max_completion_price": 0.01}, {"max_request_price": 0.01},
                       {"allow_paid": "true"}, {"allow_paid": True}, {"allow_paid": 0},
                       {"model": "constructed/paid", "allow_paid": True,
                        "max_prompt_price": 0.05, "max_completion_price": 0.1}):
            with self.subTest(kwargs=kwargs), patch("threadplan.openrouter_transport._open_request") as opened:
                with self.assertRaises(ValueError):
                    self.transport(**kwargs)
                opened.assert_not_called()

    def test_only_explicit_free_slugs_no_routers_or_embedded_parameters(self):
        for model in ("openrouter/auto", "openrouter/free", "openrouter/auto:free", "openrouter/free:free",
                      "constructed/model", "constructed/model:free:paid", "constructed/model:paid:free",
                      "constructed/model?price=0:free", "constructed/model:free/extra",
                      "constructed/model:free\n", "constructed/model:free "):
            with self.subTest(model=model), patch("threadplan.openrouter_transport._open_request") as opened:
                with self.assertRaises(ValueError):
                    self.transport(model=model)
                opened.assert_not_called()
        transport = self.transport(model="constructed/vendor-model_v1.2:free")
        self.assertIsNone(transport.last_record)

    async def test_mutated_attributes_rejected_before_key_lookup_or_http(self):
        for attribute, value in (("model", "constructed/paid"), ("allow_paid", True),
                                 ("max_prompt_price", 0.01), ("max_completion_price", 0.01),
                                 ("max_request_price", 0.01),
                                 ("max_price", {"prompt": 1, "completion": 0, "request": 0}),
                                 ("max_price", {"prompt": 0, "completion": 0})):
            with self.subTest(attribute=attribute, value=value):
                transport = OpenRouterTransport(runs_dir=self.runs)
                setattr(transport, attribute, value)
                fake_environment = Mock()
                with patch("threadplan.openrouter_transport.os.environ", fake_environment), \
                        patch("threadplan.openrouter_transport._open_request") as opened:
                    with self.assertRaises(ValueError):
                        await self.call(transport)
                    opened.assert_not_called()
                    fake_environment.get.assert_not_called()

    async def test_positive_reported_cost_is_post_request_failure_not_claimed_prevention(self):
        transport = self.transport()
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(fake_completion(usage={"cost": 0.01}))) as opened:
            with self.assertRaises(OpenRouterError) as caught:
                await self.call(transport)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(caught.exception.status, "unexpected_nonzero_cost")
        self.assertIn("post-request", transport.last_record["cost_guard"])
        self.assertEqual(transport.last_record["cost"], 0.01)

    async def test_actual_model_without_free_suffix_is_recorded_without_false_rejection(self):
        transport = self.transport()
        actual_model = DEFAULT_MODEL.removesuffix(":free")
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(fake_completion(model=actual_model))):
            result = await self.call(transport)
        self.assertEqual(result, FAKE_RESULT)
        self.assertEqual(transport.last_record["actual_model"], actual_model)

    async def test_portable_json_mode_keeps_strict_domain_validation(self):
        schema = {"type": "object", "properties": {"values": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}},
                  "required": ["values"], "additionalProperties": False}
        response = fake_completion(choices=[{"finish_reason": "stop", "message": {"content": '{"values":["same","same"]}'}}])
        transport = self.transport()
        with patch("threadplan.openrouter_transport._open_request", return_value=FakeHTTPResponse(response)):
            with self.assertRaises(OpenRouterError) as caught:
                await transport(system_prompt="Constructed rules", input_json="{}", response_schema=schema)
        self.assertEqual(caught.exception.status, "response_schema_error")

    def test_invalid_configuration_rejected_without_key_lookup_or_network(self):
        for kwargs in ({"max_tokens": 1025}, {"max_tokens": True}, {"timeout_seconds": 0},
                       {"timeout_seconds": float("inf")}, {"max_prompt_price": float("nan")},
                       {"model": " "}, {"api_key": "bad\nkey"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                OpenRouterTransport(runs_dir=self.runs, **kwargs)

    def test_redirect_does_not_forward_bearer_credentials(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "constructed redirect", {}, "https://example.test"))


if __name__ == "__main__":
    unittest.main()
