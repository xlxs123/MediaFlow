"""Read actual Windows version resources; never run the recycle executable."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from utils.branding import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class NativeVersionInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != 'win32':
            raise RuntimeError('These native build checks require the Windows release-test environment.')
        cls.pwsh = os.environ.get('MEDIAFLOW_PWSH') or shutil.which('pwsh')
        if not cls.pwsh:
            raise RuntimeError('PowerShell 7 is required; native-version checks cannot be skipped.')
        cls.csc = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not cls.csc.is_file():
            raise RuntimeError('The existing Windows .NET x64 compiler is required.')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mediaflow-native-version-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def powershell(self, command, *, extra_env=None, cwd=None):
        environment = dict(os.environ)
        environment.update(extra_env or {})
        return subprocess.run(
            [self.pwsh, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command],
            cwd=cwd or self.work, env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=90, creationflags=NO_WINDOW,
        )

    def version_info(self, executable):
        result = self.powershell(
            "$ErrorActionPreference='Stop'; "
            "$v=[Diagnostics.FileVersionInfo]::GetVersionInfo($env:MEDIAFLOW_TEST_PE); "
            "[ordered]@{product=$v.ProductName;product_version=$v.ProductVersion;"
            "file_version=$v.FileVersion;file_parts=@($v.FileMajorPart,$v.FileMinorPart,"
            "$v.FileBuildPart,$v.FilePrivatePart)}|ConvertTo-Json -Compress",
            extra_env={'MEDIAFLOW_TEST_PE': str(executable)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip())

    @staticmethod
    def protected_output_digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def test_recycle_real_build_has_current_product_and_numeric_file_version(self):
        """Exercise the actual build script, then inspect rather than launch its PE."""
        existing = ROOT / 'tools/MediaFlow.Recycle.exe'
        before = self.protected_output_digest(existing)
        output = self.work / 'isolated-recycle'
        result = self.powershell(
            "$ErrorActionPreference='Stop'; & $env:MEDIAFLOW_TEST_BUILD "
            "-OutputDirectory $env:MEDIAFLOW_TEST_OUTPUT",
            extra_env={'MEDIAFLOW_TEST_BUILD': str(SCRIPTS / 'build-recycle.ps1'),
                       'MEDIAFLOW_TEST_OUTPUT': str(output)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        executable = output / 'MediaFlow.Recycle.exe'
        self.assertTrue(executable.is_file())
        binary = executable.read_bytes()
        self.assertEqual(binary[:2], b'MZ')
        offset = struct.unpack_from('<I', binary, 0x3c)[0]
        self.assertEqual(binary[offset:offset + 4], b'PE\0\0')
        self.assertEqual(struct.unpack_from('<H', binary, offset + 4)[0], 0x8664)
        actual = self.version_info(executable)
        expected_parts = [int(part) for part in APP_VERSION.split('.')] + [0]
        self.assertEqual(actual['product'], 'MediaFlow')
        self.assertEqual(actual['product_version'], APP_VERSION)
        self.assertEqual(actual['file_version'], APP_VERSION + '.0')
        self.assertEqual(actual['file_parts'], expected_parts)
        self.assertEqual(self.protected_output_digest(existing), before)

    def test_requested_project_version_is_compiled_in_an_unrelated_working_directory(self):
        """A second real PE proves metadata comes from ProjectRoot, not a constant/CWD."""
        project = self.work / 'synthetic-project'
        (project / 'utils').mkdir(parents=True)
        (project / 'utils/branding.py').write_text("APP_VERSION = '7.8.9'\n", encoding='utf-8')
        version_source = self.work / 'VersionInfo.cs'
        unrelated = self.work / 'unrelated-cwd'
        unrelated.mkdir()
        result = self.powershell(
            "$ErrorActionPreference='Stop'; . $env:MEDIAFLOW_TEST_HELPER; "
            "Write-MediaFlowNativeVersion -ProjectRoot $env:MEDIAFLOW_TEST_PROJECT "
            "-Path $env:MEDIAFLOW_TEST_VERSION_SOURCE",
            extra_env={'MEDIAFLOW_TEST_HELPER': str(SCRIPTS / 'native-version-info.ps1'),
                       'MEDIAFLOW_TEST_PROJECT': str(project),
                       'MEDIAFLOW_TEST_VERSION_SOURCE': str(version_source)}, cwd=unrelated,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(version_source.is_file())
        program = self.work / 'Program.cs'
        program.write_text('internal class Program { public static void Main() {} }', encoding='utf-8')
        executable = self.work / 'version-fixture.exe'
        compiled = subprocess.run(
            [str(self.csc), '/nologo', '/target:exe', '/platform:x64',
             '/out:' + str(executable), str(program), str(version_source)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=45, creationflags=NO_WINDOW,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stdout.decode('utf-8', errors='replace'))
        actual = self.version_info(executable)
        self.assertEqual(actual['product'], 'MediaFlow')
        self.assertEqual(actual['product_version'], '7.8.9')
        self.assertEqual(actual['file_version'], '7.8.9.0')
        self.assertEqual(actual['file_parts'], [7, 8, 9, 0])

    def test_invalid_or_out_of_range_source_version_never_creates_csharp_output(self):
        for index, version in enumerate((
            '', '2.1', '2.1.0-beta.1', '-1.2.3', '2.1.0.0',
            '65536.0.0', '0.65536.0', '0.0.65536',
            '2147483648.0.0', '999999999999999999999999999999.0.0',
            '2.1.0"; invalid source text',
        )):
            with self.subTest(version=version):
                project = self.work / ('invalid-' + str(index))
                (project / 'utils').mkdir(parents=True)
                (project / 'utils/branding.py').write_text(
                    "APP_VERSION = '" + version + "'\n", encoding='utf-8',
                )
                output = project / 'must-not-exist.cs'
                result = self.powershell(
                    "$ErrorActionPreference='Stop'; . $env:MEDIAFLOW_TEST_HELPER; "
                    "Write-MediaFlowNativeVersion -ProjectRoot $env:MEDIAFLOW_TEST_PROJECT "
                    "-Path $env:MEDIAFLOW_TEST_VERSION_SOURCE",
                    extra_env={'MEDIAFLOW_TEST_HELPER': str(SCRIPTS / 'native-version-info.ps1'),
                               'MEDIAFLOW_TEST_PROJECT': str(project),
                               'MEDIAFLOW_TEST_VERSION_SOURCE': str(output)},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists(), 'Invalid branding must fail before output creation.')

    def test_webview_offline_missing_sdk_fails_without_fetch_or_host(self):
        """Run a clean isolated project with -Offline; no network or SDK skip required."""
        project = self.work / 'offline-webview-project'
        scripts = project / 'scripts'
        scripts.mkdir(parents=True)
        (project / 'utils').mkdir()
        for name in ('build-webview2.ps1', 'native-version-info.ps1'):
            shutil.copyfile(SCRIPTS / name, scripts / name)
        shutil.copyfile(ROOT / 'utils/branding.py', project / 'utils/branding.py')
        output = self.work / 'offline-webview-output'
        result = self.powershell(
            "$ErrorActionPreference='Stop'; "
            "function Invoke-WebRequest { throw 'TEST_UNEXPECTED_NETWORK_REQUEST' }; "
            "& $env:MEDIAFLOW_TEST_BUILD -Offline -OutputDirectory $env:MEDIAFLOW_TEST_OUTPUT",
            extra_env={'MEDIAFLOW_TEST_BUILD': str(scripts / 'build-webview2.ps1'),
                       'MEDIAFLOW_TEST_OUTPUT': str(output)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('The pinned WebView2 SDK package is not cached.', result.stderr)
        self.assertNotIn('TEST_UNEXPECTED_NETWORK_REQUEST', result.stdout + result.stderr)
        self.assertFalse((output / 'MediaFlow.WebView2Host.exe').exists())
        self.assertFalse((project / 'tools/webview2/MediaFlow.WebView2Host.exe').exists())
        self.assertFalse(any((project / 'native/webview2/.sdk').glob('*.nupkg')))


if __name__ == '__main__':
    unittest.main()
