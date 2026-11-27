import time
from test_api import client,insight
from apps.api import main
from apps.worker import main as worker


def setup(client,monkeypatch):
    i=client.post('/api/v1/insights',json=insight()).json()
    r=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',problem_id='P001',type='lean_work'))
    assert r.status_code==200,r.text
    c=r.json();assert 1199<=c['task']['expires_at']-c['task']['claimed_at']<=1201
    monkeypatch.setattr(worker,'STORE',main.store)
    return i,c

def payload(i,c,name):return dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],task_id=c['task']['id'],claim_token=c['claim_token'],source='theorem '+name+' : True := True.intro',theorem=name)


def test_failed_proof_retries_same_lease_with_logs_until_pass(client,monkeypatch):
    i,c=setup(client,monkeypatch)
    r=client.post('/api/v1/lean/artifacts',json=payload(i,c,'first'));assert r.status_code==200,r.text
    assert r.json()['task_feedback_url'].endswith('/proof-feedback')
    assert client.post('/api/v1/lean/artifacts',json=payload(i,c,'premature')).status_code==409
    monkeypatch.setattr(worker,'verify_artifact',lambda *a:dict(status='failed',exit_code=1,log='Type mismatch: inspect the theorem arguments'))
    assert worker.once()
    feedback=client.get(r.json()['task_feedback_url']).json()
    assert feedback['status']=='claimed' and feedback['can_retry']
    assert feedback['attempts'][0]['exit_code']==1 and 'Type mismatch' in feedback['attempts'][0]['log']
    r=client.post('/api/v1/lean/artifacts',json=payload(i,c,'fixed'));assert r.status_code==200,r.text
    monkeypatch.setattr(worker,'verify_artifact',lambda a,*rest:dict(status='passed',target_matched=True,target_hash=a['target_hash'],log='Kernel checked'))
    assert worker.once()
    feedback=client.get(r.json()['task_feedback_url']).json()
    assert feedback['status']=='submitted' and not feedback['can_retry'] and len(feedback['attempts'])==2
    assert client.get('/api/v1/contributors/2/score').json()['score']==8


def test_lease_expiry_blocks_retry_and_renewal_uses_twenty_minutes(client,monkeypatch):
    i,c=setup(client,monkeypatch)
    r=client.post('/api/v1/tasks/'+c['task']['id']+'/renew',json=dict(claim_token=c['claim_token']))
    assert 1198<r.json()['expires_at']-time.time()<=1200
    with main.store.transaction() as tx:
        t=tx.get('tasks',c['task']['id']);t['expires_at']=time.time()-1;tx.put('tasks',t)
    assert client.post('/api/v1/lean/artifacts',json=payload(i,c,'late')).status_code==409
    with main.store.transaction() as tx:assert tx.all('lean_artifacts')==[]
