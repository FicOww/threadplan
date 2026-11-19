"""Installed-adapter request shape with a constructed CLI; no real service calls."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from threadplan import UpstreamContext
from threadplan.adapters.feishu import FeishuCalendar, LarkCLI
from threadplan_host.planning_feishu import FeishuPlanningReader
from threadplan_host.feishu import FeishuHost


class ReadCLI:
    def __init__(self):
        self.calls=[]
        self.event={"event_id":"manual-provider-id","summary":"聊一下","status":"confirmed",
            "description":"讨论方案","start_time":{"timestamp":"1794376800","timezone":"Asia/Shanghai"},
            "end_time":{"timestamp":"1794378600","timezone":"Asia/Shanghai"},"location":{"name":"二号会议室"}}

    def invoke(self,resource,method,**kwargs):
        self.calls.append((resource,method,deepcopy(kwargs)))
        if resource=="calendars":
            return {"calendar_id":"private-test","summary":"ThreadPlan Test","type":"shared","permissions":"private","role":"owner"}
        if method=="instance_view":return {"items":[deepcopy(self.event)]}
        if method=="get":return {"event":deepcopy(self.event)}
        raise AssertionError("Read adapter attempted an unexpected operation")


class FeishuPlanningReadChecks(unittest.TestCase):
    def setUp(self):
        self.cli=ReadCLI()
        self.calendar=FeishuCalendar("private-test",cli=self.cli)
        self.context=UpstreamContext("me","Asia/Shanghai","connection","scope")
        self.reader=FeishuPlanningReader(self.calendar,connection_ref="connection",scope_ref="scope")

    def test_successful_empty_native_view_is_not_a_format_error_or_partial_success(self):
        window={"start_at":"2026-11-20T00:00:00+08:00","end_at":"2026-11-21T00:00:00+08:00"}
        original=self.cli.invoke
        def invoke(resource,method,**kwargs):
            return {} if method=="instance_view" else original(resource,method,**kwargs)
        with patch.object(self.cli,"invoke",side_effect=invoke), tempfile.TemporaryDirectory() as folder:
            read=self.reader(query={"kind":"find_events","range":window},context=self.context)
            self.assertEqual((read['read_status'],read['events']),("COMPLETE",[]))
            backend=FeishuHost(self.calendar,Path(folder)/'ledger.sqlite')
            self.assertEqual(backend.snapshot(range=window),([],{}))
        for view in ({'items':{}},{'has_more':True}):
            def incomplete(resource,method,**kwargs):
                return view if method=="instance_view" else original(resource,method,**kwargs)
            with patch.object(self.cli,"invoke",side_effect=incomplete), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(Exception):
                    FeishuHost(self.calendar,Path(folder)/'ledger.sqlite').snapshot(range=window)

    def test_time_window_read_keeps_generic_titles_raw_fields_and_unknown_attendees(self):
        query={"kind":"find_events","range":{"start_at":"2026-11-11T00:00:00+08:00","end_at":"2026-11-12T00:00:00+08:00"},"hints":{"title":"方案评审"}}
        result=self.reader(query=query,context=self.context)
        record=result["events"][0]
        self.assertEqual(record["fields"]["title"],"聊一下")
        self.assertEqual(record["fields"]["description"],"讨论方案")
        self.assertNotIn("participants",record["fields"])
        self.assertEqual(record["raw"],self.cli.event)
        self.assertEqual(self.calendar.managed_event_ids,set())
        params=self.cli.calls[-1][2]["params"]
        self.assertEqual(params["calendar_id"],"private-test")
        self.assertEqual(int(params["start_time"]),int(datetime.fromisoformat(query["range"]["start_at"]).timestamp()))
        self.assertNotIn("query",params)
        self.assertEqual(result["read_status"],"COMPLETE")

    def test_detail_and_scope_checks_do_not_adopt_ownership_or_write(self):
        result=self.reader(query={"kind":"get_events","event_ids":["manual-provider-id"]},context=self.context)
        self.assertEqual(result["events"][0]["id"],"manual-provider-id")
        self.assertFalse(self.calendar.managed_event_ids)
        self.assertFalse(any(method in {"create","patch","delete"} for _,method,_ in self.cli.calls))
        with self.assertRaisesRegex(ValueError,"scope_mismatch"):
            self.reader(query={"kind":"get_events","event_ids":["manual-provider-id"]},
                        context=UpstreamContext("me","Asia/Shanghai","other","scope"))

    def test_invalid_native_timestamp_is_not_normalized_into_an_invented_date(self):
        self.cli.event["start_time"]["timestamp"]=True
        with self.assertRaisesRegex(Exception,"Invalid source timestamp"):
            self.reader(query={"kind":"get_events","event_ids":["manual-provider-id"]},context=self.context)

    def test_real_cli_boundary_counts_attempts_including_failed_first_read(self):
        def success(command, **kwargs):
            data = ({"calendar_id":"private-test","summary":"ThreadPlan Test","type":"shared","permissions":"private","role":"owner"}
                    if command[2] == "calendars" else {"event": self.cli.event})
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok":True,"identity":"user","data":data}), "")
        calendar = FeishuCalendar("private-test", cli=LarkCLI("constructed-native.exe"))
        reader = FeishuPlanningReader(calendar, connection_ref="connection", scope_ref="scope")
        query = {"kind":"get_events","event_ids":["manual-provider-id"]}
        with patch("threadplan.adapters.feishu.subprocess.run", side_effect=success):
            result = reader(query=query, context=self.context)
        self.assertEqual(result["native_cli_invocations"], 2)
        failure = subprocess.CompletedProcess([], 1, "", json.dumps({"ok":False,"error":{"type":"authentication"}}))
        with patch("threadplan.adapters.feishu.subprocess.run", return_value=failure):
            with self.assertRaises(Exception) as error:
                reader(query=query, context=self.context)
        self.assertEqual(error.exception.native_cli_invocations, 1)
