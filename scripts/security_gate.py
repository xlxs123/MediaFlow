"""Fail-closed security workflow, SARIF and official CI-provenance gates."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = '.github/workflows/security.yml'
CHECKS = ('CodeQL / Python', 'Resolved Python dependencies',
    'Git history and staged secrets', 'Workflow security policy')
PROVENANCE_TYPE = 'https://slsa.dev/provenance/v1'
TRUSTED_PROVENANCE_CONDITION = "${{ github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}"
TRUSTED_SIGNPATH_CONDITION = "${{ github.event_name == 'workflow_dispatch' && inputs.signpath_request && github.ref == 'refs/heads/main' && github.repository == 'xlxs123/MediaFlow' }}"
_SIGNPATH_PREDICATE = TRUSTED_SIGNPATH_CONDITION[4:-3]
SIGNPATH_CONCURRENCY_GROUP = "${{ (" + _SIGNPATH_PREDICATE + ") && 'mediaflow-signpath-main' || format('mediaflow-ci-{0}-{1}', github.workflow, github.ref) }}"
SIGNPATH_CANCEL_CONDITION = "${{ !(" + _SIGNPATH_PREDICATE + ") }}"
SIGNPATH_ACTION = 'signpath/github-action-submit-signing-request@f6d04783b4569d051e0c80105fe66e82819d0092'


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


PUBLIC_METADATA_RULES = frozenset(('py/clear-text-storage-sensitive-data', 'py/clear-text-logging-sensitive-data'))
PUBLIC_METADATA_GUARDS = frozenset(('core/update_manifest.py', 'core/update_channel.py',
    'utils/build_info.py', 'assets/update-channel.json'))


def _review_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
        or any(ord(c) < 32 for c in value) or any(c in value for c in '*?[')
        or PurePosixPath(value).is_absolute() or any(part in ('', '.', '..') for part in value.split('/'))):
        raise SecurityGateError('CodeQL review requires exact repository-relative source paths.')
    return value


def codeql_primary(entry):
    location = next(iter(entry.get('locations', [])), {}).get('physicalLocation', {})
    return dict(path=_review_path(location.get('artifactLocation', {}).get('uri')),
        region=deepcopy(location.get('region', {})))


def _canonical_flow_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def codeql_flow_digest(entry):
    flows = deepcopy(entry.get('codeFlows', []))
    if not flows:
        return ''

    def normalize(value):
        if isinstance(value, dict):
            artifact = value.get('artifactLocation')
            if isinstance(artifact, dict):
                # Indexes refer to the SARIF artifact table, not source identity.
                # Drop them only when an explicit exact URI is also present.
                _review_path(artifact.get('uri'))
                artifact.pop('index', None)
            for child in value.values():
                normalize(child)
        elif isinstance(value, list):
            for child in value:
                normalize(child)
    normalize(flows)
    for flow in flows:
        flow['threadFlows'] = sorted(flow.get('threadFlows', []), key=_canonical_flow_json)
    flows.sort(key=_canonical_flow_json)
    return hashlib.sha256(_canonical_flow_json(flows).encode('utf-8')).hexdigest()


def _load_codeql_reviews(path):
    if not path.exists():
        return []
    if path.is_symlink() or path.is_junction() or not 0 < path.stat().st_size < 2 * 1024 * 1024:
        raise SecurityGateError('CodeQL public-metadata review document is invalid.')
    value = json.loads(path.read_text('utf-8'))
    if (set(value) != {'schema_version', 'canonicalization', 'reviews'} or value['schema_version'] != 1
        or value['canonicalization'] != 'codeql-complete-flow-v1' or not isinstance(value['reviews'], list)
        or len(value['reviews']) > 100):
        raise SecurityGateError('CodeQL public-metadata review schema is invalid.')
    required = {'rule_id', 'classification', 'primary_location', 'partial_fingerprints',
        'codeflows_sha256', 'codeflow_count', 'dependency_sha256', 'reason', 'evidence', 'reviewed_by'}
    seen = set()
    for review in value['reviews']:
        if (not isinstance(review, dict) or set(review) != required
            or review['rule_id'] not in PUBLIC_METADATA_RULES or review['classification'] != 'reviewed_public_metadata'
            or not isinstance(review['reason'], str) or len(review['reason']) < 20
            or not isinstance(review['reviewed_by'], str) or not review['reviewed_by'].strip()
            or type(review['codeflow_count']) is not int or not 0 < review['codeflow_count'] <= 100
            or not re.fullmatch('[0-9a-f]{64}', str(review['codeflows_sha256']))):
            raise SecurityGateError('CodeQL reviews may cover only exact proven public-metadata flows, never whole rules.')
        primary = review['primary_location']
        if not isinstance(primary, dict) or set(primary) != {'path', 'region'} or not isinstance(primary['region'], dict):
            raise SecurityGateError('CodeQL review lacks an exact primary location.')
        sink = _review_path(primary['path'])
        fingerprints = review['partial_fingerprints']
        if not isinstance(fingerprints, dict) or not fingerprints or any(
            not isinstance(k, str) or not isinstance(v, str) or not 0 < len(k) < 200 or not 0 < len(v) < 500
            for k, v in fingerprints.items()):
            raise SecurityGateError('CodeQL review requires the complete official partial fingerprints.')
        dependencies = review['dependency_sha256']
        if (not isinstance(dependencies, dict) or not PUBLIC_METADATA_GUARDS.issubset(dependencies)
            or sink not in dependencies or len(dependencies) > 50):
            raise SecurityGateError('CodeQL review must bind the sink, all sources and the public-key validators.')
        for name, digest in dependencies.items():
            if (not (_review_path(name).endswith('.py') or name == 'assets/update-channel.json')
                or not re.fullmatch('[0-9a-f]{64}', str(digest))):
                raise SecurityGateError('CodeQL review source identities are invalid.')
        evidence = review['evidence']
        if (not isinstance(evidence, dict) or not re.fullmatch('[0-9a-f]{64}', str(evidence.get('source_sarif_sha256', '')))
            or not re.fullmatch('[0-9a-f]{40}', str(evidence.get('source_commit', '')))
            or not re.fullmatch('[0-9]+', str(evidence.get('codeql_run', '')))):
            raise SecurityGateError('CodeQL review lacks traceable original scan evidence.')
        identity = _canonical_flow_json([review['rule_id'], primary, fingerprints, review['codeflows_sha256']])
        if identity in seen:
            raise SecurityGateError('Duplicate CodeQL public-metadata review.')
        seen.add(identity)
    return value['reviews']


def _reviewed_codeql_entry(entry, reviews, root):
    try:
        primary = codeql_primary(entry)
        digest = codeql_flow_digest(entry)
        sources = set()
        def collect(value):
            if isinstance(value, dict):
                if isinstance(value.get('artifactLocation'), dict):
                    sources.add(_review_path(value['artifactLocation'].get('uri')))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(entry.get('codeFlows', []))
        for review in reviews:
            if (entry.get('ruleId') != review['rule_id'] or primary != review['primary_location']
                or entry.get('partialFingerprints') != review['partial_fingerprints']
                or digest != review['codeflows_sha256'] or len(entry.get('codeFlows', [])) != review['codeflow_count']
                or not sources.issubset(review['dependency_sha256'])):
                continue
            valid = True
            for name, expected in review['dependency_sha256'].items():
                path = root / name
                if (any(part.is_symlink() or part.is_junction() for part in (path, *path.parents))
                    or not path.is_file() or not 0 < path.stat().st_size < 8 * 1024 * 1024):
                    valid = False
                    break
                with path.open('rb') as handle:
                    if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                        valid = False
                        break
            if valid:
                return True
    except (OSError, SecurityGateError, TypeError, ValueError):
        pass
    return False


def scan_sarif(directory, *, root=ROOT, reviews_path=None):
    root = Path(root)
    reviews = _load_codeql_reviews(Path(reviews_path) if reviews_path else root / 'security/codeql-reviews.json')
    paths = sorted(Path(directory).glob('*.sarif'))
    if not paths:
        raise SecurityGateError('CodeQL produced no SARIF; absence of evidence is not a passed scan.')
    findings, reviewed = [], []
    runs_seen = 0
    for path in paths:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise SecurityGateError('CodeQL SARIF exceeds the reviewed size bound.')
        document = json.loads(path.read_text('utf-8'))
        if document.get('version') != '2.1.0' or not document.get('runs'):
            raise SecurityGateError('CodeQL SARIF has no valid analysis runs.')
        for run in document['runs']:
            runs_seen += 1
            if 'diff-informed' in str(run.get('properties', {}).get('incrementalMode', '')).lower():
                raise SecurityGateError('Diff-informed SARIF cannot certify a complete repository security scan.')
            if any(invocation.get('executionSuccessful') is False for invocation in run.get('invocations', [])):
                raise SecurityGateError('CodeQL reported an unsuccessful analysis invocation.')
            for entry in run.get('results', []):
                location = next(iter(entry.get('locations', [])), {}).get('physicalLocation', {})
                uri = location.get('artifactLocation', {}).get('uri', '')
                # Preserve useful repository locations, never host absolute paths or messages.
                if uri.startswith(('/', '\\', 'file:')) or re.match(r'^[A-Za-z]:', uri):
                    uri = '<non-relative-location>'
                target = reviewed if _reviewed_codeql_entry(entry, reviews, root) else findings
                target.append(dict(rule_id=entry.get('ruleId', 'unknown'),
                    level=entry.get('level', 'warning'), path=uri,
                    line=location.get('region', {}).get('startLine')))
    return dict(passed=not findings, tool='GitHub CodeQL', runs=runs_seen,
        total_findings=len(findings) + len(reviewed), reviewed_public_metadata=len(reviewed),
        unresolved_findings=len(findings), blocking_policy='All unresolved findings block; only exact source-bound public metadata reviews, no ignored rules.',
        findings=findings, reviewed_findings=reviewed)


def _workflow_expressions(value, *, implicit=False):
    """Find expression boundaries without treating a quoted '}}' as the end."""
    expressions = []
    start = 0
    while (opening := value.find('${{', start)) != -1:
        cursor = opening + 3
        quoted = False
        while cursor < len(value):
            if value[cursor] == "'":
                if quoted and value[cursor:cursor + 2] == "''":
                    cursor += 2
                    continue
                quoted = not quoted
            elif not quoted and value[cursor:cursor + 2] == '}}':
                expressions.append((opening, cursor + 2, value[opening + 3:cursor]))
                start = cursor + 2
                break
            cursor += 1
        else:
            raise SecurityGateError('Unclosed workflow expression; credential scope cannot be verified.')
    if implicit and not expressions:
        expressions.append((0, len(value), value))
    return expressions


def _expression_tokens(source):
    # This is deliberately a lexer, not an evaluator for GitHub expressions.
    # Only the two complete, narrowly specified expressions below are allowed.
    return re.findall(r"'(?:[^']|'')*'|[A-Za-z_][A-Za-z_0-9-]*|!=|\S", source)


def _secret_access(tokens, index):
    """Return a fixed secret name/end offset, or None for opaque context access."""
    if tokens[index].casefold() != 'secrets' or (index and tokens[index - 1] == '.'):
        return None
    if (tokens[index + 1:index + 2] == ['.'] and index + 2 < len(tokens)
            and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9-]*', tokens[index + 2])):
        return tokens[index + 2], index + 3
    if (tokens[index + 1:index + 2] == ['['] and index + 3 < len(tokens)
            and tokens[index + 2].startswith("'") and tokens[index + 3] == ']'):
        return tokens[index + 2][1:-1].replace("''", "'"), index + 4
    return '', index + 1  # Dynamic indexing, wildcards and bare secrets fail closed.


def _signpath_expression_kind(value):
    """Accept only a whole scalar Secret lookup, or that lookup != empty string."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        expressions = _workflow_expressions(value)
    except SecurityGateError:
        return None
    if len(expressions) != 1 or expressions[0][:2] != (0, len(value)):
        return None
    tokens = _expression_tokens(expressions[0][2])
    access = _secret_access(tokens, 0) if tokens else None
    if access and access[0].casefold() == 'signpath_api_token':
        remaining = tokens[access[1]:]
        if not remaining:
            return 'value'
        if remaining == ['!=', "''"]:
            return 'presence'
    return None


