"""Seal a pinned Mathlib installation after downloading its official build cache.

Run from the project root: .venv/bin/python -m deploy.check_mathlib
This checks installation inputs; run the real Lean integration tests before rollout.
"""
import json, os, subprocess, time
from pathlib import Path
from apps.api.modules.lean import MATHLIB_MANIFEST, MATHLIB_REVISION, MATHLIB_VERSION, LIBRARY_ID, TOOLCHAIN
from apps.api.infrastructure.store import atomic

root=Path(os.environ.get('MATHLIB_HOME',Path(__file__).resolve().parents[1] / 'tools' / f'mathlib-{MATHLIB_VERSION}'))

def check_git(path,revision):
    actual=subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()
    if actual!=revision: raise SystemExit(f'Wrong revision: {path}')
    dirty=subprocess.check_output(['git','-C',str(path),'status','--porcelain','--untracked-files=no'],text=True).strip()
    if dirty: raise SystemExit(f'Modified tracked library files: {path}')

check_git(root,MATHLIB_REVISION)
if (root/'lean-toolchain').read_text().strip()!=TOOLCHAIN: raise SystemExit('Wrong Lean toolchain')
if json.loads((root/'lake-manifest.json').read_text())!=MATHLIB_MANIFEST: raise SystemExit('Wrong dependency manifest')
for package in MATHLIB_MANIFEST['packages']:
    check_git(root/'.lake/packages'/package['name'],package['rev'])
if not (root/'.lake/build/lib/lean/Mathlib.olean').is_file(): raise SystemExit('Compiled Mathlib missing')
modules=list((root/'.lake/build/lib/lean/Mathlib').rglob('*.olean'))
atomic(root/'platform-install.json',dict(revision=MATHLIB_REVISION,library_id=LIBRARY_ID,
       toolchain=TOOLCHAIN,mathlib_compiled_modules=len(modules)+1,sealed_at=time.time()))
print(f'Mathlib installation sealed: {len(modules)+1} compiled Mathlib modules, {len(MATHLIB_MANIFEST["packages"])} pinned dependencies')
