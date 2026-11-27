import copy, hashlib, io, json, os, secrets, time, zipfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import PlainTextResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from apps.api.infrastructure.store import Store, digest
from apps.api.models import Insight, Review, ReviewBatch, Claim, Lease, Artifact, Audit, Solution, ContributorClaim, ContributorAction, CredentialLogin, CredentialChange, ResearchPlan, LeanTargetRegistration, LeanTargetBatch, BoardMessage, TargetAuditBatch, TargetAudit
from apps.api.modules.core import need, find_content, public_task, create_content, clean, finish_task, contribute, check_supersedes, supported_task
from apps.api.modules.lean import create_artifact, closure, environment, artifact_summary, MATHLIB_REVISION, bundle_manifest

from apps.api.modules.recommendations import recommend, sort_tasks, note as note_exposure, CATALOG_SIZE, EXPLORATION_CATALOG_SIZE
from apps.api.modules.contributors import claim_name, validate_identity
from apps.api.modules.credentials import issue as issue_credential, verify as verify_credential, public_person
from apps.api.modules.scoring import Scores, policy as scoring_policy
from apps.api.modules.completion import sync_candidates
from apps.api.modules.research import assign_direction, direction_instruction, final_target, research_policy, describe_problem, validate_completion_claim
from apps.api.modules.exploration import ensure_exploration
from apps.api.modules.scheduling import sync_schedule, dispatch_priority
from apps.api.modules.management import submissions as owned_submissions, submission as owned_submission, ranked
from apps.api.modules.lifecycle import state as submission_state, withdrawn, change as change_submission
from apps.api.modules.targets import get_target, register_target, validate_review_alignment, target_source, target_proof_matches

BASE=Path(__file__).resolve().parents[2]
REGISTRATION_CREDENTIALS=True
PREFIX=os.environ.get('PUBLIC_PREFIX','')
store=Store()
app=FastAPI(root_path=PREFIX,title='Math Collab', version='0.1.0', description='File-backed collaborative mathematics. Immutable submissions; Private eight-digit credentials for new accounts; stable internal attribution IDs.')

@app.middleware('http')
async def limits(request: Request, call_next):
    if request.method in ('POST','PUT','PATCH'):
        raw=await request.body()
        if len(raw)>512000: return Response('Request exceeds 512 KB',status_code=413)
    try:
        response=await call_next(request)
        if request.method!='GET': response.headers['Cache-Control']='no-store'
        return response
    except ValueError: return Response('Invalid record identifier',status_code=422)

def submit(request, body, operation):
    key=request.headers.get('Idempotency-Key')
    if key and not 1 <= len(key) <= 200: raise HTTPException(422,'Invalid idempotency key')
    fingerprint=digest(dict(path=request.url.path,body={k:v for k,v in body.items() if k!='contributor_password'}))
    key_id=digest(dict(key=key,contributor=body.get('contributor_id'))) if key else None
    with store.transaction() as tx:
        validate_identity(tx,body)
        from apps.api.modules.migration import mainline
        if not request.url.path.endswith('/lean-targets/batch') and not request.url.path.endswith('/lean-target/recheck') and '/message-board' not in request.url.path and not (request.url.path.endswith('/reviews') and body.get('score')==0):mainline(tx)
        old=tx.get('idempotency',key_id) if key_id else None
        if old:
            if old['fingerprint']!=fingerprint: raise HTTPException(409,'Idempotency key reused for different input')
            return old['response']
        result=operation(tx)
        if key_id: tx.put('idempotency',dict(id=key_id,fingerprint=fingerprint,response=result))
        return result

def page(items, offset, limit): return dict(total=len(items),offset=offset,limit=limit,items=items[offset:offset+limit])

def prefix_agent_paths(text):
    import re
    return re.sub(r'(?<![\w/])/(?:api/v1|docs|openapi\.json)',lambda match:PREFIX+match.group(0),text)

def prompt_for(t):
    name=t['type']
    if name=='review': name='insight_review'
    path=BASE/'prompts'/f'{name}.md'
    text=prefix_agent_paths(path.read_text())
    if t.get('research_direction') and name in ('insight_explore',):
        text=text.replace('\n\n# ', '\n\n'+direction_instruction(t['research_direction'])+'\n\n# ',1)
    if name=='insight_explore' and t.get('problem_id') in ('P007','P009'):
        text=text.replace('\n\n# ', '\n\nProblem policy: pursue a proof of irrationality (100%). Only completion flags 0 and 1 are allowed; -1 is disabled. Every intermediate result, including a proved local lemma, uses 0.\n\n# ',1)
    return dict(type=name,text=text)

def target_registration_package(tx,t):
    items=[need(tx,'insights',ident) for ident in t['target_ids']]
    schemas=app.openapi()['components']['schemas']
    if t.get('mode')=='alignment':
        schema=copy.deepcopy(schemas['TargetAuditBatch'])
        schema['properties']['audits']['items']=copy.deepcopy(schemas['TargetAuditItem'])
        schema['properties']['audits'].update(minItems=len(items),maxItems=len(items))
        return dict(collaboration_language='en',mode='alignment',problem=describe_problem(need(tx,'problems',t['problem_id'])),goal=t['goal'],target_ids=t['target_ids'],target_hashes=t['target_hashes'],materials=[dict(i,lean_target=get_target(tx,i['id'])) for i in items],prompt=dict(type='lean_target_alignment',text=(BASE/'prompts/lean_target_alignment.md').read_text()),submission=dict(method='POST',url=PREFIX+'/api/v1/lean-targets/audits/batch',schema=schema),batch_points=8,flag_threshold=2)
    schema=copy.deepcopy(schemas['LeanTargetBatch'])
    schema['properties']['targets']['items']=copy.deepcopy(schemas['LeanTargetItem'])
    schema['properties']['targets']['items']['properties']['lean_target']=copy.deepcopy(schemas['LeanTarget'])
    schema['properties']['targets'].update(minItems=len(items),maxItems=len(items))
    refs=sorted({ref for item in items for ref in item.get('references',[])})
    reference_targets=[dict(insight_id=ident,lean_target=get_target(tx,ident),detail_url=PREFIX+'/api/v1/insights/'+ident) for ident in refs]
    return dict(collaboration_language='en',problem=describe_problem(need(tx,'problems',t['problem_id'])),goal=t['goal'],target_ids=t['target_ids'],materials=[dict(item,lean_target=get_target(tx,item['id'])) for item in items],reference_targets=reference_targets,lean_environment=environment(PREFIX),prompt=prompt_for(t),submission=dict(method='POST',url=PREFIX+'/api/v1/lean-targets/batch',schema=schema,include_fields=['contributor_id','contributor_name','contributor_password','task_id','claim_token']),mode='register',maintenance=dict(batch_size=len(items),batch_points=8,proofs_required=False,note='Register complete propositions, not proofs. Target-definition imports do not prove their statements. Root dependencies are prioritized.'))

