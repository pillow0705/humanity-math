import time
import pytest
from fastapi.testclient import TestClient
from apps.api import main
from apps.api.infrastructure.store import Store
from apps.api.modules.scoring import Scores
from apps.api.modules.migration import progress

@pytest.fixture
def client(tmp_path,monkeypatch):
    store=Store(tmp_path);monkeypatch.setattr(main,'store',store);monkeypatch.setattr(main,'REGISTRATION_CREDENTIALS',False)
    with store.transaction() as tx:
        tx.put('problems',dict(id='P1',title='Test',statement='True',status='open',created_at=1))
        for n in range(5):tx.put('insights',dict(id='I'+str(n),problem_id='P1',contributor_id='99',contributor_name='Original',title='Claim '+str(n),claim='True',proof='Introduction',proof_status='claimed_complete',references=[],created_at=n,gaps=[]))
        tx.put('platform_states',dict(id='lean_target_migration',phase='target_backfill',required_insight_ids=['I'+str(n) for n in range(5)]))
    with TestClient(main.app) as c:
        c.post('/api/v1/contributors/claim',json=dict(name='Alice'));c.post('/api/v1/contributors/claim',json=dict(name='Bob'));yield c

def claim(c,person='1',name='Alice'):
    r=c.post('/api/v1/tasks/claim',json=dict(contributor_id=person,contributor_name=name))
    assert r.status_code==200,r.text
    return r.json()

def body(c,person='1',name='Alice'):
    return dict(contributor_id=person,contributor_name=name,task_id=c['task']['id'],claim_token=c['claim_token'],targets=[dict(insight_id=i,lean_target=dict(expression='True')) for i in c['task']['target_ids']])

def validate_targets():
    with main.store.transaction() as tx:
        for t in tx.all('insight_targets'):t['status']='valid';tx.put('insight_targets',t)
        return progress(tx)

def test_exclusive_five_item_batches_reservations_rewards_and_resume(client):
    with main.store.transaction() as tx:
        for n in (5,6):
            i=dict(tx.get('insights','I0'),id='I'+str(n),created_at=n)
            tx.put('insights',i)
        state=tx.get('platform_states','lean_target_migration')
        state['required_insight_ids']+=['I5','I6'];tx.put('platform_states',state)
    first=claim(client);second=claim(client,'2','Bob')
    assert first['task']['type']=='lean_target' and len(first['task']['target_ids'])==5
    assert len(first['research_package']['materials'])==5
    assert first['research_package']['submission']['url']=='/api/v1/lean-targets/batch'
    assert len(second['task']['target_ids'])==2
    assert set(first['task']['target_ids']).isdisjoint(second['task']['target_ids'])
    assert client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',type='insight_explore')).status_code==423
    assert client.post('/api/v1/insights',json=dict(contributor_id='1',contributor_name='Alice',problem_id='P1',title='Forbidden',claim='True',proof='Intro',proof_status='claimed_complete',lean_target=dict(expression='True'))).status_code==423
    assert client.post('/api/v1/lean-targets/batch',json=body(first)).status_code==200
    assert client.get('/api/v1/contributors/1/score').json()['score']==0
    assert validate_targets()['phase']=='target_backfill'
    assert client.get('/api/v1/contributors/1/score').json()['score']==8
    assert client.post('/api/v1/lean-targets/batch',json=body(second,'2','Bob')).status_code==200
    assert validate_targets()['phase']=='normal'
    assert client.get('/api/v1/contributors/2/score').json()['score']==8
    assert client.get('/api/v1/platform-status').json()['remaining']==0
    resumed=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',type='insight_explore'))
    assert resumed.status_code==200 and resumed.json()['task']['type']=='insight_explore'
    with main.store.transaction() as tx:assert all(i['contributor_id']=='99' for i in tx.all('insights'))

def test_batch_failure_is_atomic_and_lease_is_enforced(client):
    c=claim(client);data=body(c);data['targets'][1]['lean_target']['definitions']='axiom bad : False'
    assert client.post('/api/v1/lean-targets/batch',json=data).status_code==422
    with main.store.transaction() as tx:assert tx.all('insight_targets')==[] and tx.all('target_batches')==[]
    assert client.post('/api/v1/lean-targets/batch',json=dict(body(c),claim_token='wrong')).status_code==403
    assert client.post('/api/v1/lean-targets/batch',json=dict(body(c),targets=body(c)['targets'][:2])).status_code==422

def test_invalid_migration_targets_can_be_corrected_without_new_insights(client):
    c=claim(client);assert client.post('/api/v1/lean-targets/batch',json=body(c)).status_code==200
    ident=c['task']['target_ids'][0]
    with main.store.transaction() as tx:
        for t in tx.all('insight_targets'):t['status']='invalid' if t['id']==ident else 'valid';tx.put('insight_targets',t)
    retry=claim(client,'2','Bob')
    assert ident in retry['task']['target_ids']
    fixed=body(retry,'2','Bob')
    for item in fixed['targets']:
        if item['insight_id']==ident:item['lean_target']['expression']='1 = 1'
    assert client.post('/api/v1/lean-targets/batch',json=fixed).status_code==200
    with main.store.transaction() as tx:assert len(tx.all('insights'))==5 and tx.all('insight_target_revisions')


