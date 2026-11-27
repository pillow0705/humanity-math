"""Reversible withdrawal metadata; published source and proof caches stay intact."""
import time
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need

def state(tx,kind,ident):
    return tx.get('submission_states',digest([kind,ident]))

def withdrawn(tx,kind,ident):
    return bool((state(tx,kind,ident) or {}).get('withdrawn'))

def change(tx,person,kind,ident,body,withdraw):
    from apps.api.modules.management import KINDS
    if kind not in KINDS: raise HTTPException(422,'Unknown submission kind')
    obj=need(tx,kind,ident)
    if not withdraw and need(tx,'problems',obj['problem_id'])['status']=='archived': raise HTTPException(409,'Cannot restore work on an archived problem')
    if body['contributor_id']!=person or obj['contributor_id']!=person: raise HTTPException(403,'Only the matching contributor can manage this submission')
    if not withdraw and kind=='solutions' and obj.get('automatic') and withdrawn(tx,'insights',obj['insight_id']):
        raise HTTPException(409,'Restore the candidate insight before restoring its automatic final check')
    current=state(tx,kind,ident)
    if current and current['withdrawn']==withdraw: return current
    current=dict(id=digest([kind,ident]),kind=kind,object_id=ident,contributor_id=person,withdrawn=withdraw,
                 reason=body.get('reason',''),updated_at=time.time())
    tx.put('submission_states',current)
    tx.new('management_events','M',dict(kind=kind,object_id=ident,contributor_id=person,action='withdraw' if withdraw else 'restore',reason=current['reason']))
    for t in tx.all('tasks'):
        if t.get('target_id')!=ident and ident not in t.get('target_ids',[]): continue
        if withdraw and t['status'] in ('available','claimed'):
            t.update(status='closed',closed_reason='Submission withdrawn');tx.put('tasks',t)
        elif not withdraw and t['status']=='closed' and t.get('closed_reason')=='Submission withdrawn' and need(tx,'problems',t['problem_id'])['status']!='formal_proof_verified':
            t.update(status='available');t.pop('closed_reason',None)
            for key in ('claim_hash','contributor_id','contributor_name','expires_at'): t.pop(key,None)
            tx.put('tasks',t)
    if withdraw and kind=='lean_artifacts':
        v=need(tx,'verifications',obj['verification_id'])
        if v['status'] in ('queued','running'):
            v.update(status='cancelled',log='Submission withdrawn');tx.put('verifications',v)
    if not withdraw and kind=='lean_artifacts':
        v=need(tx,'verifications',obj['verification_id'])
        if v['status']=='cancelled': v.update(status='queued');tx.put('verifications',v)
    if withdraw:
        for sol in tx.all('solutions'):
            if (kind=='solutions' and sol['id']==ident) or (kind=='insights' and sol.get('automatic') and sol.get('insight_id')==ident):
                if sol['status'] in ('queued','running'):
                    sol.update(status='cancelled',log='Submission withdrawn');tx.put('solutions',sol)
    elif kind=='solutions' and obj.get('status')=='cancelled' and need(tx,'problems',obj['problem_id'])['status']!='formal_proof_verified':
        obj.update(status='queued');tx.put('solutions',obj)
    return current
