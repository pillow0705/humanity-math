"""Seed explicit compatibility leases and pre-existing revision history for scenarios.

New protocol rejection tests call TestClient.post directly to bypass this helper.
"""
import time
from apps.api import main
from apps.api.infrastructure.store import digest

class ReviewLeaseFixture:
    def post(self,url,*args,**kwargs):
        body=kwargs.get('json')
        if isinstance(body,dict) and str(url).startswith('/api/v1/insights/') and str(url).endswith('/reviews') and not body.get('task_id'):
            body=dict(body)
            with main.store.transaction() as tx:
                item=tx.get('insights',str(url).split('/')[-2])
                archived=not item or tx.get('problems',item['problem_id'])['status']=='archived'
            if archived:return super().post(url,*args,**kwargs)
            with main.store.transaction() as tx:
                t=tx.new('tasks','T',dict(type='review',status='claimed',problem_id=tx.get('insights',str(url).split('/')[-2])['problem_id'],target_id=str(url).split('/')[-2],contributor_id=body.get('contributor_id'),expires_at=time.time()+3600,claim_hash=digest('fixture-token'),created_at=time.time()))
            body.update(task_id=t['id'],claim_token='fixture-token');kwargs['json']=body
        return super().post(url,*args,**kwargs)

def legacy_revision(client,body):
    """Create a synthetic historical revision, never reopen the production API."""
    old_id=body['supersedes_id'];body=dict(body);body.pop('supersedes_id')
    response=client.post('/api/v1/insights',json=body)
    assert response.status_code==200,response.text
    ident=response.json()['id']
    with main.store.transaction() as tx:
        item=tx.get('insights',ident);old=tx.get('insights',old_id)
        item.update(supersedes_id=old_id,contribution_group_ids=old.get('contribution_group_ids',[]));tx.put('insights',item)
    return client.get('/api/v1/insights/'+ident)
