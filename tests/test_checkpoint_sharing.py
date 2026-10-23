"""真实 Windows 句柄和改名竞态；不依赖下载速度碰运气。"""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.downloader import DownloadRequest
from core.recovery_workspace import RecoveryWorkspace, file_hash


class CheckpointSharingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        request=DownloadRequest('yt-dlp.exe','https://example.test/video.mp4',self.temp.name)
        self.workspace=RecoveryWorkspace(request,{'layout_sha256':'test'},'DASH')
        self.workspace.prepare()

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipUnless(os.name=='nt','Windows文件共享行为')
    def test_checkpoint_reader_does_not_block_native_rename(self):
        source=self.workspace.temporary/'中文媒体.mp4'
        source.write_bytes(b'actual-media-bytes')
        renamed=source.with_name('renamed.mp4')
        original=hashlib.file_digest
        def rename_while_reading(stream,algorithm):
            source.rename(renamed)
            return original(stream,algorithm)
        with patch('core.recovery_workspace.hashlib.file_digest',side_effect=rename_while_reading):
            self.assertEqual(file_hash(source),hashlib.sha256(b'actual-media-bytes').hexdigest())
        self.assertEqual(renamed.read_bytes(),b'actual-media-bytes')

    def test_active_hls_fragment_is_not_hashed_as_final_media(self):
        self.workspace.protocol='HLS'
        fragment=self.workspace.temporary/'video.mp4.part-Frag1';fragment.write_bytes(b'active')
        with patch('core.recovery_workspace.file_hash',side_effect=AssertionError('active fragment must not be read')):
            self.workspace.checkpoint()
        self.assertFalse(self.workspace.state['files'])

    @unittest.skipUnless(os.name=='nt','Windows字节范围锁')
    def test_busy_active_file_skips_background_but_final_check_remains_strict(self):
        import msvcrt
        path=self.workspace.temporary/'track.m4a';path.write_bytes(b'locked media')
        with path.open('r+b') as handle:
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            try:
                self.workspace.checkpoint()
                self.assertFalse(self.workspace.state['files'])
                with self.assertRaises(PermissionError):
                    self.workspace.checkpoint(final=True)
            finally:
                handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        self.workspace.checkpoint(final=True)
        self.assertTrue(self.workspace.state['files'])
