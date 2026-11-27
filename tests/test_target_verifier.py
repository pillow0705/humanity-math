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
    with verifier.transaction() as tx:assert tx.get('insight_targets',t['id'])['status']=='invalid'
    assert not worker.check_next_target()
