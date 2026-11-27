"""Worker lifecycle/cache regressions; real type comparison lives in Lean integration tests."""
import json
from types import SimpleNamespace
import pytest
from apps.worker import main as worker
from apps.api.infrastructure.store import Store
from apps.api.modules.lean import POLICY, TOOLCHAIN, LIBRARY_ID
from apps.api.modules.targets import target_source


@pytest.fixture
def verifier(tmp_path,monkeypatch):
    store=Store(tmp_path/'data');(store.root/'artifacts').mkdir(parents=True)
    monkeypatch.setattr(worker,'STORE',store)
    monkeypatch.setattr(worker,'CACHE',store.root/'artifacts/cache')
    monkeypatch.setattr(worker,'docker',lambda args:SimpleNamespace(stdout='image-test\n',returncode=0))
    return store


def target(expression='True'):
    return dict(id='Itest',insight_id='Itest',target_hash='h-target',target_module='MCTargets.Itest',target_constant='MCTargets.Itest.statement',definitions='',expression=expression,dependencies=[],dependency_snapshot=[],toolchain=TOOLCHAIN,library_id=LIBRARY_ID,policy=POLICY,status='queued',created_at=1)


def artifact(t):
    return dict(id='Ltest',insight_id=t['id'],target=t,target_hash=t['target_hash'],source='theorem result : True := True.intro',theorem='result',dependencies=[],file_path='MC/Ltest.lean',content_hash='h-proof',toolchain=TOOLCHAIN,library_id=LIBRARY_ID,policy=POLICY)


def fake_success(work,command,writable=False):
    if '-o' in command:
        output=work/command[command.index('-o')+1];output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'test olean')
        return 0,'','image-test'
    return 0,"'MCVerifiedResult' does not depend on any axioms\nresult : True\n",'image-test'


def test_target_is_elaborated_separately_without_claiming_a_proof(verifier,monkeypatch):
    monkeypatch.setattr(worker,'sandbox',fake_success)
    t=target('False');result=worker.verify_target(t,[])
    assert result['status']=='valid' and 'target_matched' not in result
    cached=worker.target_cache_files(t)
    assert 'def statement : Prop := (' in (cached/'source.lean').read_text()
    work=verifier.root/'consumer';work.mkdir()
    worker.prepare_target(work,t)
    assert (work/'MCTargets/Itest.olean').read_bytes()==b'test olean'
    (cached/'module.olean').write_bytes(b'corruption')
    with pytest.raises(RuntimeError,match='target cache corruption'):worker.prepare_target(work,t)


def test_bad_target_is_invalid_and_not_cached(verifier,monkeypatch):
    monkeypatch.setattr(worker,'sandbox',lambda *args:(1,'Unknown identifier','image-test'))
    t=target('not_defined')
    assert worker.verify_target(t,[])['status']=='invalid'
    assert not worker.target_cache_files(t).exists()


def test_checker_uses_compiled_constant_and_exact_type_comparison():
    t=target();a=artifact(t);source=worker.proof_checker(a)
    assert 'Lean.Meta.mkConstWithFreshMVarLevels `result' in source
    assert 'Lean.mkConst `MCTargets.Itest.statement' in source
    assert 'Lean.Meta.isDefEq t expected' in source
    assert 'elabTerm' not in source
    assert 'def MCVerifiedResult : _root_.MCTargets.Itest.statement := @_root_.result' in source
    wrapped=target_source(dict(t,definitions='import Mathlib\ndef custom : Prop := True',expression='custom'))
    assert wrapped.index('import Mathlib')<wrapped.index('namespace MCTargets.Itest')<wrapped.index('def custom')<wrapped.index('def statement')


