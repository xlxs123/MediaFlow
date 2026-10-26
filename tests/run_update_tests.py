"""Focused update/release regression; each QApplication suite owns a process."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.process_tree import ProcessTree, NO_WINDOW
from utils.build_info import source_digest

GROUPS = {
    'core': ['test_update_manifest', 'test_update_ed25519', 'test_update_manager',
             'test_update_health', 'test_update_package', 'test_publish_release',
             'test_online_update', 'test_update_transport', 'test_update_channel',
             'test_hosted_release', 'test_release_workflow', 'test_brand_migration',
             'test_security', 'test_tool_manager', 'test_update_github',
             'test_public_source_audit', 'test_release_structure', 'test_github_publisher',
             'test_ci_build_receipt', 'test_release_pipeline', 'test_accept_release_update',
             'test_dependency_audit', 'test_secret_scan', 'test_sbom', 'test_security_gate', 'test_security_tools',
             'test_codeql_reviews', 'test_fixture_security', 'test_public_metadata_contract',
             'test_authenticode', 'test_release_security', 'test_native_sbom', 'test_sbom_validation'],
    'ui': ['test_update_ui'],
    'output': ['test_output_integration'],
}


def worker(group, directory):
    before = source_digest(ROOT)
    suite = unittest.defaultTestLoader.loadTestsFromNames(['tests.' + name for name in GROUPS[group]])
    stream = io.StringIO()
    # Legacy fixtures describe an unconfigured installation. They must not
    # contact the publisher merely because an operator approved a build channel.
    # Channel-specific cases explicitly inject approved roots; native acceptance
    # always reads the real resource embedded in the final EXE.
    empty = {'schema_version': 1, 'product': 'MediaFlow', 'channel': 'stable',
             'source_url': '', 'trusted_keys': {}, 'key_state': 'pending'}
    with patch('core.update_manager.load_channel', return_value=empty):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    value = {'passed': result.wasSuccessful(), 'group': group, 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'source_sha256': before, 'source_unchanged': before == source_digest(ROOT),
        'channel_fixture': 'unconfigured; channel tests explicitly override with test roots'}
    value['skips'] = [{'test':test.id(), 'reason':reason} for test,reason in result.skipped]
    value['unexpected_skips'] = [item for item in value['skips'] if item != {
        'test':'tests.test_public_source_audit.PublicSourceAuditTests.test_worktree_symlink_is_never_followed',
        'reason':'Creating symlinks is not permitted for this test user'}]
    value['passed'] = value['passed'] and not value['unexpected_skips']
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (group + '.txt')).write_text(stream.getvalue(), 'utf-8')
    (directory / (group + '.json')).write_text(json.dumps(value, indent=2), 'utf-8')
    print(json.dumps(value))
    return 0 if value['passed'] and value['source_unchanged'] else 1


def run(directory):
    directory.mkdir(parents=True, exist_ok=True)
    before = source_digest(ROOT)
    groups = []
    for group in GROUPS:
        tree = None
        started = time.monotonic()
        with (directory / (group + '.stdout.log')).open('w', encoding='utf-8') as stdout, \
                (directory / (group + '.stderr.log')).open('w', encoding='utf-8') as stderr:
            process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                '--report-dir', str(directory), '--group', group], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                env=dict(os.environ, PYTHONIOENCODING='utf-8', QT_QPA_PLATFORM='offscreen'),
                creationflags=NO_WINDOW)
            try:
                tree = ProcessTree(process)
                # Core includes complete-file receipt/tamper checks. Keep a
                # finite budget for real disk reads rather than skipping them.
                timeout = 900 if group == 'core' else 360
                while process.poll() is None and time.monotonic() - started < timeout:
                    time.sleep(.1)
                if process.poll() is None:
                    tree.terminate()
                exit_code = process.wait(timeout=10)
            finally:
                if tree:
                    tree.close()
        file = directory / (group + '.json')
        value = json.loads(file.read_text('utf-8')) if file.is_file() else {
            'group': group, 'tests': 0, 'failures': 0, 'errors': 1, 'skipped': 0, 'passed': False}
        value.update(exit_code=exit_code, elapsed_seconds=round(time.monotonic()-started, 3))
        value['passed'] = value['passed'] and exit_code == 0 and value.get('source_sha256') == before
        groups.append(value)
        if not value['passed']:
            break
    result = {'passed': len(groups) == len(GROUPS) and all(value['passed'] for value in groups),
        'groups': groups, 'source_sha256': before, 'source_unchanged': before == source_digest(ROOT),
        **{name: sum(value[name] for value in groups) for name in ('tests', 'failures', 'errors', 'skipped')}}
    result['passed'] = result['passed'] and result['source_unchanged']
    (directory / 'results.json').write_text(json.dumps(result, indent=2), 'utf-8')
    print(json.dumps(result))
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', required=True)
    parser.add_argument('--group', choices=GROUPS)
    options = parser.parse_args()
    directory = Path(options.report_dir).resolve()
    return worker(options.group, directory) if options.group else run(directory)


if __name__ == '__main__':
    raise SystemExit(main())
