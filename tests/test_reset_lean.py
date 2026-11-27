import json
from pathlib import Path
import pytest
from deploy.reset_lean import engineering,reset


def put(root,kind,obj):
    p=root/kind/(obj['id']+'.json');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj));return p


def test_offline_reset_clears_proofs_engineering_credit_and_starts_five_item_phase(tmp_path):
    data=tmp_path/'data';data.mkdir();backup=tmp_path/'private';backup.mkdir();(backup/'data-before-reset.tar.gz').write_bytes(b'Existing private backup')
    put(data,'problems',dict(id='P1',status='open'))
    good=dict(id='Igood',problem_id='P1',contributor_id='1',references=['Iprobe'],initial_lean_artifact_id='Lold',title='A real lemma',claim='For every n, n=n')
    put(data,'insights',good)
    put(data,'insights',dict(id='Iprobe',problem_id='P1',title='Even-loss probe: test',claim='Diagnostic probe module test for the even-loss Lean chain'))
    put(data,'insights',dict(id='Ibisect',problem_id='P1',title='Midpoint-bisection Sondow certificate',claim='A mathematical theorem',references=[],contributor_id='1'))
    put(data,'lean_artifacts',dict(id='Lold'));put(data,'verifications',dict(id='Vold'));put(data,'reviews',dict(id='Rprobe',target_id='Iprobe'))
    put(data,'contributions',dict(id='Cprobe',object_id='Iprobe',kind='insights'));put(data,'contributions',dict(id='Cproof',object_id='Lold',kind='lean_work'));put(data,'contributions',dict(id='Cgood',object_id='Igood',kind='insights'))
    put(data,'tasks',dict(id='Told',status='claimed',target_id=None,type='review'))
    (data/'artifacts/cache').mkdir(parents=True);(data/'artifacts/cache/compiled').write_bytes(b'old')
    dry=reset(data,backup)
    assert dry['engineering_insights']==1 and (data/'lean_artifacts/Lold.json').exists()
    result=reset(data,backup,True)
    assert result['old_proof_records']==1 and result['required_insights']==2
    assert not list((data/'lean_artifacts').glob('*.json')) and not list((data/'verifications').glob('*.json'))
    assert not (data/'insights/Iprobe.json').exists() and (data/'insights/Ibisect.json').exists()
    cleaned=json.loads((data/'insights/Igood.json').read_text());assert cleaned['references']==[] and 'initial_lean_artifact_id' not in cleaned
    assert [p.stem for p in (data/'contributions').glob('*.json')]==['Cgood']
    state=json.loads((data/'platform_states/lean_target_migration.json').read_text())
    assert state['phase']=='target_backfill' and state['batch_size']==5 and state['batch_points']==8
    tasks=[json.loads(p.read_text()) for p in (data/'tasks').glob('*.json')]
    assert any(t['type']=='lean_target' and t['status']=='available' for t in tasks)
    assert (backup/'removed/insights/Iprobe.json').exists()
    with pytest.raises(ValueError,match='already been applied'):reset(data,backup,True)


def test_reset_requires_external_backup_and_keeps_independent_lemmas(tmp_path):
    data=tmp_path/'data';data.mkdir()
    with pytest.raises(ValueError):reset(data,data/'backup')
    with pytest.raises(ValueError):reset(data,tmp_path/'missing')
    assert not engineering(dict(title='Even-loss lemma',claim='At most l-2 occurrences'))
    assert not engineering(dict(title='Bisection lemma',claim='A real mathematical result'))


def test_classifier_only_removes_explicit_engineering_records():
    assert engineering(dict(title='Internal: crash probes (disposable, part 2)',claim='Internal calibration continuation.'))
    assert engineering(dict(title='Internal: crash probes (part 4; disposable)',claim='Internal calibration, part 4.'))
    assert not engineering(dict(title='Midpoint-bisection certificate',claim='A theorem about a rational approximation.'))
    assert not engineering(dict(title='Even-loss lemma',claim='For every integer cycle, a pattern count is bounded.'))
