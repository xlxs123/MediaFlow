"""Install only checksum-pinned public scanner binaries into a fresh directory."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import ssl
import tarfile
import time
from urllib.parse import urlsplit
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


class ToolVerificationError(RuntimeError):
    pass


def approved_url(value):
    address = urlsplit(value)
    return (address.scheme == 'https' and address.hostname in HOSTS and address.port in (None, 443)
        and not address.username and not address.password and not address.fragment)


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not approved_url(newurl):
            raise ToolVerificationError('Scanner download redirected outside the approved HTTPS hosts.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def platform_key():
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        raise ToolVerificationError('Only explicitly pinned Windows/Linux x64 scanners are supported.')
    if os.name == 'nt':
        return 'windows-x64'
    if platform.system() == 'Linux':
        return 'linux-x64'
    raise ToolVerificationError('Scanner platform has no independently verified binary pin.')


def extract_binary(data, record):
    """Read one ordinary named member; never extract an archive's paths."""
    name = record['executable_name']
    if not re.fullmatch(r'(?:gitleaks|actionlint)(?:\.exe)?', name):
        raise ToolVerificationError('Unexpected scanner executable name.')
    if record['archive_url'].endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            matches = [item for item in archive.infolist() if item.filename == name]
            if len(matches) != 1 or matches[0].is_dir() or matches[0].flag_bits & 1:
                raise ToolVerificationError('Scanner archive member is missing, duplicated or encrypted.')
            member = matches[0]
            mode = (member.external_attr >> 16) & 0o170000
            if mode not in (0, 0o100000) or not 0 < member.file_size < 64 * 1024 * 1024:
                raise ToolVerificationError('Scanner archive member is not an ordinary bounded file.')
            binary = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            matches = [item for item in archive.getmembers() if item.name == name]
            if len(matches) != 1 or not matches[0].isfile() or not 0 < matches[0].size < 64 * 1024 * 1024:
                raise ToolVerificationError('Scanner archive member is not an ordinary bounded file.')
            binary = archive.extractfile(matches[0]).read()
    if hashlib.sha256(binary).hexdigest() != record['executable_sha256']:
        raise ToolVerificationError('Scanner executable SHA-256 differs from the reviewed pin.')
    return binary


def install(name, directory, *, manifest=None):
    manifest = manifest or json.loads((ROOT / 'security/toolchain.json').read_text('utf-8'))
    if name not in ('gitleaks', 'actionlint'):
        raise ToolVerificationError('Scanner is not in the fixed tool allowlist.')
    record = manifest['binary_tools'][name]['platforms'][platform_key()]
    if not approved_url(record['archive_url']) or urlsplit(record['archive_url']).query:
        raise ToolVerificationError('Pinned scanner source must be an ordinary approved HTTPS URL.')
    if not 0 < record['archive_size'] < 32 * 1024 * 1024:
        raise ToolVerificationError('Pinned scanner archive exceeds its size bound.')
    directory = Path(directory).absolute()
    if directory.exists() or any(path.is_symlink() or path.is_junction() for path in directory.parents):
        raise ToolVerificationError('Use a fresh ordinary scanner directory; existing files are preserved.')
    directory.mkdir(parents=True)
    opener = urllib.request.build_opener(Redirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(record['archive_url'], headers={'User-Agent': 'MediaFlow-Security-Tools',
        'Accept-Encoding': 'identity'})
    started = time.monotonic()
    chunks = []
    received = 0
    with opener.open(request, timeout=15) as response:
        if response.status != 200 or not approved_url(response.geturl()):
            raise ToolVerificationError('Official scanner source returned an unexpected response.')
        while block := response.read(128 * 1024):
            received += len(block)
            if received > record['archive_size'] or time.monotonic() - started > 180:
                raise ToolVerificationError('Scanner download exceeded the pinned size or deadline.')
            chunks.append(block)
    data = b''.join(chunks)
    if len(data) != record['archive_size'] or hashlib.sha256(data).hexdigest() != record['archive_sha256']:
        raise ToolVerificationError('Official scanner archive size or SHA-256 mismatch.')
    binary = extract_binary(data, record)
    target = directory / record['executable_name']
    with target.open('xb') as handle:
        handle.write(binary)
    if os.name != 'nt':
        target.chmod(0o700)
    receipt = dict(passed=True, tool=name, version=manifest['binary_tools'][name]['version'],
        platform=platform_key(), archive_sha256=record['archive_sha256'],
        executable_sha256=record['executable_sha256'], official_source=record['archive_url'])
    with (directory / 'verified-tool.json').open('x', encoding='utf-8') as handle:
        json.dump(receipt, handle, indent=2)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool', choices=('gitleaks', 'actionlint'), required=True)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        install(args.tool, args.directory)
        print(json.dumps(dict(passed=True, tool=args.tool)))
        return 0
    except Exception as error:
        print(json.dumps(dict(passed=False, error_type=type(error).__name__,
            error=str(error) if isinstance(error, ToolVerificationError) else
                'Pinned scanner preparation failed; no unverified tool was executed.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
