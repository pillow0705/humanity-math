"""Registration and review contracts without pretending to run the Lean kernel."""
import io
import time
import zipfile
import pytest
from fastapi.testclient import TestClient
from apps.api import main
from apps.api.infrastructure.store import Store, digest
from apps.api.modules.lean import POLICY, LIBRARY_ID, TOOLCHAIN
from apps.api.modules.targets import get_target, target_proof_matches, target_source


@pytest.fixture
def target_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'store', Store(tmp_path))
    monkeypatch.setattr(main, 'REGISTRATION_CREDENTIALS', False)
    with main.store.transaction() as tx:
        tx.put('problems', dict(id='PTEST', title='Test', statement='True', status='open', created_at=time.time()))
    with TestClient(main.app) as client:
        for name in ('Alice', 'Bob'):
            assert client.post('/api/v1/contributors/claim', json={'name': name}).status_code == 200
        yield client


def body(**updates):
    return dict(dict(contributor_id='1', contributor_name='Alice', problem_id='PTEST',
                     title='True', claim='True', proof='By introduction', proof_status='claimed_complete',
                     lean_target=dict(expression='True')), **updates)


def test_optional_target_is_queued_and_immutable_when_supplied(target_client):
    client = target_client
    missing = body(title='Without formal target'); missing.pop('lean_target')
    assert client.post('/api/v1/insights', json=missing).status_code == 200
    result = client.post('/api/v1/insights', json=body())
    assert result.status_code == 200, result.text
    insight_obj = result.json()
    target = insight_obj['lean_target']
    assert target['status'] == 'queued'
    assert target['expression'] == 'True'
    assert target['target_format'] == 'insight-target-v1'
    assert target['target_constant'] == 'MCTargets.'+insight_obj['id']+'.statement'
    assert len(target['target_hash']) == 64
    assert client.get('/api/v1/insights/'+insight_obj['id']+'/lean-target').json() == target
    assert client.get('/api/v1/insights/'+insight_obj['id']).json()['lean_target'] == target
    replacement = dict(contributor_id='1', contributor_name='Alice', lean_target=dict(expression='False'))
    denied = client.post('/api/v1/insights/'+insight_obj['id']+'/lean-target', json=replacement)
    assert denied.status_code == 409
    assert client.get('/api/v1/insights/'+insight_obj['id']+'/lean-target').json() == target


@pytest.mark.parametrize('target', [dict(expression='sorry'), dict(expression='True', definitions='axiom bad : False'),
                                  dict(expression='True', definitions='import Uninstalled.Package'),
                                  dict(expression='True', dependencies=['missing', 'missing'])])
def test_invalid_target_input_rolls_back_insight_and_credit(target_client, target):
    result = target_client.post('/api/v1/insights', json=body(lean_target=target))
    assert result.status_code == 422, result.text
    with main.store.transaction() as tx:
        assert tx.all('insights') == [] and tx.all('contributions') == [] and tx.all('insight_targets') == []


def test_legacy_owner_can_register_once_without_new_credit(target_client):
    with main.store.transaction() as tx:
        old = tx.new('insights', 'I', dict(body(), references=[], gaps=[], status='received'))
        old.pop('lean_target'); tx.put('insights', old)
    client = target_client
    assert client.get('/api/v1/insights/'+old['id']+'/lean-target').json() is None
    outsider = dict(contributor_id='2', contributor_name='Bob', lean_target=dict(expression='True'))
    assert client.post('/api/v1/insights/'+old['id']+'/lean-target', json=outsider).status_code == 403
    owner = dict(outsider, contributor_id='1', contributor_name='Alice')
    result = client.post('/api/v1/insights/'+old['id']+'/lean-target', json=owner)
    assert result.status_code == 200 and result.json()['status'] == 'queued'
    assert client.post('/api/v1/insights/'+old['id']+'/lean-target', json=owner).status_code == 409
    with main.store.transaction() as tx:
        assert tx.all('contributions') == []


def test_review_no_longer_requires_or_counts_target_alignment(target_client):
    client=target_client
    obj=client.post('/api/v1/insights',json=body()).json()
    with main.store.transaction() as tx:
        lease=tx.new('tasks','T',dict(type='review',problem_id=obj['problem_id'],target_id=obj['id'],status='claimed',expires_at=time.time()+600,contributor_id='2',claim_hash=digest('review-lease')))
    review=dict(contributor_id='2',contributor_name='Bob',verdict='support',rationale='Deep reusable argument.',score=4,task_id=lease['id'],claim_token='review-lease')
    result=client.post('/api/v1/insights/'+obj['id']+'/reviews',json=dict(review,target_alignment='misaligned',target_hash='0'*64))
    assert result.status_code==200
    assert result.json()['target_alignment']=='not_applicable' and result.json()['target_hash'] is None
    with main.store.transaction() as tx:assert tx.all('target_audits')==[]