def research_package(tx,t):
    if not supported_task(t): raise HTTPException(409,'Unsupported task')
    if t['type']=='lean_target':return target_registration_package(tx,t)
    problem=describe_problem(need(tx,'problems',t['problem_id'])); target=None; materials=[]
    if t['type']=='review' and t.get('target_ids'):
        materials=[need(tx,'insights',i) for i in t['target_ids']]
        target=materials[0]
    elif t.get('target_id'):
        if t['type']=='lean_work' and t.get('mode')=='audit': target=need(tx,'lean_artifacts',t['target_id'])
        else: target=find_content(tx,t['target_id'])
        materials.append(target)
        if t['type']=='lean_work' and t.get('mode')=='audit': materials.append(need(tx,'insights',target['insight_id']))
    else:
        materials=sorted([x for x in tx.all('insights') if x['problem_id']==problem['id'] and not withdrawn(tx,'insights',x['id'])],key=lambda x:x['created_at'],reverse=True)[:10]
    seen={x['id'] for x in materials}; queue=list(materials) if t.get('target_id') else []
    while queue:
        item=queue.pop(0)
        for ref in item.get('references',[]):
            if ref.startswith('I') and ref not in seen:
                obj=find_content(tx,ref); seen.add(ref); materials.append(obj); queue.append(obj)
    materials=[dict(x,lean_target=get_target(tx,x['id'])) if x['id'].startswith('I') else x for x in materials]
    reviews=[x for x in tx.all('reviews') if x['target_id'] in seen]
    endpoint={'insight_explore':'/insights','review':'/reviews/batch','research_synthesis':'/problems/'+problem['id']+'/research-guidance','lean_work':f"/lean/artifacts/{t.get('target_id')}/audits" if t.get('mode')=='audit' else '/lean/artifacts'}[t['type']]
    schemas={'insight_explore':'Insight','review':'ReviewBatch','research_synthesis':'ResearchPlan','lean_work':'Audit' if t.get('mode')=='audit' else 'Artifact'}
    from apps.api.modules.lean import canonical_proof_ids
    canonical=canonical_proof_ids(tx)
    code=[artifact_summary(tx,x,PREFIX,canonical) for x in tx.all('lean_artifacts') if x['insight_id'] in seen or x['id'] in seen]
    code=[x for x in code if not x['historical_duplicate']]
    repair=None
    if t.get('repair_artifact_id'):
        failed=need(tx,'lean_artifacts',t['repair_artifact_id'])
        repair=dict(artifact=failed,verification=need(tx,'verifications',failed['verification_id']))
    alternative=None
    if t['type']=='lean_work' and t.get('mode')=='audit':
        alternative=dict(method='POST',url=PREFIX+'/api/v1/lean/artifacts',schema=app.openapi()['components']['schemas']['Artifact'],insight_id=target['insight_id'],note='Submit corrected code instead of an audit report when you can fix the proof; preserve the mathematical statement.')
    final_check=tx.get('solutions',t['solution_id']) if t.get('solution_id') else None
    candidate_context=[c for c in tx.all('proof_candidates') if t.get('target_id') in c.get('required_insight_ids',[])]
    submission_schema=copy.deepcopy(app.openapi()['components']['schemas'][schemas[t['type']]])
    if t['type']=='review':
        submission_schema['properties']['reviews']['items']=copy.deepcopy(app.openapi()['components']['schemas']['ReviewItem'])
        for deprecated in ('target_alignment','target_hash'):submission_schema['properties']['reviews']['items']['properties'].pop(deprecated,None)
        submission_schema['properties']['reviews'].update(minItems=len(t['target_ids']),maxItems=len(t['target_ids']))
    if t['type']=='research_synthesis':
        def inline_schema(value):
            if isinstance(value,dict):
                if '$ref' in value:return inline_schema(copy.deepcopy(app.openapi()['components']['schemas'][value['$ref'].split('/')[-1]]))
                return {k:inline_schema(v) for k,v in value.items()}
            if isinstance(value,list):return [inline_schema(v) for v in value]
            return value
        submission_schema=inline_schema(submission_schema)
    if t['type']=='insight_explore':
        submission_schema['properties']['lean_target']={'anyOf':[copy.deepcopy(app.openapi()['components']['schemas']['LeanTarget']),{'type':'null'}],'default':None}
        field=submission_schema['properties']['claims_problem_solved']
        field.update(enum=research_policy(problem)['allowed_completion_claims'],minimum=min(research_policy(problem)['allowed_completion_claims']))
    from apps.api.modules.synthesis import get_guide,catalog
    current_guide=get_guide(tx,problem['id'])
    guidance=dict(latest=current_guide,base_hash=current_guide['content_hash'],detail_url=PREFIX+'/api/v1/problems/'+problem['id']+'/research-guidance',file_url=PREFIX+'/api/v1/problems/'+problem['id']+'/research-guidance/file')
    synthesis_catalog=catalog(tx,problem['id'],PREFIX) if t['type']=='research_synthesis' else None
    insight_catalog=recommend(tx,problem['id'],PREFIX,surface='task_packages',sample_size=EXPLORATION_CATALOG_SIZE if t['type']=='insight_explore' else CATALOG_SIZE)
    return dict(lean_target=get_target(tx,target.get('insight_id',target['id'])) if t['type']=='lean_work' and target else None,research_guidance=guidance,synthesis_catalog=synthesis_catalog,collaboration_language='en',submission_language='en',historical_content_policy='Historical research remains in its original language. Submit all new explanatory prose in English; preserve mathematical notation, identifiers and original source titles.',research_policy=research_policy(problem),research_direction=t.get('research_direction'),completion_context=candidate_context,final_check_context=final_check,insight_catalog=insight_catalog,review_targets=[dict(insight_id=i,detail_url=PREFIX+'/api/v1/insights/'+i) for i in t.get('target_ids',[])],alternative_submission=alternative,repair_context=repair,problem=problem,goal=t.get('goal') or f"{t['type']} for {t.get('target_id') or problem['id']}",materials=materials,reviews=reviews,lean_environment=environment(PREFIX),lean_artifacts=code,prompt=prompt_for(t),submission=dict(method='POST',url=PREFIX+'/api/v1'+endpoint,schema=submission_schema,include_fields=['contributor_id','contributor_name','contributor_password','task_id','claim_token']))

