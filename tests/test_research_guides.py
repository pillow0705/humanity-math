import copy,time
import pytest
from fastapi.testclient import TestClient
from apps.api import main
from apps.api.infrastructure.store import Store,digest
from apps.api.models import GuideHeader
from apps.api.modules.guide_format import render_document


def graph(insight_id='Iknown',number=0):
    return dict(id='route'+str(number),title='Approach '+str(number),approach='A distinct intermediate target '+str(number),goal_node_id='goal',nodes=[dict(id='known',kind='insight',statement='A known conclusion',status='supported',insight_ids=[insight_id]),dict(id='gap',kind='lemma',statement='Missing lemma '+str(number),status='missing',insight_ids=[]),dict(id='goal',kind='goal',statement='True',status='target',insight_ids=[])],steps=[dict(id='finish',requires=['known','gap'],concludes='goal',explanation='These two statements together imply the original goal.')],missing=[dict(node_id='gap',description='Prove the missing lemma '+str(number),next_step='Establish its uniform bound.')])


def header(insight_id='Iknown',count=1):
    return GuideHeader.model_validate(dict(problem_id='P1',graphs=[graph(insight_id,n) for n in range(count)])).model_dump()


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'store',Store(tmp_path));monkeypatch.setattr(main,'REGISTRATION_CREDENTIALS',False)
    with main.store.transaction() as tx:
        tx.put('problems',dict(id='P1',title='Original conjecture',statement='True',status='open',created_at=1))
        tx.put('problems',dict(id='P2',title='Other',statement='True',status='open',created_at=1))
        tx.put('insights',dict(id='Iknown',problem_id='P1',contributor_id='99',title='A result',claim='True',proof='Introduction',proof_status='claimed_complete',references=[],created_at=1))
        for n in range(24):tx.put('insights',dict(id='Iextra'+str(n),problem_id='P1',contributor_id='99',title='Extra '+str(n),claim='True',proof='Introduction',proof_status='claimed_complete',references=[],created_at=n+2))
    with TestClient(main.app) as c:
        c.post('/api/v1/contributors/claim',json=dict(name='Alice'));c.post('/api/v1/contributors/claim',json=dict(name='Bob'));yield c


def claim(c,person='1',name='Alice'):
    r=c.post('/api/v1/tasks/claim',json=dict(contributor_id=person,contributor_name=name,problem_id='P1',type='research_synthesis'))
    assert r.status_code==200,r.text
    return r.json()


def body(task,person='1',name='Alice',**updates):
    result=dict(contributor_id=person,contributor_name=name,problem_id='P1',task_id=task['task']['id'],claim_token=task['claim_token'],base_hash=task['research_package']['research_guidance']['base_hash'],header=header(),body_markdown='Focus on the distinct missing lemma.')
    result.update(updates);return result


def test_one_public_file_overwritten_by_atomic_edits_and_private_lease_hash(client):
    first=claim(client)
    assert first['research_package']['submission']['url']=='/api/v1/problems/P1/research-guidance'
    assert first['research_package']['submission']['schema']['properties']['header']['properties']['graphs']['items']['properties']['nodes']['items']['properties']['status']
    denied=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Bob',problem_id='P1',type='research_synthesis'))
    assert denied.status_code==409
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(first),headers={'Idempotency-Key':'guide-one'})
    assert r.status_code==200,r.text
    result=r.json();assert result['id']=='P1'
    assert client.post('/api/v1/problems/P1/research-guidance',json=body(first),headers={'Idempotency-Key':'guide-one'}).json()==result
    assert client.get('/api/v1/contributors/1/score').json()['score']==15
    with main.store.transaction() as tx:
        for n in range(20):
            i=dict(tx.get('insights','Iknown'),id='Inew'+str(n),created_at=100+n);tx.put('insights',i)
    second=claim(client,'2','Bob')
    assert second['research_package']['research_guidance']['latest']['content_hash']==result['content_hash']
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(second,'2','Bob',body_markdown='A revised concise explanation.'))
    assert r.status_code==200,r.text
    with main.store.transaction() as tx:
        assert len(tx.all('research_guides'))==1 and len(tx.all('research_updates'))==2
    public=client.get('/api/v1/problems/P1/research-guidance/file')
    assert public.status_code==200 and public.text.startswith('---\n{')
    assert 'A revised concise explanation.' in public.text
    assert client.get('/api/v1/contributors/2/score').json()['score']==15


@pytest.mark.parametrize('length',[39999,40000,40001])
def test_complete_graph_header_and_markdown_budget(client,length):
    task=claim(client);h=header()
    text='x'*(length-len(render_document(h,'')))
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(task,header=h,body_markdown=text))
    assert r.status_code==(200 if length<=40000 else 422),r.text
    if length<=40000:assert len(client.get('/api/v1/problems/P1/research-guidance/file').text)==length
    else:
        assert client.get('/api/v1/tasks/'+task['task']['id']).json()['status']=='claimed'
        with main.store.transaction() as tx:assert tx.all('research_updates')==[]


