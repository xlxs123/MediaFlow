import unittest
from unittest.mock import patch

from core.yt_dlp_manager import detect_yt_dlp, detect_ffmpeg, detect_ffprobe, ToolInfo


class ManagerTests(unittest.TestCase):
    def test_missing_no_crash(self):
        with patch('core.yt_dlp_manager.get_tool_manager') as manager:
            manager.return_value.detect.return_value = ToolInfo(error='未检测到 yt-dlp', status='missing')
            result = detect_yt_dlp()
            self.assertFalse(result.found)
            self.assertIn('未检测到', result.error)

    def test_explicit_exe_and_parameter_array(self):
        # Public compatibility functions must forward the explicit choice and
        # cancellation to the shared manager, rather than maintain a second probe.
        for function, name in ((detect_yt_dlp, 'yt-dlp'), (detect_ffmpeg, 'ffmpeg'), (detect_ffprobe, 'ffprobe')):
            with patch('core.yt_dlp_manager.get_tool_manager') as manager:
                expected = ToolInfo('chosen.exe', '2026.09.23')
                manager.return_value.detect.return_value = expected
                self.assertIs(function('chosen.exe'), expected)
                manager.return_value.detect.assert_called_once_with(name, 'chosen.exe', mode='custom', cancel_event=None)

    def test_timeout_handled(self):
        with patch('core.yt_dlp_manager.get_tool_manager') as manager:
            manager.return_value.detect.return_value = ToolInfo(error='版本检测超时', status='timeout')
            self.assertEqual(detect_yt_dlp().status, 'timeout')


if __name__ == '__main__':
    unittest.main()
