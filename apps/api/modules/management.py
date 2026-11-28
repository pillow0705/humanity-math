"""Unified public contributor views over immutable submissions."""
import json
from fastapi import HTTPException
from apps.api.modules.core import need
from apps.api.modules.scoring import Scores
from apps.api.modules.lifecycle import state

KINDS=('insights','reviews','lean_artifacts','audits','solutions','research_plans','research_updates','target_batches','target_audit_batches')

def submission(tx,person,kind,ident,prefix=''):
    if kind not in KINDS: raise HTTPException(422,'Unknown submission kind')
    obj=need(tx,kind,ident)
    if obj['contributor_id']!=person: raise HTTPException(404,'Submission does not belong to this contributor')
    result=dict(obj,kind=kind,withdrawal=state(tx,kind,ident),management=dict(
        correction_method='POST a complete corrected submission to its normal endpoint with supersedes_id='+ident,
        original_preserved=True,owner_identity_required=True,
        withdraw_url=prefix+'/api/v1/contributors/'+person+'/submissions/'+kind+'/'+ident+'/withdraw',
        restore_url=prefix+'/api/v1/contributors/'+person+'/submissions/'+kind+'/'+ident+'/restore'))
    if kind=='lean_artifacts': result['verification']=need(tx,'verifications',obj['verification_id'])
    if kind=='insights': result['evaluation']=Scores(tx).evaluation(obj,kind)
    if kind=='insights': result['completion']=tx.get('proof_candidates',ident)
    return result

def submissions(tx,person,kind,problem_id,status,q,offset,limit,prefix=''):
    need(tx,'contributors',person)
    if kind and kind not in KINDS: raise HTTPException(422,'Unknown submission kind')
    scores=Scores(tx);events={e['object_id']:scores.event(e) for e in scores.events if e['contributor_id']==person}
    items=[]
    for k in ([kind] if kind else KINDS):
        for obj in scores.objects[k].values():
            if obj['contributor_id']!=person or (problem_id and obj['problem_id']!=problem_id): continue
            state=obj.get('status','received')
            if k=='lean_artifacts': state=scores.verifications.get(obj['verification_id'],{}).get('status','queued')
            if obj['id'] in scores.superseded: state='superseded'
            if obj['id'] in scores.withdrawn: state='withdrawn'
            if status and state!=status: continue
            if q and q.casefold() not in json.dumps(obj,ensure_ascii=False).casefold(): continue
            event=events.get(obj['id'],{})
            title=obj.get('title') or obj.get('theorem') or k+': '+obj.get('target_id',obj.get('artifact_id',obj['id']))
            items.append(dict(id=obj['id'],kind=k,title=title,problem_id=obj['problem_id'],status=state,
                              created_at=obj['created_at'],score=event.get('score',0),pending_points=event.get('pending_points',0),
                              supersedes_id=obj.get('supersedes_id'),
                              detail_url=prefix+'/api/v1/contributors/'+person+'/submissions/'+k+'/'+obj['id']))
    items.sort(key=lambda x:(x['created_at'],x['id']),reverse=True)
    return dict(total=len(items),offset=offset,limit=limit,items=items[offset:offset+limit])

def ranked(tx,problem_id=None,scores=None):
    if problem_id: need(tx,'problems',problem_id)
    scores=scores or Scores(tx)
    people=[dict(p,contribution=scores.total(p['id'],problem_id)) for p in scores.people.values()]
    people=[p for p in people if p['contribution']['submissions'] or p['contribution'].get('demo_points')]
    people.sort(key=lambda p:(-p['contribution']['score'],p['number']))
    return [dict(p,rank=n+1) for n,p in enumerate(people)]
