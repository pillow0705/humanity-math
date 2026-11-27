"""Candidate proof chains and automatic final verification against reviewed targets."""
import time
from apps.api.infrastructure.store import digest
from apps.api.modules.core import task
from apps.api.modules.lean import POLICY, LIBRARY_ID, closure
from apps.api.modules.lifecycle import withdrawn
from apps.api.modules.research import final_target, research_policy

def candidate_chains(tx):
    insights={i['id']:i for i in tx.all('insights')}
    superseded={i['supersedes_id'] for i in insights.values() if i.get('supersedes_id') in insights and insights[i['supersedes_id']]['contributor_id']==i['contributor_id']}
    result=[]
    for candidate in insights.values():
        p=tx.get('problems',candidate['problem_id'])
        if p['status']=='archived' or candidate.get('claims_problem_solved',0) not in research_policy(p)['allowed_completion_claims']: continue
        if candidate.get('claims_problem_solved') not in (-1,1) or candidate['id'] in superseded or withdrawn(tx,'insights',candidate['id']): continue
        depths={};visiting=set()
        def visit(ident,depth):
            if ident in visiting: raise ValueError('Cyclic insight proof dependencies')
            if depth<=depths.get(ident,-1): return
            depths[ident]=depth;visiting.add(ident)
            for ref in insights[ident].get('references',[]):
                if ref in insights: visit(ref,depth+1)
            visiting.remove(ident)
        visit(candidate['id'],0)
        result.append((candidate,depths))
    return result

def priority_targets(tx):
    result={}
    for candidate,depths in candidate_chains(tx):
        p=tx.get('problems',candidate['problem_id'])
        if p and p['status']!='formal_proof_verified':
            for ident,depth in depths.items(): result[ident]=max(depth,result.get(ident,0))
    return result

