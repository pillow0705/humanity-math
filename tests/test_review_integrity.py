import time
import pytest
from fastapi.testclient import TestClient
from test_api import client,insight
from apps.api import main
from apps.api.infrastructure.store import digest


def result(c,**values):
    r=c.post('/api/v1/insights',json=dict(insight(),**values));assert r.status_code==200,r.text
    return r.json()


def review(c,ident,person='2',name='Aster',score=2):
    return c.post('/api/v1/insights/'+ident+'/reviews',json=dict(contributor_id=person,contributor_name=name,verdict='concern',score=score,rationale='A specific mathematical issue.'))


def test_same_author_citations_do_not_score_but_preserve_dependencies(client):
    base=result(client);own=result(client,title='Own extension',references=[base['id']])
    external=result(client,title='Independent use',contributor_id='2',contributor_name='Aster',references=[base['id'],base['id']])
    assert own['references']==[base['id']]
    detail=client.get('/api/v1/insights/'+base['id']).json()
    assert detail['evaluation']['citation_points']==1
    assert set(client.get('/api/v1/insights/'+base['id']+'/dependencies').json()['referenced_by'])=={own['id'],external['id']}


@pytest.mark.parametrize('score',[0,2,5])
def test_unleased_review_rejected_without_records_or_credit(client,score):
    i=result(client)
    r=TestClient.post(client,'/api/v1/insights/'+i['id']+'/reviews',json=dict(contributor_id='2',contributor_name='Aster',verdict='concern',rationale='Not a leased review',score=score))
    assert r.status_code==422
    with main.store.transaction() as tx:
        assert tx.all('reviews')==[] and tx.all('trash')==[]
    assert client.get('/api/v1/contributors/2/score').json()['score']==0


def test_two_distinct_latest_low_ratings_trash_not_repeated_or_self_ratings(client):
    i=result(client)
    assert review(client,i['id']).status_code==200
    assert review(client,i['id'],score=1).status_code==200
    x=client.get('/api/v1/insights/'+i['id']).json()
    assert x['evaluation']['low_review_count']==1 and not x['withdrawal']
    assert review(client,i['id'],'1','Alice',0).status_code==422
    assert review(client,i['id'],'3','Bob',2).status_code==200
    x=client.get('/api/v1/insights/'+i['id']).json()
    assert x['withdrawal']['withdrawn'] and x['evaluation']['contribution_points']==0
    trash=client.get('/api/v1/trash').json()['items'][0]
    assert trash['low_review_count']==2 and len(trash['review_ids'])==2


def test_revised_rating_supersedes_previous_low_rating(client):
    i=result(client)
    for score in [2,4]:assert review(client,i['id'],score=score).status_code==200
    assert review(client,i['id'],'3','Bob',2).status_code==200
    x=client.get('/api/v1/insights/'+i['id']).json()
    assert not x['withdrawal'] and x['evaluation']['low_review_count']==1


def test_single_review_discount_and_confidence_increase(client):
    i=result(client)
    for person,name,points in [('2','Aster',15),('3','Bob',18),('4','Beta',20)]:
        assert review(client,i['id'],person,name,5).status_code==200
        ev=client.get('/api/v1/insights/'+i['id']).json()['evaluation']
        assert ev['review_adjusted_points']==points
    # Updating the same review does not manufacture additional confidence or points.
    assert review(client,i['id'],score=5).status_code==200
    assert client.get('/api/v1/insights/'+i['id']).json()['evaluation']['effective_reviews']==3


def test_no_new_revision_forks_or_content_mutation(client):
    old=result(client)
    for title in ['Correction A','Correction B']:
        r=client.post('/api/v1/insights',json=dict(insight(),supersedes_id=old['id'],title=title))
        assert r.status_code==422
    current=client.get('/api/v1/insights/'+old['id']).json()
    assert current['claim']==old['claim'] and current['replaced_by']==[]
    assert client.get('/api/v1/insights').json()['total']==1
