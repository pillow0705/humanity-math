"""Fair exposure with bounded review and research-impact weighting."""
import math, random, time
from collections import deque
from apps.api.infrastructure.store import digest
from apps.api.modules.scoring import Scores

ALGORITHM='weighted-sample-v3'
RNG=random.SystemRandom()
CATALOG_SIZE=300
EXPLORATION_CATALOG_SIZE=100

def policy(sample_size=CATALOG_SIZE):
    return dict(sample_size=sample_size,all_when_at_most=sample_size,exploration_sample_size=EXPLORATION_CATALOG_SIZE,without_replacement=True,min_weight=0.5,max_weight=2.0,review_prior_count=3,
                smoothed_review='(9 + sum(effective non-self review scores)) / (3 + count)',
                review_weight='clamp(smoothed_review / 3, 0.5, 1.5)',
                impact_points='Contribution points above review-adjusted base: insight citations/Lean/final-proof bonuses',
                impact_bonus='0.5 * impact_points / (impact_points + 10)',
                weight='min(2, review_weight * (1 + impact_bonus))',
                selection=f'Return all active results when count <= {sample_size}; otherwise weighted random sampling of {sample_size} distinct results',
                task_selection='Automatic dispatch: research synthesis refill (5% to 7%), complete-proof Lean chains, review refill (2N to 3N), Lean coverage refill (35% to 55%), then exploration. Within each class use foundational dependencies, review coverage and fair exposure.',
                contribution_citation_cap=None)

def weight(scores,obj):
    reviews=[x for (target,person),x in scores.latest_reviews.items() if target==obj['id'] and person!=obj['contributor_id'] and type(x.get('score')) is int]
    smoothed=(9+sum(x['score'] for x in reviews))/(3+len(reviews))
    review_weight=max(0.5,min(1.5,smoothed/3))
    kind=next((k for k in ('insights',) if obj['id'] in scores.objects[k]),None)
    impact=0.0
    if kind:
        evaluation=scores.evaluation(obj,kind)
        impact=max(0.0,evaluation['contribution_points']-evaluation['review_adjusted_points'])
    bonus=0.5*impact/(impact+10)
    return dict(weight=min(2.0,review_weight*(1+bonus)),smoothed_review=smoothed,
                review_weight=review_weight,impact_points=impact,impact_bonus=bonus)

def record(tx,surface,target):
    ident=digest(dict(surface=surface,target=target))
    return tx.get('exposures',ident) or dict(id=ident,surface=surface,target=target,count=0,last_at=0)

def ranking(tx,scores,surface,obj):
    exposure=record(tx,surface,obj['id']); factors=weight(scores,obj)
    return (0 if exposure['count']==0 else 1,(exposure['count']+1)/factors['weight'],exposure['last_at'],obj['created_at'],obj['id'])

def note(tx,surface,target):
    r=record(tx,surface,target); r.update(count=r['count']+1,last_at=time.time()); tx.put('exposures',r)

def weighted_sample(items,weights,count):
    if len(items)<=count: return list(items)
    # Exponential races give weighted sampling without replacement.
    return [x for _,_,x in sorted((-math.log(max(RNG.random(),1e-300))/w,n,x) for n,(x,w) in enumerate(zip(items,weights)))[:count]]

def recommend(tx,problem_id,prefix='',kind='insights',surface='recommendations',sample_size=CATALOG_SIZE):
    scores=Scores(tx)
    items=[x for x in tx.all(kind) if x['problem_id']==problem_id and x['id'] not in scores.superseded and x['id'] not in scores.withdrawn]
    selected=weighted_sample(items,[weight(scores,x)['weight'] for x in items],sample_size)
    result=[]
    for x in selected:
        factors=weight(scores,x); exposure=record(tx,surface,x['id'])
        summary=x.get('claim',x.get('summary',''))
        result.append(dict(id=x['id'],title=x['title'],type='insight',
                           summary=summary[:800],summary_truncated=len(summary)>800,
                           claims_problem_solved=x.get('claims_problem_solved',0),detail_url=prefix+'/api/v1/'+kind+'/'+x['id'],
                           **{k:round(v,6) for k,v in factors.items()},previous_exposures=exposure['count'],
                           reason='All available results' if len(items)<=sample_size else 'Weighted random sample'))
        note(tx,surface,x['id'])
    return dict(algorithm=ALGORITHM,kind=kind,total_available=len(items),items=result,policy=policy(sample_size))

def dependency_depths(scores):
    """Longest citation distance from a result to each of its prerequisites."""
    insights=scores.objects['insights']
    edges={i:{r for r in obj.get('references',[]) if r in insights and r!=i} for i,obj in insights.items()}
    incoming={i:0 for i in insights};depth={i:0 for i in insights}
    for refs in edges.values():
        for ref in refs: incoming[ref]+=1
    ready=deque(i for i,count in incoming.items() if count==0)
    while ready:
        ident=ready.popleft()
        for ref in edges[ident]:
            depth[ref]=max(depth[ref],depth[ident]+1);incoming[ref]-=1
            if incoming[ref]==0: ready.append(ref)
    return depth

def sort_tasks(tx,options):
    from apps.api.modules.completion import priority_targets
    scores=Scores(tx)
    priority=priority_targets(tx)
    depths=dependency_depths(scores)
    def key(t):
        chain_priority=(0,-priority[t['target_id']]) if t['type']=='lean_work' and t.get('mode') in ('formalize','repair') and t.get('target_id') in priority else (1,0)
        foundation=(0,-depths[t['target_id']]) if t['type']=='lean_work' and t.get('mode') in ('formalize','repair') and depths.get(t.get('target_id'),0)>0 else (1,0)
        obj=next((scores.objects[k].get(t.get('target_id')) for k in ('insights',) if t.get('target_id') in scores.objects[k]),None)
        if obj: return chain_priority+foundation+ranking(tx,scores,'task_claims',obj)
        # Unbound design tasks and Lean artifacts also have exposure tracking.
        synthetic=dict(id=t.get('target_id') or t['id'],contributor_id=None,created_at=t['created_at'])
        return chain_priority+foundation+ranking(tx,scores,'task_claims',synthetic)
    return sorted(options,key=key)