def test_more_than_four_distinct_routes_are_supported(client):
    task=claim(client)
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(task,header=header(count=5)))
    assert r.status_code==200 and len(r.json()['header']['graphs'])==5,r.text


@pytest.mark.parametrize('problem',['cycle','unknown','missing','duplicate','disconnected','wrong_goal'])
def test_graph_structure_rejected_atomically(client,problem):
    task=claim(client);h=header();g=h['graphs'][0]
    if problem=='cycle':g['steps'].append(dict(id='cycle',requires=['goal'],concludes='known',explanation='Circular assumption'))
    if problem=='unknown':g['steps'][0]['requires'].append('absent')
    if problem=='missing':g['missing']=[]
    if problem=='duplicate':
        other=copy.deepcopy(g);other.update(id='renamed',title='Cosmetic renaming',approach='Cosmetic phrasing');h['graphs'].append(other)
    if problem=='disconnected':g['nodes'].append(dict(id='stray',kind='lemma',statement='Unrelated',status='supported',insight_ids=[]))
    if problem=='wrong_goal':g['goal_node_id']='known'
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(task,header=h))
    assert r.status_code==422,r.text
    with main.store.transaction() as tx:assert tx.all('research_updates')==[]


def test_stale_hash_cannot_overwrite_and_goal_is_bound_to_original_problem(client):
    task=claim(client)
    with main.store.transaction() as tx:
        guide=tx.get('research_guides','P1');guide['content_hash']='f'*64;tx.put('research_guides',guide)
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(task))
    assert r.status_code==409
    with main.store.transaction() as tx:
        guide=tx.get('research_guides','P1');guide['content_hash']=task['research_package']['research_guidance']['base_hash'];tx.put('research_guides',guide)
    h=header();h['graphs'][0]['nodes'][-1]['statement']='A weaker replacement'
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(task,header=h))
    assert r.status_code==200 and r.json()['header']['graphs'][0]['nodes'][-1]['statement']=='True'


def test_only_active_same_problem_insights_are_referenced(client):
    with main.store.transaction() as tx:
        tx.put('insights',dict(id='Iother',problem_id='P2',contributor_id='99',title='Other',claim='True',proof='Intro',created_at=1))
    task=claim(client)
    assert client.post('/api/v1/problems/P1/research-guidance',json=body(task,header=header('Iother'))).status_code==422
    with main.store.transaction() as tx:tx.put('submission_states',dict(id=digest(['insights','Iknown']),object_id='Iknown',withdrawn=True))
    assert client.post('/api/v1/problems/P1/research-guidance',json=body(task)).status_code==422


def test_old_routes_are_archived_and_only_three_shared_files_are_created(tmp_path):
    import json
    from deploy.reset_lean import write
    from deploy.reset_research_guides import reset
    data=tmp_path/'data';data.mkdir();backup=tmp_path/'private';backup.mkdir();(backup/'routes-before-reset.tar.gz').write_bytes(b'Private backup')
    for n in range(3):write(data/'problems'/('P'+str(n)+'.json'),dict(id='P'+str(n),status='open'))
    write(data/'research_plans/Dold.json',dict(id='Dold',problem_id='P0',contributor_id='1',contributor_name='Alice',created_at=1,title='Old route',summary='Do not retain old public body',paths=[dict(strategy='Old independent plan')]))
    write(data/'contributions/Cold.json',dict(id='Cold',kind='research_synthesis',object_id='Dold',object_kind='research_plans'))
    assert reset(data,backup)['old_route_files']==1
    result=reset(data,backup,True)
    assert result['shared_files']==3 and not list((data/'research_plans').glob('*.json'))
    assert len(list((data/'research_guides').glob('*.json')))==3
    metadata=json.loads((data/'research_updates/Dold.json').read_text())
    assert metadata['legacy'] and 'paths' not in metadata and 'summary' not in metadata
    assert json.loads((data/'contributions/Cold.json').read_text())['object_kind']=='research_updates'
    assert (backup/'removed_routes/research_plans/Dold.json').exists()


