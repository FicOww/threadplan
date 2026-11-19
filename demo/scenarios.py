"""Versioned scenario inputs and model-response replay. No evaluation labels."""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

from threadplan import Intake, UpstreamContext
from threadplan.codec import _aware_time


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "runs/diversity50-replay/receipt.json"
ANCHOR = "2026-11-19T10:00:00+08:00"
SOURCE_RECORDED = "recorded_development_output"
SOURCE_SCRIPTED = "scripted_demo_output"


@dataclass(frozen=True)
class Phase:
    messages: list
    steps: list
    choice: str | None = None


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    category: str
    intake: dict
    context: dict
    fixture: dict
    anchor: datetime
    phases: tuple[Phase, ...]
    source: str
    fault: str | None = None

    def summary(self):
        return {"id": self.id, "title": self.title, "category": self.category,
                "messages": len(self.intake["messages"]), "source": self.source,
                "has_followup": len(self.phases) > 1}


class ReplayModel:
    def __init__(self, phase):
        self.phase = phase
        self.index = 0
        self.mismatch = None

    async def __call__(self, *, system_prompt, input_json, response_schema):
        actual = json.loads(input_json)
        if self.index >= len(self.phase.steps) or actual["messages"] != self.phase.messages:
            self.mismatch = "replay_source_or_steps_changed"
            raise ValueError(self.mismatch)
        step = self.phase.steps[self.index]
        expected = step.get("input")
        if expected is not None:
            # The planner records the clock at task registration. Scenario business
            # time is frozen separately from wall-clock elapsed approval time.
            changed = sorted(key for key in actual.keys() | expected.keys()
                             if key != "evaluation_now" and actual.get(key) != expected.get(key))
            if changed:
                self.mismatch = "replay_facts_changed:" + ",".join(changed)
                raise ValueError(self.mismatch)
        elif len(actual["reads"]) != step["reads"]:
            self.mismatch = "scripted_read_sequence_changed"
            raise ValueError(self.mismatch)
        self.index += 1
        return deepcopy(step["output"])


