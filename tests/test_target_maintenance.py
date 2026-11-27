import time
from collections import Counter
import pytest
from fastapi.testclient import TestClient
from apps.api import main
from apps.api.infrastructure.store import Store
from apps.api.modules.scoring import Scores
from apps.api.modules.scheduling import fair_order,record_dispatch

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'store',Store(tmp_path));monkeypatch.setattr(main,'REGISTRATION_CREDENTIALS',False)
    with main.store.transaction() as tx:tx.put('problems',dict(id='P1',title='Test',statement='True',status='open',created_at=1))
    with TestClient(main.app) as c:
        for name in ['Alice','Bob','Carol','Diana']:c.post('/api/v1/contributors/claim',json={'name':name})
        yield c

def insight(c,expression='True',**kwargs):
    b=dict(problem_id='P1',contributor_id='1',contributor_name='Alice',title='Claim',claim='A precise theorem',proof='Argument',proof_status='claimed_complete')
    if expression is not None:b['lean_target']=dict(expression=expression)
    b.update(kwargs);r=c.post('/api/v1/insights',json=b);assert r.status_code==200,r.text
    i=r.json()
    with main.store.transaction() as tx:
        t=tx.get('insight_targets',i['id'])
        if t:t['status']='valid';tx.put('insight_targets',t)
    return i

def audit(c,i,person,name,alignment='misaligned',target_hash=None):
    return c.post('/api/v1/insights/'+i['id']+'/lean-target/audits',json=dict(contributor_id=person,contributor_name=name,target_hash=target_hash or i['lean_target']['target_hash'],alignment=alignment,rationale='The quantifier domain omits the positive-length hypothesis.'))

def claim(c,person='2',name='Bob'):
    r=c.post('/api/v1/tasks/claim',json=dict(contributor_id=person,contributor_name=name,problem_id='P1',type='lean_target'))
    assert r.status_code==200,r.text
    return r.json()

def test_two_independent_flags_revoke_only_current_version_and_repair(client):
    i=insight(client)
    assert audit(client,i,'1','Alice').status_code==403
    assert audit(client,i,'2','Bob',target_hash='0'*64).status_code==409
    assert audit(client,i,'2','Bob').status_code==200
    assert audit(client,i,'2','Bob').status_code==409
    assert client.get('/api/v1/insights/'+i['id']+'/lean-target').json()['status']=='valid'
    r=audit(client,i,'3','Carol');assert r.status_code==200,r.text
    assert client.get('/api/v1/insights/'+i['id']+'/lean-target').json()['status']=='revoked'
    task=claim(client)
    assert task['task'].get('mode')!='alignment'
    data=dict(contributor_id='2',contributor_name='Bob',task_id=task['task']['id'],claim_token=task['claim_token'],targets=[dict(insight_id=i['id'],lean_target=dict(expression='1 = 1'))])
    r=client.post('/api/v1/lean-targets/batch',json=data);assert r.status_code==200,r.text
    new=r.json()['targets'][0];assert new['target_hash']!=i['lean_target']['target_hash']
    with main.store.transaction() as tx:
        new['status']='valid';tx.put('insight_targets',new)
        assert tx.get('insights',i['id']) and tx.all('target_versions')
    assert audit(client,i,'3','Carol',target_hash=new['target_hash']).status_code==200
    assert client.get('/api/v1/insights/'+i['id']+'/lean-target').json()['status']=='valid'

def test_alignment_batch_is_separate_from_math_review_and_earns_batch_credit(client):
    i=insight(client);t=claim(client)
    assert t['task']['mode']=='alignment'
    assert t['research_package']['submission']['url']=='/api/v1/lean-targets/audits/batch'
    data=dict(contributor_id='2',contributor_name='Bob',task_id=t['task']['id'],claim_token=t['claim_token'],audits=[dict(insight_id=i['id'],target_hash=i['lean_target']['target_hash'],alignment='aligned',rationale='All domains, premises and conclusions correspond exactly.')])
    r=client.post('/api/v1/lean-targets/audits/batch',json=data);assert r.status_code==200,r.text
    assert client.get('/api/v1/contributors/2/score').json()['score']==8
    with main.store.transaction() as tx:assert tx.all('reviews')==[]
    assert client.post('/api/v1/lean-targets/audits/batch',json=data).status_code==409

def test_missing_targets_are_ordinary_work_without_migration(client):
    i=insight(client,None);t=claim(client)
    assert t['task']['target_ids']==[i['id']]
    assert client.get('/api/v1/platform-status').json()['phase']=='normal'
    assert t['research_package']['materials'][0]['lean_target'] is None
    assert client.post('/api/v1/tasks/claim',json=dict(contributor_id='3',contributor_name='Carol',type='insight_explore')).status_code==200