def test_conditional_status_rejected_and_legacy_guide_preserved_as_gap(client):
    task=claim(client);h=header();h['graphs'][0]['nodes'][0]['status']='conditional'
    assert client.post('/api/v1/problems/P1/research-guidance',json=body(task,header=h)).status_code==422
    with main.store.transaction() as tx:
        guide=tx.get('research_guides','P1');guide.update(header=h,body_markdown='Historical argument',content_hash='old-hash')
        tx.put('research_guides',guide)
    current=client.get('/api/v1/problems/P1/research-guidance').json()['current']
    node=current['header']['graphs'][0]['nodes'][0]
    assert node==dict(h['graphs'][0]['nodes'][0],status='missing')
    assert current['body_markdown']=='Historical argument'
    assert current['content_hash']==digest(dict(header=current['header'],body_markdown=current['body_markdown']))
    assert len(current['header']['graphs'][0]['missing'])==2
    again=client.get('/api/v1/problems/P1/research-guidance').json()['current']
    assert again==current
    # An editor holding the old hash cannot overwrite the clarified document.
    assert client.post('/api/v1/problems/P1/research-guidance',json=body(task)).status_code==409


def test_implication_and_missing_premise_remain_distinct(client):
    task=claim(client);h=header();g=h['graphs'][0]
    g['nodes'][0]['statement']='P → Q'
    g['nodes'][1]['statement']='P'
    r=client.post('/api/v1/problems/P1/research-guidance',json=body(task,header=h))
    assert r.status_code==200,r.text
    nodes=r.json()['header']['graphs'][0]['nodes']
    assert nodes[0]['statement']=='P → Q' and nodes[0]['status']=='supported'
    assert nodes[1]['statement']=='P' and nodes[1]['status']=='missing'


def test_roadmap_node_detail_mouse_and_keyboard():
    import pathlib,subprocess
    source=pathlib.Path('apps/web/assets/app.js').read_text()
    fragment=source[source.index('function roadmapSvg('):source.index('function bindRoadmapTabs(')]
    script='''
const assert=require('node:assert/strict');
const window={UI:{lang:'en'}},esc=s=>String(s).replaceAll('<','&lt;'),referenceButtons=ids=>ids.join(','),bindDetailActions=()=>{};
let detail='';const showDialog=(title,html)=>{detail=html;};
'''+fragment+'''
const graph={id:'route',title:'Route',goal_node_id:'goal',nodes:[{id:'known',kind:'insight',statement:'P → Q',status:'supported',insight_ids:['Iknown']},{id:'gap',kind:'lemma',statement:'P',status:'missing',insight_ids:[]},{id:'goal',kind:'goal',statement:'Q',status:'target',insight_ids:[]}],steps:[{requires:['known','gap'],concludes:'goal',explanation:'Apply the implication.'}],missing:[{node_id:'gap',description:'Need P',next_step:'Prove P'}]};
const svg=roadmapSvg(graph);assert(svg.includes('data-roadmap-node="known"'));assert(svg.includes('role="button"'));assert(!svg.includes('Conditional'));
const button={dataset:{roadmapGraph:'route',roadmapNode:'known'}};
bindRoadmapNodes({querySelectorAll:()=>[button]},{current:{header:{graphs:[graph]}}});
button.onclick();assert(detail.includes('P → Q'));assert(detail.includes('Iknown'));assert(detail.includes('Apply the implication.'));
button.dataset.roadmapNode='gap';let prevented=false;button.onkeydown({key:'Enter',preventDefault:()=>{prevented=true;}});assert(prevented);assert(detail.includes('Need P'));assert(detail.includes('Prove P'));
window.UI.lang='zh';bindRoadmapNodes({querySelectorAll:()=>[button]},{current:{header:{graphs:[graph]}}});button.onclick();assert(detail.includes('缺口与下一步'));
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)


def test_route_coverage_excludes_goal_and_highlights_best_route():
    import pathlib,subprocess
    source=pathlib.Path('apps/web/assets/app.js').read_text()
    fragment=source[source.index('function roadmapCoverage('):source.index('function roadmapSvg(')]
    script="""
const assert=require('node:assert/strict');
const window={UI:{lang:'en'}},esc=String;let selected=false,tabbed=false;
const host={hidden:true,querySelector:()=>button={}},tab={dataset:{routeTab:'best'},click:()=>{tabbed=true;}},view={querySelectorAll:()=>[tab]};let button;
const $=id=>id==='roadmap-progress'?host:view,setGraphTab=()=>{selected=true;};
"""+fragment+"""
const graph=(id,statuses)=>({id,title:id,goal_node_id:'goal',nodes:statuses.map((status,i)=>({id:'n'+i,kind:'lemma',status})).concat([{id:'goal',kind:'goal',status:'target'}])});
assert.deepEqual(roadmapCoverage(graph('one',['supported','missing'])),{supported:1,missing:1,total:2,ratio:0.5});
renderRoadmapProgress({current:{header:{graphs:[graph('other',['missing','missing']),graph('best',['supported','missing'])]}}});
assert(!host.hidden);assert(host.innerHTML.includes('best'));assert(host.innerHTML.includes('50%'));assert(host.innerHTML.includes('1/2'));button.onclick();assert(selected&&tabbed);
renderRoadmapProgress({current:{header:{graphs:[]}}});assert(host.hidden);
"""
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
