import concurrent.futures, io, json, time, zipfile
import pytest
from fastapi.testclient import TestClient
from review_fixtures import ReviewLeaseFixture,legacy_revision
from apps.api import main
from apps.api.infrastructure.store import Store, atomic
from apps.api.modules.core import task

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"REGISTRATION_CREDENTIALS",False)
    store=Store(tmp_path); monkeypatch.setattr(main,'store',store)
    with store.transaction() as tx:
        tx.put('problems',dict(id='P001',title='Test',statement='True',lean_target=None,status='open',created_at=time.time()))
        task(tx,'P001','insight_explore')
    class FixtureClient(ReviewLeaseFixture,TestClient):
        def post(self,*args,**kwargs):
            response=super().post(*args,**kwargs)
            # These API-only tests use the proposition True. Kernel elaboration is
            # exercised independently by target-verifier and real Lean tests.
            with store.transaction() as tx:
                for target in tx.all('insight_targets'):
                    if target['status']=='queued':
                        target['status']='valid';tx.put('insight_targets',target)
            return response
    with FixtureClient(main.app) as c:
        for name in ['Alice','Aster','Bob','Beta']:
            assert c.post('/api/v1/contributors/claim',json={'name':name}).status_code==200
        yield c

def put_test_verification(tx,v):
    """Stand in for a successful exact-target kernel check in API-only tests."""
    if v.get('status')=='passed':
        a=tx.get('lean_artifacts',v.get('artifact_id','missing'))
        if a and a.get('target'):
            v.update(target_hash=a['target']['target_hash'],target_matched=True)
    tx.put('verifications',v)

def route(): return dict(contributor_id='1',contributor_name='Alice',problem_id='P001',title='Route',summary='Short',body='Detailed argument',references=[])
def insight(): return dict(lean_target=dict(expression='True'),contributor_id='1',contributor_name='Alice',problem_id='P001',title='True',claim='True',proof='By introduction',proof_status='claimed_complete',gaps=[],references=[])

def test_package_and_submission(client):
    claim=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',type='insight_explore')).json()
    assert claim['research_package']['prompt']['text']
    assert claim['research_package']['submission']['schema']['required']
    assert all(x['id'].startswith('I') for x in claim['research_package']['materials'])
    assert len(claim['research_package']['insight_catalog']['items'])<=100
    body=dict(insight(),task_id=claim['task']['id'],claim_token=claim['claim_token'])
    result=client.post('/api/v1/insights',json=body,headers={'Idempotency-Key':'one'})
    assert result.status_code==200,result.text
    assert 'claim_token' not in result.json()
    assert client.post('/api/v1/insights',json=body,headers={'Idempotency-Key':'one'}).json()==result.json()
    assert client.get('/api/v1/tasks/'+claim['task']['id']).json()['status']=='submitted'
    assert client.post('/api/v1/insights',json=dict(body,title='Changed'),headers={'Idempotency-Key':'one'}).status_code==409
    assert client.get('/api/v1/contributors/1/contributions').json()['total']==1

def test_claim_concurrent(client):
    task_id=client.get('/api/v1/tasks?type=insight_explore').json()['items'][0]['id']
    def claim(_): return client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',task_id=task_id)).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: codes=list(pool.map(claim,range(6)))
    assert codes.count(200)==1 and codes.count(409)==5

def test_atomic_rollback_bad_lease(client):
    claimed=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice')).json()
    body=dict(insight(),task_id=claimed['task']['id'],claim_token='wrong')
    assert client.post('/api/v1/insights',json=body).status_code==403
    assert client.get('/api/v1/insights').json()['total']==0

def test_references_replacement_and_review(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    assert client.post('/api/v1/insights',json=dict(insight(),references=['missing'])).status_code==422
    new=legacy_revision(client,dict(insight(),proof='Different proof',supersedes_id=i['id'])).json()
    assert client.get('/api/v1/insights/'+i['id']).json()['replaced_by']==[new['id']]
    r=client.post('/api/v1/insights/'+i['id']+'/reviews',json=dict(contributor_id='4',contributor_name='Beta',verdict='support',rationale='Checked claim',target_alignment='aligned',score=4))
    assert r.status_code==200
    assert len(client.get('/api/v1/insights/'+i['id']+'/reviews').json()['items'])==1
    assert client.patch('/api/v1/insights/'+i['id'],json={}).status_code==405

def test_lean_bound_and_restricted(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    b=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],source='theorem result : True := True.intro',theorem='result')
    a=client.post('/api/v1/lean/artifacts',json=b)
    assert a.status_code==200,a.text
    assert client.get('/api/v1/lean/verifications/'+a.json()['verification_id']).json()['status']=='queued'
    assert client.post('/api/v1/lean/artifacts',json=dict(b,source='theorem result : True := by sorry')).status_code==422
    assert client.post('/api/v1/lean/artifacts',json=dict(b,dependencies=[a.json()['id']])).status_code==422
    assert client.post('/api/v1/problems/P001/solutions',json=dict(contributor_id='2',contributor_name='Aster',artifact_id=a.json()['id'],proof='proof')).status_code==409
    assert client.get('/api/v1/lean/artifacts/'+a.json()['id']+'/bundle').headers['content-type']=='application/zip'

def test_expiry_releases_task(client):
    claimed=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice')).json()
    with main.store.transaction() as tx:
        t=tx.get('tasks',claimed['task']['id']); t['expires_at']=0; tx.put('tasks',t)
    assert client.post('/api/v1/tasks/'+t['id']+'/renew',json=dict(claim_token=claimed['claim_token'])).status_code==409
    assert client.post('/api/v1/tasks/claim',json=dict(contributor_id='3',contributor_name='Bob',task_id=t['id'])).status_code==200

def test_journal_recovery(tmp_path):
    s=Store(tmp_path)
    atomic(tmp_path/'.pending.json',[['problems','P001',dict(id='P001',title='recover')],['tasks','T1',dict(id='T1',status='available')]])
    with s.transaction() as tx:
        assert tx.get('problems','P001')['title']=='recover'
        assert tx.get('tasks','T1')['status']=='available'
    assert not (tmp_path/'.pending.json').exists()

def test_long_summary_and_missing_gaps(client):
    assert client.post('/api/v1/routes',json=route()).status_code==404
    assert client.post('/api/v1/insights',json=dict(insight(),proof_status='partial')).status_code==422


def test_review_score_contract(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    url='/api/v1/insights/'+i['id']+'/reviews'
    body=dict(contributor_id='4',contributor_name='Beta',verdict='support',rationale='Evidence-based rating')
    assert client.post(url,json=body).status_code==422
    for bad in [-1,6,2.5,4.0,"4",True,None]:
        assert client.post(url,json=dict(body,target_alignment='aligned',score=bad)).status_code==422
    assert client.post(url,json=dict(body,target_alignment='aligned',score=4,suggestions='removed')).status_code==422
    for value in [1,5]:
        response=client.post(url,json=dict(body,target_alignment='aligned',score=value))
        assert response.status_code==200,response.text
        assert type(response.json()['score']) is int
    stored=client.get(url).json()['items']
    assert sorted(x['score'] for x in stored)==[1,5]
    assert all('suggestions' not in x for x in stored)
    assert client.post('/api/v1/routes/RBASE/reviews',json=dict(body,target_alignment='aligned',score=3)).status_code==404


def test_lean_environment_and_catalog(client):
    env=client.get('/api/v1/lean/environment').json()
    assert env['toolchain']=='leanprover/lean4:v4.36.0'
    assert env['submission']['local_download_required'] is False
    assert env['submission']['server_assembles_dependencies'] is True
    assert any(x['name']=='Mathlib' and x['available'] and x['precompiled'] and x['revision'] for x in env['libraries'])
    assert client.get('/api/v1/lean/artifacts').json()['total']==0
    i=client.post('/api/v1/insights',json=insight()).json()
    a=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],source='theorem identity_result : True := True.intro',theorem='identity_result')).json()
    assert client.get('/api/v1/lean/artifacts').json()['total']==0
    queued=client.get('/api/v1/lean/artifacts',params={'status':'queued'}).json()['items'][0]
    assert not queued['reusable']
    with main.store.transaction() as tx:
        v=tx.get('verifications',a['verification_id'])
        v.update(status='passed',axioms=[],theorem_signature='identity_result : True')
        put_test_verification(tx,v)
    results=client.get('/api/v1/lean/artifacts',params={'q':'identity_result','problem_id':'P001','insight_id':i['id']}).json()
    entry=results['items'][0]
    assert entry['reusable'] and entry['import_statement']=='import MC.'+a['id']
    assert entry['theorem_signature']=='identity_result : True'
    assert client.get(entry['detail_url']).json()['source']==a['source']
    assert client.get('/api/v1/lean/artifacts',params={'q':'not-present'}).json()['total']==0
    assert client.get('/api/v1/lean/artifacts',params={'limit':1,'offset':1}).json()['items']==[]
    client.post('/api/v1/insights',json=dict(insight(),title='Full proof',claims_problem_solved=1,references=[i['id']]))
    task=client.post('/api/v1/tasks/claim',json={'contributor_id':'2','contributor_name':'Aster','type':'lean_work'}).json()
    package=task['research_package']
    assert package['lean_environment']['environment_id']==env['environment_id']
    assert package['lean_artifacts'][0]['theorem']=='identity_result'
    assert package['lean_artifacts'][0]['import_statement']=='import MC.'+a['id']


def test_lean_prompt_and_environment_use_deployment_prefix(client,monkeypatch):
    monkeypatch.setattr(main,'PREFIX','/math-collab')
    env=client.get('/api/v1/lean/environment').json()
    assert env['artifact_catalog_url']=='/math-collab/api/v1/lean/artifacts'
    prompt=client.get('/api/v1/prompts/lean_work').text
    assert 'GET /math-collab/api/v1/lean/environment' in prompt
    assert 'GET /api/v1/' not in prompt


def test_identity_numbering_and_matching(client):
    r=client.post('/api/v1/contributors/claim',json={'name':'Gauss'},headers={'Idempotency-Key':'gauss'})
    assert r.status_code==200 and r.json()['number']==5
    assert client.post('/api/v1/contributors/claim',json={'name':'Gauss'},headers={'Idempotency-Key':'gauss'}).json()==r.json()
    assert client.post('/api/v1/contributors/claim',json={'name':'gauss'}).status_code==409
    assert client.post('/api/v1/contributors/claim',json={'name':'中国'}).status_code==422
    assert client.post('/api/v1/insights',json=dict(insight(),contributor_name='Wrong')).status_code==422
    body=insight();body.pop('contributor_name')
    assert client.post('/api/v1/insights',json=body).status_code==422
    def register(n): return client.post('/api/v1/contributors/claim',json={'name':'Person'+str(n)}).json()['number']
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: numbers=list(pool.map(register,range(6)))
    assert sorted(numbers)==list(range(6,12))