def test_unbound_or_mismatched_proof_never_receives_target_attestation(verifier,monkeypatch):
    assert worker.verify_artifact({'target':None},[])['status']=='failed'
    t=target();a=artifact(t)
    monkeypatch.setattr(worker,'prepare_target',lambda *args:None)
    def mismatch(work,command,writable=False):
        if '-o' in command:return fake_success(work,command,writable)
        assert 'Lean.Meta.isDefEq t expected' in (work/'Check.lean').read_text()
        return 1,'Proof does not match the fixed Insight target','image-test'
    monkeypatch.setattr(worker,'sandbox',mismatch)
    result=worker.verify_artifact(a,[a])
    assert result['status']=='failed' and result.get('target_matched') is not True
    assert not worker.cache_files(a).exists()


def test_matching_proof_caches_target_identity(verifier,monkeypatch):
    t=target();a=artifact(t)
    monkeypatch.setattr(worker,'sandbox',fake_success)
    worker.verify_target(t,[])
    result=worker.verify_artifact(a,[a])
    assert result['status']=='passed' and result['target_matched'] is True
    assert result['target_hash']==t['target_hash']
    meta=json.loads((worker.cache_files(a)/'manifest.json').read_text())
    assert meta['target_matched'] is True and meta['target_hash']==t['target_hash']


def test_target_queue_records_invalid_and_recovers_without_proof_submission(verifier,monkeypatch):
    t=target()
    with verifier.transaction() as tx:tx.put('insight_targets',t)
    monkeypatch.setattr(worker,'verify_target',lambda *args:dict(status='invalid',log='Unknown identifier'))
    assert worker.check_next_target()
    with verifier.transaction() as tx:
        checked=tx.get('insight_targets',t['id'])
        assert checked['status']=='invalid' and checked['finished_at']
    assert not worker.check_next_target()


@pytest.mark.parametrize('snapshot',[dict(id='Lmissing',content_hash='missing'),dict(id='Lchanged',content_hash='original')])
def test_target_dependency_errors_do_not_poison_queue(verifier,snapshot):
    t=target();t['dependency_snapshot']=[snapshot]
    with verifier.transaction() as tx:
        tx.put('insight_targets',t)
        if snapshot['id']=='Lchanged':tx.put('lean_artifacts',dict(id='Lchanged',content_hash='modified'))
    assert worker.check_next_target()
    with verifier.transaction() as tx:assert tx.get('insight_targets',t['id'])['status']=='error'
    assert not worker.check_next_target()


def test_old_queued_solution_without_fixed_target_is_rejected(verifier,monkeypatch):
    from apps.api.infrastructure.store import digest
    t=target();a=artifact(t);a.pop('target');a.update(verification_id='Vlegacy',created_at=1)
    problem=dict(id='Ptest',status='open',lean_target=dict(expression='True'),target_status='reviewed')
    with verifier.transaction() as tx:
        tx.put('problems',problem)
        tx.put('lean_artifacts',a)
        tx.put('verifications',dict(id='Vlegacy',status='passed',policy=POLICY))
        tx.put('solutions',dict(id='Slegacy',artifact_id=a['id'],problem_id='Ptest',status='queued',target_hash=digest(dict(expression='True')),created_at=1))
    monkeypatch.setattr(worker,'sync_candidates',lambda tx:None)
    def unexpected(*args):pytest.fail('Legacy unrelated proof reached final verifier')
    monkeypatch.setattr(worker,'verify_solution',unexpected)
    assert worker.once()
    with verifier.transaction() as tx:
        result=tx.get('solutions','Slegacy')
        assert result['status']=='failed' and 'immutable Insight target' in result['log']