def test_batch_review_alignment_is_atomic(target_client):
    client = target_client
    insights = [client.post('/api/v1/insights', json=body(title='Insight '+str(n))).json() for n in range(2)]
    with main.store.transaction() as tx:
        task = tx.new('tasks', 'T', dict(type='review', problem_id='PTEST', target_id=insights[0]['id'],
                      target_ids=[i['id'] for i in insights], status='claimed', expires_at=time.time()+600,
                      contributor_id='2', claim_hash=digest('lease')))
    items = [dict(insight_id=i['id'], verdict='support', rationale='Checked.', score=4, target_alignment='aligned') for i in insights]
    items[-1].pop('target_alignment')
    payload = dict(contributor_id='2', contributor_name='Bob', task_id=task['id'], claim_token='lease', reviews=items)
    result = client.post('/api/v1/reviews/batch', json=payload)
    assert result.status_code == 200, result.text
    assert [r['target_alignment'] for r in result.json()['items']] == ['not_applicable', 'not_applicable']
    assert all(r['target_hash'] is None for r in result.json()['items'])


def test_target_snapshot_and_bundle_include_frozen_source(target_client):
    client = target_client
    insight = client.post('/api/v1/insights', json=body()).json()
    proof = dict(contributor_id='2', contributor_name='Bob', insight_id=insight['id'], source='theorem answer : True := True.intro', theorem='answer')
    result = client.post('/api/v1/lean/artifacts', json=proof)
    assert result.status_code == 200, result.text
    artifact = result.json()
    assert artifact['target']['target_hash'] == insight['lean_target']['target_hash']
    with zipfile.ZipFile(io.BytesIO(client.get('/api/v1/lean/artifacts/'+artifact['id']+'/bundle').content)) as archive:
        source = archive.read('MCTargets/'+insight['id']+'.lean').decode()
        assert source == target_source(insight['lean_target'])
        assert 'import MCTargets.'+insight['id'] in archive.read(artifact['file_path']).decode()
        assert 'name = "MCTargets"' in archive.read('lakefile.toml').decode()
    with main.store.transaction() as tx:
        target = get_target(tx, insight['id'])
    verification = dict(status='passed', policy=POLICY, target_hash=target['target_hash'], target_matched=True)
    assert not target_proof_matches(artifact, verification, target)
    target['status'] = 'valid'
    assert target_proof_matches(artifact, verification, target)
    assert not target_proof_matches(artifact, dict(verification, target_hash='0'*64), target)
    assert not target_proof_matches(artifact, dict(verification, target_matched=False), target)


def test_legacy_target_registration_is_owner_only_and_has_no_extra_credit(target_client):
    client=target_client
    with main.store.transaction() as tx:
        legacy=tx.new('insights','I',dict(contributor_id='1',contributor_name='Alice',problem_id='PTEST',title='Legacy',claim='True',proof='Introduction',proof_status='claimed_complete',references=[],gaps=[]))
    url='/api/v1/insights/'+legacy['id']+'/lean-target'
    assert client.get(url).json() is None
    assert client.get('/api/v1/insights/'+legacy['id']).json()['lean_target'] is None
    assert client.post(url,json=dict(contributor_id='2',contributor_name='Bob',lean_target=dict(expression='True'))).status_code==403
    result=client.post(url,json=dict(contributor_id='1',contributor_name='Alice',lean_target=dict(expression='True')))
    assert result.status_code==200 and result.json()['status']=='queued'
    with main.store.transaction() as tx:
        assert tx.all('contributions')==[]
        assert tx.all('tasks')==[]
    assert client.post(url,json=dict(contributor_id='1',contributor_name='Alice',lean_target=dict(expression='False'))).status_code==409


def test_target_dependencies_are_frozen_and_environment_checked(target_client):
    client=target_client
    with main.store.transaction() as tx:
        tx.put('lean_artifacts',dict(id='Llegacy',insight_id='Ilegacy',verification_id='Vlegacy',policy=POLICY,library_id=LIBRARY_ID,content_hash='a'*64))
        tx.put('verifications',dict(id='Vlegacy',status='passed',policy=POLICY))
    definitions='import MC.Llegacy\ndef helper : Prop := True'
    result=client.post('/api/v1/insights',json=body(lean_target=dict(definitions=definitions,expression='helper',dependencies=['Llegacy'])))
    assert result.status_code==200,result.text
    target=result.json()['lean_target']
    assert target['dependency_snapshot']==[dict(id='Llegacy',content_hash='a'*64)]
    assert 'import MC.Llegacy' in target_source(target)
    with main.store.transaction() as tx:
        item=tx.get('insight_targets',target['id']);item['status']='valid';tx.put('insight_targets',item)
    listed=client.get('/api/v1/insights').json()['items'][0]
    assert listed['lean_target']['status']=='valid'
    with main.store.transaction() as tx:
        artifact=tx.get('lean_artifacts','Llegacy');artifact['library_id']='different';tx.put('lean_artifacts',artifact)
    denied=client.post('/api/v1/insights',json=body(title='Other environment',lean_target=dict(expression='True',dependencies=['Llegacy'])))
    assert denied.status_code==422


