"""Run every test, isolating the new Qt suite's QApplication process."""
from pathlib import Path
from contextlib import nullcontext
import io
import json
import os
import re
import subprocess
import sys
import unittest
import faulthandler
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SECURITY_MODULES = ('test_sbom_validation', 'test_workflow_policy')


def report_directory():
    if '--report-dir' in sys.argv:
        return Path(sys.argv[sys.argv.index('--report-dir') + 1]).resolve()
    return ROOT / 'verification'


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def gui_modules():
    return sorted(path.stem for path in (ROOT / 'tests').glob('test_*.py')
        if re.search(r'\bQ(?:Core|Gui)?Application\b', path.read_text('utf-8')))


def run_group(group):
    from utils.build_info import source_digest
    before = source_digest(ROOT)
    directory = report_directory()
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the last executed test visible even if a native dependency ends the process.
    live = (directory / f'unit-{group}-live.txt').open('w', encoding='utf-8')
    trace = (directory / f'unit-{group}-trace.txt').open('w', encoding='utf-8')

    # CPython 3.14.0 gh-140815: the native watchdog can itself crash while
    # dumping freed frames. External owned-process deadlines remain enforced.

    class EvidenceStream(io.StringIO):
        def write(self, value):
            live.write(value)
            live.flush()
            return super().write(value)
    stream = EvidenceStream()
    if group == 'core':
        found = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py', top_level_dir=str(ROOT))
    elif group == 'security-tools':
        found = unittest.defaultTestLoader.loadTestsFromNames(['tests.' + name for name in SECURITY_MODULES])
    else:
        found = unittest.defaultTestLoader.loadTestsFromName('tests.' + group)
    gui_prefixes = tuple('tests.' + name + '.' for name in gui_modules())
    security_prefixes = tuple('tests.' + name + '.' for name in SECURITY_MODULES)
    suite = unittest.TestSuite(item for item in cases(found)
        if group != 'core' or not item.id().startswith(gui_prefixes + security_prefixes))
    # Match the focused runner: legacy unconfigured-installation fixtures must
    # not contact the production channel. Channel tests inject their own roots;
    # the packaged EXE acceptance separately reads the real channel resource.
    empty = {'schema_version': 1, 'product': 'MediaFlow', 'channel': 'stable',
             'source_url': '', 'trusted_keys': {}, 'key_state': 'pending'}
    with (nullcontext() if group == 'security-tools' else
          patch('core.update_manager.load_channel', return_value=empty)):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    trace.close()
    live.close()
    (directory / f'unit-{group}-results.txt').write_text(stream.getvalue(), encoding='utf-8')
    summary = {'passed': result.wasSuccessful(), 'tests': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'skipped': len(result.skipped), 'group': group}
    summary.update(source_sha256=before, source_unchanged=before == source_digest(ROOT))
    (directory / f'unit-{group}-results.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    return 0 if result.wasSuccessful() else 1


def main():
    if '--group' in sys.argv:
        group = sys.argv[sys.argv.index('--group') + 1]
        return run_group(group) if group in ('core', 'security-tools') or group in gui_modules() else 2
    from utils.build_info import source_digest
    before = source_digest(ROOT)
    directory = report_directory()
    if '--report-dir' in sys.argv and directory.exists():
        raise RuntimeError('Use a fresh full-regression directory; previous evidence is preserved.')
    directory.mkdir(parents=True, exist_ok=True)
    groups = []
    # Every case still runs. Separate QApplication processes avoid native
    # objects and deferred deletions leaking across independent GUI fixtures.
    expected_groups = ['core', 'security-tools', *gui_modules()]
    for group in expected_groups:
        with (directory / f'unit-{group}.stdout.log').open('w', encoding='utf-8') as output, \
             (directory / f'unit-{group}.stderr.log').open('w', encoding='utf-8') as errors:
            interpreter = (sys.argv[sys.argv.index('--security-python') + 1]
                if group == 'security-tools' and '--security-python' in sys.argv else sys.executable)
            child = subprocess.run([interpreter, '-B', str(Path(__file__).resolve()), '--group', group,
                                    '--report-dir', str(directory)],
                # The complete core now includes real archive inventories and
                # full-history scanner integration in addition to media tests.
                cwd=ROOT, stdout=output, stderr=errors, timeout=1200 if group == 'core' else 900,
                env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONFAULTHANDLER='1'),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        path = directory / f'unit-{group}-results.json'
        if not path.exists():
            groups.append({'group':group,'passed':False,'tests':0,'failures':0,
                           'errors':1,'skipped':0,'exit_code':child.returncode})
            break
        group_result = json.loads(path.read_text('utf-8'))
        group_result['passed'] = group_result['passed'] and child.returncode == 0
        groups.append(group_result)
        if not group_result['passed']:
            break
    summary = {'passed':len(groups) == len(expected_groups) and all(item['passed'] for item in groups),
        **{name:sum(item[name] for item in groups) for name in ('tests','failures','errors','skipped')},
        'groups':groups,'source_sha256':before,'source_unchanged':before == source_digest(ROOT)}
    (directory / 'unit-results.json').write_text(json.dumps(summary, indent=2), 'utf-8')
    (directory / 'unit-results.txt').write_text('\n'.join(
        (directory / f'unit-{item["group"]}-results.txt').read_text('utf-8') for item in groups
        if (directory / f'unit-{item["group"]}-results.txt').exists()), 'utf-8')
    print(json.dumps(summary))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
