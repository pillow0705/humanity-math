"""Single worker, file queue and sandboxed incremental Lean verification."""
import fcntl, hashlib, json, os, re, shutil, subprocess, tempfile, time, traceback, uuid
from pathlib import Path
from functools import lru_cache
from apps.api.lean_config import LEAN_VERSION, MATHLIB_VERSION
from apps.api.infrastructure.store import Store, atomic, digest
from apps.api.modules.lean import ALLOWED_AXIOMS, POLICY, closure, MATHLIB_MANIFEST, MATHLIB_REVISION, LIBRARY_ID, TIMEOUT, MEMORY_MB, TOOLCHAIN
from apps.api.modules.core import need, task
from apps.api.modules.completion import sync_candidates, priority_targets, close_problem
from apps.api.modules.lifecycle import withdrawn
from apps.api.modules.research import final_target
from apps.api.modules.targets import target_source, target_proof_matches

ROOT=Path(__file__).resolve().parents[2]
STORE=Store()
TOOLS=Path(os.environ.get('LEAN_HOME',ROOT / 'tools' / f'lean-{LEAN_VERSION}-linux'))
IMAGE=os.environ.get('LEAN_IMAGE',f'math-collab-lean:{LEAN_VERSION}')
CACHE=STORE.root/'artifacts/cache'
MATHLIB=Path(os.environ.get('MATHLIB_HOME',ROOT / 'tools' / f'mathlib-{MATHLIB_VERSION}'))

@lru_cache(maxsize=1)
def library_paths():
    version = subprocess.check_output([str(TOOLS / 'bin/lean'), '--version'], text=True)
    if not version.startswith(f'Lean (version {LEAN_VERSION},'):
        raise RuntimeError('Mounted Lean executable does not match the pinned toolchain')
    if (MATHLIB/'lean-toolchain').read_text().strip()!=TOOLCHAIN: raise RuntimeError('Mathlib toolchain mismatch')
    if json.loads((MATHLIB/'lake-manifest.json').read_text())!=MATHLIB_MANIFEST: raise RuntimeError('Mathlib dependency lock mismatch')
    receipt=json.loads((MATHLIB/'platform-install.json').read_text())
    if receipt.get('revision')!=MATHLIB_REVISION or receipt.get('library_id')!=LIBRARY_ID: raise RuntimeError('Mathlib installation identity mismatch')
    if not (MATHLIB/'.lake/build/lib/lean/Mathlib.olean').is_file(): raise RuntimeError('Mathlib compiled cache missing')
    paths=['.lake/build/lib/lean']+['.lake/packages/'+p['name']+'/.lake/build/lib/lean' for p in MATHLIB_MANIFEST['packages']]
    return ':'.join(['/work']+['/opt/mathlib/'+p for p in paths if (MATHLIB/p).is_dir()])

def docker(args):
    return subprocess.run(['docker',*args],capture_output=True,text=True,timeout=30)

def sandbox(work,command,writable=False):
    name='mc-'+uuid.uuid4().hex
    # Pin the concrete image ID; tags alone are not verification identities.
    image=docker(['image','inspect','--format','{{.Id}}',IMAGE])
    if image.returncode: raise RuntimeError('Lean sandbox image is unavailable: '+image.stderr[-1000:])
    args=['docker','run','--rm','--name',name,'--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128',f'--memory={MEMORY_MB}m',f'--memory-swap={MEMORY_MB+1024}m','--cpus=2','--user=1000:1000','--ulimit','fsize=131072:131072','--tmpfs','/tmp:rw,noexec,nosuid,size=64m','-v',str(TOOLS)+':/opt/lean:ro','-v',str(MATHLIB)+':/opt/mathlib:ro','-v',str(work)+':/work'+('' if writable else ':ro'),'-w','/work','-e','LEAN_PATH='+library_paths(),'-e','HOME=/tmp',image.stdout.strip(),*command]
    logpath=work/'run.log'
    try:
        # File output avoids unbounded RAM from a malicious diagnostic stream.
        with open(logpath,'w') as log:
            proc=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
            try: code=proc.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                docker(['kill',name]); proc.kill(); proc.wait(); return 124,'Verification timeout',image.stdout.strip()
        text=logpath.read_text(errors='replace')
        if len(text)>200000: return 1,'Excessive diagnostic output',image.stdout.strip()
        return code,text,image.stdout.strip()
    finally: docker(['rm','-f',name])

