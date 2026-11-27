"""Operator only: fix an independently reviewed problem target once."""
import argparse,json
from apps.api.infrastructure.store import Store,digest
p=argparse.ArgumentParser(); p.add_argument('problem'); p.add_argument('file'); p.add_argument('--reviewer',required=True); args=p.parse_args()
target=json.load(open(args.file))
assert set(target)<= {'definitions','expression'} and target.get('expression')
with Store().transaction() as tx:
    obj=tx.get('problems',args.problem)
    if not obj: raise SystemExit('Unknown problem')
    if obj.get('lean_target'): raise SystemExit('Target already fixed; create a distinct problem for a different statement')
    obj.update(lean_target=target,target_hash=digest(target),target_status='reviewed',target_reviewer=args.reviewer)
    tx.put('problems',obj)
print('Fixed target recorded')
