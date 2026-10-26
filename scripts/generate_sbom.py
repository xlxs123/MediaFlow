"""Create a read-only CycloneDX inventory from a verified actual portable build.

Run in the original Python build environment. The actual EXE archive, PYZ byte
code, final PKG TOC and wheel RECORD ownership determine bundled components.
Installed packages alone are never evidence of runtime inclusion. This does not
execute the EXE, publish anything or authenticate the unsigned CI receipt.
"""
from __future__ import annotations

import argparse
import ast
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import quote
import uuid
from types import CodeType

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))

from core.data_migration import frozen_file
from scripts.ci_build_receipt import verify_receipt
from scripts.package_update import _plain
from scripts.native_sbom import discover_native, native_components, provenance_release_version


class SBOMError(RuntimeError):
    pass


_ENTRY_TYPES = {'PYMODULE', 'PYMODULE-1', 'PYMODULE-2', 'PYSOURCE', 'PYSOURCE-1',
                'PYSOURCE-2', 'BINARY', 'EXTENSION', 'DATA', 'PYZ', 'OPTION'}
_PRIVATE_PATH = re.compile(r'(?i)(?<![a-z0-9+.-])[a-z]:[\\/]|/(?:home|users|tmp)/|\\\\[^\\]+\\')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,160}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, maximum=32 * 1024 * 1024):
    path = _plain(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise SBOMError('Required build evidence is absent or exceeds the read limit.')
    with frozen_file(path) as handle:
        return handle.read()


def _json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise SBOMError('Duplicate JSON fields in build evidence.')
            value[key] = item
        return value
    value = json.loads(_read(path).decode('utf-8-sig'), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise SBOMError('Build evidence must contain a JSON object.')
    return value


def _relative(name):
    name = name.replace('\\', '/')
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts or '.' in name.split('/')
            or ':' in name or '\x00' in name or _PRIVATE_PATH.search(name)):
        raise SBOMError('Build archive contains an unsafe or personal member name.')
    return name


def _entries(value):
    if not isinstance(value, (list, tuple)):
        return
    if (len(value) == 3 and all(isinstance(part, str) for part in value)
            and value[2] in _ENTRY_TYPES):
        yield value
    else:
        for part in value:
            yield from _entries(part)


def _toc(path):
    data = _read(path)
    # These three TOCs are literals. Never evaluate EXE-00.toc, which can have
    # executable version-info constructor expressions.
    try:
        value = ast.literal_eval(data.decode('utf-8-sig'))
    except (ValueError, SyntaxError, RecursionError):
        raise SBOMError('PyInstaller evidence is not a supported literal TOC.') from None
    return value, _digest(data)


def _properties(**values):
    return [{'name': 'mediaflow:' + name.replace('_', '-'), 'value': str(value)}
            for name, value in values.items()]


def _component(name, ref, role, *, kind='library', version=None, sha256=None, location=None):
    result = {'type': kind, 'bom-ref': ref, 'name': name, 'scope': 'required',
              'properties': _properties(role=role)}
    if version is not None:
        if not isinstance(version, str) or not _NAME.fullmatch(version):
            raise SBOMError('A component version is not safe for public output.')
        result['version'] = version
    if sha256:
        result['hashes'] = [{'alg': 'SHA-256', 'content': sha256}]
    if location:
        result['evidence'] = {'occurrences': [{'location': location}]}
    return result


def _site_roots(entries):
    roots = set()
    for _, source, _ in entries:
        path = Path(source)
        for parent in path.parents:
            if parent.name.lower() in ('site-packages', 'dist-packages'):
                roots.add(parent)
                break
    return sorted(roots, key=str)


def _distributions(entries, source_hashes):
    """Map only source files actually selected by the actual archive to RECORDs."""
    selected = {os.path.normcase(str(Path(source).resolve())) for _, source, _ in entries
                if source not in ('-', '')}
    owners, documents = {}, {}
    for distribution in metadata.distributions(path=[str(path) for path in _site_roots(entries)]):
        name, version = distribution.metadata.get('Name'), distribution.version
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SBOMError('Bundled distribution has invalid package metadata.')
        canonical = re.sub(r'[-_.]+', '-', name).lower()
        key = canonical + '@' + version
        relevant = []
        for member in distribution.files or ():
            path = distribution.locate_file(member)
            normalized = os.path.normcase(str(path.resolve()))
            if normalized in selected:
                # RECORD must describe these actual source bytes. A source edit
                # after installation cannot inherit a package version silently.
                record_hash = member.hash
                own_record = str(member).replace('\\', '/').endswith('.dist-info/RECORD')
                if record_hash is None and own_record:
                    # Wheel RECORD is deliberately not self-hashed. Its bytes
                    # already match the actual embedded archive; it cannot be
                    # used to excuse an unhashed module or native binary.
                    owners.setdefault(normalized, set()).add(key)
                    relevant.append(normalized)
                    continue
                if record_hash is None or record_hash.mode != 'sha256':
                    raise SBOMError('Bundled package source lacks a SHA-256 wheel RECORD entry.')
                actual_hash = _digest(_read(path, 512 * 1024 * 1024))
                if actual_hash != source_hashes.get(normalized):
                    raise SBOMError('Package source changed after actual archive comparison.')
                digest = base64.urlsafe_b64encode(bytes.fromhex(actual_hash)).rstrip(b'=').decode('ascii')
                if digest != record_hash.value:
                    raise SBOMError('Bundled package bytes differ from installed wheel RECORD.')
                owners.setdefault(normalized, set()).add(key)
                relevant.append(normalized)
        if relevant:
            metadata_members = [member for member in distribution.files or ()
                                if str(member).replace('\\', '/').endswith('.dist-info/METADATA')]
            if len(metadata_members) != 1:
                raise SBOMError('Bundled distribution must have one verifiable METADATA file.')
            metadata_bytes = _read(distribution.locate_file(metadata_members[0]))
            metadata_record_hash = metadata_members[0].hash
            if (metadata_record_hash is None or metadata_record_hash.mode != 'sha256'
                    or metadata_record_hash.value != base64.urlsafe_b64encode(hashlib.sha256(metadata_bytes).digest()).rstrip(b'=').decode('ascii')):
                raise SBOMError('Distribution version METADATA does not match its wheel RECORD.')
            documents[key] = {'name': name, 'version': version,
                              'metadata_sha256': _digest(metadata_bytes)}
    return owners, documents


def _archive_inventory(release, build):
    from PyInstaller.archive.readers import CArchiveReader
    from PyInstaller.building.utils import replace_filename_in_code_object

    analysis, analysis_sha = _toc(build / 'Analysis-00.toc')
    pyz, pyz_sha = _toc(build / 'PYZ-00.toc')
    pkg, pkg_sha = _toc(build / 'PKG-00.toc')
    versions = [item.split()[0] for item in analysis if isinstance(item, str)
                and re.match(r'\d+\.\d+\.\d+\s+\(', item)]
    if len(versions) != 1 or versions[0] != sys.version.split()[0]:
        raise SBOMError('Run SBOM generation in the exact Python build environment.')
    runtime_version = versions[0]
    selected_pyz = list(_entries(pyz))
    selected_pkg = list(_entries(pkg))
    archive = CArchiveReader(str(release / 'MediaFlow.exe'))
    pyz_names = [name for name, entry in archive.toc.items() if entry[-1] == 'z']
    if len(pyz_names) != 1:
        raise SBOMError('Actual EXE must contain exactly one supported PYZ archive.')
    embedded = archive.open_embedded_archive(pyz_names[0])
    if (len({name for name, _, _ in selected_pyz}) != len(selected_pyz)
            or set(embedded.toc) != {name for name, _, _ in selected_pyz}):
        raise SBOMError('PYZ TOC does not match the actual EXE module inventory.')
    if sum(kind == 'PYZ' for _, _, kind in selected_pkg) != 1:
        raise SBOMError('Final PKG TOC must contain one PYZ source.')
    # PyInstaller deliberately changes the build filename PYZ-00.pyz to the
    # canonical embedded member PYZ.pyz. Its complete byte comparison below
    # binds that single, explicitly typed substitution.
    pkg_entries = {_relative(pyz_names[0] if kind == 'PYZ' else name): (source, kind) for name, source, kind in selected_pkg
                   if kind != 'OPTION'}
    if len(pkg_entries) != len([item for item in selected_pkg if item[2] != 'OPTION']):
        raise SBOMError('Duplicate final PKG members are not supported.')
    actual_names = {_relative(name): name for name in archive.toc}
    if set(pkg_entries) != set(actual_names):
        raise SBOMError('Final PKG TOC does not match the actual EXE archive.')
    # No EXE code is executed. Existing PyInstaller readers extract data or
    # unmarshal code for equality comparison with the original source.
    modules, source_hashes = [], {}
    for name, source, kind in selected_pyz:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', name):
            raise SBOMError('Unsupported Python module name in archive.')
        if source == '-':
            modules.append({'module': name, 'namespace_package': True})
            continue
        if Path(source).suffix != '.py':
            raise SBOMError('PYZ source must be readable Python source for version attribution.')
        data = _read(source)
        source_hashes[os.path.normcase(str(Path(source).resolve()))] = _digest(data)
        code = embedded.extract(name)
        optimize = 0 if kind == 'PYMODULE' else int(kind.rsplit('-', 1)[1])
        expected = replace_filename_in_code_object(
            compile(data, code.co_filename, 'exec', optimize=optimize, dont_inherit=True), code.co_filename)
        if code != expected:
            raise SBOMError('Actual packaged Python bytecode differs from its build source.')
        modules.append({'module': name, 'source_sha256': _digest(data),
                        'bytecode_sha256': _digest(marshal.dumps(code))})
    members = []
    for normalized, (source, kind) in sorted(pkg_entries.items()):
        original = actual_names[normalized]
        data = archive.extract(original)
        if kind in ('DATA', 'BINARY', 'EXTENSION', 'PYZ'):
            source_hash = _digest(_read(source, 512 * 1024 * 1024))
            source_hashes[os.path.normcase(str(Path(source).resolve()))] = source_hash
            if source_hash != _digest(data):
                raise SBOMError('Actual archive member differs from its final build source.')
        elif kind.startswith(('PYSOURCE', 'PYMODULE')):
            original_data = _read(source)
            source_hashes[os.path.normcase(str(Path(source).resolve()))] = _digest(original_data)
            code = marshal.loads(data)
            if not isinstance(code, CodeType):
                raise SBOMError('Embedded bootstrap entry must contain a Python code object.')
            if Path(source).suffix == '.pyc':
                from importlib.util import MAGIC_NUMBER
                if original_data[:4] != MAGIC_NUMBER:
                    raise SBOMError('Bootstrap bytecode is incompatible with the build Python.')
                expected = marshal.loads(original_data[16:])
            else:
                optimize = int(kind.rsplit('-', 1)[1]) if '-' in kind else 0
                expected = compile(original_data, code.co_filename, 'exec', optimize=optimize, dont_inherit=True)
            if not isinstance(expected, CodeType) or code != replace_filename_in_code_object(expected, code.co_filename):
                raise SBOMError('Actual bootstrap bytecode differs from its build source.')
        members.append({'path': normalized, 'size': len(data), 'sha256': _digest(data), 'kind': kind,
                        'is_pe': data[:2] == b'MZ'})
    owners, distributions = _distributions(selected_pyz + selected_pkg, source_hashes)
    for item, (_, source, _) in zip(modules, selected_pyz):
        item['distributions'] = sorted(owners.get(os.path.normcase(str(Path(source).resolve())), ()))
    for item in members:
        source, _ = pkg_entries[item['path']]
        item['distributions'] = sorted(owners.get(os.path.normcase(str(Path(source).resolve())), ()))
    evidence = {'Analysis-00.toc': analysis_sha, 'PYZ-00.toc': pyz_sha, 'PKG-00.toc': pkg_sha}
    return runtime_version, distributions, modules, members, evidence


def _tool_version(name, provenance):
    # Keep callers compatible with the original release-identifier contract.
    return provenance_release_version(name, provenance)


def validate_public_document(document):
    data = _canonical(document)
    if _PRIVATE_PATH.search(data.decode('utf-8')):
        raise SBOMError('Public SBOM output must not contain machine-specific absolute paths.')
    return data


def generate(release_dir, build_dir, receipt_path, output_dir, *, policy_path=None):
    release, build, receipt, output = map(lambda item: _plain(item).resolve(),
                                        (release_dir, build_dir, receipt_path, output_dir))
    if output.exists() or output.is_relative_to(release) or output.is_relative_to(build):
        raise SBOMError('SBOM output must be a new directory outside release and build directories.')
    if not output.parent.is_dir():
        raise SBOMError('SBOM output parent must already exist.')
    policy_path = policy_path or ROOT / 'security/sbom-policy.json'
    policy = _json(policy_path)
    if policy.get('schema_version') != 1 or policy.get('spec_version') != '1.6':
        raise SBOMError('Unsupported SBOM policy.')
    with ExitStack() as stack:
        for name in ('Analysis-00.toc', 'PYZ-00.toc', 'PKG-00.toc'):
            stack.enter_context(frozen_file(_plain(build / name)))
        stack.enter_context(frozen_file(receipt))
        stack.enter_context(frozen_file(release / 'MediaFlow.exe'))
        checked = verify_receipt(receipt, release)
        document = _json(receipt)
        # The receipt verifier holds an atomic read snapshot internally. Keep
        # the selected release files frozen for the subsequent inventory too,
        # and bind the handover by hashing the newly held handles. Tool metadata
        # cannot be replaced after receipt verification to invent a version.
        for item in document['files']:
            path = release / _relative(item['path'])
            handle = stack.enter_context(frozen_file(_plain(path)))
            if hashlib.file_digest(handle, 'sha256').hexdigest() != item['sha256']:
                raise SBOMError('Release files changed after CI receipt verification.')
        runtime, distributions, modules, members, toc_hashes = _archive_inventory(release, build)
        provenance = _json(release / 'tools/provenance.json')
        files = document['files']
        components, limitations = [], list(policy['limitations'])
        build_tool_names = {re.sub(r'[-_.]+', '-', name).lower() for name in policy['build_tools']}
        runtime_distributions = {key: package for key, package in distributions.items()
            if not (key.split('@', 1)[0] in build_tool_names
                    and not any(key in item['distributions'] for item in modules))}
        support_distributions = set(distributions) - set(runtime_distributions)
        for key, package in sorted(runtime_distributions.items()):
            component = _component(package['name'], 'pkg:pypi/' + quote(key, safe='@.-'),
                                   'packaged-python-distribution', version=package['version'])
            component['purl'] = 'pkg:pypi/' + quote(key, safe='@.-')
            component['properties'] += _properties(metadata_sha256=package['metadata_sha256'],
                included_modules=sum(key in item['distributions'] for item in modules),
                included_archive_members=sum(key in item['distributions'] for item in members),
                attribution='actual archive plus exact source-byte/bytecode and wheel RECORD comparison')
            components.append(component)
        python_members = [item for item in members if re.fullmatch(r'python\d+\.dll', item['path'], re.I)]
        if not python_members:
            raise SBOMError('Actual archive lacks the Python runtime DLL.')
        python = _component('CPython', 'runtime:cpython', 'packaged-python-runtime',
                            kind='framework', version=runtime)
        python['properties'] += _properties(version_evidence='Analysis runtime string and byte-matched embedded Python DLL')
        components.append(python)
        # Native PE files have their own complete byte-bound inventory below.
        # Keep non-PE bootstrap support here; never invent a runtime package
        # merely because PyInstaller supplied a helper during the build.
        for item in members:
            if item['is_pe']:
                continue
            is_support = bool(set(item['distributions']) & support_distributions)
            if not is_support and (item['kind'] not in ('BINARY', 'EXTENSION') or item['distributions'] or item in python_members):
                continue
            component = _component(PurePosixPath(item['path']).name,
                'embedded-file:' + item['path'],
                'bundled-build-runtime-support-file' if is_support else 'unattributed-embedded-native-file', kind='file',
                sha256=item['sha256'], location='MediaFlow.exe!/' + item['path'])
            component['properties'] += _properties(version_state='unknown',
                verified_distribution_records=','.join(item['distributions']))
            components.append(component)
        native = discover_native(release, files, members, provenance=provenance,
                                 distributions=distributions, policy=policy)
        native_files, native_dependencies = native_components(native)
        components.extend(native_files)
        limitations.append('Native import and delay-import tables are observed dependencies; unresolved, managed and static dependencies remain explicit inventory gaps.')
        # Tools are inventory-generation/build-environment facts, not runtime
        # components. They do not appear under root dependencies.
        observed_tools = []
        for name in policy['build_tools']:
            try:
                observed_tools.append(_component(name, 'build-tool:' + name.lower(),
                    'observed-build-tool-not-runtime', kind='application', version=metadata.version(name)))
            except metadata.PackageNotFoundError:
                continue
        github = document['github']
        inventory = {'schema_version': 2, 'release_files': files, 'embedded_members': members,
                     'python_modules': modules, 'toc_sha256': toc_hashes,
                     'distribution_metadata': distributions, 'native_inventory': native}
        inventory_bytes = validate_public_document(inventory)
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        root = _component('MediaFlow', 'application:MediaFlow', 'portable-release', kind='application',
                          version=document['version'], sha256=document['exe_sha256'])
        properties = _properties(repository=github['repository'], commit=github['commit'],
            workflow_run_id=github['run_id'], source_sha256=document['source_sha256'],
            build_id=document['build_id'], receipt_sha256=checked['receipt_sha256'],
            inventory_sha256=_digest(inventory_bytes), tests_passed=document['tests']['passed'],
            tests_count=document['tests']['tests'], structure_verified=True,
            receipt_authenticated=False, provenance_attestation_verified=False,
            generation_policy_sha256=_digest(_read(policy_path)))
        properties += _properties(**{'limitation-' + str(index + 1): item for index, item in enumerate(limitations)})
        dependency_map = {root['bom-ref']: {item['bom-ref'] for item in components}}
        runtime_refs = dependency_map[root['bom-ref']] | {root['bom-ref']}
        for dependency in native_dependencies:
            # Build-only distributions have no runtime component. Their actual
            # support files remain independently identified native components.
            if dependency['ref'] in runtime_refs:
                dependency_map.setdefault(dependency['ref'], set()).update(dependency['dependsOn'])
        dependency_map.setdefault('runtime:cpython', set()).update(
            'native-file:MediaFlow.exe!/' + item['path'] for item in python_members)
        bom = {'$schema': 'http://cyclonedx.org/schema/bom-1.6.schema.json',
            'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'serialNumber': 'urn:uuid:' + str(uuid.uuid4()),
            'metadata': {'timestamp': timestamp, 'component': root,
                         'tools': {'components': observed_tools}, 'properties': properties},
            'components': sorted(components, key=lambda item: item['bom-ref']),
            'dependencies': [{'ref': ref, 'dependsOn': sorted(targets - {ref})}
                             for ref, targets in sorted(dependency_map.items())],
            'externalReferences': [
                {'type': 'vcs', 'url': 'https://github.com/' + github['repository'] + '/tree/' + github['commit']},
                {'type': 'build-system', 'url': 'https://github.com/' + github['repository'] + '/actions/runs/' + github['run_id']}],
            'compositions': [{'aggregate': 'incomplete', 'assemblies': [root['bom-ref']]}]}
        bom_bytes = validate_public_document(bom)
        result = {'schema_version': 1, 'passed': True, 'format': 'CycloneDX', 'spec_version': '1.6',
            'receipt_verified': True, 'receipt_authenticated': False,
            'schema_validation': 'pending-independent-official-schema-validation',
            'github': github, 'release_files': len(files), 'embedded_members': len(members),
            'python_modules': len(modules), 'packaged_python_distributions': len(runtime_distributions),
            'packaged_python_distribution_records': len(distributions),
            'bundled_build_support_members': sum(bool(set(item['distributions']) & support_distributions) for item in members),
            'components': len(components), 'observed_build_tools': len(observed_tools),
            'native_objects': len(native['objects']), 'native_relations': len(native['relations']),
            'native_inventory_gaps': len(native['gaps']),
            'sbom_sha256': _digest(bom_bytes), 'inventory_sha256': _digest(inventory_bytes),
            'receipt_sha256': checked['receipt_sha256'], 'toc_sha256': toc_hashes,
            'tests': {'passed': True, 'count': document['tests']['tests']},
            'limitations': limitations, 'provenance_attestation_verified': False}
        # All validation finishes before any output is created. Never replace an
        # earlier SBOM or place analysis output in the original release tree.
        output.mkdir()
        for name, data in (('mediaflow.cdx.json', bom_bytes), ('inventory.json', inventory_bytes),
                           ('generation.json', validate_public_document(result))):
            with (output / name).open('xb') as handle:
                handle.write(data)
        (output / 'SHA256SUMS.txt').write_text(
            result['sbom_sha256'] + '  mediaflow.cdx.json\n' + result['inventory_sha256'] + '  inventory.json\n',
            encoding='ascii')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('release-dir', 'build-dir', 'receipt', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = generate(args.release_dir, args.build_dir, args.receipt, args.output)
    except Exception as error:
        # TOCs deliberately contain private build paths. Even unexpected IO or
        # parser errors must not echo those values into public CI diagnostics.
        message = str(error) if isinstance(error, SBOMError) else 'SBOM evidence validation failed; no public inventory was accepted.'
        print(json.dumps({'passed': False, 'error_type': type(error).__name__, 'error': message}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
