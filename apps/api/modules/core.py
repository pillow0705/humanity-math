import secrets, time, math
from pathlib import Path
from fastapi import HTTPException
from apps.api.infrastructure.store import digest

INSIGHT_MIN_SECONDS=180

def check_research_time(tx,body):
    if INSIGHT_MIN_SECONDS<=0 or body.get('supersedes_id'):return
    if not body.get('task_id') or not body.get('claim_token'):
        raise HTTPException(422,'Claim an insight_explore task before submitting a new Insight; research for at least 180 seconds under that lease')
    t=need(tx,'tasks',body['task_id'])
    if t['type']!='insight_explore' or t['status']!='claimed' or t.get('expires_at',0)<=time.time():raise HTTPException(409,'Claim an active Insight exploration task')
    if t.get('contributor_id')!=body['contributor_id'] or t['problem_id']!=body['problem_id'] or not secrets.compare_digest(t.get('claim_hash',''),digest(body['claim_token'])):raise HTTPException(403,'Invalid exploration lease')
    started=t.get('claimed_at',t['expires_at']-3600)
    remaining=math.ceil(INSIGHT_MIN_SECONDS-(time.time()-started))
    if remaining>0:
        prompt=(Path(__file__).resolve().parents[3]/'prompts/insight_early_submission.md').read_text()
        raise HTTPException(429,dict(code='research_time_remaining',minimum_research_seconds=INSIGHT_MIN_SECONDS,retry_after_seconds=remaining,message='Continue research on this task before submitting your Insight.',research_prompt=prompt,task_id=t['id']),headers={'Retry-After':str(remaining)})


def need(tx, kind, ident):
    value = tx.get(kind, ident)
    if not value: raise HTTPException(404, f'{kind}: {ident} not found')
    return value

def find_content(tx, ident):
    for kind in ('insights',):
        x = tx.get(kind, ident)
        if x: return x
    raise HTTPException(422, f'Unknown reference: {ident}')

def public_task(task):
    return {k:v for k,v in task.items() if k not in ('claim_hash',)}

def supported_task(t):
    return t['type'] in ('insight_explore','review','lean_work','research_synthesis','lean_target') and (not t.get('target_id') or str(t['target_id']).startswith(('I','L')))

def task(tx, problem_id, type_, target=None, mode=None):
    from apps.api.modules.lifecycle import withdrawn
    if not supported_task(dict(type=type_,target_id=target)): raise ValueError('Unsupported task')
    p=tx.get('problems',problem_id)
    kind={'I':'insights','L':'lean_artifacts'}.get((target or '')[:1])
    status='closed' if (p and p['status'] in ('formal_proof_verified','archived')) or (kind and withdrawn(tx,kind,target)) else 'available'
    return tx.new('tasks','T',dict(problem_id=problem_id,type=type_,target_id=target,mode=mode,status=status,attempts=0))

def finish_task(tx, body, expected, target, problem_id, result_id):
    if not body.get('task_id'): return
    t = need(tx,'tasks',body['task_id'])
    if not supported_task(t): raise HTTPException(409,'Unsupported task')
    if expected=='review' and t.get('target_ids'): raise HTTPException(409,'Submit all assigned reviews to /api/v1/reviews/batch')
    if t['status'] != 'claimed' or t.get('expires_at',0) <= time.time(): raise HTTPException(409,'Task lease is not active')
    if not secrets.compare_digest(t.get('claim_hash',''), digest(body.get('claim_token'))): raise HTTPException(403,'Invalid lease token')
    if t['type'] != expected or t['problem_id'] != problem_id or t['contributor_id'] != body['contributor_id']: raise HTTPException(409,'Task does not match submission')
    if expected=='insight_explore' and body.get('is_prerequisite'): return
    if expected=='review' and t['target_id']!=target: raise HTTPException(409,'Wrong task target')
    if expected=='lean_work' and t['target_id']!=target:
        artifact=tx.get('lean_artifacts',t['target_id'])
        if not artifact or artifact['insight_id']!=target: raise HTTPException(409,'Wrong Lean task target')
    t.update(status='submitted',result_id=result_id,submitted_at=time.time()); tx.put('tasks',t)

def contribute(tx, obj, kind):
    tx.new('contributions','C',dict(contributor_id=obj['contributor_id'],object_id=obj['id'],kind=kind,problem_id=obj['problem_id'],score=None,object_kind='lean_artifacts' if obj['id'].startswith('L') else 'audits' if obj['id'].startswith('U') else {'review':'reviews','solution':'solutions','research_synthesis':'research_updates','lean_target':'target_batches'}.get(kind,kind)))

def clean(body): return {k:v for k,v in body.items() if k not in ('claim_token','lean','contributor_password','new_password')}

def check_supersedes(tx, kind, body, problem_id):
    if need(tx,'problems',problem_id)['status']=='archived': raise HTTPException(409,'This problem is archived and accepts no new research submissions')
    if body.get('supersedes_id'):
        old = need(tx,kind,body['supersedes_id'])
        if old['problem_id'] != problem_id: raise HTTPException(422,'Replacement must concern the same problem')
        if old['contributor_id'] != body['contributor_id']: raise HTTPException(403,'Only the original contributor may submit a replacement')

def contribution_group(task_id,contributor_id):
    return 'G'+digest(dict(task_id=task_id,contributor_id=contributor_id))

def create_content(tx, kind, body):
    if kind!='insights': raise HTTPException(422,'Unsupported content type')
    need(tx,'problems',body['problem_id'])
    check_supersedes(tx,kind,body,body['problem_id'])
    check_research_time(tx,body)
    previous=need(tx,'insights',body['supersedes_id']) if body.get('supersedes_id') else None
    if previous and bool(previous.get('is_prerequisite'))!=bool(body.get('is_prerequisite')):
        raise HTTPException(422,'Corrections must preserve the original prerequisite/base-credit role')
    if body.get('is_prerequisite') and not body.get('task_id') and not previous:
        raise HTTPException(422,'Register a new prerequisite with the current exploration task_id and claim_token')
    if body.get('is_prerequisite') and body.get('task_id') and not body.get('claim_token'):
        raise HTTPException(422,'A prerequisite registration requires its exploration lease token')
    groups=set(previous.get('contribution_group_ids',[]) or ([contribution_group(previous['task_id'],previous['contributor_id'])] if previous.get('task_id') else [])) if previous else set()
    if body.get('task_id'): groups.add(contribution_group(body['task_id'],body['contributor_id']))
    if kind=='insights':
        from apps.api.modules.research import validate_completion_claim
        validate_completion_claim(need(tx,'problems',body['problem_id']),body.get('claims_problem_solved',0))
    for ref in body['references']:
        if not ref.startswith('I'): raise HTTPException(422,'References must identify insights')
        need(tx,'insights',ref)
    if kind == 'insights' and body['proof_status'] != 'claimed_complete' and not body['gaps']:
        raise HTTPException(422,'Incomplete results must state gaps')
    fingerprint = digest({k:v for k,v in body.items() if k not in ('task_id','claim_token','contributor_id','contributor_name','lean','is_prerequisite')})
    for previous in tx.all(kind):
        if previous.get('content_hash') == fingerprint: raise HTTPException(409,dict(message='Duplicate content',existing_id=previous['id']))
    obj = tx.new(kind,'I',dict(clean(body),contribution_group_ids=sorted(groups),content_hash=fingerprint,status='received'))
    finish_task(tx,body,'insight_explore',None,body['problem_id'],obj['id'])
    contribute(tx,obj,kind)
    task(tx,obj['problem_id'],'review',obj['id'])
    return obj
