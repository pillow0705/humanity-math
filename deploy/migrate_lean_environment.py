"""Offline Lean environment migration. Preview by default; use on a private backup first."""
import argparse
import fcntl
import json
import time
from pathlib import Path

from fastapi import HTTPException

from apps.api.infrastructure.store import Store, Transaction
from apps.api.modules.lean import LIBRARY_ID
from apps.api.modules.scoring import Scores
from apps.api.modules.targets import register_target


def migrate(tx):
    migration_id = 'lean-' + LIBRARY_ID
    previous = tx.get('platform_states', migration_id)
    if previous:
        return dict(previous['report'], already_applied=True)
    scores = Scores(tx)
    open_problems = {p['id'] for p in tx.all('problems') if p['status'] == 'open'}
    insights = {
        ident: item for ident, item in scores.objects['insights'].items()
        if item['problem_id'] in open_problems and ident not in scores.withdrawn | scores.superseded
    }
    old_targets = {
        t['id']: t for t in tx.all('insight_targets')
        if t.get('library_id') != LIBRARY_ID and t.get('migration_library_id') != LIBRARY_ID
    }
    reason = 'Lean environment changed; a new target version and proof check are required'
    report = dict(requeued_targets=0, targets_needing_repair=0, cancelled_checks=0, closed_leases=0)
    now = time.time()
    for ident, old in old_targets.items():
        if not tx.get('target_versions', old['target_hash']):
            tx.put('target_versions', dict(old, id=old['target_hash']))
        if ident in insights:
            tx.put('insight_targets', dict(old, status='stale', invalidation_reason=reason,
                                           migration_library_id=LIBRARY_ID))

    remaining = {
        ident: old for ident, old in old_targets.items()
        if ident in insights and old['status'] in ('valid', 'queued', 'checking', 'error')
        and not old.get('dependencies') and not old.get('alignment_flags')
    }
    migrated = set()
    while remaining:
        ready = []
        for ident, old in remaining.items():
            snapshots = old.get('insight_dependency_snapshot', [])
            if {s['insight_id'] for s in snapshots} != set(old.get('insight_dependencies', [])):
                continue
            if all(s['insight_id'] in migrated
                   and old_targets[s['insight_id']]['target_hash'] == s['target_hash']
                   for s in snapshots):
                ready.append(ident)
        if not ready:
            break
        for ident in ready:
            old = remaining.pop(ident)
            body = {
                key: old[key] for key in ('definitions', 'expression', 'insight_dependencies')
                if key in old
            }
            try:
                target = register_target(tx, insights[ident], body, replace_invalid=True,
                                         allow_pending_target_ids=migrated)
            except HTTPException:
                # Keep the old statement available for a contributor to repair.
                continue
            target['registered_by'] = old.get('registered_by', insights[ident]['contributor_id'])
            tx.put('insight_targets', target)
            migrated.add(ident)
    report['requeued_targets'] = len(migrated)
    report['targets_needing_repair'] = len((old_targets.keys() & insights.keys()) - migrated)

    artifacts = {a['id']: a for a in tx.all('lean_artifacts')}
    for collection in ('verifications', 'solutions'):
        for check in tx.all(collection):
            artifact = artifacts.get(check.get('artifact_id'))
            if check['status'] in ('queued', 'running') and artifact and artifact.get('library_id') != LIBRARY_ID:
                check.update(status='cancelled', finished_at=now, log=reason)
                tx.put(collection, check)
                report['cancelled_checks'] += 1
    for lease in tx.all('tasks'):
        if lease['type'] in ('lean_work', 'lean_target') and lease['status'] in ('available', 'claimed'):
            lease.update(status='closed', closed_reason=reason, expires_at=0, claim_hash='')
            tx.put('tasks', lease)
            report['closed_leases'] += 1
    tx.put('platform_states', dict(id=migration_id, report=report, migrated_at=now))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Apply with API and worker stopped and a verified backup')
    args = parser.parse_args()
    if not args.data.is_dir():
        parser.error('The existing data directory is required')
    store = Store(args.data)
    with (args.data / '.worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.transaction() as tx:
            result = migrate(tx if args.apply else Transaction(store))
    print(json.dumps(dict(result, library_id=LIBRARY_ID, applied=args.apply), indent=2))


if __name__ == '__main__':
    main()
