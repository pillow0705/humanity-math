"""Public research milestones; never expose private credentials or review events."""
import time
from apps.api.modules.lifecycle import withdrawn
from apps.api.modules.scoring import Scores


def recent_activity(tx, limit=8,scores=None):
    problems={p['id']:p for p in tx.all('problems') if p.get('status')!='archived'}
    insights={i['id']:i for i in tx.all('insights') if i['problem_id'] in problems and not withdrawn(tx,'insights',i['id'])}
    items=[]
    scores=scores or Scores(tx)
    milestones={m['id']:m for m in tx.all('activity_milestones')}
    for i in insights.values():
        items.append(dict(event='insight_submitted',object_id=i['id'],kind='insights',problem_id=i['problem_id'],title=i['title'],author_name=i.get('contributor_name',''),timestamp=i['created_at']))
    for i in insights.values():
        if i['id'] in scores.superseded:continue
        points=scores.evaluation(i,'insights')['contribution_points']
        ident='score30-'+i['id']
        # Persist the first observed crossing so refreshes and score fluctuations
        # cannot generate repeat announcements. This is an observation timestamp.
        if points>30 and ident not in milestones:
            milestone=dict(id=ident,timestamp=time.time())
            tx.put('activity_milestones',milestone);milestones[ident]=milestone
        if ident in milestones:
            items.append(dict(event='insight_score_30',object_id=i['id'],kind='insights',problem_id=i['problem_id'],title=i['title'],author_name=i.get('contributor_name',''),timestamp=milestones[ident]['timestamp']))
    people={p['id']:p for p in tx.all('contributors')}
    for target in tx.all('insight_targets'):
        i=insights.get(target['insight_id'])
        if not i or target.get('status')!='valid' or not target.get('migration_batch_id'):continue
        actor=people.get(target.get('registered_by'),{})
        items.append(dict(event='lean_target_valid',object_id=i['id'],kind='insights',problem_id=i['problem_id'],title=i['title'],author_name=actor.get('name',''),timestamp=target.get('finished_at') or target['created_at']))
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
