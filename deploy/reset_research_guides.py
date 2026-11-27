"""Offline replacement of old standalone route files with one shared guide per problem."""
import argparse,fcntl,hashlib,json,os,shutil,time
from pathlib import Path

def read_all(data,kind):return [json.loads(p.read_text()) for p in (data/kind).glob('*.json')]
def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n');os.replace(temp,path)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def reset(data,backup,apply=False):
    data=Path(data).resolve();backup=Path(backup).resolve()
    if data==backup or data in backup.parents:raise ValueError('Use a private backup outside active data')
    if not (backup/'routes-before-reset.tar.gz').is_file():raise ValueError('A routes-before-reset.tar.gz backup is required')
    if (backup/'ROUTES_RESET.json').exists():raise ValueError('The route reset is already complete')
    with open(data/'.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if (data/'.pending.json').exists():raise ValueError('Recover the pending transaction before reset')
        old=read_all(data,'research_plans')
        report=dict(old_route_files=len(old),applied=apply)
        if not apply:return report
        destination=backup/'removed_routes';destination.mkdir(mode=0o700,parents=True,exist_ok=True)
        if (data/'research_plans').exists():shutil.move(data/'research_plans',destination/'research_plans')
        (data/'research_plans').mkdir(exist_ok=True)
        # Retain attribution/earned-work bookkeeping, not old route documents.
        for plan in old:
            metadata={k:plan[k] for k in ('id','problem_id','contributor_id','contributor_name','created_at','title','supersedes_id') if k in plan}
            metadata.update(legacy=True,status='legacy',retired_at=time.time())
            write(data/'research_updates'/(plan['id']+'.json'),metadata)
        for event in read_all(data,'contributions'):
            if event.get('kind')=='research_synthesis':
                event['object_kind']='research_updates';write(data/'contributions'/(event['id']+'.json'),event)
        for task in read_all(data,'tasks'):
            if task['type']=='research_synthesis' and task['status'] in ('available','claimed'):
                task.update(status='closed',claim_hash='',expires_at=0,closed_reason='Shared proof-graph guide migration');write(data/'tasks'/(task['id']+'.json'),task)
        active=[p for p in read_all(data,'problems') if p['status']=='open']
        for problem in active:
            header=dict(format='humanity-proof-graphs-v1',problem_id=problem['id'],graphs=[]);body='No proof roadmap has been submitted yet.'
            obj=dict(id=problem['id'],problem_id=problem['id'],header=header,body_markdown=body,content_hash=digest(dict(header=header,body_markdown=body)),updated_at=None,updated_by=None)
            write(data/'research_guides'/(problem['id']+'.json'),obj)
        report.update(shared_files=len(active),finished_at=time.time(),legacy_update_metadata=len(old))
        write(backup/'ROUTES_RESET.json',report)
        uid,gid=data.stat().st_uid,data.stat().st_gid
        for folder in ('research_plans','research_updates','research_guides','contributions','tasks'):
            for p in [data/folder,*list((data/folder).glob('*.json'))]:
                if p.exists():os.chown(p,uid,gid)
        return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--backup',required=True);p.add_argument('--apply',action='store_true');args=p.parse_args()
    print(json.dumps(reset(args.data,args.backup,args.apply),indent=2))
