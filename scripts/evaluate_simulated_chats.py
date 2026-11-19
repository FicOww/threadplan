"""Opt-in small evaluation: new conversations and an evidence-matched direct baseline.

Calendars are explicitly simulated. Choose the fixed free route or explicitly
authorized subscription development route; neither falls back to the other.
The direct baseline reuses the chain's retrieved evidence, so it is NOT a complete
one-call replacement architecture and does not get free retrieval in cost claims.
"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from threadplan import Intake, UpstreamContext
from threadplan.codec import _aware_time, canonical_json, fingerprint
from threadplan.planning import AGENT_PROMPT
from threadplan.protocol import decode, model_schema
from threadplan_host.planning_validation import resolve_candidate
from threadplan.openrouter_transport import DEFAULT_MODEL, OpenRouterTransport
from threadplan.codex_transport import CodexJSONSubscription
from threadplan_host import ConversationPlanner, PlanningLimits, PlanningStore
from threadplan_host.model_config import openrouter_key
from threadplan_host.planning import recorded_model_call

DIRECT_PROMPT = AGENT_PROMPT + "\nThis comparison has fixed retrieved evidence; no further reads are available. Return final items only."


class CallBudget:
    def __init__(self, transport, limit):
        self.transport, self.limit, self.calls = transport, limit, 0
        self.valid = False

    @property
    def last_record(self):
        return self.transport.last_record if self.valid else None

    async def __call__(self, **kwargs):
        self.valid = False
        if self.calls >= self.limit:
            raise RuntimeError('evaluation_model_budget_exhausted')
        self.calls += 1
        try:
            return await self.transport(**kwargs)
        finally:
            self.valid = True


class SimulatedCalendarReader:
    """Case-scoped constructed records; never a remote calendar adapter."""
    def __init__(self, fixture):
        self.fixture = deepcopy(fixture)

    async def __call__(self, *, query, context):
        events = deepcopy(self.fixture['events'])
        if query['kind'] == 'get_events':
            events = deepcopy(self.fixture.get('detail_events', events))
            events = [e for e in events if e['id'] in query['event_ids']]
        else:
            low, high = (_aware_time(query['range'][k]) for k in ('start_at', 'end_at'))
            events = [e for e in events if _aware_time(e['fields']['start_at']) < high
                      and low < _aware_time(e['fields']['end_at'])]
        return {'read_status': self.fixture['read_status'], 'events': events,
                'searched': {'range': deepcopy(query.get('range')), 'applied_filters': {}},
                'connection_ref': context.connection_ref,
                'source_category': 'assistant_constructed_calendar_fixture'}


def score(result, expected):
    """Frozen concrete constraints only; language meaning still needs review."""
    errors = []
    ops = result.get('operations', [])
    wanted = expected['operations']
    decisions = [i.get('decision') for i in result.get('items', [])]
    if expected.get('evaluation_scope') == 'technical_failure_contract':
        if ops:
            errors.append('write_after_incomplete_or_failed_read')
        allowed = expected.get('terminal_one_of', expected['decisions'])
        if not decisions or any(state not in allowed for state in decisions):
            errors.append('technical_failure_not_exposed')
        statuses = {read['read_status'] for read in result.get('reads', [])}
        if expected.get('required_failed_read') and 'FAILED' not in statuses:
            errors.append('missing_actual_failed_read')
        if expected.get('required_incomplete_read') and not statuses & {'PARTIAL', 'FAILED'}:
            errors.append('missing_actual_incomplete_read')
        return {'concrete_constraints_passed': not errors, 'technical_contract_passed': not errors,
                'evaluation_scope': 'technical_failure_contract', 'errors': errors,
                'semantic_review': 'not_a_semantic_accuracy_sample_guard_result_only'}
    if not set(expected['decisions']) <= set(decisions):
        errors.append('missing_expected_decision')
    if any(state in {'FAILED', 'NEED_MORE'} for state in decisions):
        errors.append('activity_failure_or_unfinished_read')
    expected_targets = {want['target_id'] for want in wanted if want.get('target_id')}
    for op in ops:
        native = result.get('records', {}).get(op.get('event_ref'), {}).get('id')
        if op['kind'] in ('UPDATE', 'CANCEL') and native not in expected_targets:
            errors.append('wrong_target')
        if native in expected.get('must_not_change', []):
            errors.append('protected_target_changed')
    if not wanted:
        if ops:
            errors.append('unexpected_write')
        if not any(d in expected.get('terminal_one_of', expected['decisions']) for d in decisions):
            errors.append('wrong_terminal_decision')
    elif not ops:
        errors.append('unnecessary_wait' if 'WAIT' in decisions else 'missed_operation')
    if len(ops) != len(wanted):
        errors.append('operation_count')
    remaining = list(ops)
    for want in wanted:
        match = next((op for op in remaining if want.get('target_id') and
                      result.get('records', {}).get(op.get('event_ref'), {}).get('id') == want['target_id']), None)
        if match is None and want.get('start_at'):
            match = next((op for op in remaining if _aware_time(op['changes'].get('start_at')) == _aware_time(want['start_at'])), None)
        if match is None:
            match = next((op for op in remaining if op['kind'] == want['kind']), None)
        if match is None:
            errors.append('missing_expected_kind:' + want['kind'])
            continue
        remaining.remove(match)
        if match['kind'] != want['kind']:
            errors.append('wrong_operation_kind')
        target = result.get('records', {}).get(match.get('event_ref'), {}).get('id')
        if want.get('target_id') and target != want['target_id']:
            errors.append('wrong_target')
        fields = want.get('changes', {k:v for k,v in want.items() if k in ('start_at','end_at','location')})
        for key, value in fields.items():
            actual = match['changes'].get(key)
            equal = _aware_time(actual) == _aware_time(value) if key.endswith('_at') else actual == value
            if not equal:
                errors.append(('wrong_time:' if key.endswith('_at') else 'wrong_or_missing_field:') + key)
        if 'participants' in want and set(match.get('metadata', {}).get('participants', [])) != set(want['participants']):
            errors.append('wrong_or_missing_participants')
        if 'allowed_changes' in want and set(match['changes']) - set(want['allowed_changes']):
            errors.append('unrequested_field_change')
        if target in expected.get('must_not_change', []):
            errors.append('protected_target_changed')
    return {'concrete_constraints_passed': not errors, 'errors': sorted(set(errors)),
            'semantic_review': 'parent_review_required_not_human_gold'}


async def run(args):
    dataset = ROOT / 'evals' / args.dataset
    manifest_path = dataset.with_suffix('.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else None
    data = {}
    files = [manifest_path] if manifest else []
    for part in ('inputs','fixtures','expectations'):
        path = (ROOT / 'evals' / manifest[part]) if manifest else dataset.with_suffix('.' + part + '.jsonl')
        files.append(path)
        data[part] = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    ids = [[r['case_id'] for r in data[part]] for part in ('inputs','fixtures','expectations')]
    if not ids[0] or ids[0] != ids[1] or ids[0] != ids[2]:
        raise ValueError('dataset_alignment_error')
    if any('decisions' not in row.get('expected', {}) for row in data['expectations']):
        raise ValueError('archived_regression_labels_are_not_current_probe_labels')
    selected = set(args.cases or ids[0])
    if not selected <= set(ids[0]):
        raise ValueError('unknown_case')
    for row in data['inputs']:
        Intake(row['input'])
    if not args.run:
        print(json.dumps({'mode':'plan_only','dataset':args.dataset,'cases':sorted(selected),
            'provider':args.provider,
            'compare_direct':args.compare_direct,'max_calls':args.max_calls,'calendar_writes':0,
            'baseline_scope':'single_decision_given_chain_evidence_not_end_to_end_retrieval'}))
        return
    key = openrouter_key(ROOT) if args.provider == 'openrouter' else None
    if args.provider == 'openrouter' and not key:
        raise ValueError('local_model_key_missing_no_fallback')
    folder = ROOT/'runs'/('product-probe-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir()
    for path in files:
        (folder/path.name).write_bytes(path.read_bytes())
    sources = [p for directory in ('threadplan','threadplan_host') for p in (ROOT/directory).rglob('*.py')] + [Path(__file__), ROOT/'threadplan/decision_policy.md']
    receipt = {'source_category':'constructed_probe_real_inference_simulated_calendar',
        'independent_human_gold':False,'provider':args.provider,
        'requested_model':DEFAULT_MODEL if args.provider == 'openrouter' else args.codex_model,
        'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'git_worktree_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),
        'max_model_calls':args.max_calls,'chain_max_attempts_per_case':3,
        'direct_baseline':args.compare_direct,'baseline_scope':'decision_given_same_retrieved_facts',
        'source_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        'dataset_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'direct_prompt_sha256':hashlib.sha256(DIRECT_PROMPT.encode()).hexdigest(),
        'calendar_writes':0,'status':'running','cases':[]}
    def save():
        (folder/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    save()
    timeout = 45 if args.provider == 'openrouter' else 120
    transport = (OpenRouterTransport(runs_dir=folder/'model',api_key=key,timeout_seconds=40)
                 if args.provider == 'openrouter' else CodexJSONSubscription(
                     model=args.codex_model,reasoning='low',runs_dir=folder/'model',timeout_seconds=110))
    budget = CallBudget(transport,args.max_calls)
    print(json.dumps({'run_directory':str(folder),'cases':len(selected),'max_calls':args.max_calls}),flush=True)
    labels = {r['case_id']:r['expected'] for r in data['expectations']}
    for inputs, fixture in zip(data['inputs'], data['fixtures']):
        case = inputs['case_id']
        if case not in selected:
            continue
        begin = time.monotonic()
        anchor = _aware_time(fixture['evaluation_now'])
        store = PlanningStore(folder/(case+'.sqlite'),clock=lambda:anchor+timedelta(seconds=time.monotonic()-begin))
        planner = ConversationPlanner(store,context=UpstreamContext(**fixture['context']),
            calendar_reader=SimulatedCalendarReader(fixture['calendar_fixture']),limits=PlanningLimits(
                max_model_calls=3,model_timeout_seconds=timeout,deadline_seconds=180 if args.provider == 'openrouter' else 420))
        before = budget.calls
        row = await planner.analyze(inputs['input'],budget)
        chain_seconds = time.monotonic()-begin
        traces = store.traces(row['id'])
        result = row.get('result') or {}
        chain = {'state':row['state'],'model_calls':budget.calls-before,'seconds':round(chain_seconds,3),
                 'error':row.get('error'),'score':score(result,labels[case])}
        if row['state'] != 'COMPLETED':
            chain['score']['concrete_constraints_passed'] = False
            chain['score']['errors'].append('analysis_failure')
        frozen = result.get('decision_input')
        if frozen is None:
            frozen = next((trace['input']['payload'] for trace in reversed(traces)
                           if trace['kind']=='MODEL' and trace['input']['stage']=='agent'), None)
        if frozen is None:
            frozen = planner.session.model_input(row['id'])
        payload = deepcopy(frozen)
        payload.pop('correction',None)
        payload['available_reads'] = []
        fact_keys = ('source_kind','messages','user','evaluation_now','time_policy','calendar_records','reads')
        fact_hash = fingerprint({k:frozen.get(k) for k in fact_keys})
        assert fact_hash == fingerprint({k:payload.get(k) for k in fact_keys})
        baseline = None
        if args.compare_direct:
            begin_direct = time.monotonic(); captured = {}; calls_before = budget.calls
            baseline = {'state':'FAILED','result':None,'error':None,'evidence_sha256':fact_hash}
            try:
                raw = await asyncio.wait_for(recorded_model_call(budget,captured,system_prompt=DIRECT_PROMPT,
                    input_json=canonical_json(payload),response_schema=model_schema()),timeout)
                candidate = decode(raw)
                work = deepcopy(row['work'])
                if not work.get('interpretation'):
                    work['interpretation'] = {'items':[]}
                # Use exactly the record snapshot visible to the chain's last decision.
                if row.get('result'):
                    work['records'] = deepcopy(result['records'])
                visible_refs = {record['event_ref'] for record in payload['calendar_records']}
                work['records'] = {ref:record for ref,record in work['records'].items() if ref in visible_refs}
                work['reads'] = deepcopy(payload['reads'])
                admission = resolve_candidate(candidate,work,planner.context)
                baseline['result'] = dict(admission,records=work['records'])
                baseline['state'] = 'COMPLETED' if not admission['item_errors'] else 'INVALID_OUTPUT'
                baseline['error'] = admission['item_errors'] or None
            except Exception as error:
                baseline['error'] = {'type':type(error).__name__,'message':str(error)[:300]}
            baseline.update(raw=captured.get('raw'),metadata=captured.get('metadata'),
                            model_calls=budget.calls-calls_before,seconds=round(time.monotonic()-begin_direct,3))
            baseline['score'] = score(baseline['result'] or {},labels[case])
            if baseline['state'] != 'COMPLETED':
                baseline['score']['concrete_constraints_passed'] = False
                baseline['score']['errors'].append('baseline_failure')
        detail = {'row':row,'traces':traces,'chain_score':chain['score'],'direct_payload':payload,'baseline':baseline}
        (folder/(case+'.json')).write_text(json.dumps(detail,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        entry = {'case_id':case,'chain':chain,'direct':{k:v for k,v in baseline.items()
                 if k not in ('result','raw','metadata')} if baseline else None,'evidence_sha256':fact_hash}
        receipt['cases'].append(entry);receipt['model_calls']=budget.calls;save()
        print(json.dumps(entry,ensure_ascii=False),flush=True)
    receipt['status']='complete_parent_semantic_review_pending';save()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--dataset',default='target_v1')
    parser.add_argument('--provider',choices=['openrouter','codex'])
    parser.add_argument('--codex-model',default='gpt-6.1-sol')
    parser.add_argument('--cases',nargs='+')
    parser.add_argument('--compare-direct',action='store_true')
    parser.add_argument('--max-calls',type=int,choices=range(1,25),default=24)
    args=parser.parse_args()
    if args.run and args.provider is None:
        parser.error('--run requires an explicit --provider codex or --provider openrouter')
    asyncio.run(run(args))