def _workflow_credential_policy(workflow, value):
    """Check every parsed YAML field, including nested values and mapping keys.

    Fixed dot/literal-index lookups are understood; dynamic or whole-context
    secrets access is rejected because it may disclose the protected token.
    This policy does not claim to implement all GitHub expression semantics.
    """
    errors = []
    signing = value.get('jobs', {}).get('signpath', {}) if workflow == 'ci.yml' else {}
    steps = signing.get('steps', [])
    submits = [index for index, step in enumerate(steps) if step.get('id') == 'submit']
    sink = ('jobs', 'signpath', 'steps', submits[0], 'with', 'api-token') if len(submits) == 1 else None
    reviewed = sink is not None and steps[submits[0]].get('uses') == SIGNPATH_ACTION
    presence = ('jobs', 'signpath', 'env', 'SIGNPATH_TOKEN_PRESENT')

    def location(path):
        parts = []
        for index, part in enumerate(path):
            label = str(part)
            if index == 3 and path[0] == 'jobs' and path[2] == 'steps' and isinstance(part, int):
                job = value.get('jobs', {}).get(path[index - 2], {})
                step_id = job.get('steps', [])[part].get('id')
                if step_id:
                    label += '(id=' + str(step_id) + ')'
            parts.append(label)
        return workflow + '/' + '/'.join(parts)

    def visit(node, path=(), ancestors=()):
        if isinstance(node, (dict, list)):
            if id(node) in ancestors:
                errors.append(location(path) + ': recursive YAML cannot be checked for credential access')
                return
            ancestors = (*ancestors, id(node))
            for key, child in (node.items() if isinstance(node, dict) else enumerate(node)):
                if isinstance(node, dict):
                    visit(key, (*path, '<mapping-key>'), ancestors)
                visit(child, (*path, key), ancestors)
        elif isinstance(node, str):
            try:
                expressions = _workflow_expressions(node, implicit=bool(path and path[-1] == 'if'))
                accesses = [access for _, _, source in expressions
                    for tokens in [_expression_tokens(source)] for index in range(len(tokens))
                    if (access := _secret_access(tokens, index)) is not None
                    and (not access[0] or access[0].casefold() == 'signpath_api_token')]
                allowed = (workflow == 'ci.yml' and (
                    path == presence and _signpath_expression_kind(node) == 'presence' or
                    reviewed and path == sink and _signpath_expression_kind(node) == 'value'))
                if accesses and not allowed:
                    ordinary = len(path) >= 2 and path[0] == 'jobs' and path[1] != 'signpath'
                    errors.append(location(path) + ': ' + ('ordinary jobs cannot access the protected SignPath credential'
                        if ordinary else 'unreviewed SignPath credential reference or opaque secrets access'))
            except SecurityGateError:
                errors.append(location(path) + ': malformed expression; credential access cannot be verified')

    visit(value)
    if workflow == 'ci.yml':
        for path, scalar, kind, context in (
                (presence, signing.get('env', {}).get('SIGNPATH_TOKEN_PRESENT'), 'presence', True),
                (sink or ('jobs', 'signpath', 'steps', '<unique-submit>', 'with', 'api-token'),
                 steps[submits[0]].get('with', {}).get('api-token') if sink else None, 'value', reviewed)):
            try:
                valid = context and _signpath_expression_kind(scalar) == kind
            except SecurityGateError:
                valid = False
            if not valid:
                errors.append(location(path) + ': required credential ' + kind + ' expression or reviewed Action context is invalid')
    return errors