def test_scoring_citations_lean_and_final_use(client):
    i=client.post('/api/v1/insights',json=dict(insight(),contributor_id='2',contributor_name='Aster')).json()
    assert 'route_id' not in i
    cite=client.post('/api/v1/insights',json=dict(insight(),contributor_id='3',contributor_name='Bob',title='Citing result',references=[i['id'],i['id']])).json()
    ev=lambda: client.get('/api/v1/insights/'+i['id']).json()['evaluation']
    assert ev()['citation_points']==1
    for rating in [5,1]:
        response=client.post('/api/v1/insights/'+i['id']+'/reviews',json=dict(contributor_id='4',contributor_name='Beta',verdict='concern',target_alignment='aligned',score=rating,rationale='Review'))
        assert response.status_code==200
    assert ev()['effective_reviews']==1 and ev()['contribution_points']==9.75
    assert client.get('/api/v1/contributors/4/score').json()['score']==6
    client.post('/api/v1/insights/'+i['id']+'/reviews',json=dict(contributor_id='2',contributor_name='Aster',verdict='support',target_alignment='aligned',score=5,rationale='Self'))
    assert ev()['effective_reviews']==1
    a=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='4',contributor_name='Beta',insight_id=i['id'],source='theorem t : True := True.intro',theorem='t')).json()
    assert client.get('/api/v1/contributors/4/score').json()['pending_points']==8
    with main.store.transaction() as tx:
        v=tx.get('verifications',a['verification_id']);v['status']='passed';put_test_verification(tx,v)
        tx.new('solutions','S',dict(problem_id='P001',status='verified',used_insight_ids=[i['id']],contributor_id='4'))
    assert ev()['lean_bonus']==10 and ev()['final_proof_bonus']==3
    assert ev()['contribution_points']==22.75
    assert client.get('/api/v1/contributors/4/score').json()['score']==14
    assert client.get('/api/v1/contributors/2/score').json()['score']==22.75
    assert client.get('/api/v1/leaderboard').json()['items'][0]['id']=='2'


def test_insight_inline_lean_is_atomic_and_optional(client):
    payload=dict(insight(),lean={'source':'theorem inlineProof : True := True.intro','theorem':'inlineProof'})
    response=client.post('/api/v1/insights',json=payload)
    assert response.status_code==200,response.text
    i=response.json();a=i['lean_submission']
    assert a['insight_id']==i['id'] and i['initial_lean_artifact_id']==a['id']
    assert client.get('/api/v1/tasks',params={'type':'lean_work'}).json()['total']==0
    bad=client.post('/api/v1/insights',json=dict(insight(),title='Bad',lean={'source':'theorem bad : True := by sorry','theorem':'bad'}))
    assert bad.status_code==422
    assert client.get('/api/v1/insights').json()['total']==1
    assert client.get('/api/v1/contributors/1/score').json()['pending_points']==8
    assert client.post('/api/v1/insights',json=dict(insight(),title='Retired route field',route_id='Rmissing')).status_code==422


def test_fair_recommendations_and_low_scores(client,monkeypatch):
    import random
    from apps.api.modules import recommendations as rec
    monkeypatch.setattr(rec,'RNG',random.Random(42))
    targets=[client.post('/api/v1/insights',json=dict(insight(),title=str(n))).json() for n in range(300)]
    low=targets[0]
    for person,name in [('2','Aster'),('3','Bob'),('4','Beta')]:
        client.post('/api/v1/insights/'+low['id']+'/reviews',json=dict(contributor_id=person,contributor_name=name,verdict='concern',rationale='Low rating',target_alignment='aligned',score=1 if person=='2' else 3))
    result=client.get('/api/v1/problems/P001/recommendations').json()
    assert len(result['items'])==300 and {x['id'] for x in result['items']}=={x['id'] for x in targets}
    for n in range(300,400): targets.append(client.post('/api/v1/insights',json=dict(insight(),title=str(n))).json())
    counts={x['id']:0 for x in targets}; samples=set()
    for _ in range(120):
        result=client.get('/api/v1/problems/P001/recommendations').json()
        ids=[x['id'] for x in result['items']]
        assert len(ids)==len(set(ids))==300 and result['total_available']==400
        samples.add(tuple(sorted(ids)))
        for ident in ids: counts[ident]+=1
    assert len(samples)>1 and min(counts.values())>0
    # A finite random sample need not rank exposure counts exactly. Check the
    # lower weight deterministically; coverage/randomness are asserted above.
    with main.store.transaction() as tx:
        from apps.api.modules.scoring import Scores
        from apps.api.modules.recommendations import weight
        scores=Scores(tx)
        assert weight(scores,low)['weight']<weight(scores,targets[1])['weight']
    claimed=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',type='insight_explore')).json()
    catalog=claimed['research_package']['insight_catalog']
    assert len(catalog['items'])==100 and all(x['detail_url'] for x in catalog['items'])


def test_frontend_entry_and_four_task_contract(client):
    home=client.get('/')
    assert home.status_code==200 and '人类求知' in home.text and '<html' in home.text
    assert client.get('/assets/app.js').status_code==200
    spec=client.get('/openapi.json').json()
    assert set(spec['components']['schemas']['Claim']['properties']['type']['anyOf'][0]['enum'])=={'insight_explore','review','lean_work','research_synthesis','lean_target'}
    assert client.get('/api/v1/contribution-policy').json()['base_points']=={'insights':10,'review':6,'lean_work':8,'research_synthesis':15,'lean_target':8}
    assert client.get('/api/v1/prompts/lean_work').status_code==200
    assert client.get('/api/v1/prompts/lean_audit').status_code==404


def test_frontend_mount_under_public_prefix(client,monkeypatch):
    monkeypatch.setattr(main.app,'root_path','/math-collab')
    monkeypatch.setattr(main,'PREFIX','/math-collab')
    assert client.get('/math-collab/assets/app.js').status_code==200
    assert client.get('/math-collab/assets/vendor/katex/katex.min.css').status_code==200
    assert client.get('/math-collab/').status_code==200


def test_mathlib_imports_and_reproducible_bundle(client):
    from apps.api.modules.lean import MATHLIB_REVISION, LIBRARY_ID, bundle_manifest
    i=client.post('/api/v1/insights',json=insight()).json()
    body=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],theorem='square_nonnegative')
    source='import Mathlib\ntheorem square_nonnegative (x : ℝ) : 0 ≤ x^2 := sq_nonneg x'
    response=client.post('/api/v1/lean/artifacts',json=dict(body,source=source))
    assert response.status_code==200,response.text
    a=response.json(); assert a['library_id']==LIBRARY_ID
    for module in ['Mathlib.Data.Real.Basic','Std','Batteries','Aesop','Qq']:
        assert client.post('/api/v1/lean/artifacts',json=dict(body,source='import '+module+'\ntheorem square_nonnegative : True := True.intro')).status_code==200
    for module in ['Lake','Unknown','MC.Lundeclared','Mathlib/../Lean','MathlibEvil']:
        assert client.post('/api/v1/lean/artifacts',json=dict(body,source='import '+module+'\ntheorem square_nonnegative : True := True.intro')).status_code==422
    for forbidden in ['set_option debug.skipKernelTC true','run_elab pure ()','run_cmd pure ()','run_meta pure ()','run_io pure ()']:
        assert client.post('/api/v1/lean/artifacts',json=dict(body,source='import Mathlib\n'+forbidden+'\ntheorem square_nonnegative : True := True.intro')).status_code==422
    data=client.get('/api/v1/lean/artifacts/'+a['id']+'/bundle').content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert json.loads(z.read('lake-manifest.json'))==bundle_manifest()
        assert MATHLIB_REVISION in z.read('lakefile.toml').decode()
        readme = z.read('README.md').decode()
        assert 'Do not install Lean' in readme
        assert 'server assembles dependencies' in readme
        assert 'lake exe cache get' not in readme


def test_routes_retired_and_insights_bind_directly_to_problem(client):
    spec=client.get('/openapi.json').json()
    assert 'route_id' not in spec['components']['schemas']['Insight']['properties']
    assert '/api/v1/routes' not in spec['paths']
    assert client.post('/api/v1/routes',json=route()).status_code==404
    assert client.post('/api/v1/insights',json=dict(insight(),route_id='RBASE')).status_code==422
    assert client.post('/api/v1/insights',json=dict(insight(),references=['RBASE'])).status_code==422
    i=client.post('/api/v1/insights',json=insight()).json()
    assert 'route_id' not in i and i['references']==[]
    assert client.get('/api/v1/routes').status_code==404
    assert client.get('/api/v1/routes/RBASE').status_code==404
    t=next(t for t in client.get('/api/v1/tasks?type=insight_explore').json()['items'] if t['target_id'] is None)
    claim=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',task_id=t['id'])).json()
    pkg=claim['research_package']
    assert 'route_selection' not in pkg and 'route_id' not in pkg['submission']['schema']['properties']
    assert client.get('/api/v1/prompts/route_design').status_code==404
    assert client.get('/api/v1/prompts/route_review').status_code==404


def test_quality_and_citations_increase_exposure_and_task_priority(client):
    with main.store.transaction() as tx:
        for t in tx.all('tasks'): t.update(status='closed');tx.put('tasks',t)
    targets={name:client.post('/api/v1/insights',json=dict(insight(),title=name)).json() for name in ['high','cited','neutral','low']}
    for name,rating in [('high',5),('low',3)]:
        for number,person in [('2','Aster'),('3','Bob'),('4','Beta')]:
            r=client.post('/api/v1/insights/'+targets[name]['id']+'/reviews',json=dict(contributor_id=number,contributor_name=person,verdict='support' if rating==5 else 'concern',rationale='Evidence',target_alignment='aligned',score=1 if name=='low' and number=='2' else rating))
            assert r.status_code==200
    for n in range(3):
        r=client.post('/api/v1/insights',json=dict(insight(),contributor_id='2',contributor_name='Aster',title='Citation '+str(n),references=[targets['cited']['id']]))
        assert r.status_code==200
    response=client.get('/api/v1/problems/P001/recommendations?limit=100').json()
    assert response['algorithm']=='weighted-sample-v3'
    entries={x['id']:x for x in response['items']}
    w={name:entries[x['id']]['weight'] for name,x in targets.items()}
    assert w['high']>w['cited']>w['neutral']>w['low']
    assert entries[targets['cited']['id']]['impact_points']==3
    assert client.get('/api/v1/problems/P001/recommendations?kind=routes').status_code==422
    assert all(0.5<=x['weight']<=2 for x in entries.values())
    import random
    from apps.api.modules import recommendations as rec
    original=rec.RNG;rec.RNG=random.Random(19)
    try:
        counts={name:0 for name in targets}
        for _ in range(5000): counts[rec.weighted_sample(list(targets),list(w.values()),1)[0]]+=1
    finally: rec.RNG=original
    assert counts['high']>counts['cited']>counts['neutral']>counts['low']>0
    claimed=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',type='insight_explore')).json()
    assert claimed['task']['target_id'] is None
    fresh=client.post('/api/v1/insights',json=dict(insight(),title='Fresh research')).json()
    assert fresh['id'] in {x['id'] for x in client.get('/api/v1/problems/P001/recommendations').json()['items']}


