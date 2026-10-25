"""Security-tool environment tests of real workflow policy mutations."""
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts import security_gate as gate


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mediaflow-workflow-policy-unit-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(gate.ROOT / '.github/workflows', self.root / '.github/workflows')
        (self.root / 'security').mkdir()
        shutil.copyfile(gate.ROOT / 'security/toolchain.json', self.root / 'security/toolchain.json')

    def extra(self, name, job):
        (self.root / '.github/workflows' / name).write_text(
            'on: workflow_dispatch\npermissions:\n  contents: read\njobs:\n' + job, 'utf-8')

    def test_reviewed_actual_workflows_pass(self):
        self.assertTrue(gate.workflow_policy(self.root)['passed'])

    def test_job_level_reusable_workflow_cannot_bypass_step_pins(self):
        self.extra('reusable.yml', '  delegate:\n    uses: other/project/.github/workflows/build.yml@main\n')
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('reusable workflow' in error for error in result['errors']))

    def test_alternate_yaml_extension_is_checked_for_mutable_actions(self):
        self.extra('alternate.yaml', '  changed:\n    runs-on: ubuntu-24.04\n    steps:\n'
            '      - uses: actions/checkout@main\n')
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('alternate.yaml/changed' in error for error in result['errors']))

    def test_unreviewed_job_write_permissions_block(self):
        self.extra('permissions.yml', '  changed:\n    runs-on: ubuntu-24.04\n'
            '    permissions:\n      contents: write\n    steps:\n      - run: echo unit\n')
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('unreviewed write permissions' in error for error in result['errors']))
