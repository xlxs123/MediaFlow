"""Restore exactly pinned FFmpeg tools if their historical upstream ZIP is gone."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import prepare_tools


# The published 2.1.0 Ed25519 manifest binds this exact package and both
# binaries. Keep the original BtbN pin in prepare_tools and its provenance.
RELEASE_URL = ('https://github.com/xlxs123/MediaFlow-Releases/releases/download/v2.1.0/'
               'MediaFlow-2.1.0-20261023T075252Z-50a032a34533.zip')
RELEASE_BYTES = 371249271
RELEASE_SHA256 = 'ab560859e0b7be22b5b2cfa2f50f9b3fc63c5489c2bd8c4246dd1c0e4b407b6c'


def download_release(path):
    request = Request(RELEASE_URL, headers={'User-Agent': 'MediaFlow-CI/1.0'})
    with urlopen(request, timeout=45) as response, path.open('xb') as output:
        remaining = RELEASE_BYTES
        while remaining:
            block = response.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError('Pinned tool cache package is truncated.')
            output.write(block)
            remaining -= len(block)
        if response.read(1):
            raise RuntimeError('Pinned tool cache package exceeds its declared size.')


def restore_ffmpeg(package, tools):
    with package.open('rb') as stream:
        if os.fstat(stream.fileno()).st_size != RELEASE_BYTES:
            raise RuntimeError('Pinned tool cache package size does not match.')
        if hashlib.file_digest(stream, 'sha256').hexdigest() != RELEASE_SHA256:
            raise RuntimeError('Pinned tool cache package SHA256 does not match.')
        with (ZipFile(stream) as archive,
              tempfile.TemporaryDirectory(prefix='mediaflow-ci-ffmpeg-', dir=tools) as directory):
            stage = Path(directory)
            for name, digest in prepare_tools.FFMPEG_FILES.items():
                with archive.open('tools/' + name) as source, (stage / name).open('xb') as output:
                    shutil.copyfileobj(source, output)
                prepare_tools.verify(stage / name, digest)
                if (tools / name).exists():
                    prepare_tools.verify(tools / name, digest)
            for name in prepare_tools.FFMPEG_FILES:
                if not (tools / name).exists():
                    (stage / name).rename(tools / name)


def main():
    tools = prepare_tools.ROOT
    if (tools / prepare_tools.FFMPEG_NAME).is_file() or all(
            (tools / name).is_file() for name in prepare_tools.FFMPEG_FILES):
        prepare_tools.main()
        return
    with tempfile.TemporaryDirectory(prefix='mediaflow-ci-tools-') as scratch:
        upstream = Path(scratch) / prepare_tools.FFMPEG_NAME
        try:
            prepare_tools.download(prepare_tools.FFMPEG_URL, upstream)
        except HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            print('Historical BtbN ZIP returned HTTP 404; verifying the pinned MediaFlow tool cache.', flush=True)
            package = Path(scratch) / 'stable-tool-cache.zip'
            download_release(package)
            restore_ffmpeg(package, tools)
            prepare_tools.main()
        else:
            prepare_tools.main(upstream)


if __name__ == '__main__':
    main()