def cache_files(a): return CACHE/a['content_hash']
def file_hash(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def target_cache_files(target): return CACHE/'targets'/target['target_hash']

def prepare_target(work,target,prepared=None,visiting=None):
    prepared=set() if prepared is None else prepared
    visiting=set() if visiting is None else visiting
    ident=target['target_hash']
    if ident in prepared:return
    if ident in visiting:raise RuntimeError('Cyclic cached target-definition dependency')
    visiting.add(ident)
    cached=target_cache_files(target)
    meta=json.loads((cached/'manifest.json').read_text())
    if (meta.get('target_hash')!=ident or meta.get('policy')!=POLICY
            or meta.get('toolchain')!=TOOLCHAIN or meta.get('library_id')!=LIBRARY_ID):
        raise RuntimeError('Fixed target cache identity mismatch')
    if file_hash(cached/'module.olean')!=meta['olean_hash']:raise RuntimeError('Fixed target cache corruption')
    current_image=docker(['image','inspect','--format','{{.Id}}',IMAGE]).stdout.strip()
    if current_image!=meta['image']:raise RuntimeError('Fixed target was checked in a different sandbox image')
    dependencies=meta.get('insight_dependency_snapshot',[])
    if 'insight_dependency_snapshot' in target and target['insight_dependency_snapshot']!=dependencies:raise RuntimeError('Fixed target dependency manifest mismatch')
    for dependency in dependencies:prepare_target(work,dependency,prepared,visiting)
    destination=work/(target['target_module'].replace('.','/')+'.olean')
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(cached/'module.olean',destination);os.chmod(destination,0o444)
    visiting.remove(ident);prepared.add(ident)


def prepare(work,arts):
    (work/'MC').mkdir(parents=True,exist_ok=True)
    for a in arts:
        if a.get('target'): prepare_target(work,a['target'])
        cached=cache_files(a)
        meta=json.loads((cached/'manifest.json').read_text())
        if meta['policy']!=POLICY or meta['content_hash']!=a['content_hash']: raise RuntimeError('Dependency cache identity mismatch')
        if meta.get('library_id')!=LIBRARY_ID: raise RuntimeError('Dependency Mathlib environment mismatch')
        if a.get('target') and (meta.get('target_hash')!=a['target']['target_hash'] or meta.get('target_matched') is not True): raise RuntimeError('Dependency fixed-target attestation mismatch')
        if file_hash(cached/'module.olean')!=meta['olean_hash']: raise RuntimeError('Dependency cache corruption')
        current_image=docker(['image','inspect','--format','{{.Id}}',IMAGE]).stdout.strip()
        if current_image!=meta['image']: raise RuntimeError('Dependency was built in a different sandbox image')
        shutil.copyfile(cached/'module.olean',work/a['file_path'].replace('.lean','.olean'))
        # Read-only file permissions protect dependencies in the writable compiler workspace.
        os.chmod(work/a['file_path'].replace('.lean','.olean'),0o444)

def parse_axioms(text):
    match=re.findall(r"'MCVerifiedResult' (?:depends on axioms: \[([^\]]*)\]|does not depend on any axioms)",text,re.S)
    if len(match)!=1: raise RuntimeError('Missing or ambiguous trusted axiom report: '+text[-3000:])
    axioms=[s.strip() for s in match[0].split(',') if s.strip()]
    if set(axioms)-ALLOWED_AXIOMS: raise RuntimeError('Disallowed transitive axioms: '+str(axioms))
    return axioms

def verify_target(target,arts):
    work=Path(tempfile.mkdtemp(prefix='target-',dir=STORE.root/'artifacts'));os.chmod(work,0o777)
    try:
        prepare(work,arts)
        relative=target['target_module'].replace('.','/')+'.lean'
        path=work/relative;path.parent.mkdir(parents=True,exist_ok=True);os.chmod(path.parent,0o777)
        for dependency in target.get('insight_dependency_snapshot',[]):prepare_target(work,dependency)
        source=target_source(target);path.write_text(source)
        code,log,image=sandbox(work,['/opt/lean/bin/lean','-j','1','-o',relative.replace('.lean','.olean'),relative],True)
        if code: return dict(status='error' if code in (124,125,126,127,137,139) else 'invalid',log=log,exit_code=code)
        checker=('import Lean\nimport '+target['target_module']+'\n'
                 +'run_cmd do\n  let info ← Lean.getConstInfo `'+target['target_constant']+'\n  unless info.levelParams.isEmpty do throwError \"Fixed Insight target must be closed over universe levels\"\n'
                 +'def MCVerifiedResult : Prop := @_root_.'+target['target_constant']+'\n#print axioms MCVerifiedResult\n')
        (work/'Check.lean').write_text(checker)
        code,checklog,_=sandbox(work,['/opt/lean/bin/lean','-j','1','Check.lean'])
        if code: return dict(status='error' if code in (124,125,126,127,137,139) else 'invalid',log=log+'\n'+checklog,exit_code=code)
        axioms=parse_axioms(checklog)
        cached=target_cache_files(target);cached.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path.with_suffix('.olean'),cached/'module.olean')
        manifest=dict(target_hash=target['target_hash'],insight_dependency_snapshot=target.get('insight_dependency_snapshot',[]),policy=POLICY,toolchain=TOOLCHAIN,library_id=LIBRARY_ID,image=image,olean_hash=file_hash(cached/'module.olean'),axioms=axioms)
        atomic(cached/'manifest.json',manifest);(cached/'source.lean').write_text(source)
        return dict(status='valid',log=log+'\n'+checklog,axioms=axioms,cache=manifest)
    finally:shutil.rmtree(work,ignore_errors=True)

