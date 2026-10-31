"""Read-only actual-build acceptance for the pinned onefile archive contract."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from importlib.metadata import version
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))

from scripts.pyinstaller_payload import (PYINSTALLER_VERSION, MAGIC,
    MAX_CARRIER, PayloadError, archive_unchanged)


def check(release, signed_executable=None):
    from scripts.package_update import _plain
    from core.data_migration import frozen_file
    from PyInstaller.archive.readers import CArchiveReader
    if version('PyInstaller') != PYINSTALLER_VERSION:
        raise PayloadError('Review the bootloader archive contract before changing PyInstaller.')
    path = _plain(Path(release) / 'MediaFlow.exe')
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_CARRIER:
        raise PayloadError('Actual main executable is absent or exceeds its bound.')
    with frozen_file(path) as stream:
        data = stream.read(MAX_CARRIER + 1)
        identity = archive_unchanged(data, data)
        upstream = CArchiveReader._find_magic_pattern(io.BytesIO(data), MAGIC)
        if upstream != identity['cookie_offset'] or data[:2] != b'MZ':
            raise PayloadError('Actual carrier differs from the pinned upstream archive selector.')
    signature = None
    if signed_executable is not None:
        returned = _plain(signed_executable)
        if not returned.is_file() or not 0 < returned.stat().st_size <= MAX_CARRIER:
            raise PayloadError('Returned normal signed carrier is absent or exceeds its bound.')
        with frozen_file(returned) as stream:
            signed_data = stream.read(MAX_CARRIER + 1)
        from scripts.release_security import first_party_change
        signature = first_party_change('MediaFlow.exe', data, signed_data)
    return dict(passed=True, packaging='pyinstaller-onefile-with-selected-archive-binding',
        main_executable_size=len(data), unsigned_carrier_sha256=hashlib.sha256(data).hexdigest(),
        signed_carrier_size=len(signed_data) if signature is not None else None,
        signed_carrier_sha256=signature['signed_sha256'] if signature is not None else None,
        archive=identity,
        upstream_selector_agrees=True, unsigned_build=signed_executable is None,
        signed_carrier_binding=signature,
        native_signature_verified=False, signed_execution_verified=False,
        scope='Read-only normal unsigned build acceptance; not production signing acceptance.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--signed-executable', help='Optional existing normal signed carrier; read-only, never executed')
    args = parser.parse_args(argv)
    from scripts.package_update import _plain
    output = Path(args.report).absolute()
    _plain(output.parent)
    if output.exists() or output.is_relative_to(Path(args.release_dir).resolve()):
        raise SystemExit('Use a new external report; original release/evidence is preserved.')
    try:
        result = check(args.release_dir, args.signed_executable)
    except Exception as error:
        result = dict(passed=False, error_type=type(error).__name__, error=str(error)
            if isinstance(error, PayloadError) else 'Packaging compatibility inspection failed.')
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(dict(passed=result['passed'])))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
