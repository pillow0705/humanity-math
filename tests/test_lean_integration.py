"""Run on deployment host: sudo -E ... pytest -q tests/test_lean_integration.py."""
import os,time
import pytest
from fastapi.testclient import TestClient
from apps.api import main
from apps.api.infrastructure.store import Store
from apps.worker import main as worker


def post_insight(client,payload):
    expression='True'
    if payload.get('problem_id')=='PMATH':
        expression='Nat.Prime 7' if payload.get('title')=='Prime seven' else '∀ x : ℝ, 0 ≤ x ^ 2'
    payload.setdefault('lean_target',dict(definitions='import Mathlib' if payload.get('problem_id')=='PMATH' else '',expression=expression))
    response=client.post('/api/v1/insights',json=payload)
    if response.status_code==200:
        assert worker.once()
        target=client.get('/api/v1/insights/'+response.json()['id']+'/lean-target').json()
        assert target['status']=='valid',target
    return response

@pytest.fixture(autouse=True)
def legacy_test_identities(monkeypatch):
    monkeypatch.setattr(main,'REGISTRATION_CREDENTIALS',False)

@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires Docker and the pinned Lean toolchain')
def test_real_lean_chain_and_fixed_target(tmp_path,monkeypatch):
    s=Store(tmp_path); (tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s); monkeypatch.setattr(worker,'STORE',s); monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:
        tx.put('problems',dict(id='PTEST',title='Test only',statement='True',lean_target={'expression':'True'},target_status='reviewed',created_at=time.time(),status='open'))
    c=TestClient(main.app)
    assert c.post('/api/v1/contributors/claim',json={'name':'Tester'}).status_code==200
    i=post_insight(c,dict(contributor_id='1',contributor_name='Tester',problem_id='PTEST',title='True',claim='True',proof='True introduction',proof_status='claimed_complete')).json()
    def create(source,theorem,deps=[]):
        current=post_insight(c,dict(contributor_id='1',contributor_name='Tester',problem_id='PTEST',title='Proof '+theorem,claim='True',proof='Test proof',proof_status='claimed_complete')).json()
        r=c.post('/api/v1/lean/artifacts',json=dict(contributor_id='1',contributor_name='Tester',insight_id=current['id'],source=source,theorem=theorem,dependencies=deps))
        assert r.status_code==200,r.text
        a=r.json(); assert worker.once()
        v=c.get('/api/v1/lean/verifications/'+a['verification_id']).json()
        return a,v
    a,v=create('theorem first : True := True.intro','first')
    assert v['status']=='passed',v
    assert v['theorem_signature']=='first : True'
    assert c.get('/api/v1/lean/artifacts').json()['items'][0]['import_statement']=='import MC.'+a['id']
    cached=worker.CACHE/a['content_hash']/'module.olean'; old_mtime=cached.stat().st_mtime_ns
    unused,unused_v=create('theorem unusedLemma : True := True.intro','unusedLemma')
    assert unused_v['status']=='passed'
    b,w=create('theorem second : True := first','second',[a['id'],unused['id']])
    assert w['status']=='passed',w
    assert set(w['reused_dependencies'])=={a['id'],unused['id']}
    assert cached.stat().st_mtime_ns==old_mtime
    sol=c.post('/api/v1/problems/PTEST/solutions',json=dict(contributor_id='1',contributor_name='Tester',artifact_id=b['id'],proof='Reuse first to prove True.'))
    assert sol.status_code==200,sol.text
    assert worker.once()
    result=c.get('/api/v1/solutions/'+sol.json()['id']).json()
    assert result['status']=='verified',result
    assert set(result['used_artifact_ids'])=={a['id'],b['id']}
    assert set(result['used_insight_ids'])=={a['insight_id'],b['insight_id']}
    assert c.get('/api/v1/problems/PTEST').json()['status']=='formal_proof_verified'
    _,v=create('def number : Nat := 3','number')
    assert v['status']=='failed' and 'not a proof' in v['log']
    wrong,v=create('theorem broken : False := True.intro','broken')
    assert v['status']=='failed',v
    repair_tasks=c.get('/api/v1/tasks',params={'type':'lean_work'}).json()['items']
    assert repair_tasks==[]
    with s.transaction() as tx:
        p=tx.get('problems','PTEST'); p['lean_target']={'expression':'False'}; tx.put('problems',p)
    sol=c.post('/api/v1/problems/PTEST/solutions',json=dict(contributor_id='1',contributor_name='Tester',artifact_id=b['id'],proof='Wrong target.')).json()
    assert worker.once()
    assert c.get('/api/v1/solutions/'+sol['id']).json()['status']=='failed'
    assert c.get('/api/v1/tasks',params={'type':'lean_work'}).json()['items']==[]
    cached.write_bytes(b'corrupted cache')
    _,v=create('theorem third : True := first','third',[a['id']])
    assert v['status']=='failed' and 'corruption' in v['log']


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires Docker and pinned Lean/Mathlib')
def test_mathlib_real_proof_reuse_and_target(tmp_path,monkeypatch):
    import io,json,subprocess,zipfile
    from apps.api.modules.lean import LIBRARY_ID
    s=Store(tmp_path); (tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s); monkeypatch.setattr(worker,'STORE',s); monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:
        tx.put('problems',dict(id='PMATH',title='Test real squares',statement='Real squares are nonnegative',
               lean_target={'expression':'∀ x : ℝ, 0 ≤ x ^ 2'},target_status='reviewed',created_at=time.time(),status='open'))
    c=TestClient(main.app)
    c.post('/api/v1/contributors/claim',json={'name':'MathlibTester'})
    identity=dict(contributor_id='1',contributor_name='MathlibTester')
    def insight(title):
        r=post_insight(c,dict(identity,problem_id='PMATH',title=title,
                   claim='For every real x, x squared is nonnegative.',proof='Apply sq_nonneg.',proof_status='claimed_complete'))
        assert r.status_code==200,r.text
        return r.json()['id']
    def create(i,source,theorem,deps=[]):
        r=c.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=i,source=source,theorem=theorem,dependencies=deps))
        assert r.status_code==200,r.text
        a=r.json(); assert worker.once()
        v=c.get('/api/v1/lean/verifications/'+a['verification_id']).json()
        return a,v
    i=insight('Squares')
    a,v=create(i,'import Mathlib\ntheorem real_square (x : ℝ) : 0 ≤ x^2 := by positivity','real_square')
    assert v['status']=='passed',v
    assert v['cache']['library_id']==LIBRARY_ID
    # A downloaded project must reproduce the same environment through Lake.
    project=tmp_path/'downloaded-bundle';project.mkdir()
    with zipfile.ZipFile(io.BytesIO(c.get('/api/v1/lean/artifacts/'+a['id']+'/bundle').content)) as z: z.extractall(project)
    packages=project/'.lake/packages';packages.mkdir(parents=True)
    for p in json.loads((project/'lake-manifest.json').read_text())['packages']:
        target=worker.MATHLIB if p['name']=='mathlib' else worker.MATHLIB/'.lake/packages'/p['name']
        (packages/p['name']).symlink_to(target.resolve(),target_is_directory=True)
    local_env=dict(os.environ,PATH=str(worker.TOOLS.resolve()/'bin')+':'+os.environ['PATH'])
    build=subprocess.run(['lake','build',a['module']],cwd=project,env=local_env,capture_output=True,text=True,timeout=180)
    assert build.returncode==0,build.stdout+build.stderr
    cached=worker.CACHE/a['content_hash']/'module.olean'; before=cached.stat().st_mtime_ns
    j=insight('Reuse squares')
    b,v=create(j,'theorem all_squares : ∀ x : ℝ, 0 ≤ x^2 := real_square','all_squares',[a['id']])
    assert v['status']=='passed',v
    assert v['reused_dependencies']==[a['id']] and cached.stat().st_mtime_ns==before
    sol=c.post('/api/v1/problems/PMATH/solutions',json=dict(identity,artifact_id=b['id'],proof='Apply real_square.'))
    assert sol.status_code==200,sol.text
    assert worker.once()
    result=c.get('/api/v1/solutions/'+sol.json()['id']).json()
    assert result['status']=='verified',result
    assert set(result['used_insight_ids'])=={i,j}
    _,v=create(insight('Prime seven'),'import Mathlib.Data.Nat.Prime.Basic\nimport Mathlib.Tactic.NormNum.Prime\ntheorem prime_seven : Nat.Prime 7 := by norm_num','prime_seven')
    assert v['status']=='passed',v
    _,v=create(insight('Invalid proof'),'import Mathlib\ntheorem false_math : False := by norm_num','false_math')
    assert v['status']=='failed',v
    manifest=worker.CACHE/a['content_hash']/'manifest.json'
    meta=json.loads(manifest.read_text());meta['library_id']='another-environment';manifest.write_text(json.dumps(meta))
    _,v=create(insight('Environment mismatch'),'theorem mismatch : ∀ x : ℝ, 0 ≤ x^2 := real_square','mismatch',[a['id']])
    assert v['status']=='failed' and 'Mathlib environment mismatch' in v['log']


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
@pytest.mark.parametrize('target',['True','False',None])
@pytest.mark.parametrize('uses_support',[True,False])
def test_automatic_candidate_completion(tmp_path,monkeypatch,target,uses_support):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:
        tx.put('problems',dict(id='PCAND',title='Target',statement=target or 'True',lean_target={'expression':target} if target else None,target_status='reviewed' if target else 'awaiting_formalization_and_review',status='open',created_at=time.time()))
    c=TestClient(main.app);c.post('/api/v1/contributors/claim',json={'name':'CandidateTester'})
    identity=dict(contributor_id='1',contributor_name='CandidateTester')
    def insight(title,flag=0,refs=[]):
        r=post_insight(c,dict(identity,problem_id='PCAND',title=title,claim='True',proof='By introduction',proof_status='claimed_complete',claims_problem_solved=flag,references=refs))
        assert r.status_code==200,r.text
        return r.json()
    dep=insight('Dependency');candidate=insight('Complete claim',1,[dep['id']])
    def submit(i,source,theorem,deps=[]):
        r=c.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=i['id'],source=source,theorem=theorem,dependencies=deps))
        assert r.status_code==200,r.text
        a=r.json();assert worker.once()
        assert c.get('/api/v1/lean/verifications/'+a['verification_id']).json()['status']=='passed'
        return a
    a=submit(dep,'theorem prerequisite : True := True.intro','prerequisite')
    # Textual references need canonical supporting proofs, but the fixed leaf need not import an unused lemma.
    b=submit(candidate,'theorem final_proof : True := '+('prerequisite' if uses_support else 'True.intro'),'final_proof',[a['id']] if uses_support else [])
    denied=c.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=candidate['id'],source='theorem duplicate : True := True.intro',theorem='duplicate'))
    assert denied.status_code==409
    before=(worker.CACHE/a['content_hash']/'module.olean').stat().st_mtime_ns
    if target is None:
        assert not worker.once()
        assert c.get('/api/v1/insights/'+candidate['id']).json()['completion']['status']=='awaiting_reviewed_target'
        assert c.get('/api/v1/problems/PCAND').json()['status']=='open'
        with s.transaction() as tx:
            p=tx.get('problems','PCAND');p.update(lean_target={'expression':'True'},target_status='reviewed');tx.put('problems',p)
    assert worker.once()  # No manual solution submission: the candidate triggers this check.
    state=c.get('/api/v1/insights/'+candidate['id']).json()['completion']
    result=c.get('/api/v1/solutions/'+state['solution_id']).json()
    if target=='False':
        assert result['status']=='failed' and state['status']=='final_failed'
        assert c.get('/api/v1/problems/PCAND').json()['status']=='open'
        repairs=c.get('/api/v1/tasks?type=lean_work').json()['items']
        assert not any(t.get('target_id')==candidate['id'] and t['status']=='available' and t.get('mode') in ('formalize','repair') for t in repairs)
        assert not worker.once()  # Failed final checks do not loop forever.
    else:
        assert result['status']=='verified' and result['automatic']
        assert set(result['used_insight_ids'])==({dep['id'],candidate['id']} if uses_support else {candidate['id']})
        assert a['id'] in result['supporting_artifact_ids']
        assert c.get('/api/v1/problems/PCAND').json()['status']=='formal_proof_verified'
        assert not c.get('/api/v1/tasks?status=available').json()['items']
        assert c.post('/api/v1/tasks/claim',json=identity).status_code==409
    assert (worker.CACHE/a['content_hash']/'module.olean').stat().st_mtime_ns==before


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
@pytest.mark.parametrize('original_target,expected',[('False','verified'),('True','failed')])
def test_disproof_candidate_verifies_exact_negation(tmp_path,monkeypatch,original_target,expected):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:
        tx.put('problems',dict(id='PNEG',title='Disproof target',statement=original_target,lean_target={'expression':original_target},target_status='reviewed',status='open',created_at=time.time()))
    c=TestClient(main.app);c.post('/api/v1/contributors/claim',json={'name':'DisproofTester'})
    identity=dict(contributor_id='1',contributor_name='DisproofTester')
    result=post_insight(c,dict(identity,problem_id='PNEG',title='Disproof',claim='¬ False',proof='Identity',proof_status='claimed_complete',claims_problem_solved=-1,lean_target=dict(expression='¬ False')))
    assert result.status_code==200,result.text
    i=result.json()
    a=c.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=i['id'],source='theorem negation : ¬ False := fun h => h',theorem='negation')).json()
    assert worker.once()
    assert c.get('/api/v1/lean/verifications/'+a['verification_id']).json()['status']=='passed'
    assert worker.once()
    state=c.get('/api/v1/insights/'+i['id']).json()['completion']
    result=c.get('/api/v1/solutions/'+state['solution_id']).json()
    assert result['status']==expected,result
    assert result['claims_problem_solved']==-1
    p=c.get('/api/v1/problems/PNEG').json()
    if expected=='verified':
        assert p['status']=='formal_proof_verified' and p['resolution']=='disproved'
    else:
        assert p['status']=='open'


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
def test_published_target_definitions_and_semantics(tmp_path):
    from pathlib import Path
    import json
    targets=worker.ROOT/'contracts/targets'
    source=(targets/'Review.lean').read_text()
    for pid in ('P007','P009','P003'):
        assert json.loads((targets/(pid+'.json')).read_text())['expression'] in source
    tmp_path.chmod(0o755)
    (tmp_path/'Review.lean').write_text(source)
    code,log,_=worker.sandbox(tmp_path,['/opt/lean/bin/lean','-j','1','Review.lean'])
    assert code==0,log
    assert 'sorryAx' not in log and "uses 'sorry'" not in log,log
    for name in ('euler_limit_spec','euler_irrationality_spec','pi_e_irrationality_spec','collatz_negation_spec'):
        assert name in log,log


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
@pytest.mark.parametrize('pid,flag,assume_target',[
    ('P007',1,False),('P009',1,False),('P003',1,False),('P003',-1,False),('P007',1,True)])