def proof_checker(a):
    target=a['target']
    return (f'import Lean\nimport {target["target_module"]}\nimport MC.{a["id"]}\n'
            +'run_cmd Lean.Elab.Command.liftTermElabM do\n'
            +f'  let e ← Lean.Meta.mkConstWithFreshMVarLevels `{a["theorem"]}\n'
            +'  let t ← Lean.Meta.inferType e\n'
            +'  unless (← Lean.Meta.isProp t) do throwError "Selected declaration is not a proof"\n'
            +f'  let expected := Lean.mkConst `{target["target_constant"]}\n'
            +'  unless (← Lean.Meta.isDefEq t expected) do throwError "Proof does not match the fixed Insight target"\n'
            +f'def MCVerifiedResult : _root_.{target["target_constant"]} := @_root_.{a["theorem"]}\n'
            +'#print axioms MCVerifiedResult\n'+f'#check @_root_.{a["theorem"]}\n')

def verify_artifact(a,arts):
    if not a.get('target') or a.get('target_hash')!=a['target'].get('target_hash'): return dict(status='failed',log='Missing or inconsistent fixed Insight target')
    work=Path(tempfile.mkdtemp(prefix='verify-',dir=STORE.root/'artifacts'))
    os.chmod(work,0o777)
    try:
        prepare(work,arts[:-1]); prepare_target(work,a['target']); os.chmod(work/'MC',0o777)
        # Protect the dependency directory; root worker owns all files except generated output.
        source='import '+a['target']['target_module']+'\n'+'\n'.join('import MC.'+d for d in a['dependencies'])+'\n'+a['source']
        (work/a['file_path']).write_text(source)
        code,log,image=sandbox(work,['/opt/lean/bin/lean','-j','1','-o',a['file_path'].replace('.lean','.olean'),a['file_path']],True)
        if code: return dict(status='failed',log=log,exit_code=code)
        # Fresh process, read-only mount: submission cannot edit the trusted checker.
        (work/'Check.lean').write_text(proof_checker(a))
        code,checklog,_=sandbox(work,['/opt/lean/bin/lean','-j','1','Check.lean'])
        if code: return dict(status='failed',log=log+'\n'+checklog,exit_code=code)
        axioms=parse_axioms(checklog)
        cached=cache_files(a); cached.mkdir(parents=True,exist_ok=True)
        target=work/a['file_path'].replace('.lean','.olean')
        if target.is_symlink() or not target.is_file(): raise RuntimeError('Invalid compiler artifact')
        shutil.copyfile(target,cached/'module.olean')
        manifest=dict(content_hash=a['content_hash'],policy=POLICY,toolchain=a['toolchain'],library_id=LIBRARY_ID,image=image,olean_hash=file_hash(cached/'module.olean'),axioms=axioms,target_hash=a['target_hash'],target_matched=True)
        atomic(cached/'manifest.json',manifest)
        signature=re.search(r'(?m)^'+re.escape(a['theorem'])+r'\s*:.*',checklog,re.S)
        return dict(status='passed',target_hash=a['target_hash'],target_matched=True,theorem_signature=signature.group(0).strip() if signature else None,axioms=axioms,log=log+'\n'+checklog,cache=manifest,semantic_alignment='not_reviewed',reused_dependencies=[x['id'] for x in arts[:-1]])
    finally: shutil.rmtree(work,ignore_errors=True)

