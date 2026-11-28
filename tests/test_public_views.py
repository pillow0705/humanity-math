import concurrent.futures,multiprocessing
from contextlib import contextmanager
import pytest
from fastapi import HTTPException
from apps.api.infrastructure.store import Store,atomic
from apps.api.modules.public_views import PublicViews
from apps.api.modules import public_views


def external_write(root):
    with Store(root).transaction() as tx:tx.put('items',dict(id='one',value=2))


def setup_store(root):
    store=Store(root)
    with store.transaction() as tx:tx.put('items',dict(id='one',value=1))
    return store


def value(tx,scores):return tx.get('items','one')


def test_hot_views_avoid_transactions_reuse_scores_and_isolate_mutations(tmp_path,monkeypatch):
    store=setup_store(tmp_path);cache=PublicViews(store);calls=[];original=public_views.Scores
    def counted(tx):calls.append(1);return original(tx)
    monkeypatch.setattr(public_views,'Scores',counted)
    first,hit=cache.read('one',value);assert first['value']==1 and not hit
    cache.read('another',value);assert len(calls)==1
    def forbidden():raise AssertionError('A hot read must not acquire the file transaction lock')
    monkeypatch.setattr(store,'transaction',forbidden)
    first['value']=99
    again,hit=cache.read('one',value);assert hit and again['value']==1
    again['value']=88
    assert cache.read('one',value)[0]['value']==1


def test_other_process_writes_invalidate_warm_public_views(tmp_path):
    store=setup_store(tmp_path);cache=PublicViews(store);cache.read('one',value)
    context=multiprocessing.get_context('spawn')
    child=context.Process(target=external_write,args=(str(tmp_path),));child.start();child.join(10)
    assert child.exitcode==0
    result,hit=cache.read('one',value);assert not hit and result['value']==2


def test_pending_recovery_and_out_of_band_edits_are_seen(tmp_path):
    store=setup_store(tmp_path);cache=PublicViews(store);cache.read('one',value)
    atomic(tmp_path/'.pending.json',[['items','one',dict(id='one',value=3)]])
    assert cache.read('one',value)[0]['value']==3
    assert not (tmp_path/'.pending.json').exists()
    (tmp_path/'items/one.json').write_text('{"id":"one","value":4}')
    cache.expires=0
    assert cache.read('one',value)[0]['value']==4
    (tmp_path/'items/one.json').unlink();cache.expires=0
    assert cache.read('one',value)[0] is None


def test_single_flight_cache_build_for_concurrent_readers(tmp_path,monkeypatch):
    store=setup_store(tmp_path);cache=PublicViews(store);calls=[]
    def build(tx,scores):calls.append(1);return value(tx,scores)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:cache.read('one',build),range(8)))
    assert len(calls)==1 and sum(hit for _,hit in results)==7
    assert all(result['value']==1 for result,_ in results)


def test_revision_is_captured_before_unlock_not_after_a_later_writer(tmp_path,monkeypatch):
    store=setup_store(tmp_path);cache=PublicViews(store);original=store.transaction;written=[]
    @contextmanager
    def interleaved():
        with original() as tx:yield tx
        if not written:
            written.append(True)
            with Store(tmp_path).transaction() as other:other.put('items',dict(id='one',value=2))
    monkeypatch.setattr(store,'transaction',interleaved)
    assert cache.read('one',value)[0]['value']==1
    result,hit=cache.read('one',value);assert not hit and result['value']==2


def test_failed_builder_does_not_publish_cache_or_partial_writes(tmp_path):
    store=setup_store(tmp_path);cache=PublicViews(store)
    def fail(tx,scores):
        tx.put('items',dict(id='one',value=99));raise HTTPException(404,'Synthetic missing record')
    with pytest.raises(HTTPException):cache.read('failed',fail)
    assert 'failed' not in cache.views
    assert cache.read('one',value)[0]['value']==1


from test_api import client,insight,put_test_verification
from apps.api import main


