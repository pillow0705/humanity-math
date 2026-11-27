"""Problem-level research guidance derived from the shared insight graph."""
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need,clean,finish_task,contribute,check_supersedes
from apps.api.modules.scoring import Scores

def active_plans(tx,pid,scores=None):
    scores=scores or Scores(tx)
    return sorted([x for x in tx.all('research_plans') if x['problem_id']==pid and x['id'] not in scores.withdrawn|scores.superseded],key=lambda x:(x['created_at'],x['id']))

def catalog(tx,pid,prefix=''):
    scores=Scores(tx)
    return [dict(id=x['id'],title=x['title'],claim=x['claim'][:1600],references=x.get('references',[]),proof_status=x['proof_status'],evaluation=scores.evaluation(x,'insights'),lean_passed=x['id'] in scores.verified_insights,detail_url=prefix+'/api/v1/insights/'+x['id']) for x in sorted(scores.objects['insights'].values(),key=lambda x:x['created_at']) if x['problem_id']==pid and x['id'] not in scores.withdrawn|scores.superseded]

def submit_plan(tx,body):
    check_supersedes(tx,'research_plans',body,body['problem_id'])
    t=need(tx,'tasks',body['task_id'])
    snapshot=t.get('synthesis_insight_ids',[])
    if not snapshot: raise HTTPException(409,'Claim a research_synthesis task before submitting a research plan')
    for path in body['paths']:
        for ident in path['insight_ids']:
            insight=need(tx,'insights',ident)
            if insight['problem_id']!=body['problem_id']: raise HTTPException(422,'Path insights must belong to this problem')
    fingerprint=digest({k:body[k] for k in ('problem_id','title','summary','paths')})
    if any(x.get('content_hash')==fingerprint for x in tx.all('research_plans')): raise HTTPException(409,'Duplicate research plan')
    obj=tx.new('research_plans','D',dict(clean(body),content_hash=fingerprint,source_insight_ids=snapshot,previous_plan_id=t.get('previous_plan_id'),status='submitted'))
    finish_task(tx,body,'research_synthesis',None,body['problem_id'],obj['id'])
    contribute(tx,obj,'research_synthesis')
    return obj