def verify_solution(s,p,a,arts):
    work=Path(tempfile.mkdtemp(prefix='solution-',dir=STORE.root/'artifacts')); os.chmod(work,0o777)
    try:
        prepare(work,arts)
        target=final_target(p,s.get('claims_problem_solved',1))
        source='import Lean\n'+'\n'.join('import MC.'+x['id'] for x in arts)+'\n'+target.get('definitions','')+'\n'+f'theorem MCVerifiedResult : {target["expression"]} := @{a["theorem"]}\n#print axioms MCVerifiedResult\n'
        source+='\n'+(ROOT/'apps/worker/used_modules.lean').read_text()
        (work/'Check.lean').write_text(source)
        code,log,image=sandbox(work,['/opt/lean/bin/lean','-j','1','Check.lean'])
        if code: return dict(status='failed',log=log)
        axioms=parse_axioms(log)
        reports=re.findall(r'MC_USED_MODULES:(\[[^\n]*\])',log)
        if len(reports)!=1: raise RuntimeError('Missing actual proof dependency report')
        used=set(json.loads(reports[0])); used_artifacts=[x for x in arts if x['module'] in used]
        return dict(status='verified',used_artifact_ids=[x['id'] for x in used_artifacts],used_insight_ids=sorted({x['insight_id'] for x in used_artifacts}),log=log,axioms=axioms,target_hash=digest(target),dependency_snapshot=[dict(id=x['id'],content_hash=x['content_hash']) for x in arts],image=image,verified_at=time.time())
    finally: shutil.rmtree(work,ignore_errors=True)

def check_next_target():
    with STORE.transaction() as tx:
        queued=[x for x in tx.all('insight_targets') if x['status']=='queued']
        if not queued:return False
        target=None
        for candidate in sorted(queued,key=lambda x:(x.get('created_at',0),x['id'])):
            dependencies=[(snapshot,tx.get('insight_targets',snapshot['insight_id'])) for snapshot in candidate.get('insight_dependency_snapshot',[])]
            broken=any(not current or current['target_hash']!=snapshot['target_hash'] or current['status'] in ('invalid','revoked','stale') for snapshot,current in dependencies)
            if broken:
                candidate.update(status='invalid',finished_at=time.time(),log='A frozen target-definition dependency is invalid or has changed. Correct this target in a new migration batch after its prerequisite is valid.')
                tx.put('insight_targets',candidate);return True
            if all(current['status']=='valid' for _,current in dependencies):target=candidate;break
        if target is None:return False
        target.update(status='checking',started_at=time.time());tx.put('insight_targets',target)
    try:
        arts={}
        with STORE.transaction() as tx:
            for snapshot in target.get('dependency_snapshot',[]):
                artifact=need(tx,'lean_artifacts',snapshot['id'])
                if artifact['content_hash']!=snapshot['content_hash']:raise RuntimeError('Fixed target dependency changed')
                for a in closure(tx,artifact):arts[a['id']]=a
        result=verify_target(target,list(arts.values()))
    except Exception as exc:result=dict(status='error',log=str(exc)[-10000:])
    with STORE.transaction() as tx:
        current=need(tx,'insight_targets',target['id'])
        if current['target_hash']!=target['target_hash'] or current['status']!='checking':return True
        current.update(result,finished_at=time.time());tx.put('insight_targets',current)
        from apps.api.modules.migration import sync
        sync(tx)
    return True