@app.get('/',response_class=HTMLResponse)
def homepage():
    html=(BASE/'apps/web/index.html').read_text()
    version=hashlib.sha256(os.environ.get('APP_RELEASE','development').encode()).hexdigest()[:16]
    return HTMLResponse(html.replace('__BASE__',PREFIX).replace('__ASSET_VERSION__',version),headers={'Cache-Control':'no-cache'})

app.mount('/assets',StaticFiles(directory=BASE/'apps/web/assets'),name='assets')

@app.post('/api/v1/contributors/claim')
def claim_contributor(body:ContributorClaim,request:Request):
    key=request.headers.get('Idempotency-Key')
    if key and not 1<=len(key)<=200: raise HTTPException(422,'Invalid idempotency key')
    with store.transaction() as tx:
        key_id=digest(dict(path='contributor_claim',key=key)) if key else None
        old=tx.get('idempotency',key_id) if key_id else None
        fingerprint=digest(body.model_dump())
        if old:
            if old['fingerprint']!=fingerprint: raise HTTPException(409,'Idempotency key reused for a different name')
            if old.get('credential_issued'): raise HTTPException(409,'Registration already completed; use your saved private credential. It is never replayed.')
            return old['response']
        result=claim_name(tx,body.name)
        password=issue_credential(tx,result) if REGISTRATION_CREDENTIALS else None
        if key_id: tx.put('idempotency',dict(id=key_id,fingerprint=fingerprint,response=public_person(result) if password else result,credential_issued=bool(password)))
        return dict(public_person(result),number=password,contributor_password=password) if password else result

@app.post('/api/v1/contributors/authenticate')
def authenticate_contributor(body:CredentialLogin):
    with store.transaction() as tx:
        person=next((p for p in tx.all('contributors') if p['name']==body.name),None)
        if not person: raise HTTPException(403,'Invalid contributor credential')
        if tx.get('credentials',person['id']): verify_credential(tx,person,body.password)
        elif body.password!=person['id']: raise HTTPException(403,'Invalid contributor credential')
        return dict(public_person(person),credential_enabled=bool(tx.get('credentials',person['id'])),contribution=Scores(tx).total(person['id']))

@app.post('/api/v1/contributors/{id}/credential')
def change_credential(id:str,body:CredentialChange):
    b=body.model_dump()
    with store.transaction() as tx:
        person=validate_identity(tx,b)
        if body.new_password and body.new_password==body.contributor_password: raise HTTPException(422,'New credential must differ from the old credential')
        if person['id']!=id: raise HTTPException(403,'Contributor does not match')
        password=issue_credential(tx,person,body.new_password)
        for t in tx.all('tasks'):
            if t.get('contributor_id')==id and t['status']=='claimed':
                t.update(status='available',expires_at=0)
                for key in ('claim_hash','contributor_id','contributor_name'): t.pop(key,None)
                tx.put('tasks',t)
        return dict(public_person(person),number=password,contributor_password=password)

@app.get('/api/v1/contributors/{id}')
def contributor(id:str):
    with store.transaction() as tx:
        person=need(tx,'contributors',id)
        return dict(public_person(person),contribution=Scores(tx).total(id))

@app.get('/api/v1/contribution-policy')
def contribution_policy(): return scoring_policy()

@app.get('/api/v1/leaderboard')
def leaderboard(problem_id:str|None=None,limit:int=Query(10,ge=1,le=100),offset:int=Query(0,ge=0)):
    with store.transaction() as tx:
        people=ranked(tx,problem_id)
        return dict(items=[public_person(p) for p in people[offset:offset+limit]],total=len(people),offset=offset,limit=limit,algorithm=scoring_policy()['version'])

@app.get('/api/v1/contributors/{id}/rank')
def contributor_rank(id:str,problem_id:str|None=None):
    with store.transaction() as tx:
        need(tx,'contributors',id);people=ranked(tx,problem_id)
        entry=next((p for p in people if p['id']==id),None)
        return dict(contributor_id=id,problem_id=problem_id,rank=entry['rank'] if entry else None,total_ranked=len(people),
                    score=Scores(tx).total(id,problem_id)['score'],tie_break='Equal scores are ordered by contributor number')

@app.get('/api/v1/contributors/{id}/submissions')
def contributor_submissions(id:str,kind:str|None=None,problem_id:str|None=None,status:str|None=None,q:str='',offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100)):
    with store.transaction() as tx: return owned_submissions(tx,id,kind,problem_id,status,q,offset,limit,PREFIX)

@app.get('/api/v1/contributors/{id}/submissions/{kind}/{object_id}')
def contributor_submission(id:str,kind:str,object_id:str):
    with store.transaction() as tx:
        need(tx,'contributors',id);return owned_submission(tx,id,kind,object_id,PREFIX)

@app.get('/api/v1/contributors/{id}/tasks')
def contributor_tasks(id:str,problem_id:str|None=None,status:str|None=None,offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100)):
    with store.transaction() as tx:
        need(tx,'contributors',id)
        items=[public_task(t) for t in tx.all('tasks') if t.get('contributor_id')==id and (not problem_id or t['problem_id']==problem_id) and (not status or t['status']==status)]
        return page(sorted(items,key=lambda t:t['created_at'],reverse=True),offset,limit)

