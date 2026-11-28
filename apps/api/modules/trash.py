"""Two independent low reviews atomically remove records from research and credit."""
import time
from fastapi import HTTPException
from apps.api.modules.core import need
from apps.api.modules.lifecycle import change


def apply_low_reviews(tx,review):
    if type(review.get('score')) is not int or review['score']>2:return
    insight=need(tx,'insights',review['target_id'])
    from apps.api.modules.scoring import Scores
    scores=Scores(tx)
    low=[r for (ident,person),r in scores.latest_reviews.items() if ident==insight['id'] and person!=insight['contributor_id'] and type(r.get('score')) is int and 0<=r['score']<=2]
    if len(low)<2 or tx.get('trash',insight['id']):return
    reason='Two independent low reviews (score <=2): '+'; '.join(r['rationale'] for r in low)
    change(tx,insight['contributor_id'],'insights',insight['id'],dict(contributor_id=insight['contributor_id'],reason=reason),True)
    for artifact in tx.all('lean_artifacts'):
        if artifact['insight_id']==insight['id']:
            change(tx,artifact['contributor_id'],'lean_artifacts',artifact['id'],dict(contributor_id=artifact['contributor_id'],reason='Linked Insight removed by two independent low reviews'),True)
    tx.put('trash',dict(id=insight['id'],insight_id=insight['id'],problem_id=insight['problem_id'],title=insight['title'],contributor_name=insight.get('contributor_name',''),review_id=review['id'],reviewer_name=review.get('contributor_name',''),reason=reason,trashed_at=time.time(),score=review['score'],review_ids=[r['id'] for r in low],low_review_count=len(low)))