def workflow_policy(root=ROOT):
    import yaml  # Isolated, pinned security tool; no application runtime dependency.

    class PolicyLoader(yaml.BaseLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, child in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise SecurityGateError('ambiguous YAML mapping at line ' + str(key_node.start_mark.line + 1)
                        + '; credential references cannot be verified')
                result[key] = self.construct_object(child, deep=deep)
            return result

    root = Path(root)
    pins = json.loads((root / 'security/toolchain.json').read_text('utf-8'))['actions']
    errors = []
    count = 0
    workflows = {}
    workflow_directory = root / '.github/workflows'
    for path in sorted([*workflow_directory.glob('*.yml'), *workflow_directory.glob('*.yaml')]):
        try:
            value = yaml.load(path.read_text('utf-8'), Loader=PolicyLoader)
        except SecurityGateError as error:
            errors.append(path.name + ': ' + str(error))
            continue
        except yaml.YAMLError:
            errors.append(path.name + ': invalid YAML; credential references cannot be verified')
            continue
        if not isinstance(value, dict):
            errors.append(path.name + ': expected a workflow mapping; credential references cannot be verified')
            continue
        workflows[path.name] = value
        errors.extend(_workflow_credential_policy(path.name, value))
        if path.name != 'ci.yml' and 'mediaflow-signpath-main' in str(value.get('concurrency', '')).casefold():
            errors.append(path.name + ': the signing concurrency group is reserved for the opt-in CI run')
        if value.get('permissions') != {'contents': 'read'}:
            errors.append(path.name + ': workflow permissions must be contents:read only')
        if 'pull_request_target' in value.get('on', {}):
            errors.append(path.name + ': privileged untrusted-code trigger is forbidden')
        for key, job in value.get('jobs', {}).items():
            if path.name != 'ci.yml' and 'mediaflow-signpath-main' in str(job.get('concurrency', '')).casefold():
                errors.append(path.name + '/' + key + ': the signing concurrency group cannot be reused by another job')
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
                if (step['uses'].startswith('signpath/') and
                        (path.name != 'ci.yml' or key != 'signpath' or step.get('id') != 'submit')):
                    errors.append(path.name + '/' + key + ': SignPath is restricted to the opt-in protected job')
                count += 1
                match = re.fullmatch(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[A-Za-z0-9_./-]+)?@([0-9a-f]{40})', step['uses'])
                if not match or match[1] not in pins or match[2] != pins[match[1]]['commit']:
                    errors.append(path.name + '/' + key + ': Action is not the reviewed official immutable commit')
    security = workflows.get('security.yml', {})
    if security.get('concurrency') != {'group': 'mediaflow-security-${{ github.ref }}', 'cancel-in-progress': 'true'}:
        errors.append('security.yml: ordinary security CI must retain its independent cancelling concurrency group')
    triggers = security.get('on', {})
    if not all(name in triggers for name in ('push', 'pull_request', 'schedule', 'workflow_dispatch')):
        errors.append('security.yml: required continuous security triggers are missing')
    names = {job.get('name') for job in security.get('jobs', {}).values()}
    if security.get('jobs', {}).get('codeql', {}).get('env', {}).get('CODEQL_ACTION_DIFF_INFORMED_QUERIES') != 'false':
        errors.append('security.yml: CodeQL PR scans must disable diff-informed result filtering')
    if not set(CHECKS).issubset(names):
        errors.append('security.yml: required blocking security jobs are missing')
    ci = workflows.get('ci.yml', {})
    if ci.get('concurrency') != {'group': SIGNPATH_CONCURRENCY_GROUP,
            'cancel-in-progress': SIGNPATH_CANCEL_CONDITION}:
        errors.append('ci.yml: signing must use an isolated non-cancelling group while ordinary CI keeps cancellation')
    if any('concurrency' in job for job in ci.get('jobs', {}).values()):
        errors.append('ci.yml: unreviewed job concurrency can collide with the protected workflow signing group')
    provenance = ci.get('jobs', {}).get('provenance', {})
    if provenance.get('needs') != 'windows' or provenance.get('if') != TRUSTED_PROVENANCE_CONDITION:
        errors.append('ci.yml: provenance must depend on the verified build and trusted branch condition')
    if any(step.get('run') or step.get('uses', '').startswith('actions/checkout@') for step in provenance.get('steps', [])):
        errors.append('ci.yml: OIDC proof job may use only reviewed artifact/attestation Actions, no checkout or project commands')
    signing = ci.get('jobs', {}).get('signpath', {})
    if (signing.get('if') != TRUSTED_SIGNPATH_CONDITION or signing.get('needs') != ['windows', 'provenance']
            or signing.get('environment') != 'signpath-foundation' or signing.get('runs-on') != 'windows-2022'
            or signing.get('permissions') != {'contents': 'read', 'actions': 'read'}):
        errors.append('ci.yml: SignPath must keep the manual main-only protected Environment and verified build/provenance dependencies')
    steps = signing.get('steps', [])
    ids = [step.get('id') for step in steps]
    required = ['preflight', 'prepare', 'unsigned', 'submit', 'verify']
    if [name for name in ids if name in required] != required:
        errors.append('ci.yml: SignPath preflight/source/upload/request/native verification order is mandatory')
    submit = next((step for step in steps if step.get('id') == 'submit'), {})
    settings = submit.get('with', {})
    if (settings.get('github-artifact-id') != '${{ steps.unsigned.outputs.artifact-id }}'
            or settings.get('wait-for-completion') != 'true' or settings.get('skip-decompress') != 'true'
            or _signpath_expression_kind(settings.get('api-token')) != 'value'):
        errors.append('ci.yml: SignPath must use its own uploaded artifact, protected token and raw returned archive')
    option = ci.get('on', {}).get('workflow_dispatch', {}).get('inputs', {}).get('signpath_request', {})
    if option.get('type') != 'boolean' or option.get('default') != 'false':
        errors.append('ci.yml: SignPath signing must be explicitly requested and default off')
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
