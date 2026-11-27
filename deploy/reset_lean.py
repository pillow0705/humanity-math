"""Offline operator cleanup. Requires stopped services and a full private backup."""
import argparse,fcntl,hashlib,json,os,re,shutil,time,uuid
from pathlib import Path


def read_all(data,kind):
    return [json.loads(p.read_text()) for p in (data/kind).glob('*.json')]


def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n');os.replace(temporary,path)


def engineering(insight):
    title=insight.get('title','');claim=insight.get('claim','')
    return ((title.startswith('Even-loss proof module ') and claim.startswith('Lean 4 chain segment of the even-loss formalization'))
        or (title.startswith('Even-loss probe:') and 'Diagnostic probe module' in claim)
        or (title.startswith('Internal:') and 'disposable' in title and re.search(r'\b(probe|calibration|crash|verifier)\b',claim,re.I)))


def reset(data,backup,apply=False):
    data=Path(data).resolve();backup=Path(backup).resolve()
    if data==backup or data in backup.parents:raise ValueError('Backups must be outside the active data directory')
    archive=backup/'data-before-reset.tar.gz'
    if not archive.is_file() or archive.stat().st_size==0:raise ValueError('A full data-before-reset.tar.gz backup is required')
    if (backup/'RESET.json').exists():raise ValueError('This reset has already been applied')
    with open(data/'.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if (data/'.pending.json').exists():raise ValueError('Recover the pending transaction before cleanup')
        all_insights=read_all(data,'insights');deleted={x['id'] for x in all_insights if engineering(x)}
        proof_collections=('lean_artifacts','verifications','audits','solutions','proof_candidates','insight_targets','target_batches','insight_target_revisions','idempotency','dispatch_states')
        proof_ids={x['id'] for kind in proof_collections[:8] for x in read_all(data,kind)}
        reviews={x['id'] for x in read_all(data,'reviews') if x.get('target_id') in deleted}
        removed_ids=deleted|proof_ids|reviews
        counts={k:len(read_all(data,k)) for k in proof_collections}
        report=dict(engineering_insights=len(deleted),removed_engineering_ids=sorted(deleted),old_proof_records=counts['lean_artifacts'],old_verifications=counts['verifications'],related_reviews=len(reviews),collection_counts=counts,applied=apply)
        if not apply:return report
        backup.mkdir(parents=True,exist_ok=True);backup.chmod(0o700)
        removed=backup/'removed';removed.mkdir(mode=0o700)
        def move(kind,ident):
            source=data/kind/(ident+'.json')
            if source.exists():
                destination=removed/kind/source.name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.move(source,destination)
        for kind in proof_collections:
            if (data/kind).exists():shutil.move(data/kind,removed/kind)
            (data/kind).mkdir(exist_ok=True)
        if (data/'artifacts').exists():shutil.move(data/'artifacts',removed/'artifacts')
        (data/'artifacts').mkdir(exist_ok=True)
        for ident in deleted:move('insights',ident)
        for ident in reviews:move('reviews',ident)
        for e in read_all(data,'contributions'):
            if e.get('object_id') in removed_ids or e.get('kind') in ('lean_work','lean_target','solution'):move('contributions',e['id'])
        for kind in ('submission_states','management_events','exposures'):
            for e in read_all(data,kind):
                if e.get('object_id') in removed_ids:move(kind,e['id'])
        for i in read_all(data,'insights'):
            refs=[r for r in i.get('references',[]) if r not in deleted]
            changed=refs!=i.get('references',[])
            if changed:
                i['maintenance_removed_engineering_references']=[r for r in i.get('references',[]) if r in deleted];i['references']=refs
            for key in ('initial_lean_artifact_id','lean_artifact_id','canonical_proof_id','lean_target'):
                if key in i:i.pop(key);changed=True
            if changed:write(data/'insights'/(i['id']+'.json'),i)
        for plan in read_all(data,'research_plans'):
            plan['source_insight_ids']=[i for i in plan.get('source_insight_ids',[]) if i not in deleted]
            for path in plan.get('paths',[]):path['insight_ids']=[i for i in path.get('insight_ids',[]) if i not in deleted]
            plan['paths']=[p for p in plan.get('paths',[]) if p['insight_ids']]
            write(data/'research_plans'/(plan['id']+'.json'),plan)
        for t in read_all(data,'tasks'):
            if t.get('target_id') in removed_ids or any(i in removed_ids for i in t.get('target_ids',[])):move('tasks',t['id']);continue
            if t['status'] in ('available','claimed'):
                t.update(status='closed',closed_reason='Fixed Lean target migration reset',expires_at=0,claim_hash='');write(data/'tasks'/(t['id']+'.json'),t)
        active={p['id'] for p in read_all(data,'problems') if p['status']=='open'}
        insights=read_all(data,'insights');hidden={s['object_id'] for s in read_all(data,'submission_states') if s.get('withdrawn')}
        superseded={i['supersedes_id'] for i in insights if i.get('supersedes_id')}
        ids=sorted(i['id'] for i in insights if i['problem_id'] in active and i['id'] not in hidden|superseded)
        write(data/'platform_states/lean_target_migration.json',dict(id='lean_target_migration',phase='target_backfill',required_insight_ids=ids,batch_size=5,batch_points=8,started_at=time.time(),backup=str(archive)))
        for pid in active:
            ident='T'+uuid.uuid4().hex
            write(data/'tasks'/(ident+'.json'),dict(id=ident,problem_id=pid,type='lean_target',target_id=None,mode=None,status='available',attempts=0,created_at=time.time()))
        report.update(required_insights=len(ids),finished_at=time.time(),backup_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        write(backup/'RESET.json',report)
        # Preserve the runtime account's access after root-owned offline maintenance.
        uid,gid=data.stat().st_uid,data.stat().st_gid
        for path in data.rglob('*'):
            if not path.is_symlink():os.chown(path,uid,gid)
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--backup',required=True);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    print(json.dumps(reset(args.data,args.backup,args.apply),ensure_ascii=False,indent=2))
