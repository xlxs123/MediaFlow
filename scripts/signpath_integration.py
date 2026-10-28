"""Opt-in SignPath bridge. No account creation, secret persistence or publishing.

The source build keeps its original receipt/SBOM/provenance. A returned signed
tree needs independent native verification and an explicit byte derivation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))

from scripts.release_security import (FIRST_PARTY, read_document, save_new,
    signature_only_change, authenticode, signing_derivation)

ACTION_COMMIT = 'f6d04783b4569d051e0c80105fe66e82819d0092'
REPOSITORY = 'xlxs123/MediaFlow'
POLICY = ROOT / 'security/signpath-policy.json'


class SignPathError(RuntimeError):
    pass


def load_policy(path):
    """Preflight must work before Qt/application dependencies are installed."""
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SignPathError('Reviewed SignPath policy cannot be a linked path.')
    if not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024:
        raise SignPathError('Reviewed SignPath policy is absent or exceeds its bound.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SignPathError('Duplicate policy fields are refused.')
            result[key] = value
        return result
    value = json.loads(path.read_text('utf-8'), object_pairs_hook=pairs)
    if type(value) is not dict:
        raise SignPathError('Reviewed SignPath policy must be a JSON object.')
    return value


def validate_policy(value, *, request=False):
    required = {'schema_version', 'product', 'enabled', 'foundation_approved',
        'license_review_accepted', 'organization_id', 'project_slug', 'signing_policy_slug',
        'artifact_configuration_slug', 'certificate_thumbprint', 'certificate_subject',
        'action_commit', 'environment'}
    if (set(value) != required or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['product'] != 'MediaFlow' or value['action_commit'] != ACTION_COMMIT
            or value['environment'] != 'signpath-foundation'
            or any(type(value[key]) is not bool for key in ('enabled', 'foundation_approved', 'license_review_accepted'))):
        raise SignPathError('The reviewed SignPath policy has invalid fields or types.')
    if not value['enabled'] and not request:
        return {'configured': False, 'request_submitted': False, 'formal_signed': False,
            'production_ready': False, 'state': 'not_enabled'}
    if not all(value[key] for key in ('enabled', 'foundation_approved', 'license_review_accepted')):
        raise SignPathError('SignPath is not enabled/approved; resolve the license review and obtain Foundation approval first.')
    try:
        if str(uuid.UUID(value['organization_id'])) != value['organization_id']:
            raise ValueError('not canonical')
    except (ValueError, TypeError, AttributeError):
        raise SignPathError('A reviewed real SignPath organization ID is required.') from None
    for key in ('project_slug', 'signing_policy_slug', 'artifact_configuration_slug'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', value[key]):
            raise SignPathError('A reviewed SignPath project/policy/artifact configuration is required.')
    if not isinstance(value['certificate_thumbprint'], str) or not re.fullmatch(r'[0-9A-F]{40}', value['certificate_thumbprint']):
        raise SignPathError('An exact reviewed public signer thumbprint is required; no arbitrary trusted signer is accepted.')
    subject = value['certificate_subject']
    if not isinstance(subject, str) or not 1 <= len(subject) <= 512 or any(ord(c) < 32 for c in subject):
        raise SignPathError('The actual reviewed certificate subject is required; do not assume a personal publisher name.')
    return {'configured': True, 'state': 'configured_not_signed', 'request_submitted': False,
        'formal_signed': False, 'production_ready': False}


def request_context(value, *, repository, ref, event, token_present):
    validate_policy(value, request=True)
    if repository != REPOSITORY or ref != 'refs/heads/main' or event != 'workflow_dispatch':
        raise SignPathError('Signing is restricted to an explicit default-branch dispatch in the source repository.')
    if token_present is not True:
        raise SignPathError('SIGNPATH_API_TOKEN is absent from the protected signpath-foundation environment.')


def inspect_archive(path, unsigned):
    """Only three exact first-party PEs; never extract supplier-controlled paths."""
    if not 0 < Path(path).stat().st_size <= 600 * 1024 * 1024:
        raise SignPathError('Returned signing archive has an invalid size.')
    result = {}
    seen = set()
    total = 0
    with zipfile.ZipFile(path) as archive:
        if not 3 <= len(archive.infolist()) <= 5:
            raise SignPathError('Unexpected signing archive member count.')
        for entry in archive.infolist():
            name = entry.filename
            if (name.casefold() in seen or entry.flag_bits & 1
                    or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or entry.external_attr & 0x400):
                raise SignPathError('Duplicate, encrypted or linked signing artifact refused.')
            seen.add(name.casefold())
            if entry.is_dir():
                if name not in ('tools/', 'tools/webview2/'):
                    raise SignPathError('Unexpected signing directory.')
                continue
            if name not in FIRST_PARTY:
                raise SignPathError('Only the three exact first-party paths can be returned by SignPath.')
            total += entry.file_size
            if not 0 < entry.file_size <= 600 * 1024 * 1024 or total > 1024 * 1024 * 1024:
                raise SignPathError('Returned PE exceeds the bounded artifact size.')
            data = archive.read(entry)
            signature_only_change((Path(unsigned) / name).read_bytes(), data)
            result[name] = data
    if set(result) != set(FIRST_PARTY):
        raise SignPathError('Incomplete first-party signing result.')
    return result


def validate_native_metadata(release, version):
    from scripts.native_sbom import pe_metadata
    for name in FIRST_PARTY:
        value = pe_metadata((Path(release) / name).read_bytes())['version_resources']
        if value.get('ProductName') != 'MediaFlow' or value.get('ProductVersion') != version:
            raise SignPathError('All signed first-party PEs must declare MediaFlow and the same actual build version.')


def bind_sbom_receipt(inventory, generation, receipt, receipt_sha256):
    """Independent valid attestations must still describe the same build bytes."""
    files = inventory.get('release_files')
    expected = receipt.get('files')
    if (generation.get('receipt_sha256') != receipt_sha256
            or generation.get('github') != receipt.get('github')
            or type(files) is not list or type(expected) is not list or not expected
            or type(generation.get('release_files')) is not int
            or generation['release_files'] != len(expected) or len(files) != len(expected)):
        raise SignPathError('SBOM generation and exact verified receipt do not describe the same build.')
    try:
        if sorted(files, key=lambda row: row['path']) != sorted(expected, key=lambda row: row['path']):
            raise ValueError('different byte inventory')
    except (TypeError, KeyError, ValueError):
        raise SignPathError('SBOM file paths/sizes/SHA-256 must exactly match the verified complete receipt.') from None


def verified_source(bundle, sbom, work, *, client, commit, run_id):
    from scripts.ci_build_receipt import verify_receipt
    from scripts.security_gate import verify_security_run, verify_ci_provenance
    from scripts.validate_sbom import validate_documents
    from utils.branding import APP_VERSION
    release = bundle / 'dist-mediaflow-ci'
    receipt = bundle / 'verification/ci/build-attestation.json'
    result = verify_receipt(receipt, release, expected_version=APP_VERSION,
        expected_repository=REPOSITORY, expected_commit=commit, expected_run_id=run_id)
    security = verify_security_run(client, REPOSITORY, commit, wait_seconds=0)
    subjects = [receipt, release / 'MediaFlow.exe'] + [sbom / name for name in
        ('mediaflow.cdx.json', 'inventory.json', 'generation.json', 'validation.json')]
    proofs = [verify_ci_provenance(client, REPOSITORY, commit, run_id, path) for path in subjects]
    # Independent existing validator, not a statement that a JSON receipt is a signature.
    names = ('mediaflow.cdx.json', 'inventory.json', 'generation.json')
    documents = [read_document(sbom / name) for name in names]
    bind_sbom_receipt(documents[1], documents[2], read_document(receipt), result['receipt_sha256'])
    hashes = {name: hashlib.sha256((sbom / name).read_bytes()).hexdigest() for name in names}
    validation = validate_documents(*documents, document_hashes=hashes, require_native=True)
    validate_native_metadata(release, APP_VERSION)
    save_new(work / 'verified-source.json', {'passed': True, 'receipt': result,
        'security': security, 'official_provenance': proofs, 'sbom': validation,
        'formal_signed': False, 'production_ready': False})
    return release, receipt


def prepare(release, output, *, expected_files):
    from scripts.package_update import _plain
    release, output = _plain(release).resolve(), _plain(output).resolve()
    if any(name not in expected_files for name in FIRST_PARTY):
        raise SignPathError('Submission requires the original verified receipt for every first-party PE.')
    if output.exists() or output.is_relative_to(release) or release.is_relative_to(output):
        raise SignPathError('Use a new non-overlapping unsigned artifact directory.')
    output.mkdir()
    for name in FIRST_PARTY:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_plain(release / name), target)
        record = expected_files[name]
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if target.stat().st_size != record['size'] or digest != record['sha256']:
            raise SignPathError('Copied signing input differs from its verified receipt; submission is blocked.')


def complete(policy, release, receipt, signed_archive, output, work):
    from scripts.package_update import _plain, _inventory
    validate_policy(policy, request=True)
    release, output, signed_archive = map(lambda p: _plain(p).resolve(), (release, output, signed_archive))
    if output.exists() or output.is_relative_to(release) or release.is_relative_to(output) or signed_archive.is_relative_to(output):
        raise SignPathError('Signed release must be a new independent static tree.')
    with signed_archive.open('rb') as stream:
        archive_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    payloads = inspect_archive(signed_archive, release)
    files = _inventory(release)
    output.mkdir()
    for name in files:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            if name in payloads:
                stream.write(payloads[name])
            else:
                with (release / name).open('rb') as source:
                    shutil.copyfileobj(source, stream)
    report = authenticode('Verify', work / 'authenticode-signed.json', release=output,
        options={'thumbprint': policy['certificate_thumbprint'], 'store': 'CurrentUser'})
    if report['certificate']['subject'] != policy['certificate_subject']:
        raise SignPathError('The actual certificate subject differs from the reviewed SignPath identity.')
    derivation = signing_derivation(release, output, receipt, report, work / 'signing-derivation.json')
    with signed_archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != archive_sha:
            raise SignPathError('The returned archive changed during independent verification.')
    save_new(work / 'signpath-result.json', {'passed': True, 'formal_signed': True,
        'signed_archive_sha256': archive_sha, 'original_ci_receipt_unchanged': True,
        'third_party_unchanged': derivation['third_party_files_unchanged'],
        'production_ready': False, 'published': False,
        'scope': 'Verified signed static tree only. Final ZIP, derived SBOM and Ed25519 publication gates remain mandatory.'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('status', 'preflight', 'prepare', 'complete'))
    parser.add_argument('--policy', default=str(POLICY))
    for name in ('bundle', 'sbom', 'work', 'output', 'signed-archive'):
        parser.add_argument('--' + name)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.operation == 'status':
            print(json.dumps(validate_policy(policy)))
            return 0
        request_context(policy, repository=os.environ.get('GITHUB_REPOSITORY'),
            ref=os.environ.get('GITHUB_REF'), event=os.environ.get('GITHUB_EVENT_NAME'),
            token_present=os.environ.get('SIGNPATH_TOKEN_PRESENT') == 'true')
        if args.operation == 'preflight':
            print(json.dumps({'passed': True, 'state': 'preflight_only', 'request_submitted': False,
                'formal_signed': False, 'production_ready': False}))
            return 0
        if not all(getattr(args, name) for name in ('bundle', 'sbom', 'work', 'output')):
            raise SignPathError('Validated build, SBOM, work and new output paths are required.')
        work = Path(args.work).resolve()
        work.mkdir(exist_ok=False)
        from scripts.release_pipeline import GitHub, Runner
        client = GitHub(Runner(ROOT, work), shutil.which('gh'), shutil.which('git'))
        release, receipt = verified_source(Path(args.bundle).resolve(), Path(args.sbom).resolve(), work,
            client=client, commit=os.environ.get('GITHUB_SHA'), run_id=os.environ.get('GITHUB_RUN_ID'))
        if args.operation == 'prepare':
            records = {row['path']: row for row in read_document(receipt)['files']}
            prepare(release, Path(args.output), expected_files=records)
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                for key in ('organization_id', 'project_slug', 'signing_policy_slug', 'artifact_configuration_slug'):
                    stream.write(key + '=' + policy[key] + '\n')
        else:
            if not args.signed_archive:
                raise SignPathError('A downloaded raw signing ZIP is required.')
            complete(policy, release, receipt, args.signed_archive, Path(args.output), work)
        return 0
    except Exception as error:
        print(json.dumps({'passed': False, 'formal_signed': False, 'production_ready': False,
            'error_type': type(error).__name__, 'error': str(error) if isinstance(error, SignPathError)
            else 'SignPath integration failed closed; no release was published and evidence is preserved.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
