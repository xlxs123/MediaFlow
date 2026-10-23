from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.downloader import DownloadRequest
from core.recovery_workspace import RecoveryWorkspace, file_hash


class AuxiliaryPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.request = DownloadRequest('test', 'https://test.local/video', str(self.root), filename='media',
            output_extension='mp4', output_path=str(self.root / 'media.mp4'))
        self.identity = {'protocol': 'Direct', 'tracks_sha256': 'selection'}
        self.workspace = RecoveryWorkspace(self.request, self.identity, 'Direct')
        self.workspace.prepare()
        self.media = self.workspace.home / 'media.mp4'
        self.media.write_bytes(b'verified-media-placeholder-for-file-publication-test')
        self.subtitle = self.workspace.home / 'subtitle.srt'
        self.subtitle.write_bytes(b'1\n00:00:00,200 --> 00:00:01,000\nSubtitle\n')
        self.auxiliary = [{'path': str(self.subtitle), 'id': 'abc123456789', 'language': 'en', 'format': 'srt', 'status': 'verified'}]

    def tearDown(self):
        self.workspace.close()
        self.temporary.cleanup()

    def test_sidecar_has_distinct_path_and_existing_file_is_not_replaced(self):
        occupied = self.root / 'media.en.abc12345.srt'
        occupied.write_bytes(b'Existing user subtitle')
        validation = {}
        files = self.workspace.publish([str(self.media)], [validation], self.auxiliary)
        self.assertEqual(files, [self.request.output_path])
        sidecar = validation['subtitle_files'][0]['path']
        self.assertNotEqual(sidecar, str(occupied))
        self.assertEqual(occupied.read_bytes(), b'Existing user subtitle')
        self.assertTrue(Path(sidecar).is_file())
        self.assertEqual(self.workspace.committed_files(), files)

    def test_interrupted_between_media_and_subtitle_renames_resumes_without_redownload(self):
        real_rename = Path.rename
        def rename(path, target):
            if path == self.subtitle:
                raise PermissionError('temporary subtitle lock')
            return real_rename(path, target)
        with patch.object(Path, 'rename', rename):
            with self.assertRaises(PermissionError):
                self.workspace.publish([str(self.media)], [{}], self.auxiliary)
        self.assertTrue(Path(self.request.output_path).is_file())
        self.workspace.checkpoint(final=True)
        self.assertEqual(self.workspace.state['status'], 'publishing')
        reopened = RecoveryWorkspace(self.request, self.identity, 'Direct')
        reopened.resume_auxiliary_publication()
        self.assertEqual(reopened.committed_files(), [self.request.output_path])
        self.assertTrue(Path(reopened.subtitle_files()[0]['path']).is_file())
        reopened.close()

    def test_changed_sidecar_blocks_completed_recovery(self):
        validation = {}
        self.workspace.publish([str(self.media)], [validation], self.auxiliary)
        Path(validation['subtitle_files'][0]['path']).write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.workspace.committed_files()
