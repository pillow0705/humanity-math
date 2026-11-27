"""Version-scoped proposition alignment, independent of mathematical reviews."""
import secrets,time
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need,task,clean
from apps.api.modules.lifecycle import withdrawn

FLAG_THRESHOLD=2

def eligible(tx,pid):
    from apps.api.modules.scoring import Scores
    scores=Scores(tx)
    return [i for i in scores.objects['insights'].values() if i['problem_id']==pid and i['id'] not in scores.withdrawn|scores.superseded]

def audit_pool(tx,pid,person=None,exclude=None):
    reserved={i for t in tx.all('tasks') if t['id']!=exclude and t['type']=='lean_target' and t.get('mode')=='alignment' and t['status']=='claimed' and t.get('expires_at',0)>time.time() for i in t.get('target_ids',[])}
    audits=tx.all('target_audits');items=[]
    for i in eligible(tx,pid):
        target=tx.get('insight_targets',i['id'])
        if not target or target['status']!='valid' or i['id'] in reserved:continue
        if person and person==target.get('registered_by',i['contributor_id']):continue
        current=[a for a in audits if a['insight_id']==i['id'] and a['target_hash']==target['target_hash']]
        if person and any(a['contributor_id']==person for a in current):continue
        items.append((len(current),i))
    return [i for _,i in sorted(items,key=lambda pair:(pair[0],pair[1]['created_at'],pair[1]['id']))]

def sync(tx,pid):
    from apps.api.modules.migration import pool
    tasks=tx.all('tasks')
    for mode,items in [('register',pool(tx,pid)),('alignment',audit_pool(tx,pid))]:
        if items and not any(t['problem_id']==pid and t['type']=='lean_target' and t.get('mode','register')==mode and (t['status']=='available' or (t['status']=='claimed' and t.get('expires_at',0)<=time.time())) for t in tasks):
            t=task(tx,pid,'lean_target',mode=mode);tx.put('tasks',t)

def prepare_audit(tx,t,person):
    from apps.api.modules.migration import BATCH_SIZE
    items=audit_pool(tx,t['problem_id'],person,t['id'])[:BATCH_SIZE]
    if not items:return False
    t.update(mode='alignment',target_id=items[0]['id'],target_ids=[i['id'] for i in items],batch_size=len(items),target_hashes={i['id']:need(tx,'insight_targets',i['id'])['target_hash'] for i in items},goal='Audit the exact frozen Lean propositions against the original mathematical Insights. Check all definitions, quantifiers and assumptions. Report aligned, misaligned or uncertain with specific evidence; do not score Insight depth or write proofs.')
    return True

def invalidate(tx,target,reason):
    """Retire active attestations and descendants; preserve historical sources/logs."""
    affected={target['insight_id']};targets=tx.all('insight_targets');artifacts=tx.all('lean_artifacts');bad_artifacts=set()
    changed=True
    while changed:
        changed=False
        for a in artifacts:
            if a['id'] not in bad_artifacts and (a['insight_id'] in affected or set(a.get('dependencies',[])) & bad_artifacts):
                bad_artifacts.add(a['id']);changed=True
        for t in targets:
            if t['insight_id'] not in affected and (set(t.get('insight_dependencies',[])) & affected or set(t.get('dependencies',[])) & bad_artifacts):
                affected.add(t['insight_id']);changed=True
    now=time.time()
    for t in targets:
        if t['insight_id'] not in affected:continue
        tx.put('target_versions',dict(t,id=t['target_hash']))
        t.update(status='revoked' if t['insight_id']==target['insight_id'] else 'stale',invalidated_at=now,invalidation_reason=reason)
        tx.put('insight_targets',t)
    for a in artifacts:
        if a['id'] not in bad_artifacts:continue
        v=need(tx,'verifications',a['verification_id']);v.update(target_obsolete=True,invalidated_at=now)
        if v['status'] in ('queued','running'):v.update(status='cancelled',log=reason)
        tx.put('verifications',v)
    for t in tx.all('tasks'):
        if t['status'] not in ('available','claimed'):continue
        if (t.get('target_id') in affected|bad_artifacts or set(t.get('target_ids',[])) & affected) and t['type'] in ('lean_target','lean_work'):
            t.update(status='closed',closed_reason=reason);tx.put('tasks',t)
    for sol in tx.all('solutions'):
        if sol.get('artifact_id') in bad_artifacts or set(sol.get('supporting_artifact_ids',[])) & bad_artifacts or set(sol.get('used_artifact_ids',[])) & bad_artifacts or set(sol.get('used_insight_ids',[])) & affected:
            if sol['status']=='verified':
                p=need(tx,'problems',sol['problem_id'])
                if p.get('status')=='formal_proof_verified':p.update(status='open',resolution=None);tx.put('problems',p)
            sol.update(status='invalidated',invalidation_reason=reason);tx.put('solutions',sol)
    return sorted(affected)

