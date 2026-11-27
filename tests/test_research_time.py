from test_api import client,insight
from apps.api import main
from apps.api.modules import core


def test_early_insight_has_prompt_and_no_writes_then_succeeds(client,monkeypatch):
    monkeypatch.setattr(core,'INSIGHT_MIN_SECONDS',180)
    c=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',type='insight_explore')).json()
    body=dict(insight(),task_id=c['task']['id'],claim_token=c['claim_token'])
    r=client.post('/api/v1/insights',json=body)
    assert r.status_code==429
    detail=r.json()['detail'];assert 1<=detail['retry_after_seconds']<=180
    assert 'Fetch relevant papers' in detail['research_prompt'] and r.headers['Retry-After']
    with main.store.transaction() as tx:
        assert not tx.all('insights') and not tx.all('contributions')
        t=tx.get('tasks',c['task']['id']);assert t['status']=='claimed';t['claimed_at']-=181;tx.put('tasks',t)
    assert client.post('/api/v1/insights',json=body).status_code==200


def test_research_delay_cannot_be_bypassed_and_renewal_preserves_start(client,monkeypatch):
    monkeypatch.setattr(core,'INSIGHT_MIN_SECONDS',180)
    assert client.post('/api/v1/insights',json=insight()).status_code==422
    c=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',type='insight_explore')).json()
    with main.store.transaction() as tx:
        t=tx.get('tasks',c['task']['id']);t['claimed_at']-=181;tx.put('tasks',t);start=t['claimed_at']
    client.post('/api/v1/tasks/'+t['id']+'/renew',json=dict(claim_token=c['claim_token']))
    with main.store.transaction() as tx:assert tx.get('tasks',t['id'])['claimed_at']==start
    body=dict(insight(),task_id=t['id'],claim_token='wrong')
    assert client.post('/api/v1/insights',json=body).status_code==403
    body['claim_token']=c['claim_token'];assert client.post('/api/v1/insights',json=body).status_code==200
