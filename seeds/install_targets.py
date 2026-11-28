"""Atomically install the three reviewed target definitions, without marking them proved."""
import hashlib,json,time
from pathlib import Path
from apps.api.infrastructure.store import Store,digest

DIRECTORY=Path(__file__).resolve().parents[1]/'contracts/targets'
IDS=('P007','P009','P003')

def install(tx):
    review=json.loads((DIRECTORY/'validation.json').read_text())
    # Preserve the historical review receipt. CI checks these exact propositions on the active toolchain.
    if review['review_source_sha256']!=hashlib.sha256((DIRECTORY/'Review.lean').read_bytes()).hexdigest():
        raise ValueError('Target review source changed after validation')
    targets={pid:json.loads((DIRECTORY/(pid+'.json')).read_text()) for pid in IDS}
    for pid,target in targets.items():
        if digest(target)!=review['target_hashes'][pid]: raise ValueError('Target changed after validation: '+pid)
        p=tx.get('problems',pid)
        if not p or p['status']=='archived': raise ValueError('Active problem missing: '+pid)
        if p.get('lean_target') and p['lean_target']!=target: raise ValueError('Refusing to replace an existing fixed target: '+pid)
    for pid,target in targets.items():
        p=tx.get('problems',pid)
        if p.get('lean_target')==target and p.get('target_status')=='reviewed': continue
        p.update(lean_target=target,target_hash=digest(target),target_status='reviewed',
                 target_reviewer='Codex: definition, quantifier and environment checks',
                 target_reviewed_at=time.time(),target_validation=review)
        tx.put('problems',p)
    return dict(targets=list(IDS),target_status='reviewed',problem_status='unchanged')

if __name__=='__main__':
    with Store().transaction() as tx: result=install(tx)
    print(json.dumps(result))