@app.post('/api/v1/contributors/{id}/submissions/{kind}/{object_id}/withdraw')
def withdraw_submission(id:str,kind:str,object_id:str,body:ContributorAction,request:Request):
    b=body.model_dump();return submit(request,b,lambda tx:change_submission(tx,id,kind,object_id,b,True))

@app.post('/api/v1/contributors/{id}/submissions/{kind}/{object_id}/restore')
def restore_submission(id:str,kind:str,object_id:str,body:ContributorAction,request:Request):
    b=body.model_dump();return submit(request,b,lambda tx:change_submission(tx,id,kind,object_id,b,False))

@app.get('/api/v1/problems/{id}/insight-graph')
def insight_graph(id:str):
    from apps.api.modules.lean import canonical_proof_ids
    with store.transaction() as tx:
        need(tx,'problems',id)
        hidden={x['object_id'] for x in tx.all('submission_states') if x.get('withdrawn')}
        verified=canonical_proof_ids(tx)
        nodes=[dict(id=x['id'],title=x['title'],claim=x['claim'][:400],created_at=x['created_at'],references=x.get('references',[]),claims_problem_solved=x.get('claims_problem_solved',0),lean_passed=x['id'] in verified) for x in tx.all('insights') if x['problem_id']==id and x['id'] not in hidden]
        return dict(items=nodes,total=len(nodes))

@app.get('/api/v1/problems/{id}/overview')
def overview(id:str):
    with store.transaction() as tx:
        p=need(tx,'problems',id)
        dispatch=sync_schedule(tx,id).get(id)
        scores=Scores(tx)
        insights=[x for x in tx.all('insights') if x['problem_id']==id and x['id'] not in scores.withdrawn|scores.superseded]
        from apps.api.modules.lean import canonical_proof_ids
        verified=canonical_proof_ids(tx)
        available=sum(supported_task(t) and t['problem_id']==id and (t['status']=='available' or (t['status']=='claimed' and t.get('expires_at',0)<=time.time())) for t in tx.all('tasks'))
        return dict(problem=describe_problem(p),stats=dict(insights=len(insights),reviews=dispatch.get('reviews',sum(scores.evaluation(i,'insights')['effective_reviews'] for i in insights)) if dispatch else sum(scores.evaluation(i,'insights')['effective_reviews'] for i in insights),verified_lean=dispatch.get('verified_insights',len(scores.verified_insights)) if dispatch else sum(i['id'] in verified for i in insights),available_tasks=available))

@app.get('/health')
def health(): return dict(status='ok',storage='files',release=os.environ.get('APP_RELEASE','development'))

@app.get('/api/v1/agent-docs',response_class=PlainTextResponse)
def docs():
    text=(BASE/'agent-docs/index.md').read_text()
    from apps.api.modules.migration import progress
    with store.transaction() as tx:phase=progress(tx)
    if phase['phase']=='target_backfill':text='CURRENT PHASE: TARGET REGISTRATION ONLY. Claim lean_target tasks; each covers up to 5 existing insights for 8 points per batch after validation. Ordinary research submissions are paused until all required fixed targets are valid. Read the Lean target registration section below.\n\n'+text
    return prefix_agent_paths(text)

@app.get('/api/v1/prompts/{type}',response_class=PlainTextResponse)
def prompt(type:str):
    if type not in ['insight_explore','insight_review','lean_work','research_synthesis','lean_target','lean_target_alignment']: raise HTTPException(404,'Unknown prompt')
    return prefix_agent_paths((BASE/'prompts'/f'{type}.md').read_text())

@app.get('/api/v1/activity')
def activity(limit:int=Query(8,ge=1,le=50)):
    from apps.api.modules.activity import recent_activity
    with store.transaction() as tx:
        return recent_activity(tx,limit)

@app.get('/api/v1/problems')
def problems(include_archived:bool=False):
    with store.transaction() as tx:
        items=[p for p in tx.all('problems') if include_archived or p['status']!='archived']
        return dict(items=[describe_problem(p) for p in sorted(items,key=lambda p:(p.get('display_order',100),p['id']))])
@app.get('/api/v1/problems/{id}')
def problem(id:str):
    with store.transaction() as tx:
        p=need(tx,'problems',id)
        return dict(describe_problem(p),solutions=[s for s in tx.all('solutions') if s['problem_id']==id])

@app.get('/api/v1/tasks')
def tasks(problem_id:str|None=None,type:str|None=None,status:str|None=None,offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100)):
    with store.transaction() as tx:
        sync_schedule(tx,problem_id)
        if type in (None,'insight_explore') and status in (None,'available'): ensure_exploration(tx,problem_id)
        from apps.api.modules.review_batches import busy_targets
        busy=busy_targets(tx)
        items=[]
        for t in tx.all('tasks'):
            from apps.api.modules.migration import active
            if active(tx) and t['type']!='lean_target' and status in (None,'available'):continue
            if not supported_task(t): continue
            if t['type']=='review' and t['status']!='claimed' and t.get('target_id') in busy: continue
            if need(tx,'problems',t['problem_id'])['status']=='archived': continue
            if t['status']=='claimed' and t.get('expires_at',0)<=time.time(): t.update(status='available'); tx.put('tasks',t)
            if (not problem_id or t['problem_id']==problem_id) and (not type or t['type']==type) and (not status or t['status']==status): items.append(public_task(t))
        return page(sorted(items,key=lambda t:t['created_at']),offset,limit)

