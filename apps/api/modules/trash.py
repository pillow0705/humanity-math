"""Zero reviews atomically remove irrelevant records from research and credit."""
import time
from fastapi import HTTPException
from apps.api.modules.core import need
from apps.api.modules.lifecycle import change


def zero_review(tx,review):
    if review.get('score')!=0:return
    insight=need(tx,'insights',review['target_id'])
    if review['contributor_id']==insight['contributor_id']:raise HTTPException(422,'A self-review cannot use zero to trigger removal; use the submission withdrawal endpoint')
    change(tx,insight['contributor_id'],'insights',insight['id'],dict(contributor_id=insight['contributor_id'],reason='Zero review: '+review['rationale']),True)
    for artifact in tx.all('lean_artifacts'):
        if artifact['insight_id']==insight['id']:
            change(tx,artifact['contributor_id'],'lean_artifacts',artifact['id'],dict(contributor_id=artifact['contributor_id'],reason='Linked Insight removed by zero review'),True)
    tx.put('trash',dict(id=insight['id'],insight_id=insight['id'],problem_id=insight['problem_id'],title=insight['title'],contributor_name=insight.get('contributor_name',''),review_id=review['id'],reviewer_name=review.get('contributor_name',''),reason=review['rationale'],trashed_at=time.time(),score=0))
