"""One public proof-graph guide per problem, edited under exclusive task leases."""
import copy,secrets,time
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need,clean,finish_task,contribute
from apps.api.modules.scoring import Scores
from apps.api.modules.guide_format import render_document,MAX_DOCUMENT_CHARS


def active_plans(tx,pid,scores=None):
    """Count accepted edits for scheduling, not the number of shared files."""
    scores=scores or Scores(tx)
    return sorted([x for x in tx.all('research_updates') if x['problem_id']==pid and not x.get('legacy') and x['id'] not in scores.withdrawn|scores.superseded],key=lambda x:(x['created_at'],x['id']))


def catalog(tx,pid,prefix=''):
    scores=Scores(tx)
    return [dict(id=x['id'],title=x['title'],claim=x['claim'][:1600],references=x.get('references',[]),proof_status=x['proof_status'],evaluation=scores.evaluation(x,'insights'),lean_passed=x['id'] in scores.verified_insights,detail_url=prefix+'/api/v1/insights/'+x['id']) for x in sorted(scores.objects['insights'].values(),key=lambda x:x['created_at']) if x['problem_id']==pid and x['id'] not in scores.withdrawn|scores.superseded]


def get_guide(tx,pid):
    need(tx,'problems',pid)
    current=tx.get('research_guides',pid)
    if current:return current
    header=dict(format='humanity-proof-graphs-v1',problem_id=pid,graphs=[])
    body='No proof roadmap has been submitted yet.'
    current=dict(id=pid,problem_id=pid,header=header,body_markdown=body,content_hash=digest(dict(header=header,body_markdown=body)),updated_at=None,updated_by=None)
    tx.put('research_guides',current)
    return current


def validate_graphs(tx,pid,header):
    from apps.api.modules.research import describe_problem
    statement=describe_problem(need(tx,'problems',pid))['statement']
    scores=Scores(tx);seen_graph_ids=set();signatures=set()
    for graph in header['graphs']:
        if graph['id'] in seen_graph_ids:raise HTTPException(422,'Graph IDs must be unique')
        seen_graph_ids.add(graph['id'])
        nodes={n['id']:n for n in graph['nodes']}
        if len(nodes)!=len(graph['nodes']):raise HTTPException(422,'Node IDs must be unique within a graph')
        goal=nodes.get(graph['goal_node_id'])
        if not goal or goal['kind']!='goal' or goal['status']!='target':raise HTTPException(422,'goal_node_id must identify the original problem target')
        if sum(n['kind']=='goal' for n in nodes.values())!=1:raise HTTPException(422,'Each proof graph must have exactly one original-problem goal')
        goal['statement']=statement
        goal['insight_ids']=[]
        for node in nodes.values():
            if not node['statement']:raise HTTPException(422,'Every mathematical node needs an exact statement')
            if node['kind']!='goal' and node['status']=='target':raise HTTPException(422,'Only the original goal uses target status')
            if node['kind']=='insight' and not node['insight_ids']:raise HTTPException(422,'An insight node must cite at least one Insight')
            if len(set(node['insight_ids']))!=len(node['insight_ids']):raise HTTPException(422,'Duplicate Insight references in a node')
            for ident in node['insight_ids']:
                insight=need(tx,'insights',ident)
                if insight['problem_id']!=pid or ident in scores.withdrawn|scores.superseded:raise HTTPException(422,'Graph references must identify active Insights of this problem')
        parents={ident:set() for ident in nodes};step_ids=set()
        for step in graph['steps']:
            if step['id'] in step_ids:raise HTTPException(422,'Step IDs must be unique within a graph')
            step_ids.add(step['id'])
            if step['concludes'] not in nodes or len(set(step['requires']))!=len(step['requires']) or any(i not in nodes for i in step['requires']):raise HTTPException(422,'Every proof-step endpoint must identify a node, without duplicate premises')
            if step['concludes'] in step['requires']:raise HTTPException(422,'A proof step cannot assume its own conclusion')
            parents[step['concludes']].update(step['requires'])
        visiting=set();visited=set()
        def visit(ident):
            if ident in visiting:raise HTTPException(422,'Proof graphs must be acyclic')
            if ident in visited:return
            visiting.add(ident)
            for premise in parents[ident]:visit(premise)
            visiting.remove(ident);visited.add(ident)
        visit(graph['goal_node_id'])
        if visited!=set(nodes):raise HTTPException(422,'Every node must contribute to the original goal')
        missing={n['id'] for n in nodes.values() if n['status']=='missing'}
        listed=[x['node_id'] for x in graph['missing']]
        if len(set(listed))!=len(listed) or set(listed)!=missing:raise HTTPException(422,'missing must describe every missing node exactly once')
        # Ignore cosmetic graph/node identifiers when catching duplicate routes.
        labels={i:digest(dict(kind=n['kind'],statement=n['statement'],status=n['status'],insight_ids=sorted(n['insight_ids']))) for i,n in nodes.items()}
        signature=digest(dict(nodes=sorted(labels.values()),steps=sorted((tuple(sorted(labels[i] for i in s['requires'])),labels[s['concludes']]) for s in graph['steps'])))
        if signature in signatures:raise HTTPException(422,'Duplicate proof routes: merge graphs that differ only in labels or wording')
        signatures.add(signature)
    return header


def submit_plan(tx,body):
    pid=body['problem_id'];problem=need(tx,'problems',pid)
    if problem['status']!='open':raise HTTPException(409,'This problem is not open for roadmap updates')
    t=need(tx,'tasks',body['task_id'])
    if t['type']!='research_synthesis' or t['status']!='claimed' or t.get('expires_at',0)<=time.time():raise HTTPException(409,'Claim an active research_synthesis task before editing the shared guide')
    if t.get('contributor_id')!=body['contributor_id'] or not secrets.compare_digest(t.get('claim_hash',''),digest(body['claim_token'])):raise HTTPException(403,'Invalid research-guide task lease')
    if t['problem_id']!=pid:raise HTTPException(422,'The task belongs to a different problem')
    current=get_guide(tx,pid)
    if body['base_hash']!=t.get('synthesis_base_hash') or body['base_hash']!=current['content_hash']:raise HTTPException(409,'The shared guide changed after this task was claimed. Reload it and claim a fresh editing lease.')
    header=validate_graphs(tx,pid,copy.deepcopy(body['header']))
    markdown=body['body_markdown']
    document=render_document(header,markdown)
    if len(document)>MAX_DOCUMENT_CHARS:raise HTTPException(422,'The complete research guide must not exceed 40000 Unicode characters after binding the original goal')
    fingerprint=digest(dict(header=header,body_markdown=markdown))
    if fingerprint==current['content_hash']:raise HTTPException(409,'No changes to the shared research guide')
    update=tx.new('research_updates','D',dict(contributor_id=body['contributor_id'],contributor_name=body['contributor_name'],problem_id=pid,task_id=t['id'],title='Shared proof-graph guide update',previous_hash=current['content_hash'],content_hash=fingerprint,graph_count=len(header['graphs']),document_characters=len(document),status='submitted',legacy=False))
    guide=dict(id=pid,problem_id=pid,header=header,body_markdown=markdown,content_hash=fingerprint,updated_at=time.time(),updated_by=body['contributor_name'],update_id=update['id'])
    tx.put('research_guides',guide)
    finish_task(tx,body,'research_synthesis',None,pid,update['id']);contribute(tx,update,'research_synthesis')
    return guide