@app.post('/api/v1/tasks/claim')
def claim(body:Claim):
    b=body.model_dump()
    with store.transaction() as tx:
        validate_identity(tx,b)
        states=sync_schedule(tx,b['problem_id'])
        from apps.api.modules.migration import active,mainline
        if active(tx) and b['type'] not in (None,'lean_target'):mainline(tx)
        from apps.api.modules.completion import priority_targets
        priority=priority_targets(tx)
        if not b['task_id'] and b['type'] in (None,'insight_explore'): ensure_exploration(tx,b['problem_id'])
        options=[need(tx,'tasks',b['task_id'])] if b['task_id'] else sort_tasks(tx,tx.all('tasks'))
        dispatch_cache={}
        if not b['task_id']:
            options=[t for t in options if dispatch_priority(tx,t,states,priority,automatic=not b['type'],cache=dispatch_cache) is not None]
            options.sort(key=lambda t:dispatch_priority(tx,t,states,priority,automatic=not b['type'],cache=dispatch_cache))
            if not active(tx):
                from apps.api.modules.target_maintenance import audit_pool
                from apps.api.modules.migration import pool as registration_pool
                options=[t for t in options if t['type']!='lean_target' or (bool(audit_pool(tx,t['problem_id'],b['contributor_id'],t['id'])) if t.get('mode')=='alignment' else bool(registration_pool(tx,t['problem_id'],t['id'])))]
                if not b['type']:
                    from apps.api.modules.scheduling import fair_order
                    options=fair_order(tx,options,priority)
                elif b['type']=='lean_target':
                    options.sort(key=lambda t:t.get('mode','register')==(tx.get('target_mode_dispatch',t['problem_id']) or {}).get('last_mode'))
        review_attempted=set()
        for t in options:
            if active(tx) and t['type']!='lean_target':continue
            if not supported_task(t): continue
            if b['problem_id'] and t['problem_id']!=b['problem_id']: continue
            if b['type'] and t['type']!=b['type']: continue
            target_kind={'I':'insights','L':'lean_artifacts'}.get((t.get('target_id') or '')[:1])
            if target_kind and withdrawn(tx,target_kind,t['target_id']): continue
            if t['status'] not in ('available','claimed') or need(tx,'problems',t['problem_id'])['status'] in ('formal_proof_verified','archived') or (t['status']=='claimed' and t['expires_at']>time.time()): continue
            if t['type']=='lean_target':
                if t.get('mode')=='alignment':
                    from apps.api.modules.target_maintenance import prepare_audit
                    if not prepare_audit(tx,t,b['contributor_id']):continue
                else:
                    from apps.api.modules.migration import prepare_batch
                    if not prepare_batch(tx,t):continue
            if t['type']=='review':
                if t['problem_id'] in review_attempted: continue
                review_attempted.add(t['problem_id'])
                from apps.api.modules.review_batches import prepare_batch
                room=states.get(t['problem_id'],{}).get('review_room',0)
                if not prepare_batch(tx,t,b['contributor_id'],limit=room,strict_target=bool(b['task_id'])): continue
            if t['type']=='research_synthesis':
                dispatch=states.get(t['problem_id'],{})
                if not dispatch.get('synthesis_refill') or not dispatch.get('synthesis_room') or dispatch.get('synthesis_reserved'): continue
                from apps.api.modules.synthesis import catalog,get_guide
                t['synthesis_insight_ids']=[i['id'] for i in catalog(tx,t['problem_id'])]
                guide=get_guide(tx,t['problem_id'])
                t['synthesis_base_hash']=guide['content_hash']
                t['previous_plan_id']=guide['id']
            if not b['type'] and not b['task_id'] and not active(tx):
                from apps.api.modules.scheduling import record_dispatch
                record_dispatch(tx,t,options)
            if t['type']=='lean_target' and not active(tx):
                tx.put('target_mode_dispatch',dict(id=t['problem_id'],last_mode=t.get('mode','register')))
            token=secrets.token_urlsafe(32)
            if t['type'] in ('insight_explore',): t['research_direction']=assign_direction(need(tx,'problems',t['problem_id']))
            t.update(status='claimed',contributor_id=b['contributor_id'],contributor_name=b['contributor_name'],claim_hash=digest(token),expires_at=time.time()+3600,attempts=t['attempts']+1)
            package=research_package(tx,t); tx.put('tasks',t)
            for target_id in t.get('target_ids',[]) or [t.get('target_id') or t['id']]:
                note_exposure(tx,'task_claims',target_id)
            return dict(task=public_task(t),claim_token=token,research_package=package,dispatch=states.get(t['problem_id']))
        raise HTTPException(409,'No matching available task')

@app.get('/api/v1/tasks/{id}')
def get_task(id:str):
    with store.transaction() as tx: return public_task(need(tx,'tasks',id))

def lease_action(id,body,renew):
    with store.transaction() as tx:
        t=need(tx,'tasks',id)
        if not supported_task(t): raise HTTPException(409,'Unsupported task')
        if t['status']!='claimed' or t['expires_at']<=time.time(): raise HTTPException(409,'Lease expired or inactive')
        if not secrets.compare_digest(t['claim_hash'],digest(body.claim_token)): raise HTTPException(403,'Invalid lease token')
        if renew: t['expires_at']=time.time()+3600
        else: t.update(status='available',claim_hash='',expires_at=0)
        tx.put('tasks',t); return public_task(t)
@app.post('/api/v1/tasks/{id}/renew')
def renew(id:str,body:Lease): return lease_action(id,body,True)
@app.post('/api/v1/tasks/{id}/release')
def release(id:str,body:Lease): return lease_action(id,body,False)


@app.post('/api/v1/insights')
def post_insight(body:Insight,request:Request):
    b=body.model_dump()
    if not b.get('is_prerequisite'): b.pop('is_prerequisite',None)
    def op(tx):
        obj=create_content(tx,'insights',b)
        if b.get('lean_target'):obj['lean_target']=register_target(tx,obj,b['lean_target'])
        tx.put('insights',obj)
        if b.get('lean'):
            if not b.get('lean_target'):raise HTTPException(422,'Inline Lean code requires a registered target; submit the Insight first and use target maintenance')
            payload=dict(b['lean'],insight_id=obj['id'],contributor_id=b['contributor_id'],contributor_name=b['contributor_name'],task_id=None,claim_token=None,supersedes_id=None)
            artifact=create_artifact(tx,payload)
            obj['initial_lean_artifact_id']=artifact['id']; tx.put('insights',obj)
            return dict(obj,lean_submission=artifact)
        return obj
    return submit(request,b,op)

