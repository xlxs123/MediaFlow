"""The CI fallback retains package and both native executable pins."""
import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from zipfile import ZipFile

from scripts import prepare_ci_tools as cache


class PrepareCIToolsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='mediaflow-ci-tool-fixture-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        self.package = self.root / 'pinned.zip'
        self.files = {'ffmpeg.exe': b'fixture ffmpeg', 'ffprobe.exe': b'fixture ffprobe'}
        self.pins = {name: hashlib.sha256(content).hexdigest() for name, content in self.files.items()}

    def make_release(self, files):
        with ZipFile(self.package, 'x') as archive:
            for name, data in files.items():
                archive.writestr('tools/' + name, data)
            archive.writestr('MediaFlow.exe', b'not a tool')
        self.enterContext(patch.object(cache, 'RELEASE_BYTES', self.package.stat().st_size))
        self.enterContext(patch.object(cache, 'RELEASE_SHA256', hashlib.sha256(self.package.read_bytes()).hexdigest()))
        self.enterContext(patch.object(cache.prepare_tools, 'FFMPEG_FILES', self.pins))

    def test_pinned_release_restores_only_the_verified_tools(self):
        self.make_release(self.files)
        cache.restore_ffmpeg(self.package, self.tools)
        self.assertEqual({path.name: path.read_bytes() for path in self.tools.iterdir()}, self.files)

    def test_tampered_archive_is_rejected_before_extracting(self):
        self.make_release(self.files)
        original = self.package.read_bytes()
        self.package.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaisesRegex(RuntimeError, 'SHA256'):
            cache.restore_ffmpeg(self.package, self.tools)
        self.assertEqual(list(self.tools.iterdir()), [])

    def test_wrong_second_tool_never_installs_first_tool(self):
        self.make_release(self.files | {'ffprobe.exe': b'incompatible tool'})
        with self.assertRaisesRegex(RuntimeError, 'ffprobe.exe SHA256'):
            cache.restore_ffmpeg(self.package, self.tools)
        self.assertEqual(list(self.tools.iterdir()), [])

    def test_only_upstream_404_enables_restore_then_normal_validation(self):
        for status in (403, 404):
            error = HTTPError(cache.prepare_tools.FFMPEG_URL, status, 'test', {}, BytesIO())
            self.addCleanup(error.close)
            with self.subTest(status=status), patch.object(cache.prepare_tools, 'ROOT', self.tools), \
                    patch.object(cache.prepare_tools, 'download', side_effect=error), \
                    patch.object(cache, 'download_release') as fetch, \
                    patch.object(cache, 'restore_ffmpeg') as restore, \
                    patch.object(cache.prepare_tools, 'main') as validate:
                if status == 404:
                    cache.main()
                    fetch.assert_called_once()
                    restore.assert_called_once()
                    validate.assert_called_once_with()
                else:
                    with self.assertRaises(HTTPError):
                        cache.main()
                    fetch.assert_not_called()
                    restore.assert_not_called()
                    validate.assert_not_called()