def test_overview_is_read_only_and_hot_reads_skip_live_scheduler(client,monkeypatch):
    i=client.post('/api/v1/insights',json=insight()).json()
    def forbidden(*args):raise AssertionError('Page reads must not run task dispatch')
    monkeypatch.setattr(main,'sync_schedule',forbidden)
    with main.store.transaction() as tx:before=tx.all('tasks')
    first=client.get('/api/v1/problems/P001/overview')
    assert first.status_code==200 and first.headers['X-Public-Cache']=='miss'
    again=client.get('/api/v1/problems/P001/overview')
    assert again.headers['X-Public-Cache']=='hit' and again.json()==first.json()
    assert again.headers['Cache-Control']=='no-store' and 'public_view;dur=' in again.headers['Server-Timing']
    with main.store.transaction() as tx:assert tx.all('tasks')==before


def test_review_and_worker_updates_invalidate_public_ranks_and_overview(client):
    i=client.post('/api/v1/insights',json=insight()).json()
    rank='/api/v1/insights/ranking?problem_id=P001'
    assert client.get(rank).json()['items'][0]['evaluation']['review_adjusted_points']==10
    assert client.get(rank).headers['X-Public-Cache']=='hit'
    task=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Aster',type='review')).json()
    r=client.post('/api/v1/reviews/batch',json=dict(contributor_id='2',contributor_name='Aster',task_id=task['task']['id'],claim_token=task['claim_token'],reviews=[dict(insight_id=i['id'],score=5,verdict='support',rationale='A rigorous useful result.')]))
    assert r.status_code==200,r.text
    assert client.get(rank).json()['items'][0]['evaluation']['review_adjusted_points']==15
    assert client.get('/api/v1/problems/P001/overview').json()['stats']['reviews']==1
    a=client.post('/api/v1/lean/artifacts',json=dict(contributor_id='3',contributor_name='Bob',insight_id=i['id'],source='theorem proof : True := True.intro',theorem='proof')).json()
    assert client.get('/api/v1/problems/P001/overview').json()['stats']['verified_lean']==0
    # A separate Store instance models the verifier's external-process commit.
    with Store(main.store.root).transaction() as tx:
        v=tx.get('verifications',a['verification_id']);v.update(status='passed',finished_at=10);put_test_verification(tx,v)
    assert client.get('/api/v1/problems/P001/overview').json()['stats']['verified_lean']==1
    assert client.get(rank).json()['items'][0]['evaluation']['lean_bonus']==10
    assert client.get('/api/v1/leaderboard?problem_id=P001').json()['items'][0]['contribution']['score']==25
    assert client.post('/api/v1/contributors/3/submissions/lean_artifacts/'+a['id']+'/withdraw',json=dict(contributor_id='3',contributor_name='Bob')).status_code==200
    assert client.get('/api/v1/problems/P001/overview').json()['stats']['verified_lean']==0


def test_metadata_writing_reader_cannot_retain_prewrite_context(tmp_path):
    store=setup_store(tmp_path);cache=PublicViews(store);cache.read('one',value)
    def write(tx,scores):tx.put('items',dict(id='one',value=2));return {'updated':True}
    assert cache.read('writer',write)[0]['updated']
    assert cache.scores is None and cache.views=={}
    assert cache.read('one',value)[0]['value']==2


def test_view_ttl_does_not_extend_when_new_keys_are_read_and_lru_is_bounded(tmp_path,monkeypatch):
    store=setup_store(tmp_path);cache=PublicViews(store);clock=[100.0]
    monkeypatch.setattr(public_views.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(public_views,'MAX_VIEWS',2)
    cache.read('one',value);deadline=cache.expires
    clock[0]=101;cache.read('two',value)
    assert cache.expires==deadline==102
    cache.read('three',value);assert set(cache.views)=={'two','three'}
    clock[0]=102
    assert cache.read('three',value)[1] is False