# Explicit paths in OpenAPI, shared implementation internally.
def list_content(kind,problem_id,q,offset,limit,include_withdrawn=False):
    with store.transaction() as tx:
        active={p['id'] for p in tx.all('problems') if p['status']!='archived'}
        items=[x for x in tx.all(kind) if (x['problem_id']==problem_id if problem_id else x['problem_id'] in active) and (not q or q.lower() in json.dumps(x,ensure_ascii=False).lower()) and (include_withdrawn or not withdrawn(tx,kind,x['id']))]
        items.sort(key=lambda x:x['created_at'],reverse=True)
        scores=Scores(tx)
        result=page(items,offset,limit)
        result['items']=[dict(x,lean_target=get_target(tx,x['id']),withdrawal=submission_state(tx,kind,x['id']),evaluation=scores.evaluation(x,kind)) for x in result['items']]
        return result

@app.get('/api/v1/insights')
def insights(problem_id:str|None=None,q:str='',offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100),include_withdrawn:bool=False): return list_content('insights',problem_id,q,offset,limit,include_withdrawn)

@app.get('/api/v1/insights/ranking')
def insight_ranking(problem_id:str|None=None,q:str='',offset:int=Query(0,ge=0),limit:int=Query(10,ge=1,le=100)):
    """Rank active insights by their current individual contribution points."""
    with store.transaction() as tx:
        if problem_id: need(tx,'problems',problem_id)
        active={p['id'] for p in tx.all('problems') if p['status']!='archived'}
        scores=Scores(tx)
        items=[x for x in scores.objects['insights'].values() if (x['problem_id']==problem_id if problem_id else x['problem_id'] in active) and x['id'] not in scores.withdrawn and x['id'] not in scores.superseded]
        items.sort(key=lambda x:(-scores.evaluation(x,'insights')['contribution_points'],x['created_at'],x['id']))
        ranked_items=[dict(id=x['id'],problem_id=x['problem_id'],title=x['title'],contributor_name=x.get('contributor_name',''),seed=x.get('seed',False),rank=rank,evaluation=scores.evaluation(x,'insights')) for rank,x in enumerate(items,1) if not q or q.lower() in json.dumps(x,ensure_ascii=False).lower()]
        return page(ranked_items,offset,limit)

def content(kind,id):
    with store.transaction() as tx:
        obj=need(tx,kind,id)
        obj['withdrawal']=submission_state(tx,kind,id)
        obj['evaluation']=Scores(tx).evaluation(obj,kind)
        obj['replaced_by']=[x['id'] for x in tx.all(kind) if x.get('supersedes_id')==id]
        if kind=='insights':
            obj['lean_target']=get_target(tx,id)
            obj['claims_problem_solved']=obj.get('claims_problem_solved',0)
            obj['completion']=tx.get('proof_candidates',id)
            from apps.api.modules.lean import canonical_proof_ids
            canonical=canonical_proof_ids(tx)
            obj['canonical_proof_id']=canonical.get(id)
            obj['lean_artifacts']=[dict(id=x['id'],verification=need(tx,'verifications',x['verification_id'])) for x in tx.all('lean_artifacts') if x['insight_id']==id and (id not in canonical or x['id']==canonical[id])]
        return obj

@app.get('/api/v1/insights/{id}')
def insight(id:str): return content('insights',id)

def dependencies(kind,id):
    with store.transaction() as tx:
        obj=need(tx,kind,id)
        return dict(references=obj['references'],referenced_by=[x['id'] for k in ('insights',) for x in tx.all(k) if id in x['references']])

@app.get('/api/v1/insights/{id}/lean-target')
def insight_lean_target(id:str):
    with store.transaction() as tx:
        need(tx,'insights',id)
        return get_target(tx,id)

@app.post('/api/v1/insights/{id}/lean-target')
def register_insight_lean_target(id:str,body:LeanTargetRegistration,request:Request):
    b=body.model_dump()
    def op(tx):
        obj=need(tx,'insights',id)
        if obj['contributor_id']!=b['contributor_id']:
            raise HTTPException(403,'Only the insight author may register its Lean target')
        check_supersedes(tx,'insights',b,obj['problem_id'])
        return register_target(tx,obj,b['lean_target'])
    return submit(request,b,op)

@app.post('/api/v1/insights/{id}/lean-target/recheck')
def recheck_insight_lean_target(id:str,body:ContributorAction,request:Request):
    b=body.model_dump()
    def op(tx):
        obj=need(tx,'insights',id)
        if b['contributor_id'] not in (obj['contributor_id'],(get_target(tx,id) or {}).get('registered_by')):
            raise HTTPException(403,'Only the Insight author or target registrant may recheck its Lean target')
        target=need(tx,'insight_targets',id)
        if target['status']!='error':
            raise HTTPException(409,'Only a Lean target with an infrastructure error may be rechecked; invalid statements require a superseding insight')
        target.update(status='queued',requeued_at=time.time(),recheck_reason=b.get('reason',''),last_error=target.get('log',''))
        for field in ('started_at','finished_at','log','exit_code'):
            target.pop(field,None)
        tx.put('insight_targets',target)
        return target
    return submit(request,b,op)

@app.get('/api/v1/insights/{id}/dependencies')
def insight_deps(id:str): return dependencies('insights',id)

def post_review(kind,id,body,request):
    b=body.model_dump()
    def op(tx):
        obj=need(tx,kind,id); check_supersedes(tx,'reviews',b,obj['problem_id'])
        if withdrawn(tx,kind,id):raise HTTPException(409,'This insight is withdrawn; review an active mathematical submission')
        validate_review_alignment(tx,id,b)
        r=tx.new('reviews','E',dict(clean(b),target_id=id,target_kind=kind,problem_id=obj['problem_id']))
        finish_task(tx,b,'review',id,obj['problem_id'],r['id'])
        contribute(tx,r,'review')
        from apps.api.modules.trash import zero_review
        zero_review(tx,r)
        return r
    return submit(request,b,op)

@app.post('/api/v1/insights/{id}/reviews')
def insight_review(id:str,body:Review,request:Request): return post_review('insights',id,body,request)

def get_reviews(kind,id):
    with store.transaction() as tx:
        need(tx,kind,id); return dict(items=[x for x in tx.all('reviews') if x['target_id']==id])

@app.get('/api/v1/insights/{id}/reviews')
def insight_reviews(id:str): return get_reviews('insights',id)

@app.post('/api/v1/reviews/batch')
def review_batch(body:ReviewBatch,request:Request):
    from apps.api.modules.review_batches import submit_batch
    b=body.model_dump()
    return submit(request,b,lambda tx:submit_batch(tx,b))

