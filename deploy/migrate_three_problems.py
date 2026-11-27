"""Operator-only, repeat-safe roster migration. Preserve historical IDs and records."""
import time
from apps.api.infrastructure.store import Store
from seeds.bootstrap import DATA, seed

ACTIVE_IDS={row[0] for row in DATA}

def migrate(tx):
    for row in DATA:
        old=tx.get('problems',row[0])
        if old and old.get('lean_target') and old['statement']!=row[2]:
            raise ValueError('Review the existing fixed target before changing its problem statement: '+row[0])
    seed(tx)
    stamp=time.time()
    for order,row in enumerate(DATA,1):
        p=tx.get('problems',row[0])
        p.update(title=row[1],statement=row[2],display_order=order)
        tx.put('problems',p)
    archived=set()
    for p in tx.all('problems'):
        if p['id'] in ACTIVE_IDS: continue
        archived.add(p['id'])
        if p['status']!='archived':
            p.update(previous_status=p['status'],status='archived',archived_at=stamp,archive_reason='Focus on Euler constant, pi + e, and Collatz')
            tx.put('problems',p)
    for t in tx.all('tasks'):
        if t['problem_id'] in archived and t['status'] in ('available','claimed'):
            t.update(status='closed',closed_reason='Problem archived');tx.put('tasks',t)
    for v in tx.all('verifications'):
        a=tx.get('lean_artifacts',v['artifact_id'])
        if a['problem_id'] in archived and v['status'] in ('queued','running'):
            v.update(status='cancelled',log='Problem archived');tx.put('verifications',v)
    for s in tx.all('solutions'):
        if s['problem_id'] in archived and s['status'] in ('queued','running'):
            s.update(status='cancelled',log='Problem archived');tx.put('solutions',s)
    return dict(active=sorted(ACTIVE_IDS),archived=sorted(archived))

if __name__=='__main__':
    with Store().transaction() as tx: result=migrate(tx)
    print(result)