@pytest.mark.parametrize('completion_flag',[1,-1])
def test_completion_flag_and_dependency_task_priority(client,completion_flag):
    ordinary=client.post('/api/v1/insights',json=dict(insight(),title='Ordinary')).json()
    assert ordinary['claims_problem_solved']==0
    dep=client.post('/api/v1/insights',json=dict(insight(),title='Prerequisite')).json()
    for bad in [True,'1',1.0,-2,2,None]:
        assert client.post('/api/v1/insights',json=dict(insight(),title='Invalid flag',claims_problem_solved=bad)).status_code==422
    for extra in [dict(proof_status='partial',gaps=['gap']),dict(gaps=['gap'])]:
        assert client.post('/api/v1/insights',json=dict(insight(),claims_problem_solved=completion_flag,**extra)).status_code==422
    candidate=client.post('/api/v1/insights',json=dict(insight(),title='Complete candidate',claims_problem_solved=completion_flag,references=[dep['id']])).json()
    assert candidate['claims_problem_solved']==completion_flag
    claimed=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',type='lean_work')).json()
    assert claimed['task']['target_id']==dep['id']
    assert set(claimed['research_package']['completion_context'][0]['required_insight_ids'])=={candidate['id'],dep['id']}
    state=client.get('/api/v1/insights/'+candidate['id']).json()['completion']
    assert state['status']=='awaiting_lean' and not state['target_ready']
    assert client.get('/api/v1/problems/P001').json()['status']=='open'
    docs=client.get('/api/v1/agent-docs').text
    assert 'ask for their preferred English username' in docs and 'never invent a name' in docs
    assert 'claims_problem_solved' in client.get('/api/v1/prompts/insight_explore').text


def test_manage_own_submissions_and_withdraw_restore(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    own=client.get('/api/v1/contributors/1/submissions').json()
    assert own['total']==1 and {x['kind'] for x in own['items']}=={'insights'}
    assert client.get('/api/v1/contributors/1/submissions?kind=insights&q=True&limit=1').json()['items'][0]['id']==i['id']
    assert client.get('/api/v1/contributors/1/submissions?offset=1&limit=1').json()['total']==1
    assert client.get('/api/v1/contributors/2/submissions').json()['total']==0
    assert client.get('/api/v1/contributors/2/submissions/insights/'+i['id']).status_code==404
    assert client.get('/api/v1/contributors/1/rank').json()['rank']==1
    assert client.get('/api/v1/leaderboard?offset=1&limit=1').json()['items']==[]
    url='/api/v1/contributors/1/submissions/insights/'+i['id']
    owner=dict(contributor_id='1',contributor_name='Alice',reason='Rechecking the argument')
    assert client.post(url+'/withdraw',json=dict(contributor_id='2',contributor_name='Aster')).status_code==403
    assert client.post(url+'/withdraw',json=dict(owner,contributor_name='Wrong')).status_code==422
    assert client.get('/api/v1/contributors/1/score').json()['score']==10
    response=client.post(url+'/withdraw',json=owner,headers={'Idempotency-Key':'withdraw-test'})
    assert response.status_code==200 and response.json()['withdrawn']
    assert client.post(url+'/withdraw',json=owner,headers={'Idempotency-Key':'withdraw-test'}).json()==response.json()
    assert client.get('/api/v1/insights/'+i['id']).json()['claim']==i['claim']
    assert client.get('/api/v1/insights').json()['total']==0
    assert client.get('/api/v1/insights?include_withdrawn=true').json()['total']==1
    assert client.get('/api/v1/problems/P001/recommendations').json()['items']==[]
    assert client.get('/api/v1/contributors/1/score').json()['score']==0
    assert client.get('/api/v1/contributors/1/submissions?status=withdrawn').json()['total']==1
    assert all(t['status']=='closed' for t in client.get('/api/v1/tasks').json()['items'] if t.get('target_id')==i['id'])
    assert client.post(url+'/restore',json=owner).status_code==200
    assert client.get('/api/v1/contributors/1/score').json()['score']==10
    assert client.get('/api/v1/problems/P001/recommendations').json()['items'][0]['id']==i['id']
    assert client.post('/api/v1/insights',json=dict(insight(),contributor_id='2',contributor_name='Aster',supersedes_id=i['id'],title='Wrong owner')).status_code==422
    replacement=legacy_revision(client,dict(insight(),supersedes_id=i['id'],title='Corrected insight')).json()
    assert replacement['supersedes_id']==i['id']
    assert client.get('/api/v1/contributors/1/submissions?status=superseded').json()['total']==1
    claimed=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',type='review')).json()
    tasks=client.get('/api/v1/contributors/2/tasks?status=claimed').json()['items']
    assert any(t['id']==claimed['task']['id'] for t in tasks)
    assert all('claim_hash' not in t and 'claim_token' not in t for t in tasks)


def test_withdraw_during_verification_and_restore(client,monkeypatch):
    from apps.worker import main as worker
    i=client.post('/api/v1/insights',json=insight()).json()
    a=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],source='theorem proof1 : True := True.intro',theorem='proof1')).json()
    monkeypatch.setattr(worker,'STORE',main.store)
    url='/api/v1/contributors/2/submissions/lean_artifacts/'+a['id']
    identity=dict(contributor_id='2',contributor_name='Aster')
    def cancelled_during_compile(*args):
        assert client.post(url+'/withdraw',json=identity).status_code==200
        return dict(status='passed',log='Result arrived after withdrawal')
    monkeypatch.setattr(worker,'verify_artifact',cancelled_during_compile)
    assert worker.once()
    assert client.get('/api/v1/lean/verifications/'+a['verification_id']).json()['status']=='cancelled'
    assert client.get('/api/v1/contributors/2/score').json()['pending_points']==0
    assert client.get(url).json()['source']==a['source']
    assert client.post(url+'/restore',json=identity).status_code==200
    assert client.get('/api/v1/lean/verifications/'+a['verification_id']).json()['status']=='queued'
    assert client.get('/api/v1/contributors/2/score').json()['pending_points']==8


def test_withdrawing_candidate_cancels_inflight_final_close(client,monkeypatch):
    from apps.worker import main as worker
    with main.store.transaction() as tx:
        p=tx.get('problems','P001');p.update(lean_target={'expression':'True'},target_status='reviewed');tx.put('problems',p)
    i=client.post('/api/v1/insights',json=dict(insight(),claims_problem_solved=1)).json()
    a=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='1',contributor_name='Alice',insight_id=i['id'],source='theorem final1 : True := True.intro',theorem='final1')).json()
    monkeypatch.setattr(worker,'STORE',main.store)
    monkeypatch.setattr(worker,'verify_artifact',lambda a,*args:dict(status='passed',axioms=[],target_matched=True,target_hash=a['target']['target_hash']))
    assert worker.once()
    url='/api/v1/contributors/1/submissions/insights/'+i['id']+'/withdraw'
    def cancel_final(*args):
        assert client.post(url,json=dict(contributor_id='1',contributor_name='Alice')).status_code==200
        return dict(status='verified',verified_at=time.time(),used_insight_ids=[i['id']])
    monkeypatch.setattr(worker,'verify_solution',cancel_final)
    assert worker.once()
    assert client.get('/api/v1/problems/P001').json()['status']=='open'
    assert client.get('/api/v1/insights/'+i['id']).json()['completion']['status']=='withdrawn'
    assert not worker.once()


def test_research_direction_distribution_and_shared_catalog(client,monkeypatch):
    from apps.api.modules import research
    original=client.post('/api/v1/insights',json=insight()).json()
    opposite=client.post('/api/v1/insights',json=dict(insight(),title='Disproof candidate',claims_problem_solved=-1)).json()
    directions=[]
    for draw in range(10):
        monkeypatch.setattr(research.secrets,'randbelow',lambda n,d=draw:d)
        with main.store.transaction() as tx: t=task(tx,'P001','insight_explore')
        response=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',task_id=t['id']))
        assert response.status_code==200,response.text
        c=response.json();package=c['research_package'];direction=package['research_direction'];directions.append(direction)
        assert c['task']['research_direction']==direction
        assert ('Assigned direction: '+direction.upper()) in package['prompt']['text']
        assert {x['id'] for x in package['insight_catalog']['items']}=={original['id'],opposite['id']}
        renewed=client.post('/api/v1/tasks/'+t['id']+'/renew',json={'claim_token':c['claim_token']}).json()
        assert renewed['research_direction']==direction
        result=client.post('/api/v1/insights',json=dict(insight(),title='Progress '+str(draw),task_id=t['id'],claim_token=c['claim_token']))
        assert result.status_code==200,result.text
        assert result.json()['claims_problem_solved']==0
        # Keep the shared catalog fixed for the next draw without affecting prior records.
        assert client.post('/api/v1/contributors/1/submissions/insights/'+result.json()['id']+'/withdraw',json={'contributor_id':'1','contributor_name':'Alice'}).status_code==200
    assert directions==['prove']*7+['disprove']*3
    claim=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',type='review')).json()
    assert claim['research_package']['research_direction'] is None


def test_signed_target_and_final_submission_validation(client):
    from apps.api.modules.research import final_target
    from apps.api.models import Solution
    from pydantic import ValidationError
    p={'lean_target':{'expression':'∀ n : Nat, n = n','definitions':'-- fixed definitions'}}
    assert final_target(p,1)==p['lean_target']
    assert final_target(p,-1)['expression']=='¬ (∀ n : Nat, n = n)'
    assert final_target(p,-1)['definitions']=='-- fixed definitions'
    assert p['lean_target']['expression']=='∀ n : Nat, n = n'
    for invalid in [0,True,'-1',-1.0,2]:
        with pytest.raises((ValueError,ValidationError)):
            Solution(contributor_id='1',contributor_name='Alice',artifact_id='LTEST',proof='Complete',claims_problem_solved=invalid)


def test_three_problem_migration_archives_without_deleting(client):
    from deploy.migrate_three_problems import migrate
    legacy=client.post('/api/v1/insights',json=insight()).json()
    before=client.get('/api/v1/insights/'+legacy['id']).json()
    with main.store.transaction() as tx:
        result=migrate(tx)
    assert result['active']==['P003','P007','P009']
    problems=client.get('/api/v1/problems').json()['items']
    assert [p['id'] for p in problems]==['P007','P009','P003']
    assert client.get('/api/v1/problems/P001').json()['status']=='archived'
    assert len(client.get('/api/v1/problems?include_archived=true').json()['items'])==4
    assert client.get('/api/v1/insights/'+legacy['id']).json()['proof']==before['proof']
    assert legacy['id'] not in {x['id'] for x in client.get('/api/v1/insights').json()['items']}
    assert legacy['id'] in {x['id'] for x in client.get('/api/v1/insights?problem_id=P001').json()['items']}
    assert all(x['problem_id']!='P001' for x in client.get('/api/v1/tasks').json()['items'])
    identity=dict(contributor_id='1',contributor_name='Alice')
    assert client.post('/api/v1/tasks/claim',json=dict(identity,problem_id='P001')).status_code==409
    assert client.post('/api/v1/insights',json=dict(insight(),title='Archived write')).status_code==409
    assert client.post('/api/v1/insights/'+legacy['id']+'/reviews',json=dict(identity,target_alignment='aligned',score=3,verdict='support',rationale='Review')).status_code==409
    assert client.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=legacy['id'],source='theorem archived : True := True.intro',theorem='archived')).status_code==409
    for pid in ['P007','P009','P003']:
        overview=client.get('/api/v1/problems/'+pid+'/overview').json()
        assert 'routes' not in overview['stats'] and overview['stats']['insights']==1
        assert overview['stats']['available_tasks']==4  # Includes permanent proposition maintenance for the legacy seed.
        claimed=client.post('/api/v1/tasks/claim',json=dict(identity,problem_id=pid,type='insight_explore'))
        assert claimed.status_code==200,claimed.text
        assert claimed.json()['research_package']['problem']['id']==pid
    with main.store.transaction() as tx:
        first={kind:tx.all(kind) for kind in ['problems','routes','insights','tasks']}
        migrate(tx)
        assert first=={kind:tx.all(kind) for kind in first}