def test_final_verification_prepares_supporting_closures_even_when_leaf_does_not_import_them(verifier,monkeypatch):
    from apps.api.infrastructure.store import digest
    t=target();t['status']='valid';a=artifact(t);a.update(verification_id='Vleaf',created_at=3)
    dependency=dict(id='Ldep',insight_id='Idep',dependencies=[],created_at=1)
    support=dict(id='Lsupport',insight_id='Isupport',dependencies=['Ldep'],created_at=2)
    problem=dict(id='Ptest',status='open',lean_target=dict(expression='True'),target_status='reviewed')
    with verifier.transaction() as tx:
        tx.put('problems',problem);tx.put('insight_targets',t)
        for art in [dependency,support,a]:tx.put('lean_artifacts',art)
        tx.put('verifications',dict(id='Vleaf',status='passed',policy=POLICY,target_hash=t['target_hash'],target_matched=True))
        tx.put('solutions',dict(id='Ssupport',artifact_id=a['id'],supporting_artifact_ids=['Lsupport','Ldep'],problem_id='Ptest',status='queued',target_hash=digest(dict(expression='True')),created_at=1))
    monkeypatch.setattr(worker,'sync_candidates',lambda tx:None)
    seen=[]
    def check(s,p,leaf,arts):
        seen.extend(x['id'] for x in arts)
        assert leaf['id']=='Ltest'
        return dict(status='failed',log='Synthetic stop after inspecting prepared closure')
    monkeypatch.setattr(worker,'verify_solution',check)
    assert worker.once()
    assert seen==['Ldep','Lsupport','Ltest']


@pytest.mark.parametrize('code',[124,125,126,127,137,139])
def test_target_infrastructure_exit_is_retryable(verifier,monkeypatch,code):
    monkeypatch.setattr(worker,'sandbox',lambda *args:(code,'Compiler infrastructure failed','image-test'))
    assert worker.verify_target(target(),[])['status']=='error'


def test_transient_target_failure_can_be_requeued_and_checked(verifier,monkeypatch):
    t=target()
    with verifier.transaction() as tx:tx.put('insight_targets',t)
    def unavailable(*args):raise RuntimeError('Docker temporarily unavailable')
    monkeypatch.setattr(worker,'verify_target',unavailable)
    assert worker.check_next_target()
    with verifier.transaction() as tx:
        current=tx.get('insight_targets',t['id']);assert current['status']=='error'
        current['status']='queued';tx.put('insight_targets',current)
    monkeypatch.setattr(worker,'verify_target',lambda *args:dict(status='valid',log='Recovered'))
    assert worker.check_next_target()
    with verifier.transaction() as tx:assert tx.get('insight_targets',t['id'])['status']=='valid'


def test_proof_queue_waits_for_target_infrastructure_retry(verifier,monkeypatch):
    t=target();t['status']='error';a=artifact(t);a.update(verification_id='Vwaiting',created_at=1)
    with verifier.transaction() as tx:
        tx.put('insight_targets',t);tx.put('lean_artifacts',a)
        tx.put('verifications',dict(id='Vwaiting',artifact_id=a['id'],status='queued',created_at=1))
    monkeypatch.setattr(worker,'sync_candidates',lambda tx:None)
    assert not worker.once()
    with verifier.transaction() as tx:assert tx.get('verifications','Vwaiting')['status']=='queued'


def test_target_cache_reuses_transitive_definition_modules_without_nested_snapshots(verifier,monkeypatch):
    monkeypatch.setattr(worker,'sandbox',fake_success)
    first=target();first.update(insight_dependency_snapshot=[])
    worker.verify_target(first,[])
    second=dict(first,id='Isecond',insight_id='Isecond',target_hash='h-second',target_module='MCTargets.Isecond',target_constant='MCTargets.Isecond.statement',insight_dependency_snapshot=[dict(insight_id=first['id'],target_hash=first['target_hash'],target_module=first['target_module'],target_constant=first['target_constant'])])
    worker.verify_target(second,[])
    third=dict(first,id='Ithird',insight_id='Ithird',target_hash='h-third',target_module='MCTargets.Ithird',target_constant='MCTargets.Ithird.statement',insight_dependency_snapshot=[dict(insight_id=second['id'],target_hash=second['target_hash'],target_module=second['target_module'],target_constant=second['target_constant'])])
    worker.verify_target(third,[])
    work=verifier.root/'consumer';work.mkdir()
    worker.prepare_target(work,third)
    assert all((work/(x['target_module'].replace('.','/')+'.olean')).exists() for x in [first,second,third])
    assert all('insight_dependency_snapshot' not in snapshot for snapshot in third['insight_dependency_snapshot'])
