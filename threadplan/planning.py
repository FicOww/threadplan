"""Standalone model instructions built from the shared behavior policy."""

from .protocol import DECISION_POLICY

AGENT_PROMPT = DECISION_POLICY + """
Return JSON {items:[...]}. For an activity needing facts use decision=NEED_MORE,
reason, evidence_message_ids, activity_ref and next_read. next_read is an object:
{kind:find_events,range:{start_at,end_at},hints?:{title,location,participants},page_token?}
or {kind:get_events,event_refs:[...],fields?:[...]} or {kind:get_messages,message_ids:[...]}.
These are read-only requests, never writes. The first response can be lightweight
retrieval hypotheses; do not provide complete extracted events before querying.
After reads submit final items using PROPOSE/NO_ACTION/WAIT/UNSUPPORTED. PROPOSE has
operation {kind:CREATE,fields:{...}} / {kind:UPDATE,event_ref,changes:{...},
preserve_duration?:true,shift_minutes?:number} / {kind:CANCEL,event_ref}.
Use only actual read_ref and event_ref values. Explain key matching/exclusion facts,
not hidden reasoning or numeric confidence. Final decisions always reread original
messages. Never include native IDs, approvals or claimed write success. JSON only.
"""
