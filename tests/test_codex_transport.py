"""Transport grammar compatibility checks; no actual model invocation."""

import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path
import os
import json
import subprocess
from threadplan.codex_transport import wire_schema, CodexJSONSubscription, CodexSubscription, TransportError


class WireSchemaChecks(unittest.TestCase):
    def test_unsupported_constraint_omitted_only_on_wire(self):
        domain = {"type": "array", "uniqueItems": True, "minItems": 1,
                  "items": {"type": "array", "uniqueItems": True}}
        wire = wire_schema(domain)
        self.assertNotIn("uniqueItems", wire)
        self.assertNotIn("uniqueItems", wire["items"])
        self.assertEqual(wire["minItems"], 1)
        self.assertTrue(domain["uniqueItems"])

    def test_subscription_route_strips_api_keys_and_refuses_api_key_auth_before_inference(self):
        captured=[]
        def status(command,**kwargs):
            captured.append((command,kwargs))
            return subprocess.CompletedProcess(command,0,stdout="Logged in using an API key",stderr="")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{"OPENAI_API_KEY":"test-only","OPENROUTER_API_KEY":"test-only","CODEX_API_KEY":"test-only"}):
            model=CodexSubscription(model="test-model",reasoning="low",runs_dir=Path(directory))
            with patch('threadplan.codex_transport.shutil.which',return_value='constructed-codex.exe'),patch('threadplan.codex_transport.subprocess.run',side_effect=status):
                with self.assertRaisesRegex(TransportError,"subscription route requires"):
                    model._run('test','{}',{})
        self.assertEqual(len(captured),1)
        for key in ('OPENAI_API_KEY','OPENROUTER_API_KEY','CODEX_API_KEY'):
            self.assertNotIn(key,captured[0][1]['env'])


class CodexCarrierChecks(unittest.IsolatedAsyncioTestCase):
    async def test_flexible_domain_is_checked_outside_strict_carrier_without_repair(self):
        class ScriptedSubscription:
            def __init__(self,**kwargs):
                self.last_record={'status':'completed','reported_usage':[]}
            async def __call__(self,**kwargs):
                self.called=kwargs
                return {'payload_json':json.dumps({'answer':'原文'})}
        model=CodexJSONSubscription(model='test-model',reasoning='low',runs_dir=Path('.'))
        schema={'type':'object','properties':{'answer':{'type':'string'}},'required':['answer'],'additionalProperties':False}
        with patch('threadplan.codex_transport.CodexSubscription',ScriptedSubscription):
            result=await model(system_prompt='test',input_json='{}',response_schema=schema)
        self.assertEqual(result,{'answer':'原文'})
        self.assertIsNone(model.last_record['cost'])
        self.assertEqual(model.last_record['transport'],'codex_subscription_json')

    async def test_invalid_domain_output_is_retained_as_explicit_error_not_success(self):
        class ScriptedSubscription:
            def __init__(self,**kwargs):self.last_record={'status':'completed'}
            async def __call__(self,**kwargs):return {'payload_json':'{"answer":123}'}
        model=CodexJSONSubscription(model='test-model',reasoning='low',runs_dir=Path('.'))
        schema={'type':'object','properties':{'answer':{'type':'string'}},'required':['answer']}
        with patch('threadplan.codex_transport.CodexSubscription',ScriptedSubscription):
            with self.assertRaises(TransportError) as caught:
                await model(system_prompt='test',input_json='{}',response_schema=schema)
        self.assertEqual(caught.exception.rejected_output,{'answer':123})
        self.assertEqual(model.last_record['status'],'domain_validation_error')