def test_irrationality_problems_are_proof_only(client,monkeypatch):
    from deploy.migrate_three_problems import migrate
    from apps.api.modules import research
    with main.store.transaction() as tx: migrate(tx)
    identity=dict(contributor_id='1',contributor_name='Alice')
    for pid in ('P007','P009'):
        p=client.get('/api/v1/problems/'+pid).json()
        assert p['research_policy']==dict(prove_probability=1.0,disprove_probability=0.0,allowed_completion_claims=[0,1])
        for draw in range(10):
            monkeypatch.setattr(research.secrets,'randbelow',lambda n,d=draw:d)
            assert research.assign_direction(p)=='prove'
        claimed=client.post('/api/v1/tasks/claim',json=dict(identity,problem_id=pid,type='insight_explore')).json()
        pkg=claimed['research_package']
        assert pkg['research_direction']=='prove'
        assert pkg['submission']['schema']['properties']['claims_problem_solved']['enum']==[0,1]
        assert 'pursue a proof of irrationality (100%)' in pkg['prompt']['text']
        body=dict(insight(),problem_id=pid)
        for flag in (0,1):
            assert client.post('/api/v1/insights',json=dict(body,title='Accepted '+str(flag),claims_problem_solved=flag)).status_code==200
        denied=client.post('/api/v1/insights',json=dict(body,title='Rejected disproof',claims_problem_solved=-1))
        assert denied.status_code==422 and '-1 is disabled' in denied.text
        assert client.post('/api/v1/problems/'+pid+'/solutions',json=dict(identity,artifact_id='LTEST',proof='Disproof',claims_problem_solved=-1)).status_code==422
        with pytest.raises(ValueError,match='disabled'): research.final_target(p,-1)
    p=client.get('/api/v1/problems/P003').json()
    assert p['research_policy']['disprove_probability']==0.3
    assert research.assign_direction(p)=='disprove'  # draw 9 remains selected
    assert client.post('/api/v1/insights',json=dict(insight(),problem_id='P003',claims_problem_solved=-1)).status_code==200
    assert client.get('/openapi.json').json()['components']['schemas']['Insight']['properties']['claims_problem_solved']['minimum']==-1


def test_install_fixed_targets_is_atomic_and_keeps_problems_open(client):
    from seeds.bootstrap import seed
    from seeds.install_targets import install
    from apps.api.infrastructure.store import digest
    with main.store.transaction() as tx:
        seed(tx)
        install(tx)
        initial={pid:tx.get('problems',pid) for pid in ('P007','P009','P003')}
        install(tx)
        assert initial=={pid:tx.get('problems',pid) for pid in initial}
    for pid in initial:
        p=client.get('/api/v1/problems/'+pid).json()
        assert p['status']=='open' and p['target_status']=='reviewed'
        assert p['target_hash']==digest(p['lean_target'])
        assert p['target_validation']['proofs_of_open_problems'] is False
    with main.store.transaction() as tx:
        p=tx.get('problems','P003');p['lean_target']={'expression':'True'};tx.put('problems',p)
        p=tx.get('problems','P007');p['lean_target']=None;p['target_status']='awaiting_formalization_and_review';tx.put('problems',p)
    with pytest.raises(ValueError,match='Refusing to replace'):
        with main.store.transaction() as tx: install(tx)
    assert client.get('/api/v1/problems/P007').json()['lean_target'] is None
    assert client.get('/api/v1/problems/P003').json()['lean_target']=={'expression':'True'}


def make_review_batch(client,count,person='2',name='Aster'):
    for n in range(count):
        r=client.post('/api/v1/insights',json=dict(insight(),title='Batch target '+str(n)))
        assert r.status_code==200,r.text
    response=client.post('/api/v1/tasks/claim',json=dict(contributor_id=person,contributor_name=name,type='review'))
    assert response.status_code==200,response.text
    claim=response.json()
    body=dict(contributor_id=person,contributor_name=name,task_id=claim['task']['id'],claim_token=claim['claim_token'],
              reviews=[dict(insight_id=i,verdict='support',rationale='Checked the exact claim and proof',target_alignment='aligned',score=4) for i in claim['task']['target_ids']])
    return claim,body


def test_review_batch_ten_atomic_individual_scores_and_idempotency(client):
    claim,body=make_review_batch(client,12)
    targets=claim['task']['target_ids'];assert len(targets)==len(set(targets))==10
    pkg=claim['research_package']
    assert {x['insight_id'] for x in pkg['review_targets']}==set(targets)
    assert pkg['submission']['url']=='/api/v1/reviews/batch'
    assert pkg['submission']['schema']['properties']['reviews']['minItems']==10
    assert pkg['submission']['schema']['properties']['reviews']['items']['properties']['score']['type']=='integer'
    before=client.get('/api/v1/contributors/2/score').json()['score']
    for reviews in [body['reviews'][:-1],body['reviews']+[body['reviews'][0]],
                    [dict(x,target_alignment='aligned',score=4.0) if n==9 else x for n,x in enumerate(body['reviews'])],
                    [dict(x,insight_id='Iunknown') if n==9 else x for n,x in enumerate(body['reviews'])]]:
        assert client.post('/api/v1/reviews/batch',json=dict(body,reviews=reviews)).status_code==422
    assert all(client.get('/api/v1/insights/'+i+'/reviews').json()['items']==[] for i in targets)
    assert client.get('/api/v1/contributors/2/score').json()['score']==before
    assert client.post('/api/v1/reviews/batch',json=dict(body,claim_token='wrong')).status_code==403
    single=dict(contributor_id='2',contributor_name='Aster',task_id=body['task_id'],claim_token=body['claim_token'],target_alignment='aligned',score=4,verdict='support',rationale='Only one')
    assert client.post('/api/v1/insights/'+targets[0]+'/reviews',json=single).status_code==409
    response=client.post('/api/v1/reviews/batch',json=body,headers={'Idempotency-Key':'batch'})
    assert response.status_code==200,response.text
    assert response.json()['count']==10 and len(response.json()['items'])==10
    assert all(r['batch_size']==10 for r in response.json()['items'])
    assert client.post('/api/v1/reviews/batch',json=body,headers={'Idempotency-Key':'batch'}).json()==response.json()
    assert client.get('/api/v1/contributors/2/score').json()['score']==6
    assert all(client.get('/api/v1/insights/'+i+'/reviews').json()['items'][0]['score']==4 for i in targets)
    assert client.get('/api/v1/tasks/'+body['task_id']).json()['status']=='submitted'
    assert client.post('/api/v1/reviews/batch',json=body).status_code==409


def test_review_batch_concurrent_reservations_release_and_expiry(client):
    first,body=make_review_batch(client,25)
    def claim(person,name): return client.post('/api/v1/tasks/claim',json=dict(contributor_id=person,contributor_name=name,type='review')).json()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(claim,'3','Bob');b=pool.submit(claim,'4','Beta');rest=[a.result(),b.result()]
    batches=[set(x['task']['target_ids']) for x in [first]+rest]
    assert sorted(map(len,batches))==[5,10,10]
    assert len(set.union(*batches))==25
    assert client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',type='review')).status_code==409
    assert client.post('/api/v1/tasks/'+body['task_id']+'/renew',json=dict(claim_token=body['claim_token'])).status_code==200
    assert client.post('/api/v1/tasks/'+body['task_id']+'/release',json=dict(claim_token=body['claim_token'])).status_code==200
    new=claim('2','Aster');assert set(new['task']['target_ids'])==batches[0]
    assert client.post('/api/v1/reviews/batch',json=body).status_code in (403,409)
    with main.store.transaction() as tx:
        t=tx.get('tasks',new['task']['id']);t['expires_at']=0;tx.put('tasks',t)
    reclaimed=claim('2','Aster');assert set(reclaimed['task']['target_ids'])==batches[0]


def test_review_batch_small_pool_and_withdrawal(client):
    claim,body=make_review_batch(client,3)
    assert len(claim['task']['target_ids'])==3
    other=claim['task']['target_ids'][-1]
    response=client.post('/api/v1/contributors/1/submissions/insights/'+other+'/withdraw',json=dict(contributor_id='1',contributor_name='Alice'))
    assert response.status_code==200
    assert client.get('/api/v1/tasks/'+body['task_id']).json()['status']=='closed'
    assert client.post('/api/v1/reviews/batch',json=body).status_code==409
    c=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',type='review')).json()
    assert len(c['task']['target_ids'])==2 and other not in c['task']['target_ids']




@pytest.mark.parametrize('count',[1,3,10])
def test_review_batch_has_six_total_base_points(client,count):
    claim,body=make_review_batch(client,count)
    result=client.post('/api/v1/reviews/batch',json=body)
    assert result.status_code==200,result.text
    scores=client.get('/api/v1/contributors/2/contributions').json()['items']
    assert len(scores)==count
    assert all(e['base_points']==pytest.approx(6/count) for e in scores)
    assert client.get('/api/v1/contributors/2/score').json()['score']==6
    # Each target's quality rating remains independent of the author's task reward.
    assert all(client.get('/api/v1/insights/'+i+'/reviews').json()['items'][0]['score']==4 for i in claim['task']['target_ids'])


def test_prerequisites_share_main_base_credit_and_keep_external_citations(client):
    outside=client.post('/api/v1/insights',json=dict(insight(),contributor_id='3',contributor_name='Bob',title='Existing result')).json()
    who=dict(contributor_id='1',contributor_name='Alice')
    claimed=client.post('/api/v1/tasks/claim',json=dict(who,type='insight_explore')).json()
    lease=dict(task_id=claimed['task']['id'],claim_token=claimed['claim_token'])
    first=client.post('/api/v1/insights',json=dict(insight(),title='Supporting lemma A',is_prerequisite=True,**lease))
    assert first.status_code==200,first.text
    second=client.post('/api/v1/insights',json=dict(insight(),title='Supporting lemma B',is_prerequisite=True,references=[first.json()['id']],**lease))
    assert second.status_code==200,second.text
    assert client.get('/api/v1/tasks/'+lease['task_id']).json()['status']=='claimed'
    assert client.get('/api/v1/contributors/1/score').json()['score']==0
    refs=[first.json()['id'],second.json()['id'],outside['id']]
    if claimed['task'].get('target_id'): refs.append(claimed['task']['target_id'])
    main_result=client.post('/api/v1/insights',json=dict(insight(),title='Main contribution',references=refs,**lease))
    assert main_result.status_code==200,main_result.text
    assert client.get('/api/v1/contributors/1/score').json()['score']==10
    assert client.get('/api/v1/contributors/3/score').json()['score']==11
    for item in [first.json(),second.json()]:
        ev=client.get('/api/v1/insights/'+item['id']).json()['evaluation']
        assert ev['base_points']==0 and ev['citation_points']==0
    # A later outside contribution still pays the author of the reused prerequisite.
    future=client.post('/api/v1/insights',json=dict(insight(),contributor_id='2',contributor_name='Aster',title='Later reuse',references=[first.json()['id']]))
    assert future.status_code==200
    assert client.get('/api/v1/contributors/1/score').json()['score']==11
    tasks=client.get('/api/v1/tasks?type=lean_work').json()['items']
    assert any(t.get('dispatch_scope')=='coverage' for t in tasks)