def test_same_batch_target_definitions_are_registered_topologically(client):
    with main.store.transaction() as tx:
        child=tx.get('insights','I1');child['references']=['I0'];tx.put('insights',child)
    c=claim(client);data=body(c)
    assert {'I0','I1'}<=set(c['task']['target_ids'])
    for item in data['targets']:
        if item['insight_id']=='I0':item['lean_target']=dict(definitions='def step (n : Nat) := n + 1',expression='True')
        if item['insight_id']=='I1':item['lean_target']=dict(expression='∀ n : Nat, MCTargets.I0.step n = n + 1',insight_dependencies=['I0'])
    data['targets'].reverse()
    r=client.post('/api/v1/lean-targets/batch',json=data)
    assert r.status_code==200,r.text
    with main.store.transaction() as tx:
        child=tx.get('insight_targets','I1');root=tx.get('insight_targets','I0')
        assert child['insight_dependency_snapshot'][0]['target_hash']==root['target_hash']


def test_migration_blocks_unleased_reviews_including_zero(client):
    payload=dict(contributor_id='1',contributor_name='Alice',verdict='refute',rationale='This record is engineering output, not mathematics.')
    assert client.post('/api/v1/insights/I0/reviews',json=dict(payload,score=1)).status_code==423
    r=client.post('/api/v1/insights/I0/reviews',json=dict(payload,score=0))
    assert r.status_code==423,r.text
    assert client.get('/api/v1/trash').json()['total']==0


def test_existing_three_item_lease_survives_larger_batch_setting(client,monkeypatch):
    from apps.api.modules import migration
    with monkeypatch.context() as patch:
        patch.setattr(migration,'BATCH_SIZE',3)
        old=claim(client)
    assert len(old['task']['target_ids'])==3
    assert client.get('/api/v1/platform-status').json()['batch_size']==5
    response=client.post('/api/v1/lean-targets/batch',json=body(old))
    assert response.status_code==200,response.text
    validate_targets()
    assert client.get('/api/v1/contributors/1/score').json()['score']==8


def test_lean_environment_migration_preserves_history_and_requeues_definitions(client):
    from apps.api.modules.lean import LIBRARY_ID, bundle_mathlib
    from apps.api.modules.targets import definition_closure
    from deploy.migrate_lean_environment import migrate

    with main.store.transaction() as tx:
        child = tx.get('insights', 'I1')
        child['references'] = ['I0']
        tx.put('insights', child)
        for ident in ('I0', 'I1', 'I2'):
            root_reference = [dict(insight_id='I0', target_hash='old-I0')] if ident == 'I1' else []
            tx.put('insight_targets', dict(
                id=ident, insight_id=ident, library_id='old', status='valid', target_hash='old-'+ident,
                expression='True', definitions='', registered_by='1', alignment_flags=int(ident == 'I2'),
                insight_dependencies=[s['insight_id'] for s in root_reference],
                insight_dependency_snapshot=root_reference,
            ))
        tx.put('lean_artifacts', dict(id='Lold', insight_id='I0', library_id='old', verification_id='Vold'))
        tx.put('verifications', dict(id='Vold', artifact_id='Lold', status='queued', log='waiting'))
        tx.put('tasks', dict(id='Told', type='lean_work', status='claimed'))
        tx.put('tasks', dict(id='Treview', type='review', status='claimed'))

    with main.store.transaction() as tx:
        result = migrate(tx)
        assert result == dict(requeued_targets=2, targets_needing_repair=1, cancelled_checks=1, closed_leases=1)
        root, child = (tx.get('insight_targets', ident) for ident in ('I0', 'I1'))
        assert root['library_id'] == child['library_id'] == LIBRARY_ID
        assert root['status'] == child['status'] == 'queued'
        assert root['registered_by'] == child['registered_by'] == '1'
        assert child['insight_dependency_snapshot'][0]['target_hash'] == root['target_hash']
        assert tx.get('target_versions', 'old-I0')['status'] == 'valid'
        old_child = tx.get('target_versions', 'old-I1')
        assert [target['target_hash'] for target in definition_closure(tx, old_child)] == ['old-I0', 'old-I1']
        assert tx.get('insight_targets', 'I2')['status'] == 'stale'
        assert tx.get('lean_artifacts', 'Lold')['library_id'] == 'old'
        assert tx.get('verifications', 'Vold')['status'] == 'cancelled'
        assert tx.get('tasks', 'Told')['status'] == 'closed'
        assert tx.get('tasks', 'Treview')['status'] == 'claimed'
        assert not tx.all('contributions')

    with main.store.transaction() as tx:
        assert migrate(tx)['already_applied'] is True
        assert not tx.writes

    revision, _ = bundle_mathlib('leanprover/lean4:v4.19.0')
    assert revision == 'c44e0c8ee63ca166450922a373c7409c5d26b00b'
