"""Exclusive five-insight target-registration phase, controlled by operator state."""
import secrets,time
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need,task,contribute,clean

STATE_ID='lean_target_migration'
BATCH_SIZE=5
REWARD=8


def active(tx):
    return (tx.get('platform_states',STATE_ID) or {}).get('phase')=='target_backfill'


def required(tx,scores=None):
    state=tx.get('platform_states',STATE_ID) or {}
    from apps.api.modules.scoring import Scores
    scores=scores or Scores(tx)
    ids=state.get('required_insight_ids',[]) if active(tx) else list(scores.objects['insights'])
    open_problems={p['id'] for p in tx.all('problems') if p['status']=='open'}
    return [i for i in ids if i in scores.objects['insights'] and i not in scores.withdrawn|scores.superseded and scores.objects['insights'][i]['problem_id'] in open_problems]


def progress(tx,scores=None):
    state=tx.get('platform_states',STATE_ID)
    if not state:return dict(phase='normal',batch_size=BATCH_SIZE,batch_points=REWARD,total=0,valid=0,remaining=0)
    ids=required(tx,scores);targets={t['insight_id']:t for t in tx.all('insight_targets')}
    counts={status:sum(targets.get(i,{}).get('status')==status for i in ids) for status in ('valid','queued','checking','invalid','error','revoked','stale')}
    if state['phase']=='target_backfill' and counts['valid']==len(ids):
        state.update(phase='normal',completed_at=time.time());tx.put('platform_states',state)
        for t in tx.all('tasks'):
            if t['type']=='lean_target' and t['status'] in ('available','claimed'):
                t.update(status='closed',closed_reason='All required Insight targets are valid',expires_at=0,claim_hash='');tx.put('tasks',t)
    return dict(phase=state['phase'],total=len(ids),remaining=len(ids)-counts['valid'],batch_size=BATCH_SIZE,batch_points=REWARD,**counts)


def mainline(tx):
    state=progress(tx)
    if state['phase']=='target_backfill':
        raise HTTPException(423,dict(message='The platform is registering fixed Lean targets for existing insights. Claim lean_target tasks (up to 5 insights, 8 points per batch). Ordinary research submissions resume only after every required target is valid.',phase='target_backfill',remaining=state['remaining'],allowed_task_type='lean_target'))


def busy(tx,exclude=None):
    return {i for t in tx.all('tasks') if t['id']!=exclude and t['type']=='lean_target' and t['status']=='claimed' and t.get('expires_at',0)>time.time() for i in t.get('target_ids',[])}


def pool(tx,pid,exclude=None):
    reserved=busy(tx,exclude);ids=set(required(tx));targets={t['insight_id']:t for t in tx.all('insight_targets')}
    from apps.api.modules.scoring import Scores
    from apps.api.modules.recommendations import dependency_depths
    scores=Scores(tx);depth=dependency_depths(scores)
    items=[i for i in scores.objects['insights'].values() if i['id'] in ids and i['problem_id']==pid and i['id'] not in reserved and targets.get(i['id'],{}).get('status') in (None,'invalid','revoked','stale')]
    return sorted(items,key=lambda i:(-depth.get(i['id'],0),i['created_at'],i['id']))


def sync(tx,problem_id=None):
    state=progress(tx)
    if state['phase']!='target_backfill':return None
    states={}
    for p in tx.all('problems'):
        if p['status']!='open' or (problem_id and p['id']!=problem_id):continue
        candidates=pool(tx,p['id'])
        available=[t for t in tx.all('tasks') if t['problem_id']==p['id'] and t['type']=='lean_target' and (t['status']=='available' or (t['status']=='claimed' and t.get('expires_at',0)<=time.time()))]
        if candidates and not available:task(tx,p['id'],'lean_target')
        states[p['id']]=dict(migration=True,target_registration_available=len(candidates),**state)
    return states


def prepare_batch(tx,t):
    chosen=pool(tx,t['problem_id'],t['id'])[:BATCH_SIZE]
    if not chosen:return False
    t.update(target_id=chosen[0]['id'],target_ids=[i['id'] for i in chosen],batch_size=len(chosen),goal='Write faithful fixed Lean propositions for all assigned insights. Reuse registered target definitions; imported target statements are not proved lemmas. Submit one atomic batch, without proofs.')
    return True


def submit_batch(tx,body):
    # Registration remains available as an ordinary task after migration.
    t=need(tx,'tasks',body['task_id'])
    if need(tx,'problems',t['problem_id'])['status']!='open':raise HTTPException(409,'This problem is not accepting target maintenance')
    if t['type']!='lean_target' or t.get('mode')=='alignment' or t['status']!='claimed' or t.get('expires_at',0)<=time.time():raise HTTPException(409,'No active target-registration lease')
    if t.get('contributor_id')!=body['contributor_id'] or not secrets.compare_digest(t.get('claim_hash',''),digest(body['claim_token'])):raise HTTPException(403,'Invalid target-registration lease')
    ids=t.get('target_ids',[])
    if len(body['targets'])!=len(ids) or {x['insight_id'] for x in body['targets']}!=set(ids):raise HTTPException(422,'Submit exactly the assigned target_ids once each')
    from apps.api.modules.targets import register_target
    batch=tx.new('target_batches','B',dict(clean(body),problem_id=t['problem_id'],target_ids=ids,target_hashes={},batch_size=len(ids),status='submitted'))
    item_map={item['insight_id']:item for item in body['targets']};ordered=[];visiting=set();done=set()
    def order(ident):
        if ident in visiting:raise HTTPException(422,'Cyclic target-definition dependencies in this batch')
        if ident in done:return
        visiting.add(ident)
        for dependency in item_map[ident]['lean_target'].get('insight_dependencies',[]):
            if dependency in item_map:order(dependency)
        visiting.remove(ident);done.add(ident);ordered.append(item_map[ident])
    for ident in ids:order(ident)
    for item in ordered:
        insight=need(tx,'insights',item['insight_id'])
        from apps.api.modules.lifecycle import withdrawn
        if withdrawn(tx,'insights',insight['id']):raise HTTPException(409,'An assigned insight has been withdrawn; release and reclaim the task')
        old=tx.get('insight_targets',insight['id'])
        if old:
            if old['status'] not in ('invalid','revoked','stale'):raise HTTPException(409,'An assigned target is already registered or awaiting checks')
            tx.new('insight_target_revisions','H',dict(insight_id=insight['id'],previous_target=old,reason='Correct an invalid, revoked or stale proposition'))
        target=register_target(tx,insight,item['lean_target'],replace_invalid=bool(old),allow_pending_target_ids=ids)
        target.update(migration_batch_id=batch['id'],registered_by=body['contributor_id']);tx.put('insight_targets',target)
        batch['target_hashes'][insight['id']]=target['target_hash']
    tx.put('target_batches',batch)
    t.update(status='submitted',result_id=batch['id'],submitted_at=time.time());tx.put('tasks',t)
    contribute(tx,batch,'lean_target')
    return dict(batch=batch,targets=[tx.get('insight_targets',i) for i in ids],migration=progress(tx))
