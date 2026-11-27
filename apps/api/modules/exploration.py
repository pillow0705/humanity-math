"""Open problems supply exploration work on demand, without a finite task quota."""
import time
from apps.api.modules.core import task
from apps.api.modules.lifecycle import withdrawn


def ensure_exploration(tx,problem_id=None):
    problems=[p for p in tx.all('problems') if p['status']=='open' and (not problem_id or p['id']==problem_id)]
    tasks=tx.all('tasks');now=time.time();created=[]
    # Retire the former per-insight exploration rule while preserving live leases.
    for t in tasks:
        if t['type']!='insight_explore' or not t.get('target_id') or (problem_id and t['problem_id']!=problem_id): continue
        if t['status']=='claimed' and t.get('expires_at',0)>now:
            t.update(target_id=None,goal='Explore the original problem using shared insights and research guidance. Select your own research target and cite the results actually used.')
            tx.put('tasks',t)
        elif t['status'] in ('available','claimed'):
            t.update(status='closed',closed_reason='Per-insight directed exploration retired')
            tx.put('tasks',t)
    for problem in problems:
        def available(t):
            if t['type']!='insight_explore' or t['problem_id']!=problem['id']: return False
            if not (t['status']=='available' or (t['status']=='claimed' and t.get('expires_at',0)<=now)): return False
            ident=t.get('target_id')
            return not ident or (tx.get('insights',ident) is not None and not withdrawn(tx,'insights',ident))
        if any(available(t) for t in tasks): continue
        item=task(tx,problem['id'],'insight_explore')
        item.update(generation='on_demand',goal='Explore this open problem using the shared insight catalog. Develop a substantive new result or approach; cite the insights actually used. No single existing insight is mandatory for this task.')
        tx.put('tasks',item);tasks.append(item);created.append(item)
    return created
