"""One lease covers up to ten distinct insights and an atomic set of reviews."""
import secrets
import time
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need, clean, contribute, supported_task
from apps.api.modules.lifecycle import withdrawn
from apps.api.modules.scoring import Scores
from apps.api.modules.recommendations import ranking

BATCH_SIZE = 10

def busy_targets(tx, exclude=None):
    return {ident for t in tx.all('tasks')
            if t['id'] != exclude and t['type']=='review' and supported_task(t)
            and t['status']=='claimed' and t.get('expires_at',0)>time.time()
            for ident in t.get('target_ids',[]) or [t.get('target_id')]}

def prepare_batch(tx,t,person,limit=BATCH_SIZE,strict_target=False):
    busy=busy_targets(tx,exclude=t['id'])
    scores=Scores(tx)
    eligible=[i for i in tx.all('insights') if i['problem_id']==t['problem_id']
              and i['id'] not in busy and i['id'] not in scores.withdrawn
              and i['id'] not in scores.superseded and i['contributor_id']!=person
              and (i['id'],person) not in scores.latest_reviews]
    def count(i):
        return sum(ident==i['id'] and reviewer!=i['contributor_id'] for ident,reviewer in scores.latest_reviews)
    eligible.sort(key=lambda i:(count(i),)+ranking(tx,scores,'task_claims',i))
    if strict_target and t.get('target_id'):
        target=next((i for i in eligible if i['id']==t['target_id']),None)
        if not target:return False
        eligible=[target]+[i for i in eligible if i['id']!=target['id']]
    selected=eligible[:min(BATCH_SIZE,limit)]
    if not selected:return False
    t['target_id']=selected[0]['id']
    t['target_ids']=[i['id'] for i in selected]
    t['batch_size']=len(selected)
    t['goal']=f"Review all {len(selected)} assigned insights separately; submit one verdict, rationale and integer score (1–5) per insight."
    return True

def submit_batch(tx,body):
    t=need(tx,'tasks',body['task_id'])
    if t['type']!='review' or not supported_task(t): raise HTTPException(409,'Not an insight review task')
    if t['status']!='claimed' or t.get('expires_at',0)<=time.time(): raise HTTPException(409,'Task lease is not active')
    if not secrets.compare_digest(t.get('claim_hash',''),digest(body['claim_token'])): raise HTTPException(403,'Invalid lease token')
    if t.get('contributor_id')!=body['contributor_id']: raise HTTPException(403,'Task belongs to another contributor')
    targets=t.get('target_ids') or [t['target_id']]
    if len(body['reviews'])!=len(targets) or {r['insight_id'] for r in body['reviews']}!=set(targets):
        raise HTTPException(422,'Review exactly the assigned target_ids, once each, in one batch')
    if need(tx,'problems',t['problem_id'])['status']!='open': raise HTTPException(409,'Problem is no longer open')
    results=[]
    for item in body['reviews']:
        obj=need(tx,'insights',item['insight_id'])
        if obj['problem_id']!=t['problem_id'] or withdrawn(tx,'insights',obj['id']): raise HTTPException(409,'An assigned insight is no longer available; release and reclaim the task')
        from apps.api.modules.targets import validate_review_alignment
        validate_review_alignment(tx,obj['id'],item)
        r=tx.new('reviews','E',dict(contributor_id=body['contributor_id'],contributor_name=body['contributor_name'],
                task_id=t['id'],batch_size=len(targets),supersedes_id=None,target_id=obj['id'],target_kind='insights',problem_id=t['problem_id'],
                verdict=item['verdict'],rationale=item['rationale'],score=item['score'],target_alignment=item.get('target_alignment','not_applicable'),target_hash=item['target_hash']))
        contribute(tx,r,'review');results.append(r)
        from apps.api.modules.trash import zero_review
        zero_review(tx,r)
    result_by_target={r['target_id']:r['id'] for r in results}
    t.update(status='submitted',result_ids=[r['id'] for r in results],submitted_at=time.time())
    tx.put('tasks',t)
    # Retire outstanding individual invitations satisfied by this batch, without touching another live lease.
    for other in tx.all('tasks'):
        if other['id']==t['id'] or other['type']!='review' or other.get('target_id') not in result_by_target: continue
        if other['status']=='available' or (other['status']=='claimed' and other.get('expires_at',0)<=time.time()):
            other.update(status='submitted',result_id=result_by_target[other['target_id']],completed_by_batch=t['id'],submitted_at=time.time())
            tx.put('tasks',other)
    return dict(task_id=t['id'],status='submitted',count=len(results),items=results)