def sync_candidates(tx):
    """Idempotent reconciliation; no target is inferred or changed from a submission."""
    artifacts=tx.all('lean_artifacts');verifications={v['id']:v for v in tx.all('verifications')}
    from apps.api.modules.lean import canonical_proof_ids
    canonical=canonical_proof_ids(tx)
    verified={}
    pending=set()
    for a in artifacts:
        v=verifications.get(a['verification_id'],{})
        if v.get('status') in ('queued','running'): pending.add(a['insight_id'])
        if canonical.get(a['insight_id'])==a['id']:
            verified.setdefault(a['insight_id'],[]).append(a)
    tasks=tx.all('tasks')
    chains=candidate_chains(tx)
    required={ident for _,depths in chains for ident in depths}
    active_required={ident for candidate,depths in chains if tx.get('problems',candidate['problem_id'])['status']=='open' for ident in depths}
    for t in tasks:
        if t['type']=='lean_work' and (t['status']=='available' or (t['status']=='claimed' and t.get('expires_at',0)<=time.time())):
            target=t.get('target_id')
            if t.get('mode')=='audit':
                artifact=tx.get('lean_artifacts',target)
                target=artifact['insight_id'] if artifact else None
            if target not in active_required and t.get('dispatch_scope')!='coverage':
                t.update(status='closed',closed_reason='Lean tasks require an active complete-proof candidate');tx.put('tasks',t)
        if t['type']=='lean_work' and t.get('mode')=='formalize' and t['status']=='available' and t.get('target_id') in required and verified.get(t['target_id']):
            t.update(status='closed',closed_reason='Verified Lean already available');tx.put('tasks',t)
    def ensure_work(ident,artifact=None,solution=None):
        target=tx.get('insight_targets',ident)
        if withdrawn(tx,'insights',ident) or not target or target.get('status')!='valid': return
        if ident in pending: return
        existing=next((t for t in tasks if t['type']=='lean_work' and t.get('target_id')==ident and t.get('mode') in ('formalize','repair') and t['status'] in ('available','claimed')),None)
        if existing:
            if artifact and existing['status']=='available':
                updated=dict(existing,mode='repair',repair_artifact_id=artifact['id'],goal='Repair the candidate proof or its Lean dependency links without changing the original statement; inspect the final target and diagnostics.')
                if solution: updated['solution_id']=solution['id']
                if updated!=existing: existing.update(updated);tx.put('tasks',existing)
            return
        insight=tx.get('insights',ident)
        if tx.get('problems',insight['problem_id'])['status']=='formal_proof_verified': return
        t=task(tx,insight['problem_id'],'lean_work',ident,mode='repair' if artifact else 'formalize')
        t['goal']='Formalize this insight faithfully as part of a claimed complete proof; declare the necessary verified Lean dependencies.'
        if artifact:
            t['repair_artifact_id']=artifact['id']
            t['goal']='Repair the candidate proof or its Lean dependency links without changing the original statement; inspect the final target and diagnostics.'
        if solution: t['solution_id']=solution['id']
        tx.put('tasks',t);tasks.append(t)
    for candidate,depths in chains:
        p=tx.get('problems',candidate['problem_id']);ident=candidate['id']
        claim=candidate['claims_problem_solved']
        state=dict(id=ident,insight_id=ident,problem_id=p['id'],claims_problem_solved=claim,verification_target=final_target(p,claim),required_insight_ids=sorted(depths),
                   missing_insight_ids=sorted(i for i in depths if not verified.get(i)),
                   missing_target_ids=sorted(i for i in depths if not tx.get('insight_targets',i) or tx.get('insight_targets',i).get('status')!='valid'),
                   target_ready=bool(p.get('lean_target') and p.get('target_status')=='reviewed'))
        if p['status']=='formal_proof_verified':
            state.update(status='problem_closed',solution_id=p.get('solution_id'))
        else:
            for missing in state['missing_insight_ids']: ensure_work(missing)
            if state['missing_target_ids']: state['status']='awaiting_insight_targets'
            elif state['missing_insight_ids']: state['status']='awaiting_lean'
            else:
                selected=None
                for a in sorted(verified.get(ident,[]),key=lambda x:x['created_at'],reverse=True):
                    chain=closure(tx,a)
                    if set(depths)<={x['insight_id'] for x in chain} and all(verifications.get(x['verification_id'],{}).get('status')=='passed' and x.get('library_id')==LIBRARY_ID for x in chain):
                        selected=a;break
                if not selected:
                    state['status']='awaiting_dependency_links'
                    ensure_work(ident,max(verified[ident],key=lambda x:x['created_at']))
                else:
                    for t in tasks:
                        if t['type']=='lean_work' and t.get('target_id')==ident and t.get('mode')=='repair' and t['status']=='available' and t.get('repair_artifact_id')!=selected['id']:
                            t.update(status='closed',closed_reason='Newer complete Lean chain available');tx.put('tasks',t)
                    if not state['target_ready']:
                        state.update(status='awaiting_reviewed_target',artifact_id=selected['id'])
                        if tx.get('proof_candidates',ident)!=state: tx.put('proof_candidates',state)
                        continue
                    target_hash=digest(final_target(p,claim))
                    matching=[s for s in tx.all('solutions') if s['artifact_id']==selected['id'] and s.get('target_hash')==target_hash and s.get('claims_problem_solved',1)==claim and (s['status']!='cancelled' or withdrawn(tx,'solutions',s['id']))]
                    solution=max(matching,key=lambda s:s['created_at']) if matching else tx.new('solutions','S',dict(
                        insight_id=ident,artifact_id=selected['id'],problem_id=p['id'],status='queued',automatic=True,claims_problem_solved=claim,
                        target_hash=target_hash,contributor_id=selected['contributor_id'],contributor_name=selected.get('contributor_name'),
                        proof=candidate['proof']))
                    state.update(status='final_'+solution['status'],artifact_id=selected['id'],solution_id=solution['id'])
                    if solution['status']=='failed': ensure_work(ident,selected,solution)
        if tx.get('proof_candidates',ident)!=state: tx.put('proof_candidates',state)
    for current in tx.all('proof_candidates'):
        if withdrawn(tx,'insights',current['insight_id']) and current['status']!='withdrawn':
            current.update(status='withdrawn');tx.put('proof_candidates',current)

def close_problem(tx,problem,solution):
    problem.update(resolution='disproved' if solution.get('claims_problem_solved',1)==-1 else 'proved',status='formal_proof_verified',solution_id=solution['id'],verified_at=solution['verified_at'])
    tx.put('problems',problem)
    for t in tx.all('tasks'):
        if t['problem_id']==problem['id'] and t['status'] in ('available','claimed'):
            t.update(status='closed',closed_reason='Problem final proof verified');tx.put('tasks',t)