def test_prerequisite_registration_validates_lease_and_cannot_promote_itself(client):
    identity=dict(contributor_id='1',contributor_name='Alice')
    claim=client.post('/api/v1/tasks/claim',json=dict(identity,type='insight_explore')).json()
    body=dict(insight(),title='Lemma',is_prerequisite=True,task_id=claim['task']['id'],claim_token=claim['claim_token'])
    assert client.post('/api/v1/insights',json=dict(insight(),is_prerequisite=True)).status_code==422
    for field,value in [('is_prerequisite','true'),('claims_problem_solved',1),('claim_token',None)]:
        assert client.post('/api/v1/insights',json=dict(body,**{field:value})).status_code==422
    assert client.post('/api/v1/insights',json=dict(body,claim_token='wrong')).status_code==403
    assert client.post('/api/v1/insights',json=dict(body,contributor_id='2',contributor_name='Aster')).status_code==409
    assert client.get('/api/v1/insights').json()['total']==0
    lemma=client.post('/api/v1/insights',json=body,headers={'Idempotency-Key':'support'}).json()
    assert client.post('/api/v1/insights',json=body,headers={'Idempotency-Key':'support'}).json()==lemma
    assert client.post('/api/v1/insights',json=dict(insight(),title='Promotion',supersedes_id=lemma['id'])).status_code==422
    revised=client.post('/api/v1/insights',json=dict(insight(),is_prerequisite=True,title='Corrected lemma',supersedes_id=lemma['id']))
    assert revised.status_code==422,revised.text
    assert client.get('/api/v1/contributors/1/score').json()['score']==0


def test_correcting_main_preserves_group_and_no_internal_citation_reward(client):
    who=dict(contributor_id='1',contributor_name='Alice')
    claim=client.post('/api/v1/tasks/claim',json=dict(who,type='insight_explore')).json()
    lease=dict(task_id=claim['task']['id'],claim_token=claim['claim_token'])
    p=client.post('/api/v1/insights',json=dict(insight(),title='Lemma',is_prerequisite=True,**lease)).json()
    result=client.post('/api/v1/insights',json=dict(insight(),title='Main',references=[p['id']],**lease)).json()
    corrected=legacy_revision(client,dict(insight(),title='Main correction',references=[p['id']],supersedes_id=result['id']))
    assert corrected.status_code==200,corrected.text
    assert corrected.json()['contribution_group_ids']==p['contribution_group_ids']
    assert client.get('/api/v1/contributors/1/score').json()['score']==10
    ev=client.get('/api/v1/insights/'+p['id']).json()['evaluation']
    assert ev['base_points']==0 and ev['citation_points']==0


def test_reclaimed_task_does_not_suppress_previous_authors_citation_reward(client):
    a=client.post('/api/v1/tasks/claim',json=dict(contributor_id='1',contributor_name='Alice',type='insight_explore')).json()
    p=client.post('/api/v1/insights',json=dict(insight(),title='Alice supporting lemma',is_prerequisite=True,task_id=a['task']['id'],claim_token=a['claim_token'])).json()
    assert client.post('/api/v1/tasks/'+a['task']['id']+'/release',json={'claim_token':a['claim_token']}).status_code==200
    b=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',task_id=a['task']['id'])).json()
    result=client.post('/api/v1/insights',json=dict(insight(),contributor_id='2',contributor_name='Aster',title='Another contributor continues',references=[p['id']],task_id=b['task']['id'],claim_token=b['claim_token']))
    assert result.status_code==200,result.text
    assert result.json()['contribution_group_ids']!=p['contribution_group_ids']
    assert client.get('/api/v1/contributors/1/score').json()['score']==1
    assert client.get('/api/v1/contributors/2/score').json()['score']==10


@pytest.mark.parametrize('pid',['P007','P009','P003'])
def test_every_open_problem_has_unlimited_on_demand_exploration(client,pid):
    from seeds.bootstrap import seed
    with main.store.transaction() as tx: seed(tx)
    who=dict(contributor_id='1',contributor_name='Alice',problem_id=pid,type='insight_explore')
    # Existing tasks can all be occupied; this does not exhaust the problem.
    claims=[client.post('/api/v1/tasks/claim',json=who) for _ in range(6)]
    assert all(r.status_code==200 for r in claims),[r.text for r in claims]
    assert len({r.json()['task']['id'] for r in claims})==6
    for r in claims[1:]:
        obj=r.json();assert obj['task']['target_id'] is None and obj['task']['generation']=='on_demand'
        assert obj['research_package']['problem']['id']==pid
        assert obj['research_package']['insight_catalog']['items']
    # Repeated discovery creates just one spare, not one record per read.
    for _ in range(5): assert client.get('/api/v1/tasks',params={'problem_id':pid,'type':'insight_explore','status':'available'}).json()['total']==1
    with main.store.transaction() as tx:
        assert len([t for t in tx.all('tasks') if t['problem_id']==pid and t['type']=='insight_explore'])==7
    c=claims[-1].json()
    result=client.post('/api/v1/insights',json=dict(insight(),problem_id=pid,title='Fresh on-demand progress',task_id=c['task']['id'],claim_token=c['claim_token']))
    assert result.status_code==200,result.text
    assert client.get('/api/v1/contributors/1/score').json()['score']==10


def test_on_demand_exploration_concurrency_and_explicit_task_conflict(client):
    def claim(_): return client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',problem_id='P001',type='insight_explore'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: responses=list(pool.map(claim,range(6)))
    assert all(r.status_code==200 for r in responses)
    assert len({r.json()['task']['id'] for r in responses})==6
    t=responses[0].json()['task']
    assert client.post('/api/v1/tasks/claim',json=dict(contributor_id='3',contributor_name='Bob',task_id=t['id'])).status_code==409


