"""Sequential public identities. This is attribution, not authentication."""
import time
from fastapi import HTTPException

def claim_name(tx, name):
    for person in tx.all('contributors'):
        if person['name'].casefold()==name.casefold():
            raise HTTPException(409,'This name is already claimed; use the existing name and number, or choose another name')
    counter=tx.get('counters','contributors') or {'id':'contributors','value':0}
    number=counter['value']+1
    counter['value']=number
    person=dict(id=str(number),contributor_id=str(number),number=number,name=name,created_at=time.time())
    tx.put('contributors',person); tx.put('counters',counter)
    return person

def validate_identity(tx, body):
    person=tx.get('contributors',body['contributor_id'])
    if not person or person['name']!=body.get('contributor_name'):
        raise HTTPException(422,'Contributor name and number do not match a claimed identity; copy the exact registered name')
    from apps.api.modules.credentials import verify
    verify(tx,person,body.pop('contributor_password',None))
    return person
