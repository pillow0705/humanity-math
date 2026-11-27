import json, re
from pathlib import Path
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need, clean, check_supersedes, finish_task, contribute

POLICY = 'lean-mathlib-v1'
TOOLCHAIN = 'leanprover/lean4:v4.19.0'
MATHLIB_REVISION = 'c44e0c8ee63ca166450922a373c7409c5d26b00b'
MATHLIB_VERSION = 'v4.19.0'
MATHLIB_MANIFEST = json.loads((Path(__file__).resolve().parents[3]/'contracts/mathlib-manifest.json').read_text())
LIBRARY_ROOTS = ('Init', 'Std', 'Mathlib', 'Batteries', 'Aesop', 'Qq')
LIBRARY_ID = digest(dict(toolchain=TOOLCHAIN,mathlib=MATHLIB_REVISION,dependencies=MATHLIB_MANIFEST))
TIMEOUT = 600
MEMORY_MB = 4096
ALLOWED_AXIOMS = {'propext','Classical.choice','Quot.sound'}

def library_import(name):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*",name)) and name.split('.')[0] in LIBRARY_ROOTS

def bundle_manifest():
    packages=[dict(p,inherited=True) for p in MATHLIB_MANIFEST['packages']]
    packages.append(dict(url='https://github.com/leanprover-community/mathlib4',type='git',subDir=None,
                         scope='leanprover-community',rev=MATHLIB_REVISION,name='mathlib',
                         manifestFile='lake-manifest.json',inputRev=MATHLIB_REVISION,inherited=False,configFile='lakefile.lean'))
    return dict(version=MATHLIB_MANIFEST['version'],packagesDir='.lake/packages',packages=packages,name='research',lakeDir='.lake')

def canonical_proof_ids(tx):
    """One active proof per insight; historical modules remain importable by old chains."""
    from apps.api.modules.targets import target_proof_matches
    targets={t['insight_id']:t for t in tx.all('insight_targets')}
    artifacts=tx.all('lean_artifacts')
    verifications={v['id']:v for v in tx.all('verifications')}
    withdrawn_ids={s['object_id'] for s in tx.all('submission_states') if s.get('withdrawn')}
    superseded={a['supersedes_id'] for a in artifacts if a.get('supersedes_id')}
    first_success={}
    for v in verifications.values():
        if v.get('status')=='passed' and v.get('artifact_id'):
            ident=v['artifact_id'];stamp=v.get('finished_at') or v.get('created_at',0)
            first_success[ident]=min(first_success.get(ident,stamp),stamp)
    result={}
    for a in sorted(artifacts,key=lambda a:(first_success.get(a['id'],float('inf')),a['created_at'],a['id'])):
        v=verifications.get(a['verification_id'],{})
        if (a['id'] not in withdrawn_ids|superseded and v.get('status')=='passed'
                and target_proof_matches(a,v,targets.get(a['insight_id']))):
            result.setdefault(a['insight_id'],a['id'])
    return result

def create_artifact(tx, body):
    insight = need(tx,'insights',body['insight_id'])
    from apps.api.modules.targets import get_target, target_proof_matches
    target=get_target(tx,insight['id'])
    if not target: raise HTTPException(409,'This insight needs an immutable Lean target before proof submission; its author must register the target first')
    if target['status']=='invalid': raise HTTPException(409,'The frozen Lean target is invalid; submit a corrected replacement insight before proving it')
    check_supersedes(tx,'lean_artifacts',body,insight['problem_id'])
    for existing in tx.all('lean_artifacts'):
        if existing['insight_id'] != insight['id']: continue
        verification = need(tx,'verifications',existing['verification_id'])
        if target_proof_matches(existing,verification,target):
            raise HTTPException(409,dict(message='This insight already has a verified Lean proof. Reuse it or submit an audit; duplicate formalization is not accepted.',existing_id=existing['id']))
    if len(set(body['dependencies'])) != len(body['dependencies']): raise HTTPException(422,'Duplicate dependencies')
    deps = [need(tx,'lean_artifacts',i) for i in body['dependencies']]
    canonical = canonical_proof_ids(tx)
    for dep in deps:
        if dep['insight_id'] in canonical and canonical[dep['insight_id']]!=dep['id']:
            raise HTTPException(409,dict(message='Use the canonical proof for this insight; the requested module is a historical duplicate.',existing_id=canonical[dep['insight_id']]))
        v = need(tx,'verifications',dep['verification_id'])
        if v['status'] != 'passed' or v['policy'] != POLICY: raise HTTPException(422,'Dependencies must pass the current verification policy')
    source = body['source']
    validate_source(source,deps)
    key = digest(dict(target_hash=target['target_hash'],source=source,theorem=body['theorem'],dependencies=[d['content_hash'] for d in deps],toolchain=TOOLCHAIN,policy=POLICY,library_id=LIBRARY_ID))
    for old in tx.all('lean_artifacts'):
        if old['content_hash']==key and old['insight_id']==body['insight_id']: raise HTTPException(409,dict(message='Duplicate code',existing_id=old['id']))
    obj = tx.new('lean_artifacts','L',dict(clean(body),problem_id=insight['problem_id'],target_hash=target['target_hash'],target={k:v for k,v in target.items() if k not in ('log','started_at','finished_at')},content_hash=key,policy=POLICY,library_id=LIBRARY_ID))
    v = tx.new('verifications','V',dict(artifact_id=obj['id'],status='queued',policy=POLICY,content_hash=key))
    obj['verification_id']=v['id']; obj['module']='MC.'+obj['id']; obj['file_path']='MC/'+obj['id']+'.lean'
    tx.put('lean_artifacts',obj)
    finish_task(tx,body,'lean_work',insight['id'],insight['problem_id'],obj['id'])
    contribute(tx,obj,'lean_work')
    return obj