def test_exhausted_withdrawn_targets_replenish_but_closed_problems_do_not(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    with main.store.transaction() as tx:
        for t in tx.all('tasks'):
            if t['type']=='insight_explore' and not t.get('target_id'):t.update(status='submitted');tx.put('tasks',t)
    owner=dict(contributor_id='1',contributor_name='Alice')
    client.post('/api/v1/contributors/1/submissions/insights/'+i['id']+'/withdraw',json=owner)
    overview=client.get('/api/v1/problems/P001/overview').json()
    assert overview['stats']['available_tasks']>=1
    available=client.get('/api/v1/tasks?type=insight_explore&status=available').json()['items']
    assert len(available)==1 and available[0]['target_id'] is None
    for status in ['formal_proof_verified','archived']:
        with main.store.transaction() as tx:
            p=tx.get('problems','P001');p['status']=status;tx.put('problems',p)
            before=len(tx.all('tasks'))
        assert client.post('/api/v1/tasks/claim',json=dict(owner,problem_id='P001',type='insight_explore')).status_code==409
        client.get('/api/v1/problems/P001/overview')
        client.get('/api/v1/tasks?type=insight_explore&status=available')
        with main.store.transaction() as tx: assert len(tx.all('tasks'))==before


def test_candidate_lean_tasks_prioritize_underlying_citation_dependencies(client):
    root=client.post('/api/v1/insights',json=dict(insight(),title='Foundation')).json()
    middle=client.post('/api/v1/insights',json=dict(insight(),title='Intermediate',references=[root['id']])).json()
    top=client.post('/api/v1/insights',json=dict(insight(),title='Main theorem',claims_problem_solved=1,references=[middle['id']])).json()
    # Make exposure history favor the top: dependency order must still win.
    from apps.api.modules.recommendations import note
    with main.store.transaction() as tx:
        for _ in range(8): note(tx,'task_claims',root['id'])
    who=dict(contributor_id='2',contributor_name='Aster',type='lean_work')
    claims=[client.post('/api/v1/tasks/claim',json=who).json() for _ in range(3)]
    assert [c['task']['target_id'] for c in claims]==[root['id'],middle['id'],top['id']]


def test_lean_coverage_and_legacy_leases(client):
    local=client.post('/api/v1/insights',json=insight()).json()
    who=dict(contributor_id='2',contributor_name='Aster',type='lean_work')
    assert client.get('/api/v1/tasks?type=lean_work&status=available').json()['total']==1
    with main.store.transaction() as tx:
        old=task(tx,'P001','lean_work',local['id'],mode='formalize')
        live=task(tx,'P001','lean_work',local['id'],mode='formalize')
        live.update(status='claimed',expires_at=time.time()+3600);tx.put('tasks',live)
    client.get('/api/v1/tasks?type=lean_work')
    with main.store.transaction() as tx:
        assert tx.get('tasks',old['id'])['status']=='closed'
        assert tx.get('tasks',live['id'])['status']=='claimed'
        live['expires_at']=0;tx.put('tasks',live)
    client.get('/api/v1/tasks?type=lean_work')
    with main.store.transaction() as tx: assert tx.get('tasks',live['id'])['status']=='closed'
    candidate=client.post('/api/v1/insights',json=dict(insight(),title='Complete',claims_problem_solved=1,references=[local['id']])).json()
    available=client.get('/api/v1/tasks?type=lean_work&status=available').json()['items']
    assert {t['target_id'] for t in available}=={local['id'],candidate['id']}
    assert client.post('/api/v1/contributors/1/submissions/insights/'+candidate['id']+'/withdraw',json=dict(contributor_id='1',contributor_name='Alice',reason='Withdraw candidate')).status_code==200
    assert client.get('/api/v1/tasks?type=lean_work&status=available').json()['total']==1


def test_demo_leaderboard_keeps_real_contributions_separate(client):
    with main.store.transaction() as tx:
        p=tx.get('contributors','2');p.update(is_demo=True,demo_score=37,demo_problem_id='P001');tx.put('contributors',p)
    row=client.get('/api/v1/leaderboard').json()['items'][0]
    assert row['is_demo'] and row['contribution']['score']==37
    assert row['contribution']['demo_points']==37 and row['contribution']['submissions']==0
    assert client.get('/api/v1/leaderboard?problem_id=P001').json()['items'][0]['contribution']['score']==37
    assert client.get('/api/v1/contributors/2/contributions').json()['total']==0
    assert client.get('/api/v1/contributors/1/score').json()['score']==0


def test_private_registration_authentication_rotation_and_no_leaks(client,monkeypatch):
    monkeypatch.setattr(main,'REGISTRATION_CREDENTIALS',True)
    person=client.post('/api/v1/contributors/claim',json={'name':'PrivateTester'},headers={'Idempotency-Key':'private-once'}).json()
    secret=person['contributor_password'];ident=person['contributor_id']
    assert len(secret)==8 and secret.isdigit() and secret[0]!='0' and person['number']==secret
    assert client.post('/api/v1/contributors/claim',json={'name':'PrivateTester'},headers={'Idempotency-Key':'private-once'}).status_code==409
    for suffix in ['', '/score', '/contributions']:
        r=client.get('/api/v1/contributors/'+ident+suffix)
        assert secret not in r.text and 'password_hash' not in r.text and '"number"' not in r.text
    who=dict(contributor_id=ident,contributor_name='PrivateTester',type='insight_explore')
    assert client.post('/api/v1/tasks/claim',json=who).status_code==403
    who['contributor_password']=secret
    claimed=client.post('/api/v1/tasks/claim',json=who)
    assert claimed.status_code==200 and secret not in claimed.text
    assert client.post('/api/v1/contributors/authenticate',json={'name':'PrivateTester','password':'1'}).status_code==403
    assert client.post('/api/v1/contributors/authenticate',json={'name':'PrivateTester','password':secret}).json()['contributor_id']==ident
    body=dict(insight(),contributor_id=ident,contributor_name='PrivateTester',contributor_password=secret)
    result=client.post('/api/v1/insights',json=body)
    assert result.status_code==200 and secret not in result.text
    changed=client.post('/api/v1/contributors/'+ident+'/credential',json=dict(contributor_id=ident,contributor_name='PrivateTester',contributor_password=secret))
    assert changed.status_code==200
    new=changed.json()['contributor_password'];assert new!=secret
    assert client.post('/api/v1/tasks/claim',json=who).status_code==403
    assert client.post('/api/v1/contributors/'+ident+'/credential',json=dict(contributor_id=ident,contributor_name='PrivateTester',contributor_password=secret)).status_code==403
    who['contributor_password']=new
    assert client.post('/api/v1/tasks/claim',json=who).status_code==200
    assert client.get('/api/v1/contributors/'+ident+'/score').json()['score']==10
    assert new not in client.get('/api/v1/leaderboard').text
    with main.store.transaction() as tx:
        for kind in ['contributors','idempotency','insights','tasks','credentials']:
            text=json.dumps(tx.all(kind))
            assert secret not in text and new not in text


def test_private_password_failures_are_throttled(client,monkeypatch):
    monkeypatch.setattr(main,'REGISTRATION_CREDENTIALS',True)
    person=client.post('/api/v1/contributors/claim',json={'name':'ThrottledTester'}).json()
    statuses=[client.post('/api/v1/contributors/authenticate',json={'name':'ThrottledTester','password':'1'}).status_code for _ in range(9)]
    assert statuses==[403]*8+[429]


def dispatch_claim(client,person='2',name='Aster',**extra):
    r=client.post('/api/v1/tasks/claim',json=dict(contributor_id=person,contributor_name=name,problem_id='P001',**extra))
    assert r.status_code==200,r.text
    return r.json()

def submit_dispatch_reviews(client,c):
    t=c['task']
    r=client.post('/api/v1/reviews/batch',json=dict(contributor_id=t['contributor_id'],contributor_name=t['contributor_name'],task_id=t['id'],claim_token=c['claim_token'],reviews=[dict(insight_id=i,verdict='support',rationale='Checked independently',target_alignment='aligned',score=4) for i in t['target_ids']]))
    assert r.status_code==200,r.text

def dispatch_metrics(client):
    return client.get('/api/v1/problems/P001/dispatch').json()['state']

def test_dynamic_review_rounds_continue_and_new_insights_get_priority(client):
    for n in range(5):client.post('/api/v1/insights',json=dict(insight(),title='Work '+str(n)))
    assert dispatch_claim(client,type='research_synthesis')['task']['type']=='research_synthesis'
    first=dispatch_claim(client,type='review');assert first['task']['type']=='review' and len(first['task']['target_ids'])==5
    lean=dispatch_claim(client,'3','Bob',type='lean_work');assert lean['task']['type']=='lean_work'
    assert dispatch_claim(client,'1','Alice',type='lean_work')['task']['type']=='lean_work'
    assert dispatch_claim(client,'1','Alice',type='lean_work')['task']['type']=='lean_work'
    fallback=dispatch_claim(client,'4','Beta',type='insight_explore');assert fallback['task']['type']=='insight_explore'
    state=dispatch_metrics(client);assert state['review_reserved']==5 and state['lean_reserved']==3
    submit_dispatch_reviews(client,first)
    assert dispatch_claim(client,type='insight_explore')['task']['type']=='insight_explore'  # No repeat reviews by Aster.
    second=dispatch_claim(client,'3','Bob',type='review');assert second['task']['type']=='review'
    submit_dispatch_reviews(client,second)
    state=dispatch_metrics(client);assert state['reviews']==10 and state['review_refill']
    third=dispatch_claim(client,'4','Beta',type='review');submit_dispatch_reviews(client,third)
    state=dispatch_metrics(client);assert state['reviews']==15 and state['review_refill'] and state['review_level']==4
    assert client.post('/api/v1/tasks/'+lean['task']['id']+'/release',json={'claim_token':lean['claim_token']}).status_code==200
    assert dispatch_claim(client,type='lean_work')['task']['type']=='lean_work'
    # A globally well-reviewed corpus must still review a newly added result.
    new=client.post('/api/v1/insights',json=dict(insight(),title='New work')).json()
    state=dispatch_metrics(client);assert state['review_refill'] and state['review_min']==0
    fresh=dispatch_claim(client,type='review')
    assert fresh['task']['target_ids']==[new['id']]


def test_dynamic_reservations_bound_concurrent_lean_dispatch(client):
    for n in range(10):client.post('/api/v1/insights',json=dict(insight(),title='Concurrent '+str(n)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        claims=list(pool.map(lambda _:dispatch_claim(client),range(9)))
    assert sum(c['task']['type']=='review' for c in claims)<=1
    lean_count=sum(c['task']['type']=='lean_work' for c in claims)
    assert 1<=lean_count<=6
    assert len({c['task']['target_id'] for c in claims if c['task']['type']=='lean_work'})==lean_count
    assert sum(c['task']['type']=='research_synthesis' for c in claims)<=1
    assert any(c['task']['type']=='insight_explore' for c in claims)
    state=dispatch_metrics(client);assert state['review_reserved']<=10 and state['lean_reserved']==lean_count
    lean=next(c for c in claims if c['task']['type']=='lean_work')
    with main.store.transaction() as tx:
        t=tx.get('tasks',lean['task']['id']);t['expires_at']=0;tx.put('tasks',t)
    assert dispatch_claim(client,type='lean_work')['task']['type']=='lean_work'
    assert dispatch_metrics(client)['lean_reserved']==lean_count


def test_dynamic_lean_hysteresis_pending_verification_and_completion(client):
    from apps.api.modules.lean import POLICY
    for n in range(10):client.post('/api/v1/insights',json=dict(insight(),title='Formalize '+str(n)))
    assert dispatch_claim(client,type='research_synthesis')['task']['type']=='research_synthesis'
    # The author cannot review their own results, so available coverage work is selected.
    for index in range(6):
        c=dispatch_claim(client,'1','Alice',type='lean_work');assert c['task']['type']=='lean_work'
        a=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='1',contributor_name='Alice',task_id=c['task']['id'],claim_token=c['claim_token'],insight_id=c['task']['target_id'],source='theorem cover : True := True.intro',theorem='cover')).json()
        assert dispatch_metrics(client)['lean_reserved']>=1
        with main.store.transaction() as tx:
            v=tx.get('verifications',a['verification_id']);v.update(status='passed',policy=POLICY);put_test_verification(tx,v)
        state=dispatch_metrics(client)
        assert state['verified_insights']==index+1
        assert state['lean_refill']==(index<5)
    assert dispatch_claim(client,'1','Alice',type='insight_explore')['task']['type']=='insight_explore'


def test_candidate_dispatch_overrides_review_deficit_then_falls_back(client):
    root=client.post('/api/v1/insights',json=dict(insight(),title='Root')).json()
    top=client.post('/api/v1/insights',json=dict(insight(),title='Complete',references=[root['id']],claims_problem_solved=1)).json()
    assert dispatch_claim(client,type='research_synthesis')['task']['type']=='research_synthesis'
    a=dispatch_claim(client);b=dispatch_claim(client,'3','Bob')
    assert [a['task']['target_id'],b['task']['target_id']]==[root['id'],top['id']]
    c=dispatch_claim(client,'4','Beta',type='review');assert c['task']['type']=='review'
    artifact=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='2',contributor_name='Aster',task_id=a['task']['id'],claim_token=a['claim_token'],insight_id=root['id'],source='theorem root : True := True.intro',theorem='root'))
    assert artifact.status_code==200
    assert dispatch_claim(client,'1','Alice')['task']['type']=='insight_explore'


def test_review_assignment_prefers_low_coverage_and_limits_last_batch(client):
    targets=[client.post('/api/v1/insights',json=dict(insight(),title='Review priority '+str(n))).json() for n in range(3)]
    dispatch_metrics(client)  # Start refill below the low watermark.
    for person,name in [('2','Aster'),('3','Bob'),('4','Beta')]:
        for index,target in enumerate(targets):
            if person=='4' and index==2:continue
            assert client.post('/api/v1/insights/'+target['id']+'/reviews',json=dict(contributor_id=person,contributor_name=name,target_alignment='aligned',score=4,verdict='support',rationale='Independent assessment')).status_code==200
    state=dispatch_metrics(client);assert state['reviews']==8 and state['review_room']==1 and state['review_refill']
    assert dispatch_claim(client,type='research_synthesis')['task']['type']=='research_synthesis'
    claimed=dispatch_claim(client,'4','Beta',type='review')
    assert claimed['task']['type']=='review' and claimed['task']['target_ids']==[targets[2]['id']]
    submit_dispatch_reviews(client,claimed)
    assert dispatch_metrics(client)['reviews']==9 and dispatch_metrics(client)['review_level']==4
    assert client.get('/api/v1/problems/P001/overview').json()['stats']['reviews']==9


def test_review_prefers_fewer_scores_and_avoids_prior_reviewer(client):
    targets=[client.post('/api/v1/insights',json=dict(insight(),title='Priority '+str(n))).json() for n in range(3)]
    for index,person,name in [(0,'2','Aster'),(0,'3','Bob'),(1,'3','Bob')]:
        client.post('/api/v1/insights/'+targets[index]['id']+'/reviews',json=dict(contributor_id=person,contributor_name=name,target_alignment='aligned',score=4,verdict='support',rationale='Reviewed'))
    assert dispatch_claim(client,type='research_synthesis')['task']['type']=='research_synthesis'
    claimed=dispatch_claim(client,type='review')
    assert claimed['task']['target_ids']==[targets[2]['id']]

