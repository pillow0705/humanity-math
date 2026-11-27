"""Private eight-digit credentials, separate from permanent attribution IDs."""
import hashlib,hmac,secrets,time,threading
from fastapi import HTTPException

_attempts={}
_lock=threading.Lock()
def public_person(person):
    return {k:v for k,v in person.items() if k!='number'}

def issue(tx,person,password=None):
    password=password or str(10_000_000+secrets.randbelow(90_000_000))
    salt=secrets.token_hex(16)
    hashed=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    old=tx.get('credentials',person['id']) or {}
    tx.put('credentials',dict(id=person['id'],salt=salt,password_hash=hashed,version=old.get('version',0)+1,changed_at=time.time()))
    return password

def verify(tx,person,password):
    credential=tx.get('credentials',person['id'])
    if not credential:return  # Legacy identities keep their existing contract.
    key=(person['id'],credential['version']);now=time.monotonic()
    with _lock:
        attempts=[t for t in _attempts.get(key,[]) if t>now-60]
        if len(attempts)>=8:raise HTTPException(429,'Too many credential attempts; retry after one minute')
        attempts.append(now);_attempts[key]=attempts
    supplied=str(password or '')
    valid=False
    if len(supplied)==8 and supplied.isascii() and supplied.isdigit():
        hashed=hashlib.scrypt(supplied.encode(),salt=bytes.fromhex(credential['salt']),n=16384,r=8,p=1).hex()
        valid=hmac.compare_digest(hashed,credential['password_hash'])
    if not valid:raise HTTPException(403,'Invalid contributor credential')
    with _lock:_attempts.pop(key,None)