def test_legacy_target_rejects_its_own_old_proof_as_dependency(target_client):
    with main.store.transaction() as tx:
        legacy=tx.new('insights','I',dict(contributor_id='1',contributor_name='Alice',problem_id='PTEST',title='Legacy',claim='True',proof='Introduction',proof_status='claimed_complete',references=[],gaps=[]))
        tx.put('lean_artifacts',dict(id='Lself',insight_id=legacy['id'],verification_id='Vself',policy=POLICY,library_id=LIBRARY_ID,content_hash='a'*64))
        tx.put('verifications',dict(id='Vself',status='passed',policy=POLICY))
    result=target_client.post('/api/v1/insights/'+legacy['id']+'/lean-target',json=dict(contributor_id='1',contributor_name='Alice',lean_target=dict(expression='True',dependencies=['Lself'])))
    assert result.status_code==422 and 'same insight' in result.text


def test_only_first_exact_target_proof_earns_credit_and_legacy_probes_do_not(target_client):
    from apps.api.modules.core import contribute
    from apps.api.modules.scoring import Scores
    from apps.api.modules.lean import canonical_proof_ids
    client=target_client
    insight=client.post('/api/v1/insights',json=body()).json()
    with main.store.transaction() as tx:
        target=get_target(tx,insight['id']);target['status']='valid';tx.put('insight_targets',target)
        for n,kind in enumerate(['legacy_probe','wrong_hash','first_exact','duplicate_exact']):
            artifact=dict(id='L'+str(n),insight_id=insight['id'],problem_id='PTEST',contributor_id='2',contributor_name='Bob',verification_id='V'+str(n),policy=POLICY,library_id=LIBRARY_ID,created_at=n,dependencies=[],content_hash=str(n)*64)
            verification=dict(id='V'+str(n),artifact_id=artifact['id'],status='passed',policy=POLICY,created_at=n,finished_at=n+1)
            if kind!='legacy_probe':
                artifact['target']=target
                verification.update(target_hash='0'*64 if kind=='wrong_hash' else target['target_hash'],target_matched=True)
            tx.put('lean_artifacts',artifact);tx.put('verifications',verification);contribute(tx,artifact,'lean_work')
        scores=Scores(tx)
        records=scores.records('2')
        assert [r['score'] for r in records]==[0,0,8,0]
        assert records[0]['status'] in ('unbound_target','duplicate_formalization')
        assert records[1]['status'] in ('unbound_target','duplicate_formalization')
        assert records[3]['status']=='duplicate_formalization'
        assert scores.evaluation(insight,'insights')['lean_bonus']==10
        assert canonical_proof_ids(tx)=={insight['id']:'L2'}
        # Invalidating the accepted target prevents both proof credit and insight bonus.
        target['status']='invalid';tx.put('insight_targets',target)
        scores=Scores(tx)
        assert scores.total('2')['score']==0
        assert scores.evaluation(insight,'insights')['lean_bonus']==0
        assert canonical_proof_ids(tx)=={}


def test_target_infrastructure_error_recheck_is_owner_only_and_preserves_target(target_client):
    client=target_client
    insight=client.post('/api/v1/insights',json=body()).json()
    with main.store.transaction() as tx:
        before=get_target(tx,insight['id'])
        before.update(status='error',log='Compiler timeout',started_at=10,finished_at=20,exit_code=124)
        tx.put('insight_targets',before)
        events_before=tx.all('contributions');tasks_before=tx.all('tasks')
    url='/api/v1/insights/'+insight['id']+'/lean-target/recheck'
    assert client.post(url,json=dict(contributor_id='2',contributor_name='Bob')).status_code==403
    result=client.post(url,json=dict(contributor_id='1',contributor_name='Alice',reason='Retry after restoring the verifier'))
    assert result.status_code==200,result.text
    after=result.json()
    assert after['status']=='queued'
    for field in ('target_hash','expression','definitions','dependencies','dependency_snapshot','target_module','target_constant','target_format','created_at'):
        assert after[field]==before[field]
    assert after['last_error']=='Compiler timeout'
    assert all(field not in after for field in ('started_at','finished_at','log','exit_code'))
    with main.store.transaction() as tx:
        assert tx.all('contributions')==events_before
        assert tx.all('tasks')==tasks_before


@pytest.mark.parametrize('status',['valid','invalid','queued','checking'])
def test_target_recheck_rejects_non_error_states(target_client,status):
    client=target_client
    insight=client.post('/api/v1/insights',json=body()).json()
    with main.store.transaction() as tx:
        target=get_target(tx,insight['id']);target['status']=status;tx.put('insight_targets',target)
    result=client.post('/api/v1/insights/'+insight['id']+'/lean-target/recheck',json=dict(contributor_id='1',contributor_name='Alice'))
    assert result.status_code==409
    with main.store.transaction() as tx:
        assert get_target(tx,insight['id'])==target