@pytest.mark.parametrize('n,passed,previous,expected,target',[(20,6,False,True,11),(20,7,False,False,11),(20,7,True,True,11),(20,10,True,True,11),(20,11,True,False,11),(3,1,False,True,2),(0,0,True,False,0)])
def test_lean_35_55_threshold_boundaries(n,passed,previous,expected,target):
    from types import SimpleNamespace
    from apps.api.modules.scheduling import snapshot
    from apps.api.modules.lean import POLICY,LIBRARY_ID
    class Tx:
        def get(self,kind,ident):return {'lean_refill':previous} if kind=='dispatch_states' else None
        def all(self,*args):return []
        def put(self,*args):pass
    insights={str(i):dict(id=str(i),problem_id='P001',contributor_id='1') for i in range(n)}
    artifacts={str(i):dict(id=str(i),insight_id=str(i),verification_id=str(i),library_id=LIBRARY_ID) for i in range(passed)}
    scores=SimpleNamespace(objects={'insights':insights,'lean_artifacts':artifacts},withdrawn=set(),superseded=set(),latest_reviews={},verified_insights=set(map(str,range(passed))),verifications={str(i):dict(status='passed',policy=POLICY) for i in range(passed)})
    state,*_=snapshot(Tx(),'P001',scores)
    assert state['lean_refill']==expected
    assert state['lean_target']==target
    assert state['lean_room']==max(0,target-passed)


def test_activity_excludes_reviews_failures_and_private_fields(client):
    from apps.api.modules.lean import POLICY,LIBRARY_ID
    first=client.post('/api/v1/insights',json=insight()).json()
    second=client.post('/api/v1/insights',json=dict(insight(),title='Newest result')).json()
    with main.store.transaction() as tx:
        for ident,status,stamp in [('Vpass','passed',2000000000),('Vfail','failed',2000000001)]:
            tx.put('verifications',dict(id=ident,artifact_id='L'+ident,status=status,policy=POLICY,finished_at=stamp,target_matched=True,target_hash=tx.get('insight_targets',first['id'])['target_hash']))
            tx.put('lean_artifacts',dict(id='L'+ident,insight_id=first['id'],problem_id='P001',verification_id=ident,policy=POLICY,target=tx.get('insight_targets',first['id']),library_id=LIBRARY_ID,created_at=1,contributor_name='Alice',contributor_password='private',contributor_id='1'))
        tx.put('reviews',dict(id='Ractivity',created_at=2000000002,target_id=first['id'],contributor_id='unregistered',score=4))
    items=client.get('/api/v1/activity').json()['items']
    assert items[0]['event']=='lean_passed' and items[0]['timestamp']==2000000000
    assert len(items)==3
    assert {x['event'] for x in items}=={'insight_submitted','lean_passed'}
    assert all(set(x)=={'event','object_id','kind','problem_id','title','author_name','timestamp'} for x in items)
    assert len(client.get('/api/v1/activity?limit=1').json()['items'])==1
    assert client.get('/api/v1/activity?limit=0').status_code==422

def test_research_synthesis_claim_submit_context_and_credit(client):
    items=[client.post('/api/v1/insights',json=dict(insight(),title='Synthesis '+str(n))).json() for n in range(30)]
    claim=dispatch_claim(client,type='research_synthesis')
    assert claim['task']['type']=='research_synthesis'
    assert len(claim['research_package']['synthesis_catalog'])==30
    assert claim['research_package']['submission']['url'].endswith('/research-guidance')
    assert dispatch_claim(client,'3','Bob')['task']['type']!='research_synthesis'
    from test_research_guides import graph
    body=dict(contributor_id='2',contributor_name='Aster',problem_id='P001',task_id=claim['task']['id'],claim_token=claim['claim_token'],base_hash=claim['research_package']['research_guidance']['base_hash'],header=dict(format='humanity-proof-graphs-v1',problem_id='P001',graphs=[graph(items[0]['id'])]),body_markdown='Focus on uniform descent.')
    bad=dict(body,claim_token='wrong')
    assert client.post('/api/v1/research-plans',json=bad).status_code==403
    result=client.post('/api/v1/research-plans',json=body,headers={'Idempotency-Key':'synthesis'});assert result.status_code==200,result.text
    plan=result.json()
    assert client.post('/api/v1/research-plans',json=body,headers={'Idempotency-Key':'synthesis'}).json()==plan
    assert client.get('/api/v1/contributors/2/score').json()['score']==15
    guide=client.get('/api/v1/problems/P001/research-guidance').json();assert guide['latest']['id']==plan['id']
    explore=dispatch_claim(client,type='insight_explore')
    assert explore['research_package']['research_guidance']['latest']['id']==plan['id']
    # The initial shared guide is covered; a second edit needs twenty new Insights.
    assert dispatch_metrics(client)['synthesis_target']==1
    assert client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',problem_id='P001',type='research_synthesis')).status_code==409
    for n in range(20):client.post('/api/v1/insights',json=dict(insight(),title='Route update trigger '+str(n)))
    assert dispatch_metrics(client)['synthesis_target']==2
    next_claim=dispatch_claim(client,type='research_synthesis');assert next_claim['task']['previous_plan_id']==plan['id']
    assert client.post('/api/v1/research-plans',json=dict(body,task_id=next_claim['task']['id'],claim_token=next_claim['claim_token'])).status_code==409
    assert client.post('/api/v1/contributors/2/submissions/research_updates/'+plan['update_id']+'/withdraw',json=dict(contributor_id='2',contributor_name='Aster')).status_code==200
    assert client.get('/api/v1/problems/P001/research-guidance').json()['latest']['id']==plan['id']

def test_route_update_budget_grows_every_twenty_new_insights(client):
    first=client.post('/api/v1/insights',json=insight()).json()
    initial=dispatch_metrics(client);assert initial['synthesis_target']==1
    for n in range(19):client.post('/api/v1/insights',json=dict(insight(),title='Growth '+str(n)))
    assert dispatch_metrics(client)['synthesis_target']==1
    client.post('/api/v1/insights',json=dict(insight(),title='Twentieth new insight'))
    assert dispatch_metrics(client)['synthesis_target']==2
    with main.store.transaction() as tx:
        schedule=tx.get('roadmap_schedule','P001');assert len(set(schedule['seen_ids'])-set(schedule['baseline_ids']))==20
    for n in range(20):client.post('/api/v1/insights',json=dict(insight(),title='Second block '+str(n)))
    assert dispatch_metrics(client)['synthesis_target']==3


def test_exploration_has_no_mandatory_insight_target(client):
    root=client.post('/api/v1/insights',json=insight()).json()
    with main.store.transaction() as tx:
        assert not any(t['type']=='insight_explore' and t.get('target_id') for t in tx.all('tasks'))
        old=task(tx,'P001','insight_explore',root['id'])
        live=task(tx,'P001','insight_explore',root['id'])
        live.update(status='claimed',contributor_id='2',contributor_name='Aster',claim_hash=main.digest('old-token'),expires_at=time.time()+1000)
        tx.put('tasks',live)
    tasks=client.get('/api/v1/tasks?type=insight_explore').json()['items']
    assert next(t for t in tasks if t['id']==old['id'])['status']=='closed'
    assert next(t for t in tasks if t['id']==live['id'])['target_id'] is None
    result=client.post('/api/v1/insights',json=dict(insight(),contributor_id='2',contributor_name='Aster',title='Independent direction',task_id=live['id'],claim_token='old-token',references=[]))
    assert result.status_code==200,result.text
    claim=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',problem_id='P001',type='insight_explore')).json()
    assert claim['task']['target_id'] is None


def test_passed_insight_rejects_more_code_and_closes_lean_leases(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    body=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],source='theorem first : True := True.intro',theorem='first')
    a=client.post('/api/v1/lean/artifacts',json=body).json()
    with main.store.transaction() as tx:
        v=tx.get('verifications',a['verification_id']);v.update(status='passed',finished_at=10);put_test_verification(tx,v)
        t=task(tx,'P001','lean_work',i['id'],mode='repair')
        t.update(status='claimed',expires_at=time.time()+3600,claim_hash='reserved',contributor_id='3');tx.put('tasks',t)
    response=client.post('/api/v1/lean/artifacts',json=dict(body,source='theorem second : True := True.intro',theorem='second'))
    assert response.status_code==409
    assert response.json()['detail']['existing_id']==a['id']
    client.get('/api/v1/problems/P001/overview')
    with main.store.transaction() as tx:
        closed=tx.get('tasks',t['id']);assert closed['status']=='closed' and closed['claim_hash']==''
    # Auditing a passed artifact remains possible.
    report=client.post('/api/v1/lean/artifacts/'+a['id']+'/audits',json=dict(contributor_id='3',contributor_name='Bob',verdict='pass',rationale='Checked the entry statement and proof'))
    assert report.status_code==200,report.text


def test_concurrent_and_historical_lean_proofs_reward_only_first_success(client):
    from apps.api.modules.scoring import Scores
    i=client.post('/api/v1/insights',json=insight()).json()
    artifacts=[]
    for person,name,entry in [('2','Aster','first'),('3','Bob','second')]:
        r=client.post('/api/v1/lean/artifacts',json=dict(contributor_id=person,contributor_name=name,insight_id=i['id'],source=f'theorem {entry} : True := True.intro',theorem=entry))
        assert r.status_code==200;artifacts.append(r.json())
    with main.store.transaction() as tx:
        for index,a in enumerate(artifacts):
            v=tx.get('verifications',a['verification_id']);v.update(status='passed',finished_at=10+index);put_test_verification(tx,v)
        scores=Scores(tx)
        assert scores.total('2')['by_kind']['lean_work']==8
        assert scores.total('3')['by_kind']['lean_work']==0
        assert scores.records('3')[0]['status']=='duplicate_formalization'
        tx.put('submission_states',dict(id='withdraw-first',object_id=artifacts[0]['id'],withdrawn=True))
        assert Scores(tx).total('3')['by_kind']['lean_work']==0


def test_lean_repairs_remain_available_after_failure(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    body=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],source='theorem first : True := True.intro',theorem='first')
    a=client.post('/api/v1/lean/artifacts',json=body).json()
    with main.store.transaction() as tx:
        v=tx.get('verifications',a['verification_id']);v['status']='failed';put_test_verification(tx,v)
    assert client.post('/api/v1/lean/artifacts',json=dict(body,source='theorem repaired : True := True.intro',theorem='repaired')).status_code==200


def test_canonical_proof_catalog_counts_and_legacy_modules(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    artifacts=[]
    for n in range(2):
        response=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],source=f'theorem entry{n} : True := True.intro',theorem=f'entry{n}'))
        assert response.status_code==200;artifacts.append(response.json())
    with main.store.transaction() as tx:
        for n,a in enumerate(artifacts):
            v=tx.get('verifications',a['verification_id']);v.update(status='passed',finished_at=10+n);put_test_verification(tx,v)
    catalog=client.get('/api/v1/lean/artifacts').json()
    assert catalog['total']==1 and catalog['items'][0]['id']==artifacts[0]['id']
    assert client.get('/api/v1/problems/P001/overview').json()['stats']['verified_lean']==1
    detail=client.get('/api/v1/insights/'+i['id']).json()
    assert detail['canonical_proof_id']==artifacts[0]['id'] and len(detail['lean_artifacts'])==1
    historical=client.get('/api/v1/lean/artifacts/'+artifacts[1]['id']).json()
    assert historical['interface']['status']=='historical_duplicate' and not historical['interface']['reusable']
    assert client.get('/api/v1/lean/artifacts/'+artifacts[1]['id']+'/bundle').status_code==200
    assert client.get('/api/v1/lean/artifacts?status=all').json()['total']==2
    j=client.post('/api/v1/insights',json=dict(insight(),title='Next lemma')).json()
    source=dict(contributor_id='3',contributor_name='Bob',insight_id=j['id'],source='theorem next : True := entry0',theorem='next',dependencies=[artifacts[1]['id']])
    rejected=client.post('/api/v1/lean/artifacts',json=source)
    assert rejected.status_code==409 and rejected.json()['detail']['existing_id']==artifacts[0]['id']
    assert client.post('/api/v1/lean/artifacts',json=dict(source,dependencies=[artifacts[0]['id']])).status_code==200


