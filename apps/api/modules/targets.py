"""Immutable Lean propositions registered independently of proof submissions."""
import re, time
from fastapi import HTTPException
from apps.api.infrastructure.store import digest
from apps.api.modules.core import need


def get_target(tx, insight_id):
    return tx.get('insight_targets', insight_id)


def register_target(tx, insight, body):
    from apps.api.modules.lean import POLICY, TOOLCHAIN, LIBRARY_ID, validate_source
    if get_target(tx, insight['id']):
        raise HTTPException(409, 'This insight already has an immutable Lean target. Correct the statement by submitting a superseding insight.')
    dependencies = body.get('dependencies', [])
    if len(dependencies) != len(set(dependencies)):
        raise HTTPException(422, 'Duplicate Lean target dependencies')
    artifacts = [need(tx, 'lean_artifacts', ident) for ident in dependencies]
    for artifact in artifacts:
        if artifact['insight_id'] == insight['id']:
            raise HTTPException(422, 'A Lean target cannot depend on a proof artifact of the same insight')
        verification = need(tx, 'verifications', artifact['verification_id'])
        if (verification.get('status') != 'passed' or verification.get('policy') != POLICY
                or artifact.get('library_id') != LIBRARY_ID):
            raise HTTPException(422, 'Lean target dependencies must be verified in the current environment')
    definitions, expression = body.get('definitions', ''), body['expression']
    validate_source(definitions, artifacts)
    validate_source(expression, artifacts)
    immutable = dict(target_format='insight-target-v1', insight_id=insight['id'], definitions=definitions, expression=expression,
                     dependencies=dependencies,
                     dependency_snapshot=[dict(id=a['id'], content_hash=a['content_hash']) for a in artifacts],
                     toolchain=TOOLCHAIN, library_id=LIBRARY_ID, policy=POLICY)
    target = dict(immutable, id=insight['id'], target_hash=digest(immutable), status='queued',
                  target_module='MCTargets.'+insight['id'],
                  target_constant='MCTargets.'+insight['id']+'.statement',
                  created_at=time.time())
    tx.put('insight_targets', target)
    return target


def target_proof_matches(artifact, verification, target):
    """A compiler success alone is not a proof of the registered proposition."""
    from apps.api.modules.lean import POLICY, LIBRARY_ID
    if not target or target.get('status') != 'valid':
        return False
    snapshot = artifact.get('target') or {}
    return bool(target.get('target_hash') and snapshot.get('target_hash') == target['target_hash']
                and verification.get('target_hash') == target['target_hash']
                and verification.get('target_matched') is True
                and verification.get('status') == 'passed'
                and verification.get('policy') == POLICY
                and artifact.get('policy') == POLICY
                and artifact.get('library_id') == LIBRARY_ID
                and target.get('library_id') == LIBRARY_ID)


def validate_review_alignment(tx, insight_id, body):
    target = get_target(tx, insight_id)
    expected_hash = body.get('target_hash')
    if expected_hash and (not target or expected_hash != target['target_hash']):
        raise HTTPException(409, 'The review does not match the registered Lean target')
    if target and body.get('target_alignment', 'not_applicable') == 'not_applicable':
        raise HTTPException(422, 'Review the registered Lean target and set target_alignment to aligned, misaligned or uncertain')
    body['target_hash'] = target['target_hash'] if target else None
    return body


def target_source(target):
    """Generate the shared, separately compiled proposition module."""
    definitions = target.get('definitions', '').splitlines()
    imports = [line.strip() for line in definitions if re.match(r'^\s*import\b', line)]
    body = '\n'.join(line for line in definitions if not re.match(r'^\s*import\b', line))
    return ('import Lean\n' + '\n'.join('import MC.'+ident for ident in target.get('dependencies', []))
            + '\n' + '\n'.join(imports) + '\nnamespace ' + target['target_module'] + '\n'
            + body + '\ndef statement : Prop := (' + target['expression'] + ')\n'
            + 'end ' + target['target_module'] + '\n')