def once():
    if check_next_target():return True
    with STORE.transaction() as tx:
        sync_candidates(tx)
        candidates=[x for x in tx.all('solutions') if x['status']=='queued']
        if candidates:
            v=min(candidates,key=lambda x:x['created_at']); v.update(status='running',started_at=time.time()); tx.put('solutions',v)
            a=need(tx,'lean_artifacts',v['artifact_id'])
            merged={}
            for supporting_id in v.get('supporting_artifact_ids',[]):
                for artifact in closure(tx,need(tx,'lean_artifacts',supporting_id)):merged.setdefault(artifact['id'],artifact)
            for artifact in closure(tx,a):merged.setdefault(artifact['id'],artifact)
            arts=list(merged.values());p=need(tx,'problems',v['problem_id']);kind='solution'
            leaf_verified=target_proof_matches(a,need(tx,'verifications',a['verification_id']),tx.get('insight_targets',a['insight_id'])) and not withdrawn(tx,'lean_artifacts',a['id'])
        else:
            candidates=[x for x in tx.all('verifications') if x['status']=='queued']
            from apps.api.modules.lean import canonical_proof_ids
            canonical=canonical_proof_ids(tx)
            for pending in candidates:
                pending_artifact=need(tx,'lean_artifacts',pending['artifact_id'])
                if pending_artifact['insight_id'] in canonical:
                    pending.update(status='cancelled',finished_at=time.time(),log='Duplicate formalization: this insight already has a canonical verified proof.')
                    tx.put('verifications',pending)
            candidates=[x for x in candidates if x['status']=='queued' and (tx.get('insight_targets',need(tx,'lean_artifacts',x['artifact_id'])['insight_id']) or {}).get('status') not in ('queued','checking','error')]
            if not candidates: return False
            priorities=priority_targets(tx)
            def order(v):
                ident=need(tx,'lean_artifacts',v['artifact_id'])['insight_id']
                return (0,-priorities[ident],v['created_at']) if ident in priorities else (1,0,v['created_at'])
            v=min(candidates,key=order); v.update(status='running',started_at=time.time()); tx.put('verifications',v)
            a=need(tx,'lean_artifacts',v['artifact_id']); arts=closure(tx,a); target=tx.get('insight_targets',a['insight_id']); kind='verification'
    try:
        if kind=='solution' and not leaf_verified:raise RuntimeError('Final verification requires a proof of the immutable Insight target')
        if kind=='solution' and (p.get('status')=='archived' or p.get('target_status')!='reviewed' or digest(final_target(p,v.get('claims_problem_solved',1)))!=v.get('target_hash')): raise RuntimeError('Final verification requires the unchanged reviewed target')
        if kind=='verification' and (not target or target.get('status')!='valid' or target.get('target_hash')!=a.get('target_hash')): raise RuntimeError('The immutable Insight target must be valid and match this proof snapshot')
        result=verify_artifact(a,arts) if kind=='verification' else verify_solution(v,p,a,arts)
    except Exception as exc: result=dict(status='failed',log=str(exc)[-10000:])
    with STORE.transaction() as tx:
        collection='verifications' if kind=='verification' else 'solutions'
        current=need(tx,collection,v['id'])
        if current['status']!='running' or (kind=='verification' and withdrawn(tx,'lean_artifacts',a['id'])) or (kind=='solution' and (withdrawn(tx,'solutions',v['id']) or (v.get('automatic') and withdrawn(tx,'insights',v['insight_id'])))):
            sync_candidates(tx);return True
        if kind=='solution' and result['status']=='verified':
            current_problem=need(tx,'problems',v['problem_id'])
            current_leaf=need(tx,'lean_artifacts',a['id'])
            current_leaf_valid=target_proof_matches(current_leaf,need(tx,'verifications',current_leaf['verification_id']),tx.get('insight_targets',current_leaf['insight_id'])) and not withdrawn(tx,'lean_artifacts',a['id'])
            if not current_leaf_valid or current_problem.get('status')=='archived' or current_problem.get('target_status')!='reviewed' or digest(final_target(current_problem,v.get('claims_problem_solved',1)))!=v['target_hash']:
                result=dict(status='failed',log='Reviewed target changed during verification')
        if kind=='verification' and result['status']=='passed':
            current_target=tx.get('insight_targets',a['insight_id'])
            if (not current_target or current_target.get('status')!='valid' or current_target.get('target_hash')!=a.get('target_hash')
                    or result.get('target_hash')!=a.get('target_hash') or result.get('target_matched') is not True):
                result=dict(status='failed',log='Missing target attestation or fixed Insight target changed during proof verification')
        v.update(result,finished_at=time.time()); tx.put('verifications' if kind=='verification' else 'solutions',v)
        if kind=='verification' and a.get('task_id'):
            leased=tx.get('tasks',a['task_id'])
            if leased and leased['status']=='claimed' and leased.get('contributor_id')==a['contributor_id'] and a['id'] in leased.get('proof_attempts',[]):
                leased.update(last_verification_id=v['id'],last_verification_status=result['status'])
                if result['status']=='failed':leased.update(repair_artifact_id=a['id'],mode='repair')
                if result['status']=='passed':leased.update(status='submitted',result_id=a['id'],submitted_at=time.time())
                tx.put('tasks',leased)
        if kind=='verification' and a['insight_id'] in priority_targets(tx) and result['status']=='passed': task(tx,a['problem_id'],'lean_work',a['id'],mode='audit')
        if kind=='verification' and a['insight_id'] in priority_targets(tx) and result['status']=='failed' and not any(t['type']=='lean_work' and t.get('target_id')==a['insight_id'] and t['status']=='claimed' and t.get('expires_at',0)>time.time() for t in tx.all('tasks')):
            repair=task(tx,a['problem_id'],'lean_work',a['insight_id'],mode='repair')
            repair.update(repair_artifact_id=a['id'],goal='Repair the failed Lean artifact using its diagnostics, and submit a corrected proof for the linked insight.')
            tx.put('tasks',repair)
        if kind=='solution' and result['status']=='verified':
            p=need(tx,'problems',v['problem_id']); close_problem(tx,p,v)
        sync_candidates(tx)
    return True

def main():
    library_paths()
    (STORE.root/'artifacts').mkdir(parents=True,exist_ok=True)
    with open(STORE.root/'.worker.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with STORE.transaction() as tx:
            for target in tx.all('insight_targets'):
                if target['status']=='checking':target['status']='queued';tx.put('insight_targets',target)
            for kind in ('verifications','solutions'):
                for v in tx.all(kind):
                    if v['status']=='running': v['status']='queued'; tx.put(kind,v)
        previous=None; checked=0
        watched=('insight_targets','insights','lean_artifacts','verifications','solutions','problems','submission_states','tasks')
        def revision():
            result=[]
            for kind in watched:
                try:
                    stat=(STORE.root/kind).stat();result.append((kind,stat.st_ino,stat.st_mtime_ns,stat.st_ctime_ns))
                except FileNotFoundError:result.append((kind,None))
            return tuple(result)
        while True:
            try:
                current=revision()
                if current==previous and time.monotonic()-checked<30:
                    time.sleep(2);continue
                if once():previous=None
                else:
                    previous=revision();checked=time.monotonic();time.sleep(2)
            except Exception: traceback.print_exc(); time.sleep(5)
if __name__=='__main__': main()
