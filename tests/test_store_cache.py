import multiprocessing
from apps.api.infrastructure.store import Store, atomic


def _external_replace(root):
    with Store(root).transaction() as tx:
        tx.put('items', {'id': 'one', 'value': 3})


def test_cached_reads_see_atomic_and_in_place_changes_and_deletes(tmp_path):
    store=Store(tmp_path)
    with store.transaction() as tx:tx.put('items',dict(id='one',value=1))
    with store.transaction() as tx:assert tx.all('items')[0]['value']==1
    context=multiprocessing.get_context('spawn')
    child=context.Process(target=_external_replace,args=(str(tmp_path),));child.start();child.join(10)
    assert child.exitcode==0
    with store.transaction() as tx:assert tx.get('items','one')['value']==3
    (tmp_path/'items/one.json').write_text('{"id":"one","value":4}')
    with store.transaction() as tx:assert tx.all('items')[0]['value']==4
    (tmp_path/'items/one.json').unlink()
    with store.transaction() as tx:
        assert tx.get('items','one') is None
        assert tx.all('items')==[]


def test_cached_objects_do_not_leak_mutations_and_put_overlays_snapshot(tmp_path):
    store=Store(tmp_path)
    with store.transaction() as tx:tx.put('items',dict(id='one',nested={'value':1}))
    with store.transaction() as tx:
        tx.get('items','one')['nested']['value']=99
        tx.all('items')[0]['nested']['value']=88
        assert tx.get('items','one')['nested']['value']==1
        value=tx.get('items','one');value['nested']['value']=2;tx.put('items',value)
        assert tx.all('items')[0]['nested']['value']==2
    value['nested']['value']=77
    with store.transaction() as tx:assert tx.get('items','one')['nested']['value']==2


def test_pending_journal_invalidates_warm_cache(tmp_path):
    store=Store(tmp_path)
    with store.transaction() as tx:tx.put('items',dict(id='one',value=1))
    with store.transaction() as tx:assert tx.all('items')[0]['value']==1
    atomic(tmp_path/'.pending.json',[['items','one',dict(id='one',value=9)]])
    with store.transaction() as tx:assert tx.all('items')[0]['value']==9
