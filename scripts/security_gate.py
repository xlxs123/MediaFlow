"""Fail-closed security workflow, SARIF and official CI-provenance gates."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = '.github/workflows/security.yml'
CHECKS = ('CodeQL / Python', 'Resolved Python dependencies',
    'Git history and staged secrets', 'Workflow security policy')
PROVENANCE_TYPE = 'https://slsa.dev/provenance/v1'
TRUSTED_PROVENANCE_CONDITION = "${{ github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}"


class SecurityGateError(RuntimeError):
    pass


def identity(repository, commit):
    if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', repository):
        raise SecurityGateError('Security gate requires an explicit repository identity.')
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise SecurityGateError('Security gate requires the complete source commit.')


def verify_security_run(client, repository, commit, *, default_branch='main', wait_seconds=1200,
                        now=None):
    """Latest exact-commit attempt wins; an earlier green run cannot mask a failure."""
    identity(repository, commit)
    deadline = time.monotonic() + max(0, min(wait_seconds, 1800))
    while True:
        response = client.api('repos/' + repository + '/actions/workflows/security.yml/runs?head_sha=' + commit + '&per_page=100')
        eligible = [run for run in response.get('workflow_runs', [])
            if run.get('head_sha') == commit and run.get('head_branch') == default_branch
            and run.get('head_repository', {}).get('full_name', '').casefold() == repository.casefold()
            and run.get('event') in ('push', 'workflow_dispatch', 'schedule')
            and run.get('path', '').split('@')[0] == WORKFLOW]
        if not eligible:
            raise SecurityGateError('No real security workflow exists for the exact default-branch commit; publication is blocked.')
        run = max(eligible, key=lambda value: int(value['id']))
        if run.get('status') == 'completed':
            if run.get('conclusion') != 'success':
                raise SecurityGateError('The latest exact-commit security workflow failed or was cancelled; publication is blocked.')
            break
        if time.monotonic() >= deadline:
            raise SecurityGateError('The exact-commit security workflow has not completed within the bounded wait.')
        time.sleep(min(10, max(.01, deadline - time.monotonic())))
    try:
        completed = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
        current = now or datetime.now(timezone.utc)
        age = current - completed
        if age > timedelta(days=8) or age < -timedelta(minutes=15):
            raise ValueError('stale or future')
    except (KeyError, TypeError, ValueError):
        raise SecurityGateError('Security evidence is stale or has an invalid timestamp; run the security workflow again.') from None
    attempt = run.get('run_attempt', 1)
    if type(attempt) is not int or not 0 < attempt < 1000:
        raise SecurityGateError('Security workflow attempt identity is invalid.')
    jobs = client.api('repos/' + repository + '/actions/runs/' + str(run['id']) +
        '/attempts/' + str(attempt) + '/jobs?per_page=100').get('jobs', [])
    for name in CHECKS:
        matching = [job for job in jobs if job.get('name') == name]
        if len(matching) != 1 or matching[0].get('conclusion') != 'success':
            raise SecurityGateError('A required security job is missing, skipped or failed: ' + name)
    return dict(passed=True, repository=repository, commit=commit, run_id=str(run['id']),
        run_attempt=attempt, url=run['html_url'], completed_at=run['updated_at'],
        checks=list(CHECKS), scope='Latest real exact-commit default-branch security workflow; no bypass switch.')


def verify_ci_provenance(client, repository, commit, run_id, path, *, default_branch='main'):
    """The official CLI verifies Sigstore/OIDC; metadata policy is additional."""
    identity(repository, commit)
    if not re.fullmatch(r'[1-9][0-9]{0,23}', str(run_id)):
        raise SecurityGateError('CI provenance requires the actual workflow run ID.')
    path = Path(path).absolute()
    if not path.is_file() or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise SecurityGateError('CI provenance subject must be an ordinary existing file.')
    with path.open('rb') as handle:
        expected = hashlib.file_digest(handle, 'sha256').hexdigest()
    command = [client.gh, 'attestation', 'verify', path, '--repo', repository,
        '--signer-workflow', repository + '/.github/workflows/ci.yml',
        '--source-ref', 'refs/heads/' + default_branch, '--source-digest', commit,
        '--deny-self-hosted-runners', '--predicate-type', PROVENANCE_TYPE, '--format', 'json']
    code, output, _ = client.runner.call(command, 'github-provenance-verification', timeout=180,
        env=client.env, logs=False, allow_failure=True)
    if code or len(output) > 4 * 1024 * 1024:
        raise SecurityGateError('Official cryptographic build provenance verification failed; signing and publication are blocked.')
    try:
        verified = json.loads(output)
    except (ValueError, UnicodeError):
        raise SecurityGateError('Official provenance verifier returned invalid JSON.') from None
    accepted = []
    for item in verified if isinstance(verified, list) else []:
        result = item.get('verificationResult', {})
        statement = result.get('statement', {})
        if statement.get('predicateType') != PROVENANCE_TYPE:
            continue
        subjects = statement.get('subject', [])
        if not any(subject.get('digest', {}).get('sha256') == expected and
            subject.get('name', '').replace('\\', '/').split('/')[-1] == path.name for subject in subjects):
            continue
        invocation = statement.get('predicate', {}).get('runDetails', {}).get('metadata', {}).get('invocationId', '')
        expected_invocation = 'https://github.com/' + repository + '/actions/runs/' + str(run_id)
        if invocation != expected_invocation and not re.fullmatch(re.escape(expected_invocation) + r'/attempts/[1-9][0-9]*', invocation):
            continue
        if not result.get('signature', {}).get('certificate') or not result.get('verifiedTimestamps'):
            continue
        accepted.append(invocation)
    with path.open('rb') as handle:
        unchanged = hashlib.file_digest(handle, 'sha256').hexdigest() == expected
    if not accepted or not unchanged:
        raise SecurityGateError('Verified provenance does not bind this exact file, commit and CI run, or the subject changed.')
    return dict(passed=True, cryptographically_verified=True, verified_by='GitHub CLI attestation verify',
        subject=path.name, sha256=expected, repository=repository, commit=commit, run_id=str(run_id),
        signer_workflow='.github/workflows/ci.yml', source_ref='refs/heads/' + default_branch,
        predicate_type=PROVENANCE_TYPE, invocations=accepted)


def scan_sarif(directory):
    paths = sorted(Path(directory).glob('*.sarif'))
    if not paths:
        raise SecurityGateError('CodeQL produced no SARIF; absence of evidence is not a passed scan.')
    findings = []
    runs_seen = 0
    for path in paths:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise SecurityGateError('CodeQL SARIF exceeds the reviewed size bound.')
        document = json.loads(path.read_text('utf-8'))
        if document.get('version') != '2.1.0' or not document.get('runs'):
            raise SecurityGateError('CodeQL SARIF has no valid analysis runs.')
        for run in document['runs']:
            runs_seen += 1
            if any(invocation.get('executionSuccessful') is False for invocation in run.get('invocations', [])):
                raise SecurityGateError('CodeQL reported an unsuccessful analysis invocation.')
            for entry in run.get('results', []):
                location = next(iter(entry.get('locations', [])), {}).get('physicalLocation', {})
                uri = location.get('artifactLocation', {}).get('uri', '')
                # Preserve useful repository locations, never host absolute paths or messages.
                if uri.startswith(('/', '\\', 'file:')) or re.match(r'^[A-Za-z]:', uri):
                    uri = '<non-relative-location>'
                findings.append(dict(rule_id=entry.get('ruleId', 'unknown'),
                    level=entry.get('level', 'warning'), path=uri,
                    line=location.get('region', {}).get('startLine')))
    return dict(passed=not findings, tool='GitHub CodeQL', runs=runs_seen,
        unresolved_findings=len(findings), blocking_policy='All unresolved CodeQL security findings block; no ignored rules.',
        findings=findings)


def workflow_policy(root=ROOT):
    import yaml  # Isolated, pinned security tool; no application runtime dependency.
    root = Path(root)
    pins = json.loads((root / 'security/toolchain.json').read_text('utf-8'))['actions']
    errors = []
    count = 0
    workflow_directory = root / '.github/workflows'
    for path in sorted([*workflow_directory.glob('*.yml'), *workflow_directory.glob('*.yaml')]):
        value = yaml.load(path.read_text('utf-8'), Loader=yaml.BaseLoader)
        if value.get('permissions') != {'contents': 'read'}:
            errors.append(path.name + ': workflow permissions must be contents:read only')
        if 'pull_request_target' in value.get('on', {}):
            errors.append(path.name + ': privileged untrusted-code trigger is forbidden')
        for key, job in value.get('jobs', {}).items():
            if job.get('uses'):
                errors.append(path.name + '/' + key + ': reusable workflow is not in the reviewed Action policy')
            permissions = job.get('permissions', {})
            privileged = {name: mode for name, mode in permissions.items() if mode == 'write'}
            expected = ({'security-events': 'write'} if path.name == 'security.yml' and key == 'codeql' else
                {'id-token': 'write', 'attestations': 'write'} if path.name == 'ci.yml' and key == 'provenance' else {})
            if privileged != expected:
                errors.append(path.name + '/' + key + ': unreviewed write permissions')
            if job.get('continue-on-error') not in (None, 'false'):
                errors.append(path.name + '/' + key + ': security/build failures cannot be ignored')
            for step in job.get('steps', []):
                if step.get('continue-on-error') not in (None, 'false'):
                    errors.append(path.name + '/' + key + ': step failure cannot be ignored')
                if 'uses' not in step:
                    continue
                count += 1
                match = re.fullmatch(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[A-Za-z0-9_./-]+)?@([0-9a-f]{40})', step['uses'])
                if not match or match[1] not in pins or match[2] != pins[match[1]]['commit']:
                    errors.append(path.name + '/' + key + ': Action is not the reviewed official immutable commit')
    security = yaml.load((root / '.github/workflows/security.yml').read_text('utf-8'), Loader=yaml.BaseLoader)
    triggers = security.get('on', {})
    if not all(name in triggers for name in ('push', 'pull_request', 'schedule', 'workflow_dispatch')):
        errors.append('security.yml: required continuous security triggers are missing')
    names = {job.get('name') for job in security.get('jobs', {}).values()}
    if not set(CHECKS).issubset(names):
        errors.append('security.yml: required blocking security jobs are missing')
    ci = yaml.load((root / '.github/workflows/ci.yml').read_text('utf-8'), Loader=yaml.BaseLoader)
    provenance = ci.get('jobs', {}).get('provenance', {})
    if provenance.get('needs') != 'windows' or provenance.get('if') != TRUSTED_PROVENANCE_CONDITION:
        errors.append('ci.yml: provenance must depend on the verified build and trusted branch condition')
    if any(step.get('run') or step.get('uses', '').startswith('actions/checkout@') for step in provenance.get('steps', [])):
        errors.append('ci.yml: OIDC proof job may use only reviewed artifact/attestation Actions, no checkout or project commands')
    return dict(passed=not errors, checked_actions=count, errors=errors,
        pinned_actions=list(pins), policy='Immutable official Actions; scoped writes; no privileged PR execution or bypass.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    sarif = sub.add_parser('sarif')
    sarif.add_argument('--directory', required=True)
    sarif.add_argument('--output', required=True)
    workflows = sub.add_parser('workflows')
    workflows.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise SystemExit('Existing security evidence is preserved; use a new output path.')
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = scan_sarif(args.directory) if args.operation == 'sarif' else workflow_policy()
    except Exception as error:
        result = dict(passed=False, error_type=type(error).__name__, error=str(error)
            if isinstance(error, SecurityGateError) else 'Security analysis failed; no successful check is claimed.')
    with output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: result[key] for key in ('passed', 'error_type', 'unresolved_findings', 'checked_actions') if key in result}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
