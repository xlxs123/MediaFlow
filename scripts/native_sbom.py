"""Bounded, non-executing PE/CArchive inventory and observed dependency graph.

Only receipt-listed files are read. Import names are observations, not proof
that an OS DLL is distributed. Version resources and notices retain their
declared/evidence status; neither is publisher authentication.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, urlsplit
import zlib

MAX_FILE = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20000
MAX_NATIVE_OBJECTS = 6000
PE_SUFFIXES = ('.exe', '.dll', '.pyd')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_MODULE = re.compile(r'[A-Za-z0-9_.+-]{1,160}\Z')
_HOST = re.compile(r'(?i)(?<![A-Za-z0-9])[a-z]:[\\/]|file://|(?:^|\s)/[A-Za-z0-9_.-]+/|\\\\[^\\]+\\')
_SECRET = re.compile(r'BEGIN (?:ENCRYPTED )?PRIVATE KEY|github_pat_|gh[pousr]_[A-Za-z0-9]{20}')


class NativeSBOMError(RuntimeError):
    pass


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value or '\x00' in value
        or PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in value.split('/'))):
        raise NativeSBOMError('Native inventory requires ordinary relative names.')
    return value


def public_text(value, limit=240):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    if not isinstance(value, str):
        return None
    value = value.strip('\x00 \t')
    if (not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or _HOST.search(value) or _SECRET.search(value)):
        return None
    return value


def _module(value):
    if isinstance(value, bytes):
        value = value.decode('ascii', errors='strict')
    if not isinstance(value, str) or not _MODULE.fullmatch(value):
        raise NativeSBOMError('PE import contains an unsupported or non-portable module name.')
    return value.lower()


def _fixed_version(high, low):
    return '.'.join(str(v) for v in (high >> 16, high & 65535, low >> 16, low & 65535))


def provenance_release_version(name, provenance):
    """Retain the existing verified-tool release-ID policy; no binary guessing."""
    record = provenance.get(name, {})
    url = urlsplit(record.get('source', ''))
    if url.scheme != 'https' or url.username or url.password or url.query or url.fragment:
        return None
    if name == 'yt-dlp.exe' and url.netloc == 'github.com':
        match = re.fullmatch(r'/yt-dlp/yt-dlp/releases/download/([A-Za-z0-9_.+-]+)/yt-dlp\.exe', url.path)
        return match[1] if match else None
    if name in ('ffmpeg.exe', 'ffprobe.exe') and url.netloc == 'github.com':
        match = re.fullmatch(r'/BtbN/FFmpeg-Builds/releases/download/[^/]+/ffmpeg-(n[0-9][A-Za-z0-9_.+-]*)-win64-gpl-[A-Za-z0-9_.+-]+\.zip', url.path)
        return match[1] if match else None
    return None


def pe_metadata(data):
    """Parse actual bytes with pefile. Never execute or inspect arbitrary strings."""
    import pefile
    if not isinstance(data, bytes) or not 2 <= len(data) <= MAX_FILE or data[:2] != b'MZ':
        raise NativeSBOMError('A PE candidate lacks bounded MZ bytes.')
    image = None
    try:
        image = pefile.PE(data=data, fast_load=True)
        image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY[name] for name in (
            'IMAGE_DIRECTORY_ENTRY_IMPORT', 'IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT', 'IMAGE_DIRECTORY_ENTRY_RESOURCE')])
        imports = sorted({_module(item.dll) for item in getattr(image, 'DIRECTORY_ENTRY_IMPORT', ())})
        delayed = sorted({_module(item.dll) for item in getattr(image, 'DIRECTORY_ENTRY_DELAY_IMPORT', ())})
        versions = {}
        for table in getattr(image, 'FileInfo', ()):
            for item in table:
                if item.Key != b'StringFileInfo':
                    continue
                for strings in item.StringTable:
                    for key, value in strings.entries.items():
                        key = key.decode('ascii', errors='replace') if isinstance(key, bytes) else key
                        if key in ('CompanyName', 'FileDescription', 'FileVersion', 'ProductName', 'ProductVersion', 'OriginalFilename'):
                            text = public_text(value)
                            if text:
                                versions.setdefault(key, text)
        fixed = getattr(image, 'VS_FIXEDFILEINFO', ())
        if fixed:
            versions['FixedFileVersion'] = _fixed_version(fixed[0].FileVersionMS, fixed[0].FileVersionLS)
            versions['FixedProductVersion'] = _fixed_version(fixed[0].ProductVersionMS, fixed[0].ProductVersionLS)
        certificate = image.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']]
        offset, size = int(certificate.VirtualAddress), int(certificate.Size)
        if size and (offset < 0 or size < 8 or offset + size > len(data)):
            raise NativeSBOMError('PE certificate-table range is outside the actual file.')
        clr = image.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR']]
        return dict(status='parsed', machine=int(image.FILE_HEADER.Machine),
            optional_header_magic=int(image.OPTIONAL_HEADER.Magic), imports=imports, delay_imports=delayed,
            version_resources=versions, clr_metadata_present=bool(clr.VirtualAddress and clr.Size),
            certificate_table=dict(present=bool(size), size=size,
                sha256=sha256(data[offset:offset + size]) if size else None,
                trust='not-verified-by-PE-inventory'), parser_warning_count=len(image.get_warnings()))
    except (pefile.PEFormatError, ValueError, AttributeError, IndexError, UnicodeError):
        return dict(status='unparsed', reason='malformed-or-unsupported-pe', imports=[], delay_imports=[],
                    version_resources={}, certificate_table=dict(present=None, trust='not-verified-by-PE-inventory'))
    finally:
        if image is not None:
            image.close()


def _read_bound(path, expected):
    from core.data_migration import frozen_file
    from scripts.package_update import _plain
    path = _plain(path)
    if not path.is_file() or not 0 <= expected['size'] <= MAX_FILE:
        raise NativeSBOMError('Native source is absent or outside the bounded read policy.')
    with frozen_file(path) as stream:
        # The receipt already supplies the exact length. Read one extra byte to
        # detect growth without allocating the global ceiling for tiny files.
        data = stream.read(expected['size'] + 1)
    if len(data) != expected['size'] or sha256(data) != expected['sha256']:
        raise NativeSBOMError('Actual file bytes differ from the receipt-bound inventory.')
    return data


def _evidence_files(files):
    return [dict(path=f['path'], size=f['size'], sha256=f['sha256'],
        kind='provenance-document' if PurePosixPath(f['path']).name.lower() == 'provenance.json' else 'bundled-license-or-notice')
        for f in files if (f['path'].startswith('licenses/') or any(word in PurePosixPath(f['path']).name.lower()
            for word in ('license', 'notice', 'provenance')))]


def _license_paths(location, owners, evidence):
    paths = []
    lower = location.lower()
    for document in evidence:
        name = document['path'].lower()
        folder = name.split('/')[1] if name.startswith('licenses/') and len(name.split('/')) > 1 else ''
        owner_match = any(re.sub(r'[-_.]+', '-', folder) ==
            re.sub(r'[-_.]+', '-', owner.replace('@', '-').lower() + '.dist-info') for owner in owners)
        tool_match = (('ffmpeg.exe' in lower or 'ffprobe.exe' in lower) and name.startswith('tools/ffmpeg-')
            or 'yt-dlp.exe' in lower and name.startswith('tools/yt-dlp-')
            or '/webview2/' in lower and name.startswith('tools/webview2/')
            or re.search(r'qt\d*webengine', lower) and name.startswith('licenses/qtwebengine/')
            or re.search(r'(?:^|!/)python\d+\.dll$', lower) and name.startswith('licenses/python/'))
        application_match = lower in ('mediaflow.exe', 'tools/mediaflow.recycle.exe',
            'tools/webview2/mediaflow.webview2host.exe') and name == 'license'
        if owner_match or tool_match or application_match:
            paths.append(document['path'])
    return sorted(paths)


def _archive_members(path, expected_members=None):
    from core.data_migration import frozen_file
    from scripts.package_update import _plain
    from PyInstaller.archive.readers import CArchiveReader
    from PyInstaller.loader.pyimod01_archive import ArchiveReadError
    try:
        archive = CArchiveReader(str(path))
    except (ValueError, OSError, ArchiveReadError):
        return None
    if not 0 < len(archive.toc) <= MAX_ARCHIVE_MEMBERS:
        raise NativeSBOMError('CArchive member count exceeds the reviewed bound.')
    if any(any(type(entry[index]) is not int or not 0 <= entry[index] <= MAX_FILE for index in (0, 1, 2))
           or entry[3] not in (0, 1) for entry in archive.toc.values()):
        raise NativeSBOMError('CArchive member size exceeds the reviewed bound.')
    if sum(entry[2] for entry in archive.toc.values()) > MAX_ARCHIVE_BYTES:
        raise NativeSBOMError('CArchive expanded size exceeds the reviewed bound.')
    expected = {m['path']:m for m in expected_members} if expected_members is not None else None
    names = [relative(name.replace('\\', '/')) for name in archive.toc]
    if len(names) != len(set(name.casefold() for name in names)) or expected is not None and set(names) != set(expected):
        raise NativeSBOMError('CArchive member names conflict with its bound inventory.')
    records, pe_values = [], {}
    for original, name in zip(archive.toc, names):
        offset, stored_size, expanded_size, compressed, _ = archive.toc[original]
        if archive._start_offset + offset < 0 or archive._start_offset + offset + stored_size > path.stat().st_size:
            raise NativeSBOMError('CArchive member points outside its actual carrier.')
        with frozen_file(_plain(path)) as source:
            source.seek(archive._start_offset + offset)
            data = source.read(stored_size)
        if len(data) != stored_size:
            raise NativeSBOMError('CArchive stored member bytes are incomplete.')
        if compressed:
            decompressor = zlib.decompressobj()
            data = decompressor.decompress(data, expanded_size + 1)
            if not decompressor.eof or decompressor.unconsumed_tail or decompressor.unused_data:
                raise NativeSBOMError('CArchive compressed member is invalid or exceeds its declared bound.')
        if len(data) != expanded_size:
            raise NativeSBOMError('CArchive did not yield the declared actual member bytes.')
        record = dict(path=name, size=len(data), sha256=sha256(data), is_pe=data[:2] == b'MZ')
        if expected is not None:
            old = expected[name]
            if any(record[key] != old[key] for key in ('size', 'sha256')):
                raise NativeSBOMError('CArchive actual member differs from original byte validation.')
        records.append(record)
        if record['is_pe']:
            pe_values[name] = pe_metadata(data)
        elif name.lower().endswith(PE_SUFFIXES):
            raise NativeSBOMError('A named native archive member does not contain PE bytes.')
    return sorted(records, key=lambda x:x['path']), pe_values


def _catalog(policy):
    path = ROOT / 'security/native-component-evidence.json'
    if not path.is_file():
        return {}, None
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise NativeSBOMError('Native evidence catalog exceeds the reviewed bound.')
    def unique_fields(items):
        values = {}
        for key, value in items:
            if key in values:
                raise NativeSBOMError('Duplicate fields in native evidence catalog.')
            values[key] = value
        return values
    document = json.loads(raw, object_pairs_hook=unique_fields)
    if (set(document) != {'schema_version', 'binaries'} or document.get('schema_version') != 1
            or not isinstance(document.get('binaries'), list)):
        raise NativeSBOMError('Native evidence catalog schema is unsupported.')
    records = {}
    for item in document['binaries']:
        fields = {'path', 'sha256', 'version_report_sha256', 'capture_kind', 'capture_exit_code',
                  'reported_linked_libraries', 'declared_static_configuration', 'upstream_provenance'}
        if not isinstance(item, dict) or set(item) != fields:
            raise NativeSBOMError('Native evidence catalog contains unreviewed fields.')
        digest = item.get('sha256')
        if not isinstance(digest, str) or not _HASH.fullmatch(digest) or digest in records:
            raise NativeSBOMError('Native evidence must bind unique exact binary SHA-256 values.')
        relative(item['path'])
        if (not _HASH.fullmatch(str(item['version_report_sha256'])) or item['capture_exit_code'] != 0
            or item['capture_kind'] != 'receipt-bound-known-tool-version-output'):
            raise NativeSBOMError('Native version evidence is not a successful byte-bound observation.')
        if not isinstance(item['reported_linked_libraries'], list):
            raise NativeSBOMError('Native library evidence is malformed.')
        for library in item['reported_linked_libraries']:
            if (not isinstance(library, dict) or set(library) != {'name', 'version'}
                or not _MODULE.fullmatch(str(library['name']))
                or not re.fullmatch(r'\d+(?:\.\d+){1,3}', str(library['version']))):
                raise NativeSBOMError('Native library evidence has no literal reported version.')
        config = item['declared_static_configuration']
        if (not isinstance(config, dict) or set(config) != {'static_pkg_config', 'enabled_libraries', 'presence', 'versions'}
            or type(config['static_pkg_config']) is not bool
            or not isinstance(config['enabled_libraries'], list)
            or config['enabled_libraries'] != sorted(set(config['enabled_libraries']))
            or any(not isinstance(name, str) or not re.fullmatch(r'lib[a-z0-9_+-]{1,80}', name) for name in config['enabled_libraries'])
            or config['presence'] != 'declared-configuration-not-proof-of-every-per-binary-link'
            or config['versions'] != 'unknown'):
            raise NativeSBOMError('Static native configuration must retain its unverified per-library linkage/version limits.')
        origin = item['upstream_provenance']
        if not isinstance(origin, dict) or set(origin) != {'source', 'archive_sha256', 'build_recipe_commit', 'ffmpeg_commit', 'upstream_workflow_run'}:
            raise NativeSBOMError('Native evidence upstream provenance has unsupported fields.')
        url = urlsplit(origin['source'])
        if (url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment
            or not _HASH.fullmatch(str(origin['archive_sha256']))
            or not re.fullmatch(r'[0-9a-f]{40}', str(origin['build_recipe_commit']))
            or not re.fullmatch(r'[0-9a-f]{7,40}', str(origin['ffmpeg_commit']))
            or type(origin['upstream_workflow_run']) is not int or origin['upstream_workflow_run'] <= 0):
            raise NativeSBOMError('Native evidence upstream provenance lacks public hash-bound identifiers.')
        records[digest] = item
    return records, sha256(raw)


ROOT = Path(__file__).resolve().parents[1]


def discover_native(release_dir, release_files, embedded_members=None, *, provenance=None, distributions=None, policy=None):
    """Inventory only immutable receipt-listed paths; no release-tree traversal."""
    from core.data_migration import frozen_file
    from scripts.package_update import _plain
    release = _plain(release_dir).resolve()
    policy = policy or {}
    provenance = provenance or {}
    distributions = distributions or {}
    files = [dict(f) for f in release_files]
    names = [relative(f['path']) for f in files]
    if len(names) != len(set(name.casefold() for name in names)):
        raise NativeSBOMError('Release paths are not unique.')
    for item in files:
        if not _HASH.fullmatch(str(item.get('sha256', ''))) or type(item.get('size')) is not int:
            raise NativeSBOMError('Release file evidence lacks actual size or SHA-256.')
    evidence = _evidence_files(files)
    catalog, catalog_hash = _catalog(policy)
    first_party = set(policy.get('first_party_pe_paths', ['MediaFlow.exe', 'tools/MediaFlow.Recycle.exe', 'tools/webview2/MediaFlow.WebView2Host.exe']))
    objects, observations, containers, gaps = [], [], [], []
    bound_members = {m['path']:m for m in embedded_members or ()}

    def add(location, file, pe, *, container=None, owners=()):
        if len(objects) >= MAX_NATIVE_OBJECTS:
            raise NativeSBOMError('Native object count exceeds the reviewed bound.')
        result = dict(ref='native-file:' + location, location=location, kind='embedded' if container else 'physical',
            container=container, path=file['path'], size=file['size'], sha256=file['sha256'], pe=pe,
            distributions=sorted(owners), first_party=not container and file['path'] in first_party,
            license_evidence_paths=_license_paths(location, owners, evidence),
            ownership_evidence='verified-wheel-RECORD' if owners else 'first-party-build-path' if not container and file['path'] in first_party else 'unresolved',
            license_status='bundled-notice-evidence-only' if _license_paths(location, owners, evidence) else 'unknown')
        if pe['status'] != 'parsed':
            gaps.append(dict(object=result['ref'], kind='pe-parser', reason=pe.get('reason', 'unparsed')))
        if not pe['version_resources']:
            gaps.append(dict(object=result['ref'], kind='version', reason='No verified PE version-resource fields.'))
        if pe.get('clr_metadata_present'):
            gaps.append(dict(object=result['ref'], kind='managed-references', reason='Managed assembly references are not inferred from the native import table.'))
        record = provenance.get(PurePosixPath(file['path']).name, {}) if not container else {}
        if record:
            if record.get('sha256') != result['sha256']:
                raise NativeSBOMError('Tool provenance differs from actual native file bytes.')
            url = urlsplit(record.get('source', ''))
            if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise NativeSBOMError('Tool provenance must contain a public HTTPS origin without credentials or signed parameters.')
            result['provenance'] = {k:v for k,v in record.items() if k in ('source', 'sha256', 'archive_sha256', 'build_recipe_commit', 'ffmpeg_commit', 'upstream_workflow_run')}
        if result['sha256'] in catalog:
            result['reported_component_evidence'] = catalog[result['sha256']]
        objects.append(result)

    for item in sorted(files, key=lambda x:x['path']):
        data = _read_bound(release / item['path'], item)
        is_pe = data[:2] == b'MZ'
        observations.append(dict(path=item['path'], sha256=item['sha256'], size=item['size'], is_pe=is_pe))
        if is_pe:
            add(item['path'], item, pe_metadata(data))
        elif item['path'].lower().endswith(PE_SUFFIXES):
            raise NativeSBOMError('A named native release file does not contain PE bytes.')
    for container in policy.get('native_archive_paths', ['MediaFlow.exe', 'tools/yt-dlp.exe']):
        record = next((f for f in files if f['path'] == container), None)
        if record is None:
            continue
        # Reopening a CArchive must not introduce a second, unbound version of
        # the carrier. Hold the same Windows read-only sharing lock throughout
        # member extraction and bind it to the already observed complete hash.
        with frozen_file(_plain(release / container)) as carrier:
            if hashlib.file_digest(carrier, 'sha256').hexdigest() != record['sha256']:
                raise NativeSBOMError('Native archive carrier changed after actual file-byte validation.')
            scanned = _archive_members(release / container, embedded_members if container == 'MediaFlow.exe' else None)
        if scanned is None:
            if container == 'MediaFlow.exe':
                raise NativeSBOMError('MediaFlow actual CArchive cannot be inspected.')
            gaps.append(dict(object='native-file:' + container, kind='nested-archive', reason='No supported CArchive; nested native content remains unassessed.'))
            continue
        records, native = scanned
        containers.append(dict(path=container, sha256=record['sha256'], format='pyinstaller-carchive', members=records))
        for member in records:
            if member['is_pe']:
                owners = bound_members.get(member['path'], {}).get('distributions', ()) if container == 'MediaFlow.exe' else ()
                add(container + '!/' + member['path'], member, native[member['path']], container=container, owners=owners)
    objects.sort(key=lambda value:value['ref'])
    by_name = defaultdict(list)
    for obj in objects:
        by_name[PurePosixPath(obj['path']).name.casefold()].append(obj)
    systems = set(policy.get('external_system_dlls', ()))
    prefixes = tuple(policy.get('external_system_prefixes', ('api-ms-win-', 'ext-ms-win-')))
    relations, external = [], []
    for obj in objects:
        for kind, modules in (('import', obj['pe']['imports']), ('delay-import', obj['pe']['delay_imports'])):
            for module in modules:
                pool = [candidate for candidate in by_name.get(module, ()) if candidate['container'] == obj['container']]
                near = [candidate for candidate in pool if PurePosixPath(candidate['path']).parent == PurePosixPath(obj['path']).parent]
                chosen = near or pool
                resolution = 'distributed' if len(chosen) == 1 else 'ambiguous' if chosen else 'external-system' if module in systems or module.startswith(prefixes) else 'unresolved'
                row = dict(source=obj['ref'], module=module, kind=kind, resolution=resolution,
                    targets=sorted(candidate['ref'] for candidate in chosen))
                relations.append(row)
                if resolution in ('external-system', 'unresolved'):
                    external.append(dict(source=obj['ref'], module=module, kind=kind, classification=resolution))
                if resolution in ('ambiguous', 'unresolved'):
                    gaps.append(dict(object=obj['ref'], kind='dependency-resolution', module=module, reason=resolution))
    return dict(schema_version=1, parser=dict(name='pefile', version=metadata.version('pefile')),
        file_observations=observations, containers=containers, objects=objects, relations=relations,
        external_prerequisites=external, evidence_files=evidence, native_evidence_catalog_sha256=catalog_hash,
        policy=dict(external_system_dlls=sorted(systems), external_system_prefixes=list(prefixes),
                    native_archive_paths=policy.get('native_archive_paths', ['MediaFlow.exe', 'tools/yt-dlp.exe'])),
        gaps=gaps, static_dependency_enumeration='incomplete', execution='none')


def native_components(inventory):
    components, edges, reported = [], defaultdict(set), {}
    for obj in inventory['objects']:
        props = [dict(name='mediaflow:role', value='distributed-native-file'),
            dict(name='mediaflow:native-location', value=obj['location']),
            dict(name='mediaflow:ownership-evidence', value=obj['ownership_evidence']),
            dict(name='mediaflow:license-status', value=obj['license_status']),
            dict(name='mediaflow:pe-parse-status', value=obj['pe']['status'])]
        comp = dict(type='file', name=PurePosixPath(obj['path']).name, **{'bom-ref':obj['ref']}, scope='required',
            hashes=[dict(alg='SHA-256', content=obj['sha256'])],
            evidence=dict(occurrences=[dict(location=obj['location'])]), properties=props)
        version = obj['pe']['version_resources'].get('FileVersion') or obj['pe']['version_resources'].get('FixedFileVersion')
        version_source = 'actual-PE-version-resource' if version else None
        if not version and obj.get('provenance'):
            version = provenance_release_version(PurePosixPath(obj['path']).name,
                {PurePosixPath(obj['path']).name:obj['provenance']})
            version_source = 'byte-bound-tool-provenance-release-identifier' if version else None
        if version:
            comp['version'] = version
            comp['properties'].append(dict(name='mediaflow:version-evidence', value=version_source))
        components.append(comp)
        if obj['container']:
            edges['native-file:' + obj['container']].add(obj['ref'])
        for owner in obj['distributions']:
            edges['pkg:pypi/' + quote(owner, safe='@.-')].add(obj['ref'])
        evidence = obj.get('reported_component_evidence', {})
        for library in evidence.get('reported_linked_libraries', ()):
            ref = 'reported-native:' + quote(library['name'] + '@' + library['version'], safe='@.-')
            row = reported.setdefault(ref, dict(type='library', name=library['name'], version=library['version'],
                **{'bom-ref':ref}, scope='required', properties=[dict(name='mediaflow:role', value='reported-linked-native-library'),
                    dict(name='mediaflow:linkage-status', value='reported-by-exact-binary; static details incomplete')], _evidence=[]))
            row['_evidence'].append(dict(binary_sha256=obj['sha256'], version_report_sha256=evidence['version_report_sha256']))
            edges[obj['ref']].add(ref)
    for edge in inventory['relations']:
        if edge['resolution'] == 'distributed':
            edges[edge['source']].update(edge['targets'])
    for ref, row in sorted(reported.items()):
        row['properties'].append(dict(name='mediaflow:binary-version-evidence', value=json.dumps(row.pop('_evidence'), sort_keys=True, separators=(',', ':'))))
        components.append(row)
    return components, [dict(ref=ref, dependsOn=sorted(targets)) for ref,targets in sorted(edges.items())]


def rebind_signed_bom(bom, inventory, changes):
    """Return an identity-only derivative after separate signature verification.

    This function does not authenticate a signature or permit arbitrary file
    changes. The publisher must first verify the exact permitted PE transform.
    Unsigned CI receipt/generation/attestation files are never rewritten here.
    """
    allowed = {'MediaFlow.exe', 'tools/MediaFlow.Recycle.exe', 'tools/webview2/MediaFlow.WebView2Host.exe'}
    if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
        raise NativeSBOMError('Signed SBOM rebinding permits only the three reviewed first-party PE paths.')
    result_bom, result_inventory = deepcopy(bom), deepcopy(inventory)
    if result_inventory.get('schema_version') != 2:
        raise NativeSBOMError('Signed SBOM rebinding requires the complete native inventory schema.')
    native = result_inventory.get('native_inventory', {})
    tables = [result_inventory.get('release_files', []), native.get('file_observations', [])]
    objects = {obj['location']:obj for obj in native.get('objects', [])}
    components = {comp['bom-ref']:comp for comp in result_bom.get('components', [])}
    for path, change in changes.items():
        if (not isinstance(change, dict) or set(change) != {'unsigned_sha256', 'signed_sha256', 'signed_size'}
            or not all(_HASH.fullmatch(str(change[key])) for key in ('unsigned_sha256', 'signed_sha256'))
            or change['unsigned_sha256'] == change['signed_sha256'] or type(change['signed_size']) is not int):
            raise NativeSBOMError('Signed identity changes require distinct exact hashes and an actual signed size.')
        rows = []
        for table in tables:
            found = [row for row in table if row.get('path') == path]
            if len(found) != 1:
                raise NativeSBOMError('Signed source identity is absent or duplicated in the actual release inventory.')
            rows.append(found[0])
        obj = objects.get(path)
        if not obj or obj.get('container') is not None or obj.get('first_party') is not True:
            raise NativeSBOMError('Signed source is not an observed first-party physical PE.')
        rows.append(obj)
        old_size = rows[0]['size']
        if (any(row.get('sha256') != change['unsigned_sha256'] or row.get('size') != old_size for row in rows)
            or not old_size <= change['signed_size'] <= MAX_FILE):
            raise NativeSBOMError('Signed source hashes/sizes do not match complete unsigned inventory evidence.')
        component = components.get('native-file:' + path)
        if not component or component.get('hashes') != [dict(alg='SHA-256', content=change['unsigned_sha256'])]:
            raise NativeSBOMError('Signed source component hash differs from the unsigned inventory.')
        component['hashes'][0]['content'] = change['signed_sha256']
        for row in rows:
            row['sha256'], row['size'] = change['signed_sha256'], change['signed_size']
        # The old certificate-table observation must not masquerade as an
        # observation of the signed file. Authenticode verification is separate.
        obj['pe']['certificate_table'] = dict(present=None, trust='not-verified-by-PE-inventory',
                                             inventory_stage='post-signing-identity-only')
        for container in native.get('containers', []):
            if container['path'] == path:
                if container['sha256'] != change['unsigned_sha256']:
                    raise NativeSBOMError('Signed archive container has a conflicting unsigned identity.')
                container['sha256'] = change['signed_sha256']
        if path == 'MediaFlow.exe':
            root = result_bom['metadata']['component']
            if root.get('hashes') != [dict(alg='SHA-256', content=change['unsigned_sha256'])]:
                raise NativeSBOMError('Root application hash differs from the original unsigned EXE.')
            root['hashes'][0]['content'] = change['signed_sha256']
    # Inventory bytes use the generator's established public JSON encoding.
    inventory_hash = sha256((json.dumps(result_inventory, ensure_ascii=False, sort_keys=True,
                                        indent=2, allow_nan=False) + '\n').encode('utf-8'))
    for prop in result_bom['metadata'].get('properties', []):
        if prop['name'] == 'mediaflow:inventory-sha256':
            prop['value'] = inventory_hash
    return result_bom, result_inventory
