"""Relocated artifact exercises real runtime with an explicitly fake provider.

No service I/O or semantic accuracy claims. No source repository on sys.path.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts.build_skill import build


DRIVER = r'''
import asyncio, io, json, sys
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from threadplan.adapters import feishu
class ExplicitFakeCLI:
    events = {}
    calls = []
    def invoke(self, resource, method, *, params=None, data=None, approved=False):
        assert approved is True
        self.calls.append((resource, method))
        if resource == 'calendars':
            return dict(calendar_id='portable-real-id',summary='Another User Calendar',type='primary',role='owner')
        if method == 'instance_view': return {'items':list(deepcopy(self.events).values())}
        if method == 'create':
            self.events['provider-event-1']=dict(deepcopy(data),event_id='provider-event-1',status='confirmed')
            return {'event':deepcopy(self.events['provider-event-1'])}
        if method == 'get': return {'event':deepcopy(self.events[params['event_id']])}
        if method == 'patch':
            self.events[params['event_id']].update(deepcopy(data))
            return {'event':deepcopy(self.events[params['event_id']])}
        if method == 'delete':
            self.events[params['event_id']]['status']='cancelled'
            return {}
        raise AssertionError((resource,method))
feishu.LarkCLI=ExplicitFakeCLI
from threadplan_host import cli
config=Path(sys.argv[2])
base=['--config',str(config)]
def call(*args,payload=None):
    args=list(args)
    if payload is not None:
        path=config.parent/'payload.json'
        path.write_text(json.dumps(payload),encoding='utf-8')
        args.extend(['--query' if args[0]=='read' else '--input',str(path)])
    output=io.StringIO()
    with redirect_stdout(output): code=cli.main(base+args,host_only=True)
    body=json.loads(output.getvalue())
    assert code==0,body
    return body['result']
call('configure','--user-id','actual-source-person','--calendar-id','portable-real-id','--timezone','UTC')
cfg=json.loads(config.read_text(encoding='utf-8'))
assert cfg['execution_policy']=={'mode':'read_only'}
assert not (config.parent/'threadplan-state').exists()
with redirect_stdout(io.StringIO()):
    assert cli.main(base+['configure','--user-id','other','--calendar-id','other','--timezone','UTC'],host_only=True)==1
assert json.loads(config.read_text(encoding='utf-8'))==cfg
cfg['execution_policy']={'mode':'preauthorized','allowed_operations':['CREATE','UPDATE','CANCEL']}
config.write_text(json.dumps(cfg),encoding='utf-8')  # Explicit engineering-test host grant.
for n,kind in enumerate(('CREATE','UPDATE','CANCEL')):
    source={'schema_version':'0.3','request_id':f'portable-{n}',
        'source':{'kind':'personal_note','provider':'explicit-engineering-fixture'},
        'messages':[{'id':f'original-{n}','sender_id':'actual-source-person',
            'sent_at':datetime.now(timezone.utc).isoformat(),'text':'Constructed transport contract '+kind}]}
    key=call('open',payload=source)['request_key']
    view=call('read','--request',key,'--activity','a',payload={'kind':'find_events',
        'range':{'start_at':'2026-11-20T00:00:00+00:00','end_at':'2026-11-21T00:00:00+00:00'}})
    if kind=='CREATE':
        operation={'kind':kind,'fields':{'title':'User title','start_at':'2026-11-20T10:00:00+00:00',
            'duration_minutes':30,'description':'User notes'}}
    else:
        ref=view['calendar_records'][0]['event_ref']
        call('read','--request',key,'--activity','a',payload={'kind':'get_events','event_refs':[ref]})
        operation={'kind':kind,'event_ref':ref}
        if kind=='UPDATE': operation['changes']={'description':'Changed user notes'}
    plan=call('propose','--request',key,payload={'items':[{'activity_ref':'a','decision':'PROPOSE',
        'evidence_message_ids':[f'original-{n}'],'reason':'Constructed contract','operation':operation}]})['plan']
    assert not plan['item_errors'],plan
    prepared=call('prepare','--request',key,'--fingerprint',plan['plan_fingerprint'])
    assert not prepared['errors'],prepared
    command=prepared['commands'][0]['id']
    result=call('execute','--command-id',command)
    assert result['state']=='SUCCEEDED',result
    assert call('execute','--command-id',command)['already_applied'] is True
    assert call('recover','--command-id',command)['state']=='SUCCEEDED'
raw=ExplicitFakeCLI.events['provider-event-1']
assert raw['summary']=='User title' and raw['description']=='Changed user notes',raw
assert raw['status']=='cancelled'
assert [m for r,m in ExplicitFakeCLI.calls if m in ('create','patch','delete')]==['create','patch','delete']
print(json.dumps({'offline_contract':'passed','model_calls':0,'remote_calls':0,'verified_operations':3}))
'''


class PortableSkillChecks(unittest.TestCase):
    def test_relocated_end_to_end_contract_and_package_content(self):
        with tempfile.TemporaryDirectory(prefix="技能 portable ") as temporary:
            root=Path(temporary)
            build(root/"bundle.zip")
            with zipfile.ZipFile(root/"bundle.zip") as archive:
                archive.extractall(root/"另一个宿主")
            skill=root/"另一个宿主/threadplan"
            for path in skill.rglob("*"):
                if path.is_file():
                    content=path.read_text(encoding="utf-8")
                    for personal in ("C:/Users/", "C:\\Users\\", "D:/F/", "D:\\F\\", "FicO", "f-workspace", "data/feishu_test"):
                        self.assertNotIn(personal,content,str(path))
            # Test-only restrictions can survive only as explicit historical data encoding.
            for path in (skill/"SKILL.md",skill/"references/setup.md",skill/"references/config.example.json"):
                self.assertNotIn("ThreadPlan Test",path.read_text(encoding="utf-8"))
            driver=root/"isolated_driver.py"
            driver.write_text(DRIVER,encoding="utf-8")
            environment=dict(os.environ)
            environment.pop("PYTHONPATH",None)
            environment.pop("THREADPLAN_CONFIG",None)
            result=subprocess.run([sys.executable,"-B","-X","utf8",str(driver),
                str(skill/"scripts/runtime"),str(root/"使用者配置/config.json")],
                cwd=root,env=environment,capture_output=True,text=True,encoding="utf-8",timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout)["verified_operations"],3)


if __name__ == "__main__":
    unittest.main()