@app.get('/api/v1/lean/environment')
def lean_environment(): return environment(PREFIX)

@app.get('/api/v1/lean/artifacts')
def list_artifacts(problem_id:str|None=None,insight_id:str|None=None,q:str='',status:str=Query('passed',pattern='^(passed|queued|running|failed|all)$'),offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100)):
    with store.transaction() as tx:
        items=[]
        from apps.api.modules.lean import canonical_proof_ids
        canonical=canonical_proof_ids(tx)
        for a in sorted(tx.all('lean_artifacts'),key=lambda x:x['created_at'],reverse=True):
            if withdrawn(tx,'lean_artifacts',a['id']): continue
            if not problem_id and not insight_id and need(tx,'problems',a['problem_id'])['status']=='archived': continue
            if problem_id and a['problem_id']!=problem_id: continue
            if insight_id and a['insight_id']!=insight_id: continue
            item=artifact_summary(tx,a,PREFIX,canonical)
            if status!='all' and item['status']!=status: continue
            if q and q.casefold() not in ' '.join(str(item[k]) for k in ('id','title','insight_claim','module','theorem','theorem_signature')).casefold(): continue
            items.append(item)
        return page(items,offset,limit)

@app.post('/api/v1/lean/artifacts')
def post_artifact(body:Artifact,request:Request):
    b=body.model_dump(); return submit(request,b,lambda tx:create_artifact(tx,b))
@app.get('/api/v1/lean/artifacts/{id}')
def get_artifact(id:str):
    with store.transaction() as tx:
        a=need(tx,'lean_artifacts',id); return dict(a,withdrawal=submission_state(tx,'lean_artifacts',id),interface=artifact_summary(tx,a,PREFIX),verification=need(tx,'verifications',a['verification_id']))
@app.get('/api/v1/lean/artifacts/{id}/bundle')
def bundle(id:str):
    from apps.api.modules.targets import definition_closure
    with store.transaction() as tx:
        artifacts=closure(tx,need(tx,'lean_artifacts',id));target_sources={}
        for artifact in artifacts:
            if artifact.get('target'):
                for target in definition_closure(tx,artifact['target']):target_sources[target['target_hash']]=target
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        written_targets=set()
        for target in target_sources.values():
            z.writestr(target['target_module'].replace('.','/')+'.lean',target_source(target));written_targets.add(target['target_hash'])
        for a in artifacts:
            imports='\n'.join('import MC.'+i for i in a['dependencies'])
            target=a.get('target')
            if target:
                imports+='\nimport '+target['target_module']
                if target['target_hash'] not in written_targets:
                    z.writestr(target['target_module'].replace('.','/')+'.lean',target_source(target))
                    written_targets.add(target['target_hash'])
            z.writestr(a['file_path'],imports+'\n'+a['source'])
        z.writestr('lean-toolchain','leanprover/lean4:v4.19.0\n')
        z.writestr('lakefile.toml','name = "research"\nversion = "0.1.0"\n[[require]]\nname = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4"\nrev = "'+MATHLIB_REVISION+'"\n[[lean_lib]]\nname = "MC"\n[[lean_lib]]\nname = "MCTargets"\n')
        z.writestr('lake-manifest.json',json.dumps(bundle_manifest(),indent=2))
        z.writestr('manifest.json',json.dumps([dict(id=a['id'],content_hash=a['content_hash'],dependencies=a['dependencies'],theorem=a['theorem'],toolchain=a['toolchain'],policy=a['policy'],library_id=a.get('library_id'),target_hash=(a.get('target') or {}).get('target_hash')) for a in artifacts],indent=2))
        z.writestr('README.md','This archive contains proof source and dependency sources for inspection. Do not install Lean, elan, Lake or Mathlib on the user computer for this task. Submit only new source, the entry theorem and dependency IDs through the API; the server assembles dependencies and performs compilation and verification. Artifact IDs name the modules. Import them and reference their declared theorem names.\n')
    return Response(buf.getvalue(),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{id}.zip"'})
@app.get('/api/v1/lean/verifications/{id}')
def verification(id:str):
    with store.transaction() as tx: return need(tx,'verifications',id)
@app.post('/api/v1/lean/artifacts/{id}/audits')
def post_audit(id:str,body:Audit,request:Request):
    b=body.model_dump()
    def op(tx):
        a=need(tx,'lean_artifacts',id); check_supersedes(tx,'audits',b,a['problem_id'])
        r=tx.new('audits','U',dict(clean(b),artifact_id=id,problem_id=a['problem_id']))
        finish_task(tx,b,'lean_work',id,a['problem_id'],r['id']); contribute(tx,r,'lean_work'); return r
    return submit(request,b,op)
@app.get('/api/v1/lean/artifacts/{id}/audits')
def audits(id:str):
    with store.transaction() as tx:
        need(tx,'lean_artifacts',id); return dict(items=[a for a in tx.all('audits') if a['artifact_id']==id])
@app.post('/api/v1/problems/{id}/solutions')
def post_solution(id:str,body:Solution,request:Request):
    b=body.model_dump()
    def op(tx):
        p=need(tx,'problems',id)
        validate_completion_claim(p,b['claims_problem_solved'])
        a=need(tx,'lean_artifacts',b['artifact_id'])
        if a['problem_id']!=id: raise HTTPException(422,'Artifact belongs to a different problem')
        if not p.get('lean_target') or p.get('target_status')!='reviewed': raise HTTPException(409,'Problem has no reviewed, fixed Lean target yet; completion cannot be certified')
        if not target_proof_matches(a,need(tx,'verifications',a['verification_id']),get_target(tx,a['insight_id'])):
            raise HTTPException(422,'Artifact must prove its registered immutable Lean target before final verification')
        check_supersedes(tx,'solutions',b,id)
        r=tx.new('solutions','S',dict(clean(b),problem_id=id,status='queued',target_hash=digest(final_target(p,b['claims_problem_solved']))))
        contribute(tx,r,'solution'); return r
    return submit(request,b,op)
@app.get('/api/v1/solutions/{id}')
def solution(id:str):
    with store.transaction() as tx: return need(tx,'solutions',id)
@app.get('/api/v1/contributors/{id}/contributions')
def contributions(id:str,offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100)):
    with store.transaction() as tx:
        need(tx,'contributors',id)
        return page(Scores(tx).records(id),offset,limit)