def test_published_targets_reject_unrelated_or_circular_proofs(tmp_path,monkeypatch,pid,flag,assume_target):
    import json
    target=json.loads((worker.ROOT/'contracts/targets'/f'{pid}.json').read_text())
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:
        tx.put('problems',dict(id=pid,title='Fixed target test',statement=target['expression'],lean_target=target,target_status='reviewed',status='open',created_at=time.time()))
    c=TestClient(main.app);c.post('/api/v1/contributors/claim',json={'name':'TargetGuardTester'})
    identity=dict(contributor_id='1',contributor_name='TargetGuardTester')
    expression=target['expression'] if flag==1 else '¬ ('+target['expression']+')'
    fixed=dict(definitions=target.get('definitions',''),expression=expression)
    i=post_insight(c,dict(identity,problem_id=pid,title='Invalid completion candidate',claim='Test only',proof='Test only',proof_status='claimed_complete',claims_problem_solved=flag,lean_target=fixed)).json()
    source='import Mathlib\ntheorem candidate : True := True.intro'
    if assume_target: source=f'theorem candidate (h : MCTargets.{i["id"]}.statement) : MCTargets.{i["id"]}.statement := h'
    response=c.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=i['id'],source=source,theorem='candidate'))
    assert response.status_code==200,response.text
    a=response.json();assert worker.once()
    verification=c.get('/api/v1/lean/verifications/'+a['verification_id']).json()
    assert verification['status']=='failed',verification
    assert 'fixed Insight target' in verification['log'],verification
    assert verification.get('target_matched') is not True
    assert c.get('/api/v1/problems/'+pid).json()['status']=='open'


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
@pytest.mark.parametrize('expression,definitions,source,expected',[
    ('∀ n : Nat, n = n','','theorem fixed_result (n : Nat) : n = n := rfl','passed'),
    ('∀ n : Nat, n = n','','theorem fixed_result : True := True.intro','failed'),
    ('∀ n : Nat, n = n','','theorem fixed_result (h : ∀ n : Nat, n = n) : ∀ n : Nat, n = n := h','failed'),
    ('Required','def Required : Prop := False','namespace Alternative\ndef Required : Prop := True\nend Alternative\ntheorem fixed_result : Alternative.Required := True.intro','failed'),
])
def test_insight_proofs_match_immutable_target_exactly(tmp_path,monkeypatch,expression,definitions,source,expected):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:tx.put('problems',dict(id='PFIX',title='Fixed statement test',statement='Test',status='open',created_at=time.time()))
    c=TestClient(main.app);c.post('/api/v1/contributors/claim',json={'name':'FixedTargetTester'})
    identity=dict(contributor_id='1',contributor_name='FixedTargetTester')
    i=post_insight(c,dict(identity,problem_id='PFIX',title='Exact target',claim='Test proposition',proof='Test proof',proof_status='claimed_complete',lean_target=dict(expression=expression,definitions=definitions))).json()
    response=c.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=i['id'],source=source,theorem='fixed_result'))
    assert response.status_code==200,response.text
    a=response.json();assert worker.once()
    verification=c.get('/api/v1/lean/verifications/'+a['verification_id']).json()
    assert verification['status']==expected,verification
    assert bool(verification.get('target_matched'))==(expected=='passed')
    if expected=='passed':assert verification['target_hash']==i['lean_target']['target_hash']
    else:assert 'fixed Insight target' in verification['log'],verification


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
def test_statement_elaboration_does_not_accept_unbound_parameters(tmp_path,monkeypatch):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:tx.put('problems',dict(id='PFIX',title='Fixed statement test',statement='Test',status='open',created_at=time.time()))
    c=TestClient(main.app);c.post('/api/v1/contributors/claim',json={'name':'ClosedTargetTester'})
    identity=dict(contributor_id='1',contributor_name='ClosedTargetTester')
    response=c.post('/api/v1/insights',json=dict(identity,problem_id='PFIX',title='Unbound parameter',claim='Test',proof='Test',proof_status='claimed_complete',lean_target=dict(definitions='variable (n : Nat)',expression='n = n')))
    assert response.status_code==200,response.text
    assert worker.once()
    checked=c.get('/api/v1/insights/'+response.json()['id']+'/lean-target').json()
    assert checked['status']=='invalid',checked


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
def test_target_definition_imports_are_reusable_without_claiming_a_proof(tmp_path,monkeypatch):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:tx.put('problems',dict(id='PDEF',title='Definition dependencies',statement='Test',status='open',created_at=time.time()))
    c=TestClient(main.app);c.post('/api/v1/contributors/claim',json={'name':'DefinitionTester'})
    identity=dict(contributor_id='1',contributor_name='DefinitionTester')
    first=post_insight(c,dict(identity,problem_id='PDEF',title='Definition',claim='Successor',proof='Definition',proof_status='claimed_complete',lean_target=dict(definitions='def step (n : Nat) : Nat := n + 1',expression='∀ n : Nat, step n = n + 1'))).json()
    target=dict(insight_dependencies=[first['id']],expression='∀ n : Nat, MCTargets.'+first['id']+'.step n = n + 1')
    second=post_insight(c,dict(identity,problem_id='PDEF',title='Reuse a definition',claim='A reused definition',proof='By reflexivity',proof_status='claimed_complete',references=[first['id']],lean_target=target)).json()
    assert c.get('/api/v1/lean/artifacts').json()['total']==0
    r=c.post('/api/v1/lean/artifacts',json=dict(identity,insight_id=second['id'],theorem='reuseDef',source='theorem reuseDef : MCTargets.'+second['id']+'.statement := by intro n; rfl'))
    assert r.status_code==200,r.text
    assert worker.once()
    verification=c.get('/api/v1/lean/verifications/'+r.json()['verification_id']).json()
    assert verification['status']=='passed' and verification['target_matched'],verification


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires pinned verifier')
def test_migration_batch_imports_targets_in_dependency_order(tmp_path,monkeypatch):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:
        tx.put('problems',dict(id='PBATCH',title='Batch references',statement='Test',status='open',created_at=time.time()))
        for n in range(3):tx.put('insights',dict(id='IB'+str(n),problem_id='PBATCH',contributor_id='99',title='Batch target',claim='True',proof='Introduction',proof_status='claimed_complete',references=['IB0'] if n==1 else [],created_at=n))
        tx.put('platform_states',dict(id='lean_target_migration',phase='target_backfill',required_insight_ids=['IB0','IB1','IB2']))
    c=TestClient(main.app);c.post('/api/v1/contributors/claim',json={'name':'BatchTester'})
    identity=dict(contributor_id='1',contributor_name='BatchTester')
    claim=c.post('/api/v1/tasks/claim',json=identity).json()
    targets=[dict(insight_id='IB2',lean_target=dict(expression='True')),dict(insight_id='IB1',lean_target=dict(expression='∀ n : Nat, MCTargets.IB0.step n = n + 1',insight_dependencies=['IB0'])),dict(insight_id='IB0',lean_target=dict(definitions='def step (n : Nat) : Nat := n + 1',expression='True'))]
    response=c.post('/api/v1/lean-targets/batch',json=dict(identity,task_id=claim['task']['id'],claim_token=claim['claim_token'],targets=targets))
    assert response.status_code==200,response.text
    for _ in range(3):assert worker.once()
    assert c.get('/api/v1/platform-status').json()['phase']=='normal'
    assert c.get('/api/v1/contributors/1/score').json()['score']==8


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires Docker and the pinned Lean toolchain')
def test_revoked_target_repair_reuses_insight_but_not_old_proof(tmp_path,monkeypatch):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:tx.put('problems',dict(id='PREV',title='Revision test',statement='True',created_at=1,status='open'))
    c=TestClient(main.app)
    for name in ['Author','Auditone','Audittwo','Repairer']:c.post('/api/v1/contributors/claim',json={'name':name})
    i=post_insight(c,dict(contributor_id='1',contributor_name='Author',problem_id='PREV',title='Formal contract',claim='A precise equality',proof='Test only',proof_status='claimed_complete')).json()
    def prove(source,name):
        r=c.post('/api/v1/lean/artifacts',json=dict(contributor_id='1',contributor_name='Author',insight_id=i['id'],source=source,theorem=name))
        assert r.status_code==200,r.text
        a=r.json();assert worker.once();return a,c.get('/api/v1/lean/verifications/'+a['verification_id']).json()
    a,v=prove('theorem original : True := True.intro','original');assert v['status']=='passed',v
    for ident,name in [('2','Auditone'),('3','Audittwo')]:
        r=c.post('/api/v1/insights/'+i['id']+'/lean-target/audits',json=dict(contributor_id=ident,contributor_name=name,target_hash=i['lean_target']['target_hash'],alignment='misaligned',rationale='The old target omits the claimed equality.'))
        assert r.status_code==200,r.text
    assert c.get('/api/v1/lean/artifacts/'+a['id']).json()['interface']['reusable'] is False
    claim=c.post('/api/v1/tasks/claim',json=dict(contributor_id='4',contributor_name='Repairer',problem_id='PREV',type='lean_target')).json()
    r=c.post('/api/v1/lean-targets/batch',json=dict(contributor_id='4',contributor_name='Repairer',task_id=claim['task']['id'],claim_token=claim['claim_token'],targets=[dict(insight_id=i['id'],lean_target=dict(expression='1 = 1'))]))
    assert r.status_code==200,r.text
    assert worker.once()
    assert c.get('/api/v1/insights/'+i['id']+'/lean-target').json()['status']=='valid'
    _,wrong=prove('theorem oldAgain : True := True.intro','oldAgain');assert wrong['status']=='failed',wrong
    _,correct=prove('theorem fixed : 1 = 1 := rfl','fixed');assert correct['status']=='passed',correct