def record_audit(tx,body,insight_id):
    i=need(tx,'insights',insight_id);target=need(tx,'insight_targets',insight_id)
    if need(tx,'problems',i['problem_id'])['status']=='archived' or withdrawn(tx,'insights',insight_id):raise HTTPException(409,'This Insight is not accepting target maintenance')
    if target['status']!='valid' or body['target_hash']!=target['target_hash']:raise HTTPException(409,'The target version changed or is not valid; fetch its current statement')
    if body['contributor_id']==target.get('registered_by',i['contributor_id']):raise HTTPException(403,'Audit a target registered by another participant')
    prior=[a for a in tx.all('target_audits') if a['insight_id']==insight_id and a['target_hash']==target['target_hash']]
    if any(a['contributor_id']==body['contributor_id'] for a in prior):raise HTTPException(409,'One alignment audit per participant and target version')
    audit=tx.new('target_audits','J',dict(clean(body),insight_id=insight_id,problem_id=i['problem_id']))
    count=len({a['contributor_id'] for a in prior+[audit] if a['alignment']=='misaligned'})
    target['alignment_flags']=count;target['alignment_audits']=len(prior)+1;tx.put('insight_targets',target)
    audit['misaligned_count']=count;tx.put('target_audits',audit)
    if count>=FLAG_THRESHOLD:audit['invalidated_insight_ids']=invalidate(tx,target,'Two distinct participants marked this target version as misaligned')
    return audit

def submit_audits(tx,body):
    t=need(tx,'tasks',body['task_id'])
    if t['type']!='lean_target' or t.get('mode')!='alignment' or t['status']!='claimed' or t.get('expires_at',0)<=time.time():raise HTTPException(409,'No active target-alignment lease')
    if t.get('contributor_id')!=body['contributor_id'] or not secrets.compare_digest(t.get('claim_hash',''),digest(body['claim_token'])):raise HTTPException(403,'Invalid target-alignment lease')
    items=body['audits'];ids=t['target_ids']
    if len(items)!=len(ids) or {a['insight_id'] for a in items}!=set(ids):raise HTTPException(422,'Audit exactly the assigned Insight IDs once each')
    # Validate snapshots before any item can invalidate another dependency in this batch.
    for a in items:
        current=need(tx,'insight_targets',a['insight_id'])
        if a['target_hash']!=t['target_hashes'][a['insight_id']] or current['target_hash']!=a['target_hash'] or current['status']!='valid':raise HTTPException(409,'An assigned target version changed; release and reclaim')
    results=[]
    # Descendant audits after an invalidation are obsolete, not grounds for an atomic rollback.
    for a in items:
        if need(tx,'insight_targets',a['insight_id'])['status']!='valid':continue
        results.append(record_audit(tx,dict(a,contributor_id=body['contributor_id'],contributor_name=body['contributor_name']),a['insight_id']))
    batch=tx.new('target_audit_batches','Q',dict(clean(body),problem_id=t['problem_id'],batch_size=len(ids),audit_ids=[a['id'] for a in results],status='submitted'))
    tx.new('contributions','C',dict(contributor_id=body['contributor_id'],object_id=batch['id'],kind='lean_target',object_kind='target_audit_batches',problem_id=t['problem_id'],score=None))
    t.update(status='submitted',result_id=batch['id'],submitted_at=time.time());tx.put('tasks',t)
    return dict(batch=batch,items=results)
