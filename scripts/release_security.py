"""Local production signing gates and an explicit unsigned-to-signed derivation.

GitHub build provenance, Authenticode and publisher Ed25519 signatures have
different subjects. Never rewrite an original CI receipt after signing a PE.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
from urllib.parse import urlsplit
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))
FIRST_PARTY = ('MediaFlow.exe', 'tools/MediaFlow.Recycle.exe',
               'tools/webview2/MediaFlow.WebView2Host.exe')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_THUMBPRINT = re.compile(r'[0-9A-Fa-f]{40}\Z')


class ReleaseSecurityError(RuntimeError):
    pass


def digest(path):
    from scripts.package_update import _plain
    with _plain(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_document(path):
    from scripts.package_update import _plain
    path = _plain(path)
    if not path.is_file() or not 0 < path.stat().st_size < 32 * 1024 * 1024:
        raise ReleaseSecurityError('Required release-security evidence is absent or exceeds its bound.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseSecurityError('Duplicate fields in release-security evidence.')
            result[key] = value
        return result
    value = json.loads(path.read_text('utf-8-sig'), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ReleaseSecurityError('Release-security evidence must be an object.')
    return value


def save_new(path, value):
    from scripts.validate_sbom import public_strings
    public_strings(value)
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def production_options(thumbprint='', store='CurrentUser', timestamp_url='', sign_tool=''):
    if not isinstance(thumbprint, str) or not _THUMBPRINT.fullmatch(thumbprint):
        raise ReleaseSecurityError('Authenticode 未配置：需要已安装的正式代码签名证书拇指纹；不会使用测试证书替代。')
    if store not in ('CurrentUser', 'LocalMachine'):
        raise ReleaseSecurityError('Authenticode certificate store must be CurrentUser or LocalMachine.')
    try:
        address = urlsplit(timestamp_url)
        valid = (address.scheme == 'https' and bool(address.hostname) and not address.username
                 and not address.password and not address.query and not address.fragment
                 and not any(ord(char) < 33 for char in timestamp_url))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ReleaseSecurityError('Authenticode 未配置：需要明确的 HTTPS RFC 3161 时间戳服务。')
    return dict(thumbprint=thumbprint.upper(), store=store,
                timestamp_url=timestamp_url, sign_tool=str(sign_tool or ''))


def official_publisher_key(keys, key_id):
    """An arbitrary test root cannot exempt a key actually pinned by the app."""
    from core.update_channel import load_channel
    selected = keys.get(key_id, {})
    public = selected.get('public_key')
    return bool(public and any(item.get('algorithm') == 'ed25519'
        and item.get('public_key') == public for item in load_channel().get('trusted_keys', {}).values()))


def authenticode(operation, report, *, release=None, output=None, mode='Production',
                 options=None, pwsh=None, runner=None, stage='authenticode'):
    if operation not in ('Inspect', 'Sign', 'Verify') or mode not in ('CI', 'Test', 'Production'):
        raise ReleaseSecurityError('Unsupported Authenticode operation or trust mode.')
    options = options or {}
    executable = pwsh or shutil.which('pwsh')
    if not executable:
        raise ReleaseSecurityError('PowerShell 7 is required for native Windows Authenticode verification.')
    report = Path(report)
    if report.exists() or not report.parent.is_dir():
        raise ReleaseSecurityError('Use a new Authenticode report outside preserved evidence.')
    command = [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
        str(ROOT / 'scripts/authenticode.ps1'), '-Operation', operation, '-Mode', mode,
        '-Report', str(report)]
    if release is not None:
        command += ['-ReleaseDirectory', str(release)]
    if output is not None:
        command += ['-OutputDirectory', str(output)]
    for argument, value in (('-CertificateThumbprint', options.get('thumbprint')),
        ('-CertificateStore', options.get('store')), ('-TimestampUrl', options.get('timestamp_url')),
        ('-SignToolPath', options.get('sign_tool'))):
        if value:
            command += [argument, str(value)]
    if runner:
        code, _, _ = runner(command, stage, timeout=900, logs=False, allow_failure=True)
    else:
        environment = {key: value for key, value in os.environ.items()
            if key.upper() not in ('GH_TOKEN', 'GITHUB_TOKEN')
            and not (key.upper().startswith('MEDIAFLOW_') and any(word in key.upper()
                     for word in ('PASSWORD', 'PRIVATE_KEY', 'SECRET')))}
        try:
            process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=900, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=environment)
            code = process.returncode
        except (OSError, subprocess.TimeoutExpired):
            raise ReleaseSecurityError('Bounded native Authenticode operation failed; publication is blocked.') from None
    result = read_document(report)
    if code or result.get('passed') is not True or result.get('mode') != mode:
        raise ReleaseSecurityError('Authenticode verification or certificate configuration failed; publication is blocked.')
    if mode == 'Production' and operation in ('Sign', 'Verify') and result.get('formal_verified') is not True:
        raise ReleaseSecurityError('The actual first-party PE files have not passed formal Authenticode verification.')
    return result


def _pe_signature_layout(data):
    """Offsets defined by the Microsoft PE/COFF specification, not guessed RVAs."""
    try:
        if data[:2] != b'MZ':
            raise ValueError
        pe = struct.unpack_from('<I', data, 0x3c)[0]
        if pe < 64 or data[pe:pe + 4] != b'PE\0\0':
            raise ValueError
        optional = pe + 24
        size = struct.unpack_from('<H', data, pe + 20)[0]
        magic = struct.unpack_from('<H', data, optional)[0]
        offset = {0x10b: 96, 0x20b: 112}[magic]
        count = struct.unpack_from('<I', data, optional + offset - 4)[0]
        security = optional + offset + 8 * 4
        checksum = optional + 64
        if count < 5 or optional + size > len(data) or security + 8 > optional + size:
            raise ValueError
        position, length = struct.unpack_from('<II', data, security)
        if bool(position) != bool(length):
            raise ValueError
        return checksum, security, position, length
    except (KeyError, ValueError, struct.error):
        raise ReleaseSecurityError('Unsupported PE header; signature-only derivation cannot be proved.') from None


def signature_only_change(before, after):
    """Allow only checksum/security-table edits plus an appended WIN_CERTIFICATE.

    This is a byte-preservation proof, not cryptographic signature verification.
    The separate Windows/SignTool checks remain mandatory for production.
    """
    old = _pe_signature_layout(before)
    new = _pe_signature_layout(after)
    if old[:2] != new[:2] or old[2:] != (0, 0):
        raise ReleaseSecurityError('Signing requires an unsigned, unchanged CI first-party PE.')
    checksum, security, position, length = new
    if (position % 8 or length < 8 or position < len(before) or position - len(before) > 7
            or position + length != len(after) or any(after[len(before):position])):
        raise ReleaseSecurityError('Signed PE has unexpected overlays or certificate-table bounds.')
    certificate_length, revision, kind = struct.unpack_from('<IHH', after, position)
    if (certificate_length < 8 or certificate_length > length or revision != 0x200
            or kind != 2 or (certificate_length + 7) // 8 * 8 != length):
        raise ReleaseSecurityError('Signed PE does not contain one bounded PKCS#7 WIN_CERTIFICATE.')
    previous, signed = bytearray(before), bytearray(after[:len(before)])
    for offset, count in ((checksum, 4), (security, 8)):
        previous[offset:offset + count] = bytes(count)
        signed[offset:offset + count] = bytes(count)
    if previous != signed:
        raise ReleaseSecurityError('Signing changed executable payload bytes outside permitted Authenticode fields.')
    return dict(unsigned_sha256=hashlib.sha256(before).hexdigest(),
        signed_sha256=hashlib.sha256(after).hexdigest(), certificate_bytes=length,
        executable_payload_unchanged=True,
        scope='Byte preservation only; native production certificate, trust and timestamp checks are separate.')


def first_party_change(name, before, after):
    """Keep ordinary PE signing and main-program archive selection separate."""
    if name not in FIRST_PARTY:
        raise ReleaseSecurityError('Unexpected first-party signing path.')
    change = signature_only_change(before, after)
    if name == 'MediaFlow.exe':
        from scripts.pyinstaller_payload import PayloadError, archive_unchanged
        try:
            change['pyinstaller_payload'] = archive_unchanged(before, after)
        except PayloadError as error:
            raise ReleaseSecurityError(str(error)) from None
    return change


def signing_derivation(unsigned_release, signed_release, unsigned_receipt, auth_report, output):
    from scripts.package_update import _plain, _inventory
    source, target = _plain(unsigned_release).resolve(), _plain(signed_release).resolve()
    document = read_document(unsigned_receipt)
    records = {item['path']: item for item in document['files']}
    if set(_inventory(source)) != set(records) or set(_inventory(target)) != set(records):
        raise ReleaseSecurityError('Signing changed the complete release inventory.')
    if auth_report.get('mode') != 'Production' or auth_report.get('formal_verified') is not True:
        raise ReleaseSecurityError('A test or unsigned Authenticode result cannot produce a formal signing derivation.')
    changes = []
    for name, record in sorted(records.items()):
        before, after = source / name, target / name
        if before.stat().st_size != record['size'] or digest(before) != record['sha256']:
            raise ReleaseSecurityError('Original CI input changed during signing.')
        if name in FIRST_PARTY:
            change = first_party_change(name, before.read_bytes(), after.read_bytes())
            changes.append(dict(path=name, **change))
        elif after.stat().st_size != record['size'] or digest(after) != record['sha256']:
            raise ReleaseSecurityError('Signing changed a third-party component, license or other CI-verified input.')
    if len(changes) != len(FIRST_PARTY):
        raise ReleaseSecurityError('The audited first-party signing set is incomplete.')
    value = dict(schema_version=1, kind='MediaFlow.authenticode-derivation', passed=True,
        github=document['github'], source_sha256=document['source_sha256'],
        unsigned_receipt_sha256=digest(unsigned_receipt),
        original_ci_receipt_rewritten=False, first_party_changes=changes,
        third_party_files_unchanged=True, native_authenticode_report_passed=True,
        scope='Signature-only byte derivation; original CI receipt and native certificate verification remain separate evidence.')
    save_new(output, value)
    return value


def publication_sbom(original_directory, signed_release, original_receipt, derivation_path,
                     manifest_path, output_directory):
    """Create a sidecar for the signed ZIP; never modify original CI evidence.

    The original BOM is attested by CI. This local derivative is independently
    schema/coverage checked and hash-bound to the final ZIP, not falsely labelled
    as another GitHub-attested build or an Ed25519-signed BOM.
    """
    from scripts.native_sbom import rebind_signed_bom
    from scripts.validate_sbom import validate_documents
    from scripts.package_update import _plain, _inventory
    original, release, output = map(lambda p: _plain(p).resolve(),
        (original_directory, signed_release, output_directory))
    if output.exists() or not output.parent.is_dir() or output.is_relative_to(release):
        raise ReleaseSecurityError('Publication SBOM requires a new independent output directory.')
    bom, inventory, generation = [read_document(original / name)
        for name in ('mediaflow.cdx.json', 'inventory.json', 'generation.json')]
    # Use original raw-byte digests; historical JSON encoding is not replaced.
    hashes = {name: digest(original / name) for name in ('mediaflow.cdx.json', 'inventory.json')}
    validate_documents(bom, inventory, generation, document_hashes=hashes, require_native=True)
    receipt, derivation, manifest = map(read_document, (original_receipt, derivation_path, manifest_path))
    if (generation.get('receipt_sha256') != digest(original_receipt)
            or derivation.get('kind') != 'MediaFlow.authenticode-derivation'
            or derivation.get('passed') is not True or derivation.get('original_ci_receipt_rewritten') is not False
            or derivation.get('unsigned_receipt_sha256') != digest(original_receipt)
            or generation.get('github') != receipt.get('github') or derivation.get('github') != receipt.get('github')
            or inventory.get('release_files') != receipt.get('files')):
        raise ReleaseSecurityError('Unsigned BOM, original CI receipt and signing derivation do not identify the same build.')
    entries = derivation.get('first_party_changes', [])
    if len(entries) != len(FIRST_PARTY) or {entry.get('path') for entry in entries} != set(FIRST_PARTY):
        raise ReleaseSecurityError('Signed BOM requires the complete audited first-party transformation set.')
    changes = {}
    for entry in entries:
        path = release / entry['path']
        if entry.get('executable_payload_unchanged') is not True or digest(path) != entry['signed_sha256']:
            raise ReleaseSecurityError('Signed first-party bytes differ from the verified payload-preserving transformation.')
        changes[entry['path']] = dict(unsigned_sha256=entry['unsigned_sha256'],
            signed_sha256=entry['signed_sha256'], signed_size=path.stat().st_size)
    final_bom, final_inventory = rebind_signed_bom(bom, inventory, changes)
    final_files = {item['path']: item for item in final_inventory['release_files']}
    manifest_files = {item['path']: item for item in manifest['files']}
    if set(_inventory(release)) != set(final_files) or final_files != manifest_files:
        raise ReleaseSecurityError('Signed BOM and final authenticated ZIP inventory are different.')
    for name, item in final_files.items():
        path = release / name
        if path.stat().st_size != item['size'] or digest(path) != item['sha256']:
            raise ReleaseSecurityError('A signed release file changed after the final ZIP was created.')
    package_name = Path(urlsplit(manifest['package']['url']).path).name
    package = Path(manifest_path).parent / package_name
    if package.stat().st_size != manifest['package']['size'] or digest(package) != manifest['package']['sha256']:
        raise ReleaseSecurityError('Final ZIP no longer matches the publication manifest.')
    properties = final_bom['metadata'].setdefault('properties', [])
    properties.extend(dict(name='mediaflow:' + key, value=str(value)) for key, value in (
        ('postbuild-transformation', 'Authenticode; original CI attestation subjects remain unsigned'),
        ('unsigned-bom-sha256', hashes['mediaflow.cdx.json']),
        ('signing-derivation-sha256', digest(derivation_path)),
        ('final-package-sha256', manifest['package']['sha256']),
        ('final-package-size', manifest['package']['size']),
        ('publication-bom-cryptographically-attested', 'false')))
    final_bom.setdefault('externalReferences', []).append(dict(type='distribution',
        url=manifest['package']['url'], hashes=[dict(alg='SHA-256', content=manifest['package']['sha256'])]))
    validation = validate_documents(final_bom, final_inventory, require_native=True)
    output.mkdir()
    save_new(output / 'final-mediaflow.cdx.json', final_bom)
    save_new(output / 'final-inventory.json', final_inventory)
    save_new(output / 'final-sbom-validation.json', validation)
    report = dict(schema_version=1, kind='MediaFlow.publication-sbom', passed=True,
        github=receipt['github'], package=manifest['package'],
        unsigned_ci_receipt_sha256=digest(original_receipt),
        unsigned_ci_sbom_sha256=hashes['mediaflow.cdx.json'],
        signing_derivation_sha256=digest(derivation_path),
        final_sbom_sha256=digest(output / 'final-mediaflow.cdx.json'),
        final_inventory_sha256=digest(output / 'final-inventory.json'),
        original_ci_documents_preserved=True, signed_bom_attested_by_github=False,
        official_ci_provenance_verified_by_this_sidecar=False,
        sbom_is_separate_from_zip=True,
        scope='Derived signed-file identities and final ZIP SHA binding; original official build proof and Authenticode are separate evidence.')
    save_new(output / 'publication-generation.json', report)
    return report


def verify_release(release, directory, *, thumbprint='', store='CurrentUser',
                   timestamp_url='', sign_tool='', pwsh=None, runner=None):
    options = production_options(thumbprint, store, timestamp_url, sign_tool)
    directory = Path(directory)
    report = directory / ('authenticode-' + uuid.uuid4().hex + '.json')
    result = authenticode('Verify', report, release=release, options=options,
                          pwsh=pwsh, runner=runner)
    files = {item['path']: item for item in result.get('files', [])}
    for name in FIRST_PARTY:
        entry = files.get(name, {})
        if (entry.get('ownership') != 'first_party' or entry.get('formal_verified') is not True
                or entry.get('after_sha256') != digest(Path(release) / name)
                or entry.get('signer_thumbprint', '').upper() != options['thumbprint']):
            raise ReleaseSecurityError('Formal Authenticode result does not bind the exact expected first-party files.')
    return result


def verify_package(package, manifest, directory, **configuration):
    """Verify actual final ZIP bytes before a stable draft or upload is created.

    Only the three audited first-party PEs are extracted into a new private
    directory. Every ZIP member is still checked against the signed inventory.
    Existing stable packages remain readable; this gate is for new publication.
    """
    from core.update_manifest import safe_release_path, MAX_FILES, MAX_UNPACKED_BYTES
    # Reject missing production configuration before reading or extracting ZIP.
    production_options(configuration.get('thumbprint', ''), configuration.get('store', 'CurrentUser'),
                       configuration.get('timestamp_url', ''), configuration.get('sign_tool', ''))
    package, parent = Path(package), Path(directory)
    if (package.stat().st_size != manifest['package']['size']
            or digest(package) != manifest['package']['sha256']):
        raise ReleaseSecurityError('Final package differs from its authenticated manifest.')
    records = {item['path']: item for item in manifest['files']}
    if any(name not in records for name in FIRST_PARTY):
        raise ReleaseSecurityError('Final package lacks an audited first-party executable.')
    output = parent / ('authenticode-package-' + uuid.uuid4().hex)
    if output.exists() or not parent.is_dir():
        raise ReleaseSecurityError('A new independent package-verification directory is required.')
    output.mkdir()
    seen, expanded = set(), 0
    destinations = {name: output.joinpath(*name.split('/')) for name in FIRST_PARTY}
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        if not 1 <= len(members) <= MAX_FILES + 1:
            raise ReleaseSecurityError('Unexpected final ZIP member count.')
        for entry in members:
            name = safe_release_path(entry.filename)
            if (entry.is_dir() or name.casefold() in seen or entry.flag_bits & 1
                    or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
                    or entry.external_attr & 0x400):
                raise ReleaseSecurityError('Unsafe, duplicate or linked member in final ZIP.')
            seen.add(name.casefold())
            expanded += entry.file_size
            if expanded > MAX_UNPACKED_BYTES:
                raise ReleaseSecurityError('Final ZIP exceeds the allowed expanded size.')
            if name == 'release.json':
                if entry.file_size > 2 * 1024 * 1024:
                    raise ReleaseSecurityError('Inner release manifest exceeds its bound.')
                continue
            expected = records.get(name)
            if expected is None or expected['size'] != entry.file_size:
                raise ReleaseSecurityError('Final ZIP inventory differs from the authenticated manifest.')
            target = destinations.get(name)
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()
            with archive.open(entry) as source:
                destination = target.open('xb') if target else None
                try:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        checksum.update(block)
                        if destination:
                            destination.write(block)
                finally:
                    if destination:
                        destination.close()
            if checksum.hexdigest() != expected['sha256']:
                raise ReleaseSecurityError('Final ZIP member SHA-256 differs from its authenticated inventory.')
    if seen != {name.casefold() for name in records} | {'release.json'}:
        raise ReleaseSecurityError('The authenticated final ZIP inventory is incomplete.')
    result = verify_release(output, parent, **configuration)
    # No remote write may consume a different package after this verification.
    if digest(package) != manifest['package']['sha256']:
        raise ReleaseSecurityError('Final package changed during Authenticode verification.')
    return dict(passed=True, formal_verified=True, package_sha256=manifest['package']['sha256'],
        first_party_files=len(FIRST_PARTY), signature_report=result,
        scope='Windows native verification of the exact final package, not a claim of SmartScreen reputation.')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    bom = commands.add_parser('publication-sbom')
    for name in ('original-directory', 'signed-release', 'original-receipt', 'derivation', 'manifest', 'output'):
        bom.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = publication_sbom(args.original_directory, args.signed_release, args.original_receipt,
            args.derivation, args.manifest, args.output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps(dict(passed=False, error_type=type(error).__name__,
            error=str(error) if isinstance(error, ReleaseSecurityError)
            else 'Publication SBOM verification failed; original evidence and files are preserved.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
