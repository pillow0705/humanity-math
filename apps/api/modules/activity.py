"""Public activity projection; expose only submission and passed-verification events."""
from apps.api.modules.lifecycle import withdrawn


def recent_activity(tx, limit=8):
    problems={p['id']:p for p in tx.all('problems') if p.get('status')!='archived'}
    insights={i['id']:i for i in tx.all('insights') if i['problem_id'] in problems and not withdrawn(tx,'insights',i['id'])}
    items=[]
    for i in insights.values():
        items.append(dict(event='insight_submitted',object_id=i['id'],kind='insights',problem_id=i['problem_id'],title=i['title'],author_name=i.get('contributor_name',''),timestamp=i['created_at']))
    from apps.api.modules.lean import canonical_proof_ids
    canonical=canonical_proof_ids(tx)
    for a in tx.all('lean_artifacts'):
        if canonical.get(a['insight_id'])!=a['id']:continue
        i=insights.get(a['insight_id'])
        if not i or withdrawn(tx,'lean_artifacts',a['id']):continue
        v=tx.get('verifications',a['verification_id']) or {}
        if v.get('status')!='passed':continue
        items.append(dict(event='lean_passed',object_id=a['id'],kind='lean',problem_id=i['problem_id'],title=i['title'],author_name=a.get('contributor_name',''),timestamp=v.get('finished_at') or v.get('created_at') or a['created_at']))
    items.sort(key=lambda x:(x['timestamp'],x['object_id'],x['event']),reverse=True)
    return dict(items=items[:limit])
