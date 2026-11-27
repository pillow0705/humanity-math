#!/usr/bin/python3
"""Root-owned, fixed deployment entry point. Never execute this from an unmerged PR."""
import fcntl, gzip, json, os, re, shutil, subprocess, sys, tarfile, tempfile, time, urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

ALLOWED={'apps','agent-docs','contracts','deploy','prompts','seeds','tests','.github','README.md','CONTRIBUTING.md','SECURITY.md','AGENTS.md','LICENSE','.gitignore','.dockerignore','.python-version','pytest.ini','requirements.txt'}


def unpack_release(archive, destination):
    with tarfile.open(archive, 'r:gz') as tar:
        members=tar.getmembers()
        if sum(m.size for m in members)>100*1024*1024:raise ValueError('Release is too large')
        for member in members:
            path=PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in ALLOWED:
                raise ValueError('Unexpected release path: '+member.name)
            if not member.isfile() and not member.isdir():raise ValueError('Links and special files are not accepted')
        tar.extractall(destination, members=members)
    if not (destination/'apps/api/main.py').is_file() or not (destination/'requirements.txt').is_file():
        raise ValueError('Incomplete release')
    for p in destination.rglob('*'):
        if p.is_dir():p.chmod(0o755)
        elif p.is_file():p.chmod(0o644)


def atomic_text(path,text):
    temp=path.with_name(path.name+'.new');temp.write_text(text);temp.chmod(0o600);os.replace(temp,path)


def point_current(current,destination):
    temp=current.with_name('current.new');temp.unlink(missing_ok=True);temp.symlink_to(destination,target_is_directory=True);os.replace(temp,current)


def run(*args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)


def health(expected,timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2) as response:body=json.load(response)
            if body.get('status')=='ok' and (expected=='legacy' or body.get('release')==expected):return
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('Release health check failed: '+expected)


@contextmanager
def data_lock(data):
    with open(data/'.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def quiesce_worker(data):
    deadline=time.monotonic()+660
    while True:
        with data_lock(data):
            # Resolve any interrupted file journal before examining the queue.
            journal=data/'.pending.json'
            if journal.exists():raise RuntimeError('Recover pending data transaction before deploying')
            running=any(json.loads(p.read_text()).get('status')=='running' for kind in ('verifications','solutions') for p in (data/kind).glob('*.json'))
            if not running:
                run('systemctl','stop','math-collab-worker');return
        if time.monotonic()>deadline:raise RuntimeError('A verification is still running; retry deployment later')
        time.sleep(2)


def restore_previous(current,previous,env_file,old_env,old_sha):
    point_current(current,previous);atomic_text(env_file,old_env)
    run('systemctl','restart','math-collab-api');health(old_sha)
    run('systemctl','start','math-collab-worker')


def deploy(sha,stream):
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('Expected a full commit SHA')
    config=json.loads(Path('/etc/humanity-deploy.json').read_text())
    root=Path(config['root']);root.mkdir(parents=True,exist_ok=True)
    with open(root/'deploy.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        current=root/'current';previous=current.resolve(strict=True)
        env_file=Path(config['environment_file']);old_env=env_file.read_text()
        old_sha=next((line.split('=',1)[1] for line in old_env.splitlines() if line.startswith('APP_RELEASE=')),'legacy')
        with tempfile.NamedTemporaryFile(dir=root,suffix='.tgz') as archive:
            received=0
            while True:
                chunk=stream.read(1024*1024)
                if not chunk:break
                received+=len(chunk)
                if received>20*1024*1024:raise ValueError('Compressed release exceeds limit')
                archive.write(chunk)
            archive.flush()
            release=root/'releases'/sha
            if release.exists():
                if release.resolve()==previous:
                    health(sha);print('Already deployed '+sha,flush=True);return
                shutil.rmtree(release)
            release.mkdir(parents=True)
            unpack_release(archive.name,release)
        run(sys.executable,'-m','venv',str(release/'.venv'))
        python=str(release/'.venv/bin/python')
        run(python,'-m','pip','install','--disable-pip-version-check','-r',str(release/'requirements.txt'))
        runtime=dict(os.environ,MATH_DATA=config['data'],LEAN_HOME=config['lean_home'],MATHLIB_HOME=config['mathlib_home'],PYTHONPATH=str(release))
        probe="import json;from apps.api.modules.lean import LIBRARY_ID;from apps.worker.main import library_paths;library_paths();print(json.dumps({'library_id':LIBRARY_ID}))"
        result=json.loads(subprocess.check_output([python,'-c',probe],cwd=release,env=runtime,text=True))
        if result['library_id']!=config['library_id']:raise RuntimeError('Provision and migrate the new Lean library before deployment')
        run(python,'-m','compileall','-q',str(release/'apps'))
        quiesce_worker(Path(config['data']))
        try:
            point_current(current,release)
            new_env='\n'.join(line for line in old_env.splitlines() if not line.startswith('APP_RELEASE='))+'\nAPP_RELEASE='+sha+'\n'
            atomic_text(env_file,new_env)
            run('systemctl','restart','math-collab-api');health(sha)
            run('systemctl','start','math-collab-worker');time.sleep(2)
            run('systemctl','is-active','--quiet','math-collab-api','math-collab-worker')
        except Exception:
            restore_previous(current,previous,env_file,old_env,old_sha)
            raise
        (release/'DEPLOYED.json').write_text(json.dumps(dict(commit=sha,deployed_at=time.time())))
        print('DEPLOYED '+sha,flush=True)
        candidates=sorted((root/'releases').iterdir(),key=lambda p:p.stat().st_mtime,reverse=True)
        for stale in candidates[5:]:
            if stale not in (release,previous):shutil.rmtree(stale)


if __name__=='__main__':
    deploy(sys.argv[1],sys.stdin.buffer)