def recorded_scenarios(receipt_path=CAMPAIGN):
    """Load only constructed cases bound to the frozen local campaign."""
    if not receipt_path.is_file():
        return {}, {"recorded": 0, "expected": 50, "archive_available": False}
    campaign = json.loads(receipt_path.read_text(encoding="utf-8"))
    names = ["diversity50.inputs.jsonl", "diversity50.fixtures.jsonl", "diversity50.catalog.json"]
    payloads = {}
    for name in names:
        path = f"evals/{name}"
        raw = (ROOT / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != campaign["frozen_sha256"].get(path):
            raise ValueError("replay_dataset_does_not_match_campaign")
        payloads[name] = raw.decode("utf-8")
    inputs = {row["case_id"]: row for row in map(json.loads, payloads[names[0]].splitlines())}
    fixtures = {row["case_id"]: row for row in map(json.loads, payloads[names[1]].splitlines())}
    catalog = json.loads(payloads[names[2]])
    families = {row["case_id"]: row["family"] for row in catalog["new_cases"]}
    cases = {}
    for batch in campaign["batches"]:
        folder = (ROOT / batch["run_directory"]).resolve()
        if not folder.is_relative_to((ROOT / "runs").resolve()):
            raise ValueError("replay_receipt_outside_runs")
        for identifier in batch["cases"]:
            intake = Intake(inputs[identifier]["input"]).to_dict()
            fixture = fixtures[identifier]
            detail = json.loads((folder / (identifier + ".json")).read_text(encoding="utf-8"))
            if detail["row"]["request"] != intake:
                raise ValueError("replay_input_does_not_match_receipt")
            steps = [{"input": trace["input"]["payload"], "output": trace["output"]}
                     for trace in detail["traces"] if trace["kind"] == "MODEL"]
            if not steps or any(not isinstance(step["output"], dict) for step in steps):
                raise ValueError("replay_response_missing")
            title, category = _recorded_summary(inputs[identifier], families.get(identifier), steps[-1]["output"])
            cases[identifier] = Scenario(identifier, title, category, intake, fixture["context"],
                                         fixture["calendar_fixture"], _aware_time(fixture["evaluation_now"]),
                                         (Phase(intake["messages"], steps),), SOURCE_RECORDED)
    if list(cases) != campaign["case_order"] or set(cases) != set(inputs) or set(inputs) != set(fixtures):
        raise ValueError("replay_campaign_case_order_mismatch")
    return cases, {"recorded": len(cases), "expected": 50, "archive_available": True,
                   "model_responses": sum(len(case.phases[0].steps) for case in cases.values()),
                   "categories": dict(Counter(case.category for case in cases.values()))}


def _recorded_summary(row, family, response):
    decisions = response["items"]
    actions = {item["operation"].get("kind") for item in decisions if isinstance(item.get("operation"), dict)}
    terminal = {item.get("decision") for item in decisions}
    name = family or row["case_id"]
    category = "新增与无需变更"
    if any(term in name for term in ("midnight", "dst", "offset", "timezone")):
        category = "时间与消息"
    elif any(term in name for term in ("partial_read", "failed_read", "read_failed", "read_partial")):
        category = "读写与恢复"
    elif "UNSUPPORTED" in terminal or any(term in name for term in ("inject", "third_party", "scope")):
        category = "权限与边界"
    elif len(decisions) > 1 or "WAIT" in terminal:
        category = "多活动与澄清"
    elif "CANCEL" in actions or any(term in name for term in ("cancel", "withdraw")):
        category = "取消"
    elif "UPDATE" in actions:
        category = "改期与字段"
    messages = row["input"]["messages"]
    first = " ".join(messages[0]["text"].split())
    return first[:46] + ("…" if len(first) > 46 else ""), category


def _chat(identifier, *texts):
    return {
        "schema_version": "0.3", "request_id": identifier + "-v1",
        "source": {"kind": "conversation", "provider": "threadplan_demo", "conversation_id": identifier},
        "messages": [{"id": f"m{i}", "sender_id": "林晨" if i % 2 else "陈嘉",
                      "sent_at": f"2026-11-19T09:{i:02d}:00+08:00", "text": value}
                     for i, value in enumerate(texts, 1)],
    }


def _event(identifier, title, start, end, location=None):
    fields = {"title": title, "start_at": start, "end_at": end}
    if location:
        fields["location"] = location
    return {"id": identifier, "status": "confirmed", "fields": fields}


def _item(ref="a1", decision="PROPOSE", reason="", *, action=None, **fields):
    if action is not None:
        fields["operation"] = action
    return {"activity_ref": ref, "decision": decision, "reason": reason,
            "evidence_message_ids": fields.pop("evidence_message_ids", ["m1", "m2"]), **fields}


def _step(reads, *items):
    return {"reads": reads, "output": {"items": list(items)}}


def _find(ref="a1", reason="查看目标时段的日程。"):
    return _item(ref, "NEED_MORE", reason, next_read={"kind": "find_events", "range": {
        "start_at": "2026-11-20T00:00:00+08:00", "end_at": "2026-11-21T00:00:00+08:00"}})


def _built_in(identifier, title, category, texts, events, steps, *, followups=(), fault=None, read_status="COMPLETE"):
    intake = Intake(_chat(identifier, *texts)).to_dict()
    context = UpstreamContext(user_id="林晨", default_timezone="Asia/Shanghai", connection_ref="demo",
                              scope_ref="simulation", namespace="demo", policy_ref="demo-v1").to_dict()
    return Scenario(identifier, title, category, intake, context,
                    {"events": events, "read_status": read_status}, _aware_time(ANCHOR),
                    (Phase(intake["messages"], steps), *followups), SOURCE_SCRIPTED, fault)


def builtin_scenarios():
    old = _event("review-a", "方案评审", "2026-11-20T14:00:00+08:00", "2026-11-20T14:30:00+08:00", "会议室 A")
    similar = _event("review-b", "方案评审", "2026-11-20T14:00:00+08:00", "2026-11-20T14:30:00+08:00", "会议室 B")
    move = ("周五两点的方案评审，改到三点半可以吗？", "可以，还是半小时，地点不变。")
    change = _item(reason="双方确认改到 15:30，地点不变。", action={"kind": "UPDATE", "event_ref": "c1",
                   "changes": {"start_at": "2026-11-20T15:30:00+08:00"}})
    cancel = _item(reason="双方确认整场取消。", action={"kind": "CANCEL", "event_ref": "c1"})
    create = _item(reason="双方已经确认 16:00 开始。", action={"kind": "CREATE", "fields": {
        "title": "复盘会", "start_at": "2026-11-20T16:00:00+08:00", "duration_minutes": 30}})
    due = ("周五四点安排半小时复盘会，可以吗？", "可以，周五四点。")
    cases = [
        _built_in("reschedule", "把方案评审改到 15:30", "改期与字段", move, [old], [_step(0, _find()), _step(1, change)]),
        _built_in("create", "新增周五复盘会", "新增与无需变更", due, [], [_step(0, _find()), _step(1, create)]),
        _built_in("cancel", "取消已经安排的评审", "取消", ("周五 14:00 的方案评审取消。", "收到，这次整场取消。"),
                  [old], [_step(0, _find()), _step(1, cancel)]),
        _built_in("lost-ack", "改期后没有收到确认", "读写与恢复", move, [old],
                  [_step(0, _find()), _step(1, change)], fault="lost_ack"),
        _built_in("pending-verification", "创建获确认，读回暂时失败", "读写与恢复", due, [],
                  [_step(0, _find()), _step(1, create)], fault="readback"),
        _built_in("unknown-create", "创建结果未知，缺少日程 ID", "读写与恢复", due, [],
                  [_step(0, _find()), _step(1, create)], fault="lost_ack"),
        _built_in("partial", "日历只返回了部分结果", "读写与恢复", due, [],
                  [_step(0, _find()), _step(1, _item(decision="WAIT", reason="日历覆盖不完整。",
                  clarification_question="稍后再检查目标时段？", missing_facts=["完整日历覆盖"], attempted_read_refs=["r1"]))],
                  read_status="PARTIAL"),
        _built_in("failed-read", "日历读取失败", "读写与恢复", due, [],
                  [_step(0, _find()), _step(1, _item(decision="WAIT", reason="日历读取失败。",
                  clarification_question="稍后再检查目标时段？", missing_facts=["完整日历覆盖"], attempted_read_refs=["r1"]))],
                  read_status="FAILED"),
        _built_in("unsupported", "识别暂不支持的每周安排", "权限与边界",
                  ("以后每周五四点开复盘会，邀请组内成员。", "好，固定每周都开。"), [],
                  [_step(0, _item(decision="UNSUPPORTED", reason="这项安排需要重复日程和邀请。",
                                  unsupported_requirements=["重复日程", "邀请参与者"]))]),
    ]
    messages = Intake(_chat("choose", *move)).to_dict()["messages"]
    wait = _item(decision="WAIT", reason="旧时段里有两场同名评审。", candidate_refs=["c1", "c2"],
                 attempted_read_refs=["r1", "r2"], missing_facts=["原会议室"],
                 clarification_question="要改的是会议室 A 还是会议室 B 的评审？")
    details = _item(decision="NEED_MORE", reason="比较两场日程的地点。",
                    next_read={"kind": "get_events", "event_refs": ["c1", "c2"]})
    followups = []
    for index, room in enumerate(("A", "B"), 1):
        answer = {"id": f"m3-{room}", "sender_id": "林晨", "sent_at": "2026-11-19T09:05:00+08:00",
                  "text": f"改会议室 {room} 那场。"}
        updated = deepcopy(change)
        updated["operation"]["event_ref"] = f"c{index}"
        updated["evidence_message_ids"].append(answer["id"])
        updated["reason"] = f"补充消息指定了会议室 {room} 的评审。"
        followups.append(Phase(messages + [answer], [_step(0, _find()), _step(1, updated)], f"会议室 {room}"))
    cases.append(_built_in("choose", "两场评审，先确认改哪一场", "多活动与澄清", move, [old, similar],
                           [_step(0, _find()), _step(1, details), _step(2, wait)], followups=followups))
    extra = (*move, "还有设计同步，要不也放在周五？")
    waiting = _item("a2", "WAIT", "设计同步只有提议。", evidence_message_ids=["m3"],
                    clarification_question="设计同步最终定在什么时候？", missing_facts=["明确时间与同意"])
    cases.append(_built_in("multi", "同一段聊天，两项安排各自处理", "多活动与澄清", extra, [old],
                           [_step(0, _find(), waiting), _step(1, change, waiting)]))
    timed = ("周五的复盘 23:45 开始，半小时。", "好，周五 23:45 开。")
    overnight = _item(reason="两人确认了开始时间与时长。", action={"kind": "CREATE", "fields": {
        "title": "复盘", "start_at": "2026-11-20T23:45:00+08:00", "duration_minutes": 30}})
    query = _find()
    query["next_read"]["range"]["end_at"] = "2026-11-21T02:00:00+08:00"
    cases.append(_built_in("midnight", "保留跨午夜的 30 分钟", "时间与消息", timed, [],
                           [_step(0, query), _step(1, overnight)]))
    return {case.id: case for case in cases}