def test_worker_cancels_queued_duplicate_without_compiling(client,monkeypatch):
    from apps.worker import main as worker
    i=client.post('/api/v1/insights',json=insight()).json()
    artifacts=[]
    for n in range(2):
        artifacts.append(client.post('/api/v1/lean/artifacts',json=dict(contributor_id='2',contributor_name='Aster',insight_id=i['id'],source=f'theorem entry{n} : True := True.intro',theorem=f'entry{n}')).json())
    with main.store.transaction() as tx:
        v=tx.get('verifications',artifacts[0]['verification_id']);v.update(status='passed',finished_at=10);put_test_verification(tx,v)
    monkeypatch.setattr(worker,'STORE',main.store)
    def unexpected(*args):raise AssertionError('Duplicate proof must not be compiled')
    monkeypatch.setattr(worker,'verify_artifact',unexpected)
    assert not worker.once()
    assert client.get('/api/v1/lean/verifications/'+artifacts[1]['verification_id']).json()['status']=='cancelled'


def test_compact_insight_graph_preserves_links_and_verified_status(client):
    first=client.post('/api/v1/insights',json=dict(insight(),proof='long private-to-detail proof '*500)).json()
    second=client.post('/api/v1/insights',json=dict(insight(),title='Second',references=[first['id']])).json()
    a=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='2',contributor_name='Aster',insight_id=first['id'],source='theorem result : True := True.intro',theorem='result')).json()
    with main.store.transaction() as tx:
        v=tx.get('verifications',a['verification_id']);v['status']='passed';put_test_verification(tx,v)
    graph=client.get('/api/v1/problems/P001/insight-graph').json()
    assert graph['total']==2
    nodes={x['id']:x for x in graph['items']}
    assert nodes[first['id']]['lean_passed']
    assert nodes[second['id']]['references']==[first['id']]
    assert all('proof' not in x and len(x['claim'])<=400 for x in graph['items'])
    assert client.get('/api/v1/problems/UNKNOWN/insight-graph').status_code==404



def test_frontend_release_assets_and_wechat_contact(client,monkeypatch):
    monkeypatch.setenv('APP_RELEASE','a'*40)
    first=client.get('/').text
    assert '__ASSET_VERSION__' not in first
    assert 'id="wechat-toggle"' in first and 'id="wechat-contact-panel" hidden' in first
    assert 'wechat-collatz.jpg?v=' in first
    assert client.get('/assets/wechat-collatz.jpg').status_code==200
    monkeypatch.setenv('APP_RELEASE','b'*40)
    assert first!=client.get('/').text


@pytest.mark.parametrize('count',[99,100,101])
def test_exploration_catalog_boundary(client,count):
    with main.store.transaction() as tx:
        for index in range(count):tx.new('insights','I',dict(insight(),title=f'Catalog {index}'))
    claimed=dispatch_claim(client,type='insight_explore')
    catalog=claimed['research_package']['insight_catalog']
    assert len(catalog['items'])==min(count,100)
    assert len({x['id'] for x in catalog['items']})==min(count,100)
    assert catalog['policy']['sample_size']==100
    assert catalog['total_available']==count
    assert 'Actively search the platform' in claimed['research_package']['prompt']['text']


def test_synthesis_retains_all_insights_and_public_catalog_keeps_default(client):
    with main.store.transaction() as tx:
        for index in range(125):tx.new('insights','I',dict(insight(),title=f'Full catalog {index}'))
    synthesis=dispatch_claim(client,type='research_synthesis')['research_package']
    assert len(synthesis['synthesis_catalog'])==125
    assert len(client.get('/api/v1/problems/P001/recommendations').json()['items'])==125
    explore=dispatch_claim(client,'3','Bob',type='insight_explore')['research_package']
    assert len(explore['insight_catalog']['items'])==100


def test_insight_ranking_scores_search_and_pagination(client):
    first=client.post('/api/v1/insights',json=dict(insight(),title='Referenced result')).json()
    second=client.post('/api/v1/insights',json=dict(insight(),title='Reviewed result')).json()
    third=client.post('/api/v1/insights',json=dict(insight(),title='Citing result',references=[first['id']])).json()
    assert client.post('/api/v1/insights/'+second['id']+'/reviews',json=dict(contributor_id='4',contributor_name='Beta',verdict='support',rationale='Checked the proof',target_alignment='aligned',score=5)).status_code==200
    data=client.get('/api/v1/insights/ranking?problem_id=P001&limit=2').json()
    assert data['total']==3
    assert [x['id'] for x in data['items']]==[second['id'],first['id']]
    assert [x['evaluation']['contribution_points'] for x in data['items']]==[15,10]
    last=client.get('/api/v1/insights/ranking?problem_id=P001&offset=2').json()['items'][0]
    assert last['id']==third['id'] and last['rank']==3
    search=client.get('/api/v1/insights/ranking',params=dict(problem_id='P001',q='Citing')).json()
    assert search['items'][0]['rank']==3 and search['total']==1
    assert all('proof' not in x and 'contributor_id' not in x for x in data['items'])
    assert client.get('/api/v1/insights/ranking?problem_id=UNKNOWN').status_code==404
    assert client.get('/api/v1/insights/ranking?limit=101').status_code==422


def test_insight_ranking_excludes_replaced_withdrawn_and_includes_lean_bonus(client):
    old=client.post('/api/v1/insights',json=insight()).json()
    replacement=legacy_revision(client,dict(insight(),title='Updated result',supersedes_id=old['id'])).json()
    hidden=client.post('/api/v1/insights',json=dict(insight(),title='Withdrawn result')).json()
    assert client.post('/api/v1/contributors/1/submissions/insights/'+hidden['id']+'/withdraw',json=dict(contributor_id='1',contributor_name='Alice')).status_code==200
    with main.store.transaction() as tx:
        from apps.api.modules.lean import POLICY,LIBRARY_ID
        target=tx.get('insight_targets',replacement['id'])
        tx.put('lean_artifacts',dict(id='L-ranking',insight_id=replacement['id'],verification_id='V-ranking',contributor_id='2',created_at=1,target=target,policy=POLICY,library_id=LIBRARY_ID))
        tx.put('verifications',dict(id='V-ranking',artifact_id='L-ranking',status='passed',created_at=1,policy=POLICY,target_hash=target['target_hash'],target_matched=True))
    data=client.get('/api/v1/insights/ranking?problem_id=P001').json()
    assert data['total']==1 and data['items'][0]['id']==replacement['id']
    assert data['items'][0]['evaluation']['contribution_points']==20
    html=client.get('/').text
    assert 'data-tab="insight-ranking"' in html and 'id="tab-insight-ranking"' in html


def test_activity_target_completion_and_one_score_milestone(client):
    item=client.post('/api/v1/insights',json=insight()).json()
    with main.store.transaction() as tx:
        tx.put('contributors',dict(id='2',name='Formalizer'))
        target=tx.get('insight_targets',item['id'])
        target.update(migration_batch_id='Bnews',registered_by='2',finished_at=2000000000)
        tx.put('insight_targets',target)
        for n in range(21):
            tx.put('insights',dict(id='Icite'+str(n),problem_id='P001',title='Lemma',created_at=1,contributor_id='2',references=[item['id']]))
    events=client.get('/api/v1/activity?limit=50').json()['items']
    target_event=next(e for e in events if e['event']=='lean_target_valid')
    assert target_event['author_name']=='Formalizer' and target_event['kind']=='insights'
    milestone=next(e for e in events if e['event']=='insight_score_30')
    assert milestone['object_id']==item['id']
    again=client.get('/api/v1/activity?limit=50').json()['items']
    assert [e for e in again if e['event']=='insight_score_30']==[milestone]
    with main.store.transaction() as tx:
        assert len(tx.all('activity_milestones'))==1
        from apps.api.infrastructure.store import digest
        tx.put('submission_states',dict(id=digest(['insights',item['id']]),object_id=item['id'],withdrawn=True))
    assert not any(e['object_id']==item['id'] for e in client.get('/api/v1/activity?limit=50').json()['items'])


def test_activity_score_exactly_thirty_has_no_milestone(client):
    item=client.post('/api/v1/insights',json=insight()).json()
    with main.store.transaction() as tx:
        for n in range(20):
            tx.put('insights',dict(id='Icite'+str(n),problem_id='P001',title='Lemma',created_at=1,contributor_id='2',references=[item['id']]))
    assert not any(e['event']=='insight_score_30' for e in client.get('/api/v1/activity?limit=50').json()['items'])


def test_configured_lean_memory_is_exposed():
    import os, subprocess, sys, json
    env=dict(os.environ,LEAN_MEMORY_MB='6144')
    code="import json;from apps.api.modules.lean import environment;print(json.dumps(environment()))"
    result=json.loads(subprocess.check_output([sys.executable,'-c',code],env=env,text=True))
    assert result['limits']['container_memory_mb']==6144


def test_review_balancing_ignores_global_total_and_quality_weights(client):
    targets=[client.post('/api/v1/insights',json=dict(insight(),title='Balanced '+str(n))).json() for n in range(3)]
    with main.store.transaction() as tx:
        for j in range(6):tx.put('contributors',dict(id='R'+str(j),name='Reviewer'+str(j),created_at=1))
        for index,count in enumerate([6,3,0]):
            for j in range(count):
                tx.put('reviews',dict(id=f'Ebalanced{index}_{j}',target_id=targets[index]['id'],target_kind='insights',problem_id='P001',contributor_id='R'+str(j),contributor_name='Reviewer'+str(j),score=5,verdict='support',rationale='Synthetic independent review',created_at=j+1))
    state=dispatch_metrics(client)
    assert state['reviews']==9 and state['review_min']==0 and state['review_room']==1
    claim=dispatch_claim(client,type='review')
    assert claim['task']['target_ids']==[targets[2]['id']]


def test_review_balancing_reservations_and_expired_leases(client):
    targets=[client.post('/api/v1/insights',json=dict(insight(),title='Reservation '+str(n))).json() for n in range(2)]
    r=client.post('/api/v1/insights/'+targets[1]['id']+'/reviews',json=dict(contributor_id='4',contributor_name='Beta',score=4,verdict='support',rationale='Independent assessment'))
    assert r.status_code==200,r.text
    first=dispatch_claim(client,type='review')
    assert first['task']['target_ids']==[targets[0]['id']]
    second=dispatch_claim(client,'3','Bob',type='review')
    assert second['task']['target_ids']==[targets[1]['id']]
    assert dispatch_metrics(client)['review_reserved']==2
    with main.store.transaction() as tx:
        t=tx.get('tasks',first['task']['id']);t['expires_at']=0;tx.put('tasks',t)
    fresh=dispatch_claim(client,'4','Beta',type='review')
    assert fresh['task']['target_ids']==[targets[0]['id']]
