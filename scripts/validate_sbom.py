"""Independent official CycloneDX schema and public inventory validation."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


class SBOMValidationError(RuntimeError):
    pass


def public_strings(value):
    if isinstance(value, str):
        if (re.search(r'(?i)(?<![A-Za-z0-9])[a-z]:[\\/]|file://|(?:^|\s)/(?:home|Users|private|tmp|var|mnt)/', value)
            or re.search(r'BEGIN (?:ENCRYPTED )?PRIVATE KEY|github_pat_|gh[pousr]_[A-Za-z0-9]{20}', value)):
            raise SBOMValidationError('SBOM contains a host path or secret marker; values were not logged.')
    elif isinstance(value, list):
        for item in value:
            public_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            public_strings(key)
            public_strings(item)


def validate(directory):
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator
    directory = Path(directory)
    output = directory / 'validation.json'
    if output.exists():
        raise SBOMValidationError('Existing SBOM validation evidence is preserved.')
    validator_version = metadata.version('cyclonedx-python-lib')
    pins = json.loads((ROOT / 'security/toolchain.json').read_text('utf-8'))['python_tools']
    if validator_version != pins['cyclonedx-python-lib']:
        raise SBOMValidationError('Official CycloneDX validator version differs from the verified pin.')
    names = ('mediaflow.cdx.json', 'inventory.json', 'generation.json')
    documents, hashes, raw = {}, {}, {}
    for name in names:
        path = directory / name
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size < 32 * 1024 * 1024:
            raise SBOMValidationError('SBOM evidence is missing or exceeds its file bound.')
        raw[name] = path.read_bytes()
        hashes[name] = hashlib.sha256(raw[name]).hexdigest()
        documents[name] = json.loads(raw[name])
        public_strings(documents[name])
    generation = documents['generation.json']
    if (generation.get('passed') is not True or generation.get('receipt_verified') is not True
        or generation.get('sbom_sha256') != hashes['mediaflow.cdx.json']
        or generation.get('inventory_sha256') != hashes['inventory.json']):
        raise SBOMValidationError('SBOM hashes and validated generation inventory do not agree.')
    checksums = (directory / 'SHA256SUMS.txt').read_text('ascii').splitlines()
    expected = [hashes[name] + '  ' + name for name in names[:2]]
    if checksums != expected:
        raise SBOMValidationError('SBOM checksum summary differs from the actual documents.')
    validator = JsonStrictValidator(SchemaVersion.V1_6)
    result = validator.validate_str(raw['mediaflow.cdx.json'].decode('utf-8'), all_errors=True)
    errors = list(result) if result is not None else []
    if errors:
        raise SBOMValidationError('Official CycloneDX 1.6 schema rejected the inventory; error count: ' + str(len(errors)))
    bom = documents['mediaflow.cdx.json']
    if bom.get('bomFormat') != 'CycloneDX' or bom.get('specVersion') != '1.6':
        raise SBOMValidationError('SBOM must explicitly declare the verified CycloneDX 1.6 format.')
    refs = [component['bom-ref'] for component in bom.get('components', [])]
    root_ref = bom['metadata']['component']['bom-ref']
    if len(refs) != len(set(refs)) or root_ref in refs:
        raise SBOMValidationError('SBOM component identities are not unique.')
    if any(reference not in set(refs) | {root_ref} for entry in bom.get('dependencies', [])
        for reference in [entry['ref'], *entry.get('dependsOn', [])]):
        raise SBOMValidationError('SBOM dependency relationship points to an absent component.')
    tools = bom.get('metadata', {}).get('tools', {}).get('components', [])
    if set(refs) & {tool['bom-ref'] for tool in tools}:
        raise SBOMValidationError('Build-only tools were mixed into runtime component identities.')
    report = dict(passed=True, format='CycloneDX', spec_version='1.6', schema_errors=0,
        validator='cyclonedx-python-lib JsonStrictValidator', validator_version=validator_version,
        document_sha256=hashes, github=generation['github'], components=len(refs),
        actual_inventory_hashes_verified=True, build_tool_components=len(tools), host_paths_or_secret_markers=0,
        cryptographic_provenance_verified=False,
        scope='Official schema plus generation-byte and public-data checks; provenance is separately attested and verified.')
    for name in names:
        if (directory / name).read_bytes() != raw[name]:
            raise SBOMValidationError('SBOM evidence changed during validation.')
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        result = validate(args.directory)
        print(json.dumps(dict(passed=True, schema_errors=0, components=result['components'])))
        return 0
    except Exception as error:
        print(json.dumps(dict(passed=False, error_type=type(error).__name__, error=str(error)
            if isinstance(error, SBOMValidationError) else 'Independent SBOM verification failed; input values were not logged.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
