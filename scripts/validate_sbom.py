"""Independent official CycloneDX schema, dependency and byte-inventory closure.

This checks consistency, not the origin of arbitrary input bytes. Actual build
attestations and release bytes are authenticated separately by the publisher.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))


class SBOMValidationError(RuntimeError):
    pass


def public_strings(value):
    if isinstance(value, str):
        if (re.search(r'(?i)(?<![A-Za-z0-9])[a-z]:[\\/]|file://|(?:^|\s)/[A-Za-z0-9_.-]+/|\\\\[^\\]+\\', value)
            or re.search(r'BEGIN (?:ENCRYPTED )?PRIVATE KEY|github_pat_|gh[pousr]_[A-Za-z0-9]{20}', value)):
            raise SBOMValidationError('SBOM contains a host path or secret marker; values were not logged.')
    elif isinstance(value, list):
        for item in value:
            public_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and re.sub(r'[-_ ]', '', key).lower() in (
                    'password', 'cookie', 'cookies', 'authorization', 'accesstoken',
                    'refreshtoken', 'privatekey', 'clientsecret'):
                raise SBOMValidationError('SBOM contains a sensitive field; values were not logged.')
            public_strings(key)
            public_strings(item)


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _unique(items, key, label):
    if not isinstance(items, list):
        raise SBOMValidationError(label + ' must be an explicit list.')
    result = {}
    for item in items:
        if not isinstance(item, dict) or key not in item or item[key] in result:
            raise SBOMValidationError(label + ' identities are absent or repeated.')
        result[item[key]] = item
    return result


def _files(items, label):
    from scripts.native_sbom import relative, NativeSBOMError
    result = _unique(items, 'path', label)
    if len(result) != len({name.casefold() for name in result}):
        raise SBOMValidationError(label + ' has case-insensitive path collisions.')
    for name, item in result.items():
        try:
            relative(name)
        except NativeSBOMError:
            raise SBOMValidationError(label + ' contains an unsafe relative path.') from None
        if (type(item.get('size')) is not int or item['size'] < 0
            or not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', '')))):
            raise SBOMValidationError(label + ' lacks actual size/hash evidence.')
    return result


def _native(bom, inventory, dependencies, *, require_native):
    from scripts.native_sbom import native_components, _catalog, _evidence_files, PE_SUFFIXES
    native = inventory.get('native_inventory')
    if inventory.get('schema_version') == 1 and native is None:
        if require_native:
            raise SBOMValidationError('Legacy SBOM has no verified native inventory coverage.')
        return dict(native_coverage_verified=False, native_coverage='legacy-not-assessed')
    if inventory.get('schema_version') != 2 or not isinstance(native, dict) or native.get('schema_version') != 1:
        raise SBOMValidationError('Native inventory schema is missing or unsupported.')
    files = _files(inventory.get('release_files'), 'Release file inventory')
    if ('MediaFlow.exe' not in files or bom['metadata']['component'].get('hashes') !=
            [dict(alg='SHA-256', content=files['MediaFlow.exe']['sha256'])]):
        raise SBOMValidationError('Root application hash differs from the actual final EXE inventory.')
    observed = _files(native.get('file_observations'), 'Actual file observations')
    if set(files) != set(observed) or any(
            any(observed[name].get(field) != item[field] for field in ('size', 'sha256'))
            or type(observed[name].get('is_pe')) is not bool for name, item in files.items()):
        raise SBOMValidationError('Native file observations do not close the actual release inventory.')
    expected = {}
    for name, item in observed.items():
        if name.lower().endswith(PE_SUFFIXES) and not item['is_pe']:
            raise SBOMValidationError('A native-named release file lacks actual PE-byte evidence.')
        if item['is_pe']:
            expected['native-file:' + name] = (name, None, item)
    main_members = _files(inventory.get('embedded_members'), 'Original embedded inventory')
    containers = _unique(native.get('containers'), 'path', 'Native container inventory')
    policy = json.loads((ROOT / 'security/sbom-policy.json').read_bytes())
    for field in ('external_system_dlls', 'external_system_prefixes', 'native_archive_paths'):
        if sorted(native.get('policy', {}).get(field, [])) != sorted(policy[field]):
            raise SBOMValidationError('Native system/archive policy differs from the reviewed source policy.')
    if set(containers) - set(policy['native_archive_paths']) or 'MediaFlow.exe' not in containers:
        raise SBOMValidationError('Native containers are absent or outside the bounded archive policy.')
    missing_archives, expected_gaps = [], []
    for name in policy['native_archive_paths']:
        if name in files and name not in containers:
            missing_archives.append(name)
            expected_gaps.append(dict(object='native-file:' + name, kind='nested-archive',
                reason='No supported CArchive; nested native content remains unassessed.'))
            if not any(gap.get('object') == 'native-file:' + name and gap.get('kind') == 'nested-archive'
                       for gap in native.get('gaps', [])):
                raise SBOMValidationError('An uninspected nested archive was omitted without its explicit coverage gap.')
    for name, container in containers.items():
        if (name not in files or container.get('sha256') != files[name]['sha256']
                or container.get('format') != 'pyinstaller-carchive'):
            raise SBOMValidationError('Native archive container is not bound to actual release bytes.')
        members = _files(container.get('members'), 'Native archive members')
        if name == 'MediaFlow.exe' and (set(members) != set(main_members) or any(
                any(item.get(key) != main_members[member].get(key) for key in ('size', 'sha256', 'is_pe'))
                for member, item in members.items())):
            raise SBOMValidationError('Native archive byte classification differs from original TOC/source validation.')
        for member, item in members.items():
            if type(item.get('is_pe')) is not bool or member.lower().endswith(PE_SUFFIXES) and not item['is_pe']:
                raise SBOMValidationError('Native archive member lacks actual PE classification.')
            if item['is_pe']:
                expected['native-file:' + name + '!/' + member] = (member, name, item)
    objects = _unique(native.get('objects'), 'ref', 'Native object inventory')
    if set(objects) != set(expected):
        raise SBOMValidationError('Physical/embedded PE coverage is incomplete or includes invented objects.')
    evidence = _files(native.get('evidence_files'), 'Native license/provenance evidence')
    if native['evidence_files'] != _evidence_files(inventory['release_files']):
        raise SBOMValidationError('Native license/provenance evidence is not the complete actual document set.')
    for name, item in evidence.items():
        if name not in files or any(item[k] != files[name][k] for k in ('size', 'sha256')):
            raise SBOMValidationError('Native license/provenance document hash does not match the release inventory.')
    catalog, catalog_hash = _catalog(policy)
    if native.get('native_evidence_catalog_sha256') != catalog_hash:
        raise SBOMValidationError('Native reported-library evidence catalog hash is not the reviewed catalog.')
    unparsed, imports, by_name = [], {}, defaultdict(list)
    for ref, obj in objects.items():
        path, container, actual = expected[ref]
        location = container + '!/' + path if container else path
        values = (('location', location), ('path', path), ('container', container),
            ('kind', 'embedded' if container else 'physical'), ('size', actual['size']), ('sha256', actual['sha256']))
        if any(obj.get(k) != value for k, value in values):
            raise SBOMValidationError('Native object identity does not match complete actual file/member evidence.')
        if obj.get('first_party') != (container is None and path in policy['first_party_pe_paths']):
            raise SBOMValidationError('Native first-party ownership is not the fixed reviewed build-path policy.')
        if any(name not in evidence for name in obj.get('license_evidence_paths', [])):
            raise SBOMValidationError('Native object references an absent license/provenance document.')
        owners = main_members[path].get('distributions', []) if container == 'MediaFlow.exe' else []
        if obj.get('distributions') != owners or any(owner not in inventory.get('distribution_metadata', {}) for owner in owners):
            raise SBOMValidationError('Native distribution ownership lacks original wheel RECORD attribution.')
        if obj.get('reported_component_evidence') != catalog.get(obj['sha256']):
            raise SBOMValidationError('Reported native library versions are not bound to the exact reviewed binary.')
        if obj.get('provenance') and obj['provenance'].get('sha256') != obj['sha256']:
            raise SBOMValidationError('Native tool provenance refers to a different actual binary hash.')
        pe = obj.get('pe', {})
        if pe.get('status') not in ('parsed', 'unparsed') or not isinstance(pe.get('version_resources'), dict):
            raise SBOMValidationError('Native PE parse/version evidence is malformed.')
        if pe['status'] == 'unparsed':
            unparsed.append(ref)
            expected_gaps.append(dict(object=ref, kind='pe-parser', reason=pe.get('reason', 'unparsed')))
            if not any(g.get('object') == ref and g.get('kind') == 'pe-parser' for g in native.get('gaps', [])):
                raise SBOMValidationError('A malformed PE parser result was hidden from the coverage gaps.')
        if not pe['version_resources']:
            expected_gaps.append(dict(object=ref, kind='version', reason='No verified PE version-resource fields.'))
        if pe.get('clr_metadata_present'):
            expected_gaps.append(dict(object=ref, kind='managed-references',
                reason='Managed assembly references are not inferred from the native import table.'))
        for kind, field in (('import', 'imports'), ('delay-import', 'delay_imports')):
            modules = pe.get(field)
            if (not isinstance(modules, list) or modules != sorted(set(modules))
                or any(not isinstance(m, str) or not re.fullmatch(r'[a-z0-9_.+-]{1,160}', m) for m in modules)):
                raise SBOMValidationError('Native PE imports are malformed or repeated.')
            for module in modules:
                imports[ref, kind, module] = obj
        by_name[PurePosixPath(path).name.casefold()].append(obj)
    relations, external = {}, []
    for edge in native.get('relations', []):
        identity = edge.get('source'), edge.get('kind'), edge.get('module')
        if identity not in imports or identity in relations:
            raise SBOMValidationError('Native dependency relation is repeated or not an observed PE import.')
        obj = imports[identity]
        pool = [candidate for candidate in by_name.get(identity[2], ()) if candidate['container'] == obj['container']]
        near = [candidate for candidate in pool if PurePosixPath(candidate['path']).parent == PurePosixPath(obj['path']).parent]
        chosen = near or pool
        resolution = ('distributed' if len(chosen) == 1 else 'ambiguous' if chosen else
                      'external-system' if identity[2] in policy['external_system_dlls']
                      or identity[2].startswith(tuple(policy['external_system_prefixes'])) else 'unresolved')
        if edge.get('resolution') != resolution or edge.get('targets') != sorted(x['ref'] for x in chosen):
            raise SBOMValidationError('Native resolution is not uniquely supported by its container/directory evidence.')
        relations[identity] = edge
        if resolution in ('external-system', 'unresolved'):
            external.append(dict(source=identity[0], module=identity[2], kind=identity[1], classification=resolution))
        if resolution in ('ambiguous', 'unresolved'):
            expected_gaps.append(dict(object=identity[0], kind='dependency-resolution',
                                      module=identity[2], reason=resolution))
    if set(relations) != set(imports) or native.get('external_prerequisites') != external:
        raise SBOMValidationError('Native import graph or external-system prerequisite coverage is incomplete.')
    if Counter(_canonical(gap) for gap in native.get('gaps', [])) != Counter(_canonical(gap) for gap in expected_gaps):
        raise SBOMValidationError('Native version/managed/import coverage gaps were omitted, duplicated or invented.')
    components = {c['bom-ref']: c for c in bom['components']}
    expected_components, expected_edges = native_components(native)
    expected_refs = {c['bom-ref'] for c in expected_components}
    actual_refs = {ref for ref in components if ref.startswith(('native-file:', 'reported-native:'))}
    if actual_refs != expected_refs or any(components.get(c['bom-ref']) != c for c in expected_components):
        raise SBOMValidationError('CycloneDX native components do not match complete actual identities, versions and evidence.')
    root = bom['metadata']['component']['bom-ref']
    expected_dependencies = {root: set(components)}
    for edge in expected_edges:
        if edge['ref'] in components:
            expected_dependencies.setdefault(edge['ref'], set()).update(set(edge['dependsOn']) - {edge['ref']})
    if 'runtime:cpython' in components:
        expected_dependencies.setdefault('runtime:cpython', set()).update(
            'native-file:MediaFlow.exe!/' + name for name in main_members if re.fullmatch(r'python\d+\.dll', name, re.I))
    if dependencies != expected_dependencies:
        raise SBOMValidationError('CycloneDX dependency graph differs from observed import/ownership/container evidence.')
    if native.get('static_dependency_enumeration') != 'incomplete' or native.get('execution') != 'none':
        raise SBOMValidationError('Native static/transitive completeness must retain its explicit limitations.')
    complete = not unparsed and not missing_archives
    if require_native and not complete:
        raise SBOMValidationError('Actual PE parse/nested-archive coverage is incomplete; native coverage cannot be claimed.')
    return dict(native_coverage_verified=complete, native_objects=len(objects),
        physical_native_files=sum(x['container'] is None for x in objects.values()),
        embedded_native_files=sum(x['container'] is not None for x in objects.values()),
        native_relations=len(relations), native_unparsed=len(unparsed), native_uninspected_archives=len(missing_archives),
        native_inventory_gaps=len(native.get('gaps', [])),
        external_system_imports=sum(e['resolution'] == 'external-system' for e in relations.values()),
        unresolved_imports=sum(e['resolution'] in ('ambiguous', 'unresolved') for e in relations.values()),
        native_coverage='actual-PE-and-supported-CArchive-byte-inventory-closure', static_dependency_enumeration='incomplete')


def validate_documents(bom, inventory, generation=None, *, document_hashes=None, require_native=False):
    """Pure validation. generation=None never reuses unsigned-CI hash claims.

    A signed derivative requires separate byte-transform/Authenticode evidence.
    This function does not sign or authenticate either the input or a build.
    """
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator
    for document in (bom, inventory, generation):
        public_strings(document)
    version = metadata.version('cyclonedx-python-lib')
    pins = json.loads((ROOT / 'security/toolchain.json').read_bytes())['python_tools']
    if version != pins['cyclonedx-python-lib']:
        raise SBOMValidationError('Official CycloneDX validator version differs from the verified pin.')
    hashes = document_hashes or {'mediaflow.cdx.json': _digest(_canonical(bom)), 'inventory.json': _digest(_canonical(inventory))}
    declared_inventory = [prop['value'] for prop in bom.get('metadata', {}).get('properties', [])
                          if prop.get('name') == 'mediaflow:inventory-sha256']
    if declared_inventory and declared_inventory != [_digest(_canonical(inventory))]:
        raise SBOMValidationError('SBOM metadata inventory identity differs from the actual public inventory bytes.')
    if generation is not None and (generation.get('passed') is not True or generation.get('receipt_verified') is not True
        or generation.get('sbom_sha256') != hashes.get('mediaflow.cdx.json')
        or generation.get('inventory_sha256') != hashes.get('inventory.json')):
        raise SBOMValidationError('SBOM hashes and validated generation inventory do not agree.')
    result = JsonStrictValidator(SchemaVersion.V1_6).validate_str(_canonical(bom).decode('utf-8'), all_errors=True)
    errors = list(result) if result is not None else []
    if errors:
        raise SBOMValidationError('Official CycloneDX 1.6 schema rejected the inventory; error count: ' + str(len(errors)))
    if bom.get('bomFormat') != 'CycloneDX' or bom.get('specVersion') != '1.6':
        raise SBOMValidationError('SBOM must explicitly declare the verified CycloneDX 1.6 format.')
    components = _unique(bom.get('components', []), 'bom-ref', 'SBOM component')
    root = bom['metadata']['component']['bom-ref']
    tools = _unique(bom.get('metadata', {}).get('tools', {}).get('components', []), 'bom-ref', 'Build-only tool')
    if root in components or root in tools or set(components) & set(tools):
        raise SBOMValidationError('Build-only tools or root were mixed into runtime component identities.')
    refs = set(components) | {root}
    dependencies = {}
    for entry in bom.get('dependencies', []):
        targets = entry.get('dependsOn', [])
        if entry['ref'] in dependencies or len(targets) != len(set(targets)):
            raise SBOMValidationError('SBOM dependency relationships are repeated.')
        if entry['ref'] not in refs or any(target not in refs for target in targets):
            raise SBOMValidationError('SBOM dependency relationship points to an absent component.')
        dependencies[entry['ref']] = set(targets)
    reached, pending = set(), [root]
    while pending:
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(dependencies.get(current, ()))
    if reached != refs:
        raise SBOMValidationError('A runtime component is disconnected from the application dependency graph.')
    native = _native(bom, inventory, dependencies, require_native=require_native)
    if generation is not None:
        for key in ('native_objects', 'native_relations', 'native_inventory_gaps', 'native_coverage_verified'):
            if key in generation and generation[key] != native.get(key):
                raise SBOMValidationError('Generation native coverage claims disagree with independently counted evidence.')
        counts = dict(components=len(components), observed_build_tools=len(tools),
                      release_files=len(inventory.get('release_files', [])), embedded_members=len(inventory.get('embedded_members', [])))
        if any(key in generation and (type(generation[key]) is not int or generation[key] != count)
               for key, count in counts.items()):
            raise SBOMValidationError('Generation component/file counts differ from independently counted evidence.')
    return dict(passed=True, format='CycloneDX', spec_version='1.6', schema_errors=0,
        validator='cyclonedx-python-lib JsonStrictValidator', validator_version=version,
        document_sha256=hashes, github=generation.get('github') if generation else None,
        components=len(components), actual_inventory_hashes_verified=generation is not None,
        build_tool_components=len(tools), host_paths_or_secret_markers=0, cryptographic_provenance_verified=False,
        **native, scope='Official schema and inventory closure; byte origin, signature transforms and attestations are verified separately.')


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SBOMValidationError('Duplicate fields in public SBOM evidence.')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def validate(directory, *, require_native=False):
    directory = Path(directory)
    output = directory / 'validation.json'
    if output.exists():
        raise SBOMValidationError('Existing SBOM validation evidence is preserved.')
    names = ('mediaflow.cdx.json', 'inventory.json', 'generation.json')
    documents, hashes, raw = {}, {}, {}
    for name in names:
        path = directory / name
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size < 32 * 1024 * 1024:
            raise SBOMValidationError('SBOM evidence is missing or exceeds its file bound.')
        raw[name] = path.read_bytes()
        hashes[name] = _digest(raw[name])
        documents[name] = _json(raw[name])
    if (directory / 'SHA256SUMS.txt').read_text('ascii').splitlines() != [hashes[name] + '  ' + name for name in names[:2]]:
        raise SBOMValidationError('SBOM checksum summary differs from the actual documents.')
    report = validate_documents(documents[names[0]], documents[names[1]], documents[names[2]],
                                document_hashes=hashes, require_native=require_native)
    for name in names:
        if (directory / name).read_bytes() != raw[name]:
            raise SBOMValidationError('SBOM evidence changed during validation.')
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--require-native', action='store_true')
    args = parser.parse_args()
    try:
        result = validate(args.directory, require_native=args.require_native)
        print(json.dumps(dict(passed=True, schema_errors=0, components=result['components'],
                              native_coverage_verified=result['native_coverage_verified'])))
        return 0
    except Exception as error:
        print(json.dumps(dict(passed=False, error_type=type(error).__name__, error=str(error)
            if isinstance(error, SBOMValidationError) else 'Independent SBOM verification failed; input values were not logged.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