@pytest.mark.skipif(os.environ.get('RUN_LEAN_TESTS')!='1',reason='Requires Docker and the pinned Lean toolchain')
def test_same_lease_real_lean_error_feedback_and_successful_retry(tmp_path,monkeypatch):
    s=Store(tmp_path);(tmp_path/'artifacts').mkdir()
    monkeypatch.setattr(main,'store',s);monkeypatch.setattr(worker,'STORE',s);monkeypatch.setattr(worker,'CACHE',tmp_path/'artifacts/cache')
    with s.transaction() as tx:tx.put('problems',dict(id='PRETRY',title='Retry test',statement='True',created_at=1,status='open'))
    c=TestClient(main.app)
    c.post('/api/v1/contributors/claim',json={'name':'Author'});c.post('/api/v1/contributors/claim',json={'name':'Prover'})
    i=post_insight(c,dict(contributor_id='1',contributor_name='Author',problem_id='PRETRY',title='True',claim='True',proof='Introduction',proof_status='claimed_complete')).json()
    claim=c.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Prover',problem_id='PRETRY',type='lean_work')).json()
    data=dict(contributor_id='2',contributor_name='Prover',insight_id=i['id'],task_id=claim['task']['id'],claim_token=claim['claim_token'])
    r=c.post('/api/v1/lean/artifacts',json=dict(data,source='theorem bad : False := True.intro',theorem='bad'));assert r.status_code==200,r.text
    assert worker.once()
    feedback=c.get(r.json()['task_feedback_url']).json();assert feedback['status']=='claimed' and feedback['can_retry']
    assert feedback['attempts'][0]['status']=='failed' and feedback['attempts'][0]['log']
    r=c.post('/api/v1/lean/artifacts',json=dict(data,source='theorem good : True := True.intro',theorem='good'));assert r.status_code==200,r.text
    assert worker.once()
    feedback=c.get(r.json()['task_feedback_url']).json();assert feedback['status']=='submitted' and not feedback['can_retry']
    assert feedback['attempts'][-1]['status']=='passed' and len(feedback['attempts'])==2
    assert c.get('/api/v1/contributors/2/score').json()['score']==8
