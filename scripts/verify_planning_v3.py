"""Opt-in constructed Chinese v0.3 discovery/execution.

Default is plan-only. Two marked events in private ThreadPlan Test only. No
invitations/reminders, automatic model fallback or production calendar writes.
Expected constraints stay in this harness, never in model inputs. This is a
small developer-defined acceptance experiment, not independent user accuracy.
"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import subprocess
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from threadplan import Proposal, UpstreamContext
from threadplan.openrouter_transport import DEFAULT_MODEL, OpenRouterTransport
from threadplan.codex_transport import CodexJSONSubscription
from threadplan.codec import _aware_time
from threadplan.adapters.feishu import TEST_PREFIX, _time
from threadplan_host import ConversationPlanner, PlanningExecutor, PlanningLimits, PlanningStore
from threadplan_host.feishu import FeishuHost
from scripts.feishu_test_support import open_test_host, check_test_calendar
from threadplan_host.model_config import openrouter_key
from threadplan_host.planning_feishu import FeishuPlanningReader


class CallBudget:
    """Explicit total call ceiling; a refused call never reuses old metadata."""
    def __init__(self,transport,limit):
        self.transport,self.limit,self.calls=transport,limit,0
        self._record_valid=False

    @property
    def last_record(self):
        return self.transport.last_record if self._record_valid else None

    async def __call__(self,**kwargs):
        self._record_valid=False
        if self.calls>=self.limit:
            raise RuntimeError('acceptance_model_call_budget_exhausted')
        self.calls+=1
        try:
            return await self.transport(**kwargs)
        finally:
            self._record_valid=True


def score(operations,items,expected,records,target):
    errors=[]
    if expected is None:
        if operations or not items or any(item.get('decision')!='NO_ACTION' for item in items):errors.append('expected_no_action')
    elif len(operations)!=1 or operations[0]['kind']!=expected['kind']:
        errors.append('wrong_operation')
    else:
        op=operations[0]
        if records.get(op.get('event_ref'),{}).get('id')!=target:errors.append('wrong_target')
        for field,value in expected.get('changes',{}).items():
            actual=op['changes'].get(field)
            if (_aware_time(actual)!=_aware_time(value)) if field.endswith('_at') else actual!=value:
                errors.append('wrong_field:'+field)
        allowed_extra={'title'} if expected['kind']=='CREATE' else set()
        if set(op['changes'])-set(expected.get('changes',{}))-{'participants'}-allowed_extra:errors.append('unrequested_field_change')
    return {'passed':not errors,'errors':errors}


async def run(args):
    key=openrouter_key(ROOT) if args.provider=='openrouter' else None
    if args.provider=='openrouter' and not key:raise ValueError('local_model_key_missing_no_fallback')
    if datetime.now(timezone.utc)>=datetime(2026,11,20,6,tzinfo=timezone.utc):raise ValueError('frozen_dates_have_passed')
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder=ROOT/'runs'/('planning-v3-'+run_id);folder.mkdir()
    receipt={'source_category':'constructed_chinese_real_model_and_test_calendar',
             'independent_semantic_gold':False,'status':'started','model_call_limit':args.max_model_calls,
             'model':DEFAULT_MODEL if args.provider=='openrouter' else args.codex_model,
             'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
             'git_worktree_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),
             'explicit_provider':args.provider,'scenario':args.scenario,'steps':[],'seed_events':[],'cleanup':[]}
    def save():
        (folder/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    backend=None;budget=None
    save()
    try:
        backend=open_test_host(db_path=folder/'provider.sqlite')
        backend.snapshot()  # verifies the existing private test connection only
        context=UpstreamContext('me','Asia/Shanghai','threadplan-test','threadplan-managed-test-events',
                                namespace='constructed-v3-acceptance')
        reader=FeishuPlanningReader(backend.calendar,connection_ref=context.connection_ref,scope_ref=context.scope_ref)
        store=PlanningStore(folder/'planning.sqlite')
        limits=PlanningLimits() if args.provider=='openrouter' else PlanningLimits(model_timeout_seconds=120,deadline_seconds=420)
        planner=ConversationPlanner(store,context=context,calendar_reader=reader,limits=limits)
        executor=PlanningExecutor(store,context=context,backend=backend)
        transport=(OpenRouterTransport(api_key=key,model=DEFAULT_MODEL,runs_dir=folder/'model',max_tokens=1024,timeout_seconds=35)
                   if args.provider=='openrouter' else CodexJSONSubscription(model=args.codex_model,reasoning='low',runs_dir=folder/'model'))
        budget=CallBudget(transport,args.max_model_calls)
        north='ThreadPlan Test Cafe North '+run_id[-12:]
        south='ThreadPlan Test Cafe South '+run_id[-12:]
        seeds=[
            ('target','聊一下','14:00','14:30',north),
            ('distractor','方案评审','16:00','16:30',south)]
        if args.scenario=='lifecycle':seeds=seeds[1:]
        if args.scenario=='mixed':
            seeds=[('lunch','午饭','12:20','13:00',north),
                   ('distractor','设备采购例会','16:10','16:40',south)]
        protected={}
        for role,title,start,end,location in seeds:
            proposal=Proposal('CREATE','confirmed',('harness-seed',),{'title':title,
                'start_at':'2026-11-20T'+start+':00+08:00','end_at':'2026-11-20T'+end+':00+08:00','location':location})
            if args.scenario=='existing':
                # Create outside the execution ledger, as an upstream/manual
                # calendar would. No managed-by marker or ownership claim.
                receipt['external_seed_in_flight']={'role':role,'reason':'native_create_started_without_returned_id'}
                save()
                check_test_calendar(backend.calendar.get_calendar(approved=True))
                created=backend.calendar.cli.invoke('events','create',params={
                    'calendar_id':backend.calendar.calendar_id,
                    'idempotency_key':str(uuid.uuid5(uuid.NAMESPACE_URL,run_id+role))},data={
                    'summary':TEST_PREFIX+title,'description':'方案评审；请带预算表。' if role=='target' else '另一个小组的评审。',
                    'start_time':_time(proposal.changes['start_at']),'end_time':_time(proposal.changes['end_at']),
                    'location':{'name':location},'visibility':'private','free_busy_status':'free',
                    'need_notification':False,'reminders':[],'vchat':{'vc_type':'no_meeting'},'attendee_ability':'none'},
                    approved=True)['event']
                if not isinstance(created.get('event_id'),str) or not created['event_id']:
                    raise ValueError('external_seed_missing_native_id_cleanup_unresolved')
            else:
                created=backend.execute(proposal,'v3-seed:'+run_id+':'+role,{},approved=True)
            receipt['seed_events'].append({'role':role,'event_id':created['event_id'],'constructed_harness_seed':True,
                                          'created_outside_execution_ledger':args.scenario=='existing'})
            if args.scenario=='mixed':protected[created['event_id']]=deepcopy(created['event']['fields'])
            receipt.pop('external_seed_in_flight',None)
            save()
        target=next((item['event_id'] for item in receipt['seed_events'] if item['role']=='target'),None)
        conversations=[
            ('accepted-change',f'2026年11月20日下午两点在{north}的方案评审，改到下午三点，时长不变，可以吗？',
             '同意，只改时间，地点和其他内容都不变。',{'kind':'UPDATE','changes':{'start_at':'2026-11-20T15:00:00+08:00','end_at':'2026-11-20T15:30:00+08:00'}}),
            ('unaccepted-change','那方案评审要不要再改到下午四点？','暂时不改，保留原来的安排。',None),
            ('cancel',f'2026年11月20日我们在{north}的方案评审整场取消，不再举行。','收到，取消这个安排。',{'kind':'CANCEL','changes':{}})
        ]
        if args.scenario=='lifecycle':
            conversations=[
                ('create',f'2026年11月20日14:00到14:30在{north}做方案评审，咱们约这个时间吧。',
                 '同意，帮我记进日历，标题就叫聊一下。',{'kind':'CREATE','changes':{'title':'聊一下',
                 'start_at':'2026-11-20T14:00:00+08:00','end_at':'2026-11-20T14:30:00+08:00','location':north}}),
                conversations[0],conversations[2]]
        elif args.scenario=='existing':
            conversations=[conversations[0],conversations[2]]
        elif args.scenario=='cancel-only':
            conversations=conversations[-1:]
        messages=[]
        # Freeze full constructed input/expectations before actual model calls.
        inputs=[];expectations=[]
        for index,(case,first,second,expected) in enumerate(conversations[:args.cases]):
            for sender,text in [('alex',first),('me',second)]:
                messages.append({'id':'m'+str(len(messages)+1),'sender_id':sender,'text':text,
                                 'sent_at':datetime.now(timezone.utc).isoformat()})
            inputs.append({'schema_version':'0.3','request_id':run_id+':'+case,
                'source':{'kind':'conversation','provider':'constructed-v3-acceptance','conversation_id':run_id},'messages':deepcopy(messages)})
            expectations.append({'case_id':case,'expected':expected,'target_event_id':target})
        if args.scenario=='mixed':
            lines=[('lin',f'2026年11月20日中午12点到12:40一起吃饭，还是{north}？'),
                   ('me','好。'),('feng','2026年11月20日下午16:10到16:40线上过票据上传的新需求。'),
                   ('me','可以，下午帮我记上，备注写：先看手机端，带流程图'),
                   ('lin','中午我来不及，改12:20到13点。'),('me','行，中午12:20再见。'),('feng','下午不改哈。')]
            messages=[{'id':'m'+str(i+1),'sender_id':sender,'text':text,'sent_at':datetime.now(timezone.utc).isoformat()}
                      for i,(sender,text) in enumerate(lines)]
            for index,parent in ((3,'m3'),(4,'m1'),(5,'m5'),(6,'m3')):messages[index]['reply_to']=parent
            inputs=[{'schema_version':'0.3','request_id':run_id+':mixed',
                     'source':{'kind':'conversation','provider':'constructed-v3-acceptance','conversation_id':run_id},'messages':messages}]
            expectations=[{'case_id':'mixed','target_event_id':None,'expected':{'kind':'CREATE','changes':{
                'start_at':'2026-11-20T16:10:00+08:00','end_at':'2026-11-20T16:40:00+08:00',
                'location':'线上','description':'先看手机端，带流程图'}}}]
        (folder/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False,indent=2),encoding='utf-8')
        (folder/'expectations.json').write_text(json.dumps(expectations,ensure_ascii=False,indent=2),encoding='utf-8')
        for data,labels in zip(inputs,expectations):
            case=labels['case_id']
            row=await planner.analyze(data,budget)
            # Longer rate limits are explicit continuations, never a model fallback.
            if row['state']=='FAILED' and row.get('error',{}).get('not_before'):
                raise ValueError('acceptance_rate_limited_receipt_preserved')
            result=row.get('result') or {}
            a=score(result.get('operations',[]),result.get('items',[]),labels['expected'],result.get('records',{}),target)
            if row['state']!='COMPLETED':a={'passed':False,'errors':[str(row.get('error'))]}
            step={'case_id':case,'request_key':row['id'],'analysis_state':row['state'],'A':a,
                  'model_calls_so_far':budget.calls,'execution':None}
            receipt['steps'].append(step);save()
            save()
            if not a['passed']:
                raise ValueError('acceptance_A_declared_constraint_failed:'+case)
            if labels['expected'] is not None:
                command=executor.prepare(row['id'],0,expected_fingerprint=result['plan_fingerprint'],approved=True)
                done=executor.execute(command['id'],approved=True)
                if labels['expected']['kind']=='CREATE':
                    target=done.get('result',{}).get('event_id')
                    step['verified_created_target_id']=target
                if done['state']!='SUCCEEDED' or not target or done['result']['event_id']!=target:
                    raise ValueError('actual_execution_not_verified')
                again=executor.execute(command['id'],approved=True)
                if not again.get('already_applied'):raise ValueError('duplicate_receipt_not_reused')
                step['execution']={'state':done['state'],'event_id':done['result']['event_id'],'duplicate_reused':True}
                if protected:
                    current,_=backend.snapshot(event_ids=list(protected))
                    actual={event.id:event.fields for event in current}
                    if set(actual)!=set(protected) or any(not executor._same_fields(fields,actual[identifier])
                                                         for identifier,fields in protected.items()):
                        raise ValueError('protected_seed_event_changed')
                    step['protected_seed_events_unchanged']=list(protected)
            step['model_calls_so_far']=budget.calls
            save()
            print(json.dumps({'case_id':case,'A':a,'calls':budget.calls},ensure_ascii=False),flush=True)
        receipt['status']='passed'
        native_counts=[trace.get('metadata',{}).get('native_cli_invocations') for step in receipt['steps']
            for trace in store.traces(step['request_key']) if trace['kind']=='READ']
        receipt['known_native_cli_read_invocations']=sum(value for value in native_counts if value is not None)
        receipt['unmeasured_read_callbacks']=sum(value is None for value in native_counts)
        receipt['A_passed']=sum(step['A']['passed'] for step in receipt['steps'])
    except Exception as error:
        receipt.update(status='failed',failure={'type':type(error).__name__,'message':str(error)[:500]})
    finally:
        if receipt.get('external_seed_in_flight'):
            receipt['cleanup'].append(dict(receipt['external_seed_in_flight'],pending=True))
        if backend is not None:
            with backend.connect() as db:
                unknown_creates=db.execute("SELECT approval_key,result FROM receipts WHERE operation='CREATE' AND state IN ('in_progress','outcome_unknown')").fetchall()
            for unknown in unknown_creates:
                known=json.loads(unknown['result']) if unknown['result'] else {}
                if not known.get('event_id'):
                    receipt['cleanup'].append({'pending':True,'reason':'unknown_create_without_returned_id',
                                               'approval_key':unknown['approval_key']})
            try:
                events,baselines=backend.snapshot()
                if args.scenario=='existing':
                    with backend.connect() as db:
                        tracked={r[0] for r in db.execute('SELECT event_id FROM managed_events UNION SELECT event_id FROM authorized_events')}
                    for seed in receipt['seed_events']:
                        if seed['event_id'] not in tracked:
                            extra,raw=backend.snapshot(event_ids=[seed['event_id']])
                            events.extend(extra);baselines.update(raw)
                for event in events:
                    try:
                        backend.execute(Proposal('CANCEL','cancelled',('harness-cleanup',),{},event.id),
                            'v3-cleanup:'+run_id+':'+event.id,baselines,approved=True)
                        receipt['cleanup'].append({'event_id':event.id,'cancelled_by_harness':True,'model_inferred':False})
                    except Exception as error:
                        receipt['cleanup'].append({'event_id':event.id,'pending':True,'error_type':type(error).__name__})
            except Exception as error:
                receipt['cleanup'].append({'pending':True,'error_type':type(error).__name__})
        receipt['model_calls']=budget.calls if budget else 0
        receipt['A_passed']=sum(step['A']['passed'] for step in receipt['steps'])
        receipt['cleanup_pending']=any(item.get('pending') for item in receipt['cleanup'])
        save()
    print(json.dumps({'status':receipt['status'],'model_calls':receipt['model_calls'],
        'cleanup_pending':receipt['cleanup_pending'],'A_passed':receipt.get('A_passed'),
        'directory':str(folder)},ensure_ascii=False))
    return 0 if receipt['status']=='passed' and not receipt['cleanup_pending'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--scenario',choices=['discovery','lifecycle','cancel-only','existing','mixed'],default='discovery')
    parser.add_argument('--max-model-calls',type=int,choices=range(3,11),default=10)
    parser.add_argument('--provider',choices=['openrouter','codex'])
    parser.add_argument('--codex-model',default='gpt-6.1-sol')
    parser.add_argument('--cases',type=int,choices=range(1,4),default=3)
    args=parser.parse_args()
    if args.run and args.provider is None:
        parser.error('--run requires an explicit --provider codex or --provider openrouter')
    if not args.run:
        print(f'Plan only: scenario={args.scenario}; marked private Test events; max {args.max_model_calls} model calls; cleanup; no invitations/reminders.')
    else:raise SystemExit(asyncio.run(run(args)))