def validate_source(source, dependencies):
    if re.search(r'\b(sorry|admit|axiom|unsafe|initialize|builtin_initialize|run_tac|run_elab|run_cmd|run_meta|run_io|elab|macro|syntax|extern|implemented_by|native_decide|skipKernelTC)\b|#|\x00', source):
        raise HTTPException(422,'Unsupported metaprogramming, unproved axiom or placeholder; submit ordinary Lean definitions and proofs')
    allowed = {'MC.'+d['id'] for d in dependencies}
    for line in source.splitlines():
        if re.match(r'^\s*import\b',line):
            imports = line.strip().split()[1:]
            if not imports or any(x not in allowed and not library_import(x) for x in imports): raise HTTPException(422,'Use installed Init/Std/Mathlib/Batteries/Aesop/Qq modules or declared verified MC dependencies')

def closure(tx, obj):
    result = {}; visiting = set()
    def visit(a):
        if a['id'] in visiting: raise HTTPException(422,'Cyclic dependency')
        if a['id'] in result: return
        visiting.add(a['id'])
        for i in dict.fromkeys(a['dependencies']+a.get('target',{}).get('dependencies',[])): visit(need(tx,'lean_artifacts',i))
        visiting.remove(a['id']); result[a['id']]=a
    visit(obj); return list(result.values())


def environment(prefix=''):
    """Public contract for the submission environment, not the host filesystem."""
    profile = dict(
        toolchain=TOOLCHAIN, lean_version='4.19.0', verification_policy=POLICY,
        library_id=LIBRARY_ID,
        libraries=[dict(name='Lean core', imports=['Init','Std'], available=True),
                   dict(name='Mathlib', version=MATHLIB_VERSION, revision=MATHLIB_REVISION, available=True, precompiled=True,
                        dependencies=[dict(name=p['name'],revision=p['rev']) for p in MATHLIB_MANIFEST['packages']])],
        permitted_imports=[root+' and '+root+'.*' for root in LIBRARY_ROOTS]+['MC.<artifact_id> for each declared verified dependency'],
        allowed_axioms=sorted(ALLOWED_AXIOMS),
        submission=dict(format='single Lean source module', source_field='source',
                        entry_theorem_field='theorem', dependencies_field='dependencies', fixed_target_required=True, exact_target_match=True,
                        upload_only='New proof source and any new helper definitions; reference existing code by artifact ID',
                        server_assembles_dependencies=True, dependencies_auto_imported=True,
                        local_download_required=False, arbitrary_packages=False,
                        compiled_uploads=False, custom_build_scripts=False,
                        custom_metaprogramming=False, max_source_characters=200000),
        dependency_policy='Immutable artifacts verified under the current policy; matching platform caches are reused. Availability and cache integrity are checked during verification.',
        limits=dict(verification_timeout_seconds=TIMEOUT, container_memory_mb=MEMORY_MB, network_access=False),
    )
    return dict(profile, environment_id=digest(profile),
                artifact_catalog_url=prefix+'/api/v1/lean/artifacts',
                submit_url=prefix+'/api/v1/lean/artifacts',
                openapi_url=prefix+'/openapi.json',
                note='Artifact modules are reusable results, not arbitrary installable packages. The catalog indexes registered entry theorems, not every helper declaration.')


def artifact_summary(tx, a, prefix='', canonical=None):
    v=need(tx,'verifications',a['verification_id'])
    i=need(tx,'insights',a['insight_id'])
    canonical=canonical_proof_ids(tx) if canonical is None else canonical
    from apps.api.modules.targets import get_target, target_proof_matches
    bound=target_proof_matches(a,v,get_target(tx,a['insight_id']))
    legacy=v['status']=='passed' and not bound
    duplicate=v['status']=='passed' and a['insight_id'] in canonical and canonical[a['insight_id']]!=a['id']
    signature=v.get('theorem_signature')
    if not signature and v['status']=='passed':
        match=re.search(r'(?m)^'+re.escape(a['theorem'])+r'\s*:.*',v.get('log',''),re.S)
        signature=match.group(0).strip() if match else None
    return dict(id=a['id'], problem_id=a['problem_id'], insight_id=a['insight_id'],
                title=i['title'], insight_claim=i['claim'], module=a['module'],
                file_path=a['file_path'], import_statement='import '+a['module'],
                theorem=a['theorem'], theorem_signature=signature,
                signature_note='Use this Lean type and inspect source for definitions; the linked insight claim is a separate natural-language statement.',
                dependencies=a['dependencies'], toolchain=a['toolchain'],
                verification_policy=a['policy'], library_id=a.get('library_id'),
                content_hash=a['content_hash'], status='legacy_unbound' if legacy else 'historical_duplicate' if duplicate else v['status'],
                target_bound=bound,target_hash=a.get('target',{}).get('target_hash'),
                historical_duplicate=duplicate, canonical_proof_id=canonical.get(a['insight_id']),
                reusable=v['status']=='passed' and v['policy']==POLICY and a.get('library_id')==LIBRARY_ID and not duplicate,
                verification_id=v['id'], axioms=v.get('axioms'),
                semantic_alignment=v.get('semantic_alignment','not_reviewed'),
                detail_url=prefix+'/api/v1/lean/artifacts/'+a['id'],
                bundle_url=prefix+'/api/v1/lean/artifacts/'+a['id']+'/bundle')
