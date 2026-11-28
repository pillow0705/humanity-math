import time
import pytest
from fastapi.testclient import TestClient
from review_fixtures import ReviewLeaseFixture
from apps.api import main
from apps.api.infrastructure.store import Store
from apps.api.modules.core import contribute
from apps.api.modules.scoring import Scores

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'store',Store(tmp_path));monkeypatch.setattr(main,'REGISTRATION_CREDENTIALS',False)
    with main.store.transaction() as tx:tx.put('problems',dict(id='P1',title='Test',statement='True',status='open',created_at=1))
    class LeasedClient(ReviewLeaseFixture,TestClient):pass
    with LeasedClient(main.app) as c:
        c.post('/api/v1/contributors/claim',json=dict(name='Alice'));c.post('/api/v1/contributors/claim',json=dict(name='Bob'));yield c

def insight(c,title='Result',refs=[]):
    r=c.post('/api/v1/insights',json=dict(contributor_id='1',contributor_name='Alice',problem_id='P1',title=title,claim='True',proof='Introduction',proof_status='claimed_complete',references=refs,lean_target=dict(expression='True')))
    assert r.status_code==200,r.text
    return r.json()

@pytest.mark.parametrize('score,expected',[(1,8.75),(2,8.847656),(3,9.53125),(4,11.386719),(5,15)])
def test_cubic_review_credit(client,score,expected):
    i=insight(client)
    r=client.post('/api/v1/insights/'+i['id']+'/reviews',json=dict(contributor_id='2',contributor_name='Bob',score=score,verdict='support',rationale='Mathematical evaluation',target_alignment='aligned'))
    assert r.status_code==200,r.text
    assert client.get('/api/v1/insights/'+i['id']).json()['evaluation']['review_adjusted_points']==expected

def test_zero_trashes_removes_score_recommendations_and_citation_credit(client):
    base=insight(client);junk=insight(client,'Not mathematics',[base['id']])
    url='/api/v1/insights/'+junk['id']
    assert client.get('/api/v1/insights/'+base['id']).json()['evaluation']['citation_points']==0
    r=client.post(url+'/reviews',json=dict(contributor_id='2',contributor_name='Bob',score=0,verdict='refute',rationale='This is a compiler probe, not a mathematical result.'))
    assert r.status_code==200,r.text
    assert not client.get(url).json()['withdrawal']
    client.post('/api/v1/contributors/claim',json=dict(name='Charlie'))
    assert client.post(url+'/reviews',json=dict(contributor_id='3',contributor_name='Charlie',score=2,verdict='refute',rationale='Serious mathematical error')).status_code==200
    assert client.get(url).json()['evaluation']['contribution_points']==0
    assert client.get('/api/v1/insights/'+base['id']).json()['evaluation']['citation_points']==0
    assert junk['id'] not in [x['id'] for x in client.get('/api/v1/insights').json()['items']]
    assert junk['id'] not in [x['id'] for x in client.get('/api/v1/insights/ranking').json()['items']]
    trash=client.get('/api/v1/trash').json()['items'][0]
    assert trash['insight_id']==junk['id'] and trash['reviewer_name']=='Charlie'
    assert client.post('/api/v1/contributors/1/submissions/insights/'+junk['id']+'/restore',json=dict(contributor_id='1',contributor_name='Alice')).status_code==409

def test_zero_self_review_is_rejected_atomically(client):
    i=insight(client)
    r=client.post('/api/v1/insights/'+i['id']+'/reviews',json=dict(contributor_id='1',contributor_name='Alice',score=0,verdict='refute',rationale='Self'))
    assert r.status_code==422
    with main.store.transaction() as tx:assert tx.all('reviews')==[] and tx.all('trash')==[]

def test_board_registered_posts_pinned_announcements_and_withdrawal(client):
    with main.store.transaction() as tx:tx.put('board_messages',dict(id='Mnotice',author_name='eggtea',body='Maintenance',body_en='Maintenance',pinned=True,created_at=0))
    body=dict(contributor_id='1',contributor_name='Alice',body='你好，期待新进展！')
    post=client.post('/api/v1/message-board',json=body)
    assert post.status_code==200 and 'contributor_id' not in post.json()
    assert client.get('/api/v1/message-board').json()['items'][0]['id']=='Mnotice'
    assert client.post('/api/v1/message-board',json=dict(body,pinned=True)).status_code==422
    ident=post.json()['id']
    assert client.post('/api/v1/message-board/'+ident+'/withdraw',json=dict(contributor_id='2',contributor_name='Bob')).status_code==403
    assert client.post('/api/v1/message-board/'+ident+'/withdraw',json=dict(contributor_id='1',contributor_name='Alice')).status_code==200
    assert client.get('/api/v1/message-board').json()['total']==1
    with main.store.transaction() as tx:assert tx.all('contributions')==[]


def test_convex_mapping_is_applied_after_averaging_reviews(client):
    i=insight(client)
    client.post('/api/v1/contributors/claim',json=dict(name='Charlie'))
    for person,name,score in [('2','Bob',2),('3','Charlie',4)]:
        assert client.post('/api/v1/insights/'+i['id']+'/reviews',json=dict(contributor_id=person,contributor_name=name,score=score,verdict='support',rationale='Relative mathematical quality',target_alignment='aligned')).status_code==200
    assert client.get('/api/v1/insights/'+i['id']).json()['evaluation']['review_adjusted_points']==9.25


def test_zero_in_review_batch_trashes_once_and_preserves_complete_batch_credit(client):
    a=insight(client);b=insight(client,'Another result')
    claim=client.post('/api/v1/tasks/claim',json=dict(contributor_id='2',contributor_name='Bob',type='review')).json()
    assert set(claim['task']['target_ids'])=={a['id'],b['id']}
    body=dict(contributor_id='2',contributor_name='Bob',task_id=claim['task']['id'],claim_token=claim['claim_token'],reviews=[dict(insight_id=ident,score=0 if ident==a['id'] else 4,verdict='refute' if ident==a['id'] else 'support',rationale='Not mathematical' if ident==a['id'] else 'A valid lemma',target_alignment='not_applicable' if ident==a['id'] else 'aligned') for ident in claim['task']['target_ids']])
    response=client.post('/api/v1/reviews/batch',json=body)
    assert response.status_code==200,response.text
    assert client.get('/api/v1/tasks/'+claim['task']['id']).json()['status']=='submitted'
    assert client.get('/api/v1/contributors/2/score').json()['score']==6
    assert client.get('/api/v1/trash').json()['total']==0
