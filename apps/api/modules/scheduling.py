"""Per-problem hysteresis and reservation-aware automatic task dispatch."""
import math,time
from apps.api.modules.core import task
from apps.api.modules.scoring import Scores
from apps.api.modules.lean import POLICY,LIBRARY_ID
from apps.api.modules.recommendations import dependency_depths,ranking
from apps.api.modules.completion import sync_candidates,priority_targets
from apps.api.modules.exploration import ensure_exploration


def live(t):
    return t['status']=='claimed' and t.get('expires_at',0)>time.time()

def available(t):
    return t['status']=='available' or (t['status']=='claimed' and not live(t))

def snapshot(tx,pid,scores=None):
    scores=scores or Scores(tx)
    insights={i['id']:i for i in scores.objects['insights'].values() if i['problem_id']==pid and i['id'] not in scores.withdrawn|scores.superseded}
    reviewed={(ident,person) for (ident,person),r in scores.latest_reviews.items() if ident in insights and person!=insights[ident]['contributor_id'] and type(r.get('score')) is int and 1<=r['score']<=5}
    review_counts={ident:sum(i==ident for i,_ in reviewed) for ident in insights}
    passed=set();pending=set()
    for a in scores.objects['lean_artifacts'].values():
        if a['insight_id'] not in insights or a['id'] in scores.withdrawn|scores.superseded:continue
        v=scores.verifications.get(a['verification_id'],{})
        if a.get('library_id')!=LIBRARY_ID:continue
        if a['insight_id'] in scores.verified_insights:passed.add(a['insight_id'])
        if v.get('status') in ('queued','running') and (a.get('target') or {}).get('target_hash')==getattr(scores,'targets',{}).get(a['insight_id'],{}).get('target_hash') and a.get('target'):pending.add(a['insight_id'])
    review_reserved=set();lean_reserved=set()
    for t in tx.all('tasks'):
        if t['problem_id']!=pid or not live(t):continue
        if t['type']=='review':
            person=t.get('contributor_id')
            for ident in t.get('target_ids',[]) or [t.get('target_id')]:
                if ident in insights and person!=insights[ident]['contributor_id'] and (ident,person) not in reviewed:review_reserved.add((ident,person))
        if t['type']=='lean_work' and t.get('mode') in ('formalize','repair') and t.get('target_id') in insights:lean_reserved.add(t['target_id'])
    n=len(insights);r=len(reviewed);l=len(passed)
    from apps.api.modules.synthesis import active_plans
    plans=active_plans(tx,pid,scores)
    synthesis_reserved=sum(t['problem_id']==pid and t['type']=='research_synthesis' and live(t) for t in tx.all('tasks'))
    old=tx.get('dispatch_states',pid) or {}
    synthesis_refill=n>0 and (100*len(plans)<5*n or (old.get('synthesis_refill',False) and 100*len(plans)<7*n))
    review_refill=n>0 and (r<2*n or (old.get('review_refill',False) and r<3*n))
    lean_refill=n>0 and (20*l<7*n or (old.get('lean_refill',False) and 20*l<11*n))
    state=dict(synthesis_completed=len(plans),synthesis_reserved=synthesis_reserved,synthesis_refill=synthesis_refill,synthesis_target=(7*n+99)//100,synthesis_room=max(0,(7*n+99)//100-len(plans)-synthesis_reserved),id=pid,insights=n,reviews=r,verified_insights=l,review_reserved=len(review_reserved),lean_reserved=len((pending|lean_reserved)-passed),review_refill=review_refill,lean_refill=lean_refill,review_target=3*n,lean_target=((11*n+19)//20),review_room=max(0,3*n-r-len(review_reserved)),lean_room=max(0,((11*n+19)//20)-len(passed|pending|lean_reserved)))
    if state!=old:tx.put('dispatch_states',state)
    return state,insights,review_counts,passed,pending,lean_reserved


def sync_schedule(tx,problem_id=None):
    from apps.api.modules.migration import sync
    migration=sync(tx,problem_id)
    if migration is not None:return migration
    sync_candidates(tx)
    ensure_exploration(tx,problem_id)
    scores=Scores(tx);depths=dependency_depths(scores);priority=priority_targets(tx)
    states={}
    for p in tx.all('problems'):
        if p['status']!='open' or (problem_id and p['id']!=problem_id):continue
        from apps.api.modules.target_maintenance import sync as sync_targets
        sync_targets(tx,p['id'])
        state,insights,counts,passed,pending,reserved=snapshot(tx,p['id'],scores);states[p['id']]=state
        tasks=tx.all('tasks')
        for t in tasks:
            if (t['problem_id']==p['id'] and t['type']=='lean_work'
                    and t.get('mode') in ('formalize','repair')
                    and t.get('target_id') in passed and t['status'] in ('available','claimed')):
                t.update(status='closed',closed_reason='Verified Lean already available; duplicate formalization is not rewarded',expires_at=0,claim_hash='')
                tx.put('tasks',t)
        synthesis_available=[t for t in tasks if t['problem_id']==p['id'] and t['type']=='research_synthesis' and available(t)]
        if state['synthesis_refill'] and state['synthesis_room'] and not state['synthesis_reserved'] and not synthesis_available:
            task(tx,p['id'],'research_synthesis')
        if not state['synthesis_refill'] or not state['synthesis_room']:
            for t in synthesis_available:
                t.update(status='closed',closed_reason='Research guidance target already covered');tx.put('tasks',t)
        review_available=[t for t in tasks if t['problem_id']==p['id'] and t['type']=='review' and available(t)]
        if state['review_room'] and not review_available:
            t=task(tx,p['id'],'review');t['generation']='dynamic_review';tx.put('tasks',t)
        if not state['review_room']:
            for t in review_available:
                t.update(status='closed',closed_reason='Review target already covered by completed or reserved reviews');tx.put('tasks',t)
        pool=[i for i in insights.values() if i['id'] not in passed|pending|reserved and i['id'] not in priority and getattr(scores,'targets',{}).get(i['id'],{}).get('status')=='valid']
        pool.sort(key=lambda i:(-depths.get(i['id'],0),)+ranking(tx,scores,'task_claims',i))
        selected={i['id'] for i in pool[:state['lean_room']]} if state['lean_refill'] else set()
        for t in tasks:
            if t['problem_id']==p['id'] and t.get('dispatch_scope')=='coverage' and available(t) and t.get('target_id') not in selected and t.get('target_id') not in priority:
                t.update(status='closed',closed_reason='Lean coverage already satisfied or reserved');tx.put('tasks',t)
        for ident in selected:
            if any(t['type']=='lean_work' and t.get('target_id')==ident and t.get('mode') in ('formalize','repair') and (available(t) or live(t)) for t in tasks):continue
            failures=[a for a in scores.objects['lean_artifacts'].values() if a['insight_id']==ident and a['id'] not in scores.withdrawn|scores.superseded and scores.verifications.get(a['verification_id'],{}).get('status')=='failed']
            failure=max(failures,key=lambda a:a['created_at']) if failures else None
            t=task(tx,p['id'],'lean_work',ident,mode='repair' if failure else 'formalize')
            t.update(dispatch_scope='coverage',goal='Faithfully formalize this insight to improve problem-wide Lean coverage; preserve its exact mathematical claim and verified dependencies.')
            if failure:t['repair_artifact_id']=failure['id']
            tx.put('tasks',t);tasks.append(t)
    return states


def dispatch_priority(tx,t,states,priority,automatic=True,cache=None):
    """None means no useful capacity; lower integer wins."""
    from apps.api.modules.migration import active
    if active(tx):return -10 if t['type']=='lean_target' and available(t) else None
    if t['type']=='lean_target':return 2 if available(t) and states.get(t['problem_id']) else None
    state=states.get(t['problem_id'])
    if not state or not available(t):return None
    if t['type']=='research_synthesis':
        return -1 if state['synthesis_refill'] and state['synthesis_room'] and not state['synthesis_reserved'] else None
    if t['type']=='lean_work':
        ident=t.get('target_id')
        if t.get('mode')=='audit':
            a=tx.get('lean_artifacts',ident);ident=a['insight_id'] if a else None
        cache={} if cache is None else cache
        if t['problem_id'] not in cache:
            snap=snapshot(tx,t['problem_id']);cache[t['problem_id']]=(snap[3],snap[4])
        passed,pending=cache[t['problem_id']]
        target=tx.get('insight_targets',ident) if ident else None
        if t.get('mode')!='audit' and (not target or target.get('status')!='valid'):return None
        if ident in passed and t.get('mode')!='audit':return None
        if ident in priority:
            return None if ident in pending and t.get('mode')!='audit' else 0
        if t.get('dispatch_scope')=='coverage' and state['lean_refill'] and state['lean_room']>0 and ident not in passed|pending:return 2
        return None
    if t['type']=='review':
        return 1 if state['review_room']>0 and (state['review_refill'] or not automatic) else None
    return 3 if t['type']=='insight_explore' else None


# Smooth weighted round robin over currently useful work. Idle classes accumulate
# no backlog, and final-proof-chain work retains its existing urgent priority.
DISPATCH_WEIGHTS={'insight_explore':40,'lean_target:register':15,'lean_target:alignment':10,'lean_work':20,'review':10,'research_synthesis':5}
def dispatch_key(t):
    return 'lean_target:'+('alignment' if t.get('mode')=='alignment' else 'register') if t['type']=='lean_target' else t['type']

def fair_order(tx,options,priority):
    def score(t):
        state=tx.get('dispatch_credits',t['problem_id']) or {}
        credits=state.get('credits',{});key=dispatch_key(t)
        ident=t.get('target_id')
        if t['type']=='lean_work' and t.get('mode')=='audit':ident=(tx.get('lean_artifacts',ident) or {}).get('insight_id')
        urgent=t['type']=='lean_work' and ident in priority
        return (not urgent,-priority.get(ident,0) if urgent else 0,-(credits.get(key,0)+DISPATCH_WEIGHTS[key]),t['created_at'],t['id'])
    return sorted(options,key=score)

def record_dispatch(tx,selected,options):
    pid=selected['problem_id'];keys={dispatch_key(t) for t in options if t['problem_id']==pid}
    old=(tx.get('dispatch_credits',pid) or {}).get('credits',{})
    credits={key:old.get(key,0)+DISPATCH_WEIGHTS[key] for key in keys}
    key=dispatch_key(selected);credits[key]-=sum(DISPATCH_WEIGHTS[k] for k in keys)
    tx.put('dispatch_credits',dict(id=pid,credits=credits,last_type=selected['type'],last_mode=selected.get('mode'),updated_at=time.time()))
