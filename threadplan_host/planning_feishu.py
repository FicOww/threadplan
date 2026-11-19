"""Read-only discovery on the explicitly authorized calendar.

This adapter never creates a calendar, adopts event ownership, mutates records,
invites people or installs anything. The host selects and authorizes the calendar scope.
"""

from copy import deepcopy

from threadplan.adapters.feishu import LarkCLI, capture_cli_calls, event_record


class FeishuPlanningReader:
    def __init__(self, calendar, *, connection_ref, scope_ref):
        self.calendar = calendar
        self.connection_ref, self.scope_ref = connection_ref, scope_ref

    def __call__(self, *, query, context):
        with capture_cli_calls() as calls:
            try:
                result = self._read(query=query, context=context)
            except Exception as error:
                error.native_cli_invocations = len(calls) if isinstance(self.calendar.cli, LarkCLI) else None
                raise
        result["native_cli_invocations"] = len(calls) if isinstance(self.calendar.cli, LarkCLI) else None
        return result

    def _read(self, *, query, context):
        if context.connection_ref != self.connection_ref or context.scope_ref != self.scope_ref:
            raise ValueError("configured_calendar_scope_mismatch")
        events = []
        if query["kind"] == "find_events":
            if query.get("page_token"):
                raise ValueError("pagination_not_supported_by_instance_view")
            data = self.calendar.find_events(query["range"], approved=True)
            events = [event_record(raw) for raw in data.get("items") or []]
            status = "PARTIAL" if data.get("has_more") or data.get("page_token") else "COMPLETE"
        elif query["kind"] == "get_events":
            for identifier in query["event_ids"]:
                raw = self.calendar.get_event(identifier, approved=True)
                events.append(event_record(raw))
            status = "COMPLETE"
        else:
            raise ValueError("unsupported_calendar_read")
        return {"read_status": status, "connection_ref": self.connection_ref, "scope_ref": self.scope_ref,
                "events": events, "searched": {"range": deepcopy(query.get("range")), "applied_filters": {}}}