def test_revocation_invalidates_definition_and_proof_descendants(client):
    i=insight(client)
    child=insight(client,'True',title='Child',references=[i['id']])
    with main.store.transaction() as tx:
        t=tx.get('insight_targets',child['id']);t['insight_dependencies']=[i['id']];tx.put('insight_targets',t)
        tx.put('lean_artifacts',dict(id='Lroot',insight_id=i['id'],problem_id='P1',verification_id='Vroot',dependencies=[],created_at=1,contributor_id='1'))
        tx.put('lean_artifacts',dict(id='Lchild',insight_id=child['id'],problem_id='P1',verification_id='Vchild',dependencies=['Lroot'],created_at=2,contributor_id='1'))
        tx.put('verifications',dict(id='Vroot',artifact_id='Lroot',status='passed'))
        tx.put('verifications',dict(id='Vchild',artifact_id='Lchild',status='running'))
    assert audit(client,i,'2','Bob').status_code==200
    assert audit(client,i,'3','Carol').status_code==200
    with main.store.transaction() as tx:
        assert tx.get('insight_targets',child['id'])['status']=='stale'
        assert tx.get('verifications','Vroot')['target_obsolete']
        assert tx.get('verifications','Vchild')['status']=='cancelled'
        assert len(tx.all('target_versions'))==2

def test_weighted_dispatch_interleaves_all_five_types_and_target_modes(client):
    options=[dict(id='T'+str(n),problem_id='P1',type=kind,mode=mode,created_at=n) for n,(kind,mode) in enumerate([('insight_explore',None),('lean_target','register'),('lean_target','alignment'),('lean_work','formalize'),('review',None),('research_synthesis',None)])]
    counts=Counter()
    with main.store.transaction() as tx:
        for _ in range(100):
            selected=fair_order(tx,options,{})[0];record_dispatch(tx,selected,options);counts[selected['type']]+=1
    assert counts==dict(insight_explore=40,lean_target=25,lean_work=20,review=10,research_synthesis=5)


def test_automatic_api_dispatch_offers_all_five_types_and_correct_schemas(client):
    for n in range(15):insight(client,title='Registered '+str(n))
    insight(client,None,title='Missing target')
    seen=set();modes=set()
    for _ in range(40):
        r=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Bob',problem_id='P1'))
        assert r.status_code==200,r.text
        result=r.json();t=result['task'];seen.add(t['type'])
        if t['type']=='lean_target':
            modes.add(t.get('mode','register'))
            assert result['research_package']['submission']['url'] in ('/api/v1/lean-targets/batch','/api/v1/lean-targets/audits/batch')
        if t['type']=='review':
            props=result['research_package']['submission']['schema']['properties']['reviews']['items']['properties']
            assert 'target_alignment' not in props and 'target_hash' not in props
    assert seen=={'insight_explore','lean_target','lean_work','review','research_synthesis'}
    assert modes=={'register','alignment'}


def test_target_check_cannot_resurrect_invalidated_inflight_version(client,monkeypatch):
    from apps.worker import main as worker
    i=insight(client)
    with main.store.transaction() as tx:
        t=tx.get('insight_targets',i['id']);t['status']='queued';tx.put('insight_targets',t)
    monkeypatch.setattr(worker,'STORE',main.store)
    def invalidated_during_check(target,artifacts):
        with main.store.transaction() as tx:
            t=tx.get('insight_targets',target['id']);t['status']='stale';tx.put('insight_targets',t)
        return dict(status='valid',log='The kernel returned after the dependency was revoked')
    monkeypatch.setattr(worker,'verify_target',invalidated_during_check)
    assert worker.check_next_target()
    with main.store.transaction() as tx:assert tx.get('insight_targets',i['id'])['status']=='stale'


def test_explicit_task_id_does_not_reset_automatic_dispatch_credits(client):
    insight(client,None)
    t=client.get('/api/v1/tasks?problem_id=P1&type=insight_explore&status=available').json()['items'][0]
    with main.store.transaction() as tx:tx.put('dispatch_credits',dict(id='P1',credits={'review':7,'insight_explore':-7}))
    r=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Bob',task_id=t['id']))
    assert r.status_code==200,r.text
    with main.store.transaction() as tx:assert tx.get('dispatch_credits','P1')['credits']=={'review':7,'insight_explore':-7}