@app.get('/api/v1/contributors/{id}/score')
def score(id:str):
    with store.transaction() as tx:
        need(tx,'contributors',id)
        return Scores(tx).total(id)
@app.get('/api/v1/problems/{id}/recommendations')
def recommendations(id:str,kind:str=Query('insights',pattern='^insights$')):
    with store.transaction() as tx:
        need(tx,'problems',id)
        return recommend(tx,id,PREFIX,kind)


@app.get('/api/v1/problems/{id}/dispatch')
def dispatch_state(id:str):
    with store.transaction() as tx:
        need(tx,'problems',id)
        from apps.api.modules.scheduling import DISPATCH_WEIGHTS
        return dict(policy='weighted-parallel-v2',state=sync_schedule(tx,id).get(id),priority=['complete_proof_lean'],weights=DISPATCH_WEIGHTS,target_flag_threshold=2)

@app.post('/api/v1/research-plans')
def post_research_plan(body:ResearchPlan,request:Request):
    from apps.api.modules.synthesis import submit_plan
    b=body.model_dump()
    return submit(request,b,lambda tx:submit_plan(tx,b))

@app.get('/api/v1/research-plans/{id}')
def research_plan(id:str):
    from apps.api.modules.synthesis import get_guide
    with store.transaction() as tx:
        need(tx,'problems',id)
        return get_guide(tx,id)

@app.get('/api/v1/problems/{id}/research-guidance')
def research_guidance(id:str):
    from apps.api.modules.synthesis import get_guide
    with store.transaction() as tx:
        guide=get_guide(tx,id)
        return dict(latest=guide,current=guide,base_hash=guide['content_hash'],file_url=PREFIX+'/api/v1/problems/'+id+'/research-guidance/file')

@app.post('/api/v1/problems/{id}/research-guidance')
def update_research_guidance(id:str,body:ResearchPlan,request:Request):
    if body.problem_id!=id:raise HTTPException(422,'Problem path and document must agree')
    return post_research_plan(body,request)

@app.get('/api/v1/problems/{id}/research-guidance/file',response_class=PlainTextResponse)
def research_guidance_file(id:str):
    from apps.api.modules.synthesis import get_guide
    from apps.api.modules.guide_format import render_document
    with store.transaction() as tx:guide=get_guide(tx,id)
    return PlainTextResponse(render_document(guide['header'],guide['body_markdown']),media_type='text/markdown',headers={'ETag':'"'+guide['content_hash']+'"','Cache-Control':'no-cache'})

@app.get('/api/v1/research-updates/{id}')
def research_update(id:str):
    with store.transaction() as tx:return need(tx,'research_updates',id)


@app.get('/api/v1/platform-status')
def platform_status():
    from apps.api.modules.migration import progress
    with store.transaction() as tx:return progress(tx)

@app.post('/api/v1/lean-targets/batch')
def post_target_batch(body:LeanTargetBatch,request:Request):
    from apps.api.modules.migration import submit_batch
    data=body.model_dump();return submit(request,data,lambda tx:submit_batch(tx,data))

@app.get('/api/v1/trash')
def trash(problem_id:str|None=None,offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100)):
    with store.transaction() as tx:
        items=[x for x in tx.all('trash') if not problem_id or x['problem_id']==problem_id]
        return page(sorted(items,key=lambda x:x['trashed_at'],reverse=True),offset,limit)


def public_board_message(obj):
    return {k:obj[k] for k in ('id','author_name','body','body_en','pinned','created_at') if k in obj}

@app.get('/api/v1/message-board')
def message_board(offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50)):
    with store.transaction() as tx:
        items=[x for x in tx.all('board_messages') if not x.get('withdrawn')]
        items.sort(key=lambda x:(not x.get('pinned',False),-x['created_at'],x['id']))
        result=page(items,offset,limit);result['items']=[public_board_message(x) for x in result['items']];return result

@app.post('/api/v1/message-board')
def post_board_message(body:BoardMessage,request:Request):
    data=body.model_dump()
    def op(tx):
        recent=[x for x in tx.all('board_messages') if x.get('contributor_id')==data['contributor_id'] and x['created_at']>time.time()-15]
        if recent:raise HTTPException(429,'Please wait 15 seconds before posting another message')
        obj=tx.new('board_messages','M',dict(contributor_id=data['contributor_id'],author_name=data['contributor_name'],body=data['body'],pinned=False,withdrawn=False))
        return public_board_message(obj)
    return submit(request,data,op)

@app.post('/api/v1/message-board/{id}/withdraw')
def withdraw_board_message(id:str,body:ContributorAction,request:Request):
    data=body.model_dump()
    def op(tx):
        obj=need(tx,'board_messages',id)
        if obj.get('contributor_id')!=data['contributor_id'] or obj.get('pinned'):raise HTTPException(403,'Only the matching author may withdraw this message')
        obj.update(withdrawn=True,withdrawn_at=time.time());tx.put('board_messages',obj);return dict(id=id,withdrawn=True)
    return submit(request,data,op)


@app.post('/api/v1/lean-targets/audits/batch')
def post_target_audits(body:TargetAuditBatch,request:Request):
    from apps.api.modules.target_maintenance import submit_audits
    data=body.model_dump();return submit(request,data,lambda tx:submit_audits(tx,data))

@app.post('/api/v1/insights/{id}/lean-target/audits')
def post_target_audit(id:str,body:TargetAudit,request:Request):
    from apps.api.modules.target_maintenance import record_audit
    data=body.model_dump();return submit(request,data,lambda tx:record_audit(tx,data,id))

@app.get('/api/v1/insights/{id}/lean-target/audits')
def target_audits(id:str):
    with store.transaction() as tx:
        need(tx,'insights',id)
        return dict(items=[{k:a[k] for k in ('id','insight_id','target_hash','alignment','rationale','contributor_name','created_at','misaligned_count')} for a in tx.all('target_audits') if a['insight_id']==id])
