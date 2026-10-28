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

    def test_pr_scan_cannot_reenable_diff_only_results(self):
        workflow = self.root / '.github/workflows/security.yml'
        workflow.write_text(workflow.read_text('utf-8').replace(
            "CODEQL_ACTION_DIFF_INFORMED_QUERIES: 'false'", "CODEQL_ACTION_DIFF_INFORMED_QUERIES: 'true'"), 'utf-8')
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('diff-informed' in error for error in result['errors']))

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

    def test_signpath_cannot_drop_branch_environment_or_provenance_gates(self):
        path = self.root / '.github/workflows/ci.yml'
        original = path.read_text('utf-8')
        for before, after in ((gate.TRUSTED_SIGNPATH_CONDITION, '${{ true }}'),
                ('needs: [windows, provenance]', 'needs: [windows]'),
                ('environment: signpath-foundation', 'environment: uncontrolled'),
                ('skip-decompress: true', 'skip-decompress: false')):
            with self.subTest(change=before):
                self.assertIn(before, original)
                path.write_text(original.replace(before, after), 'utf-8')
                self.assertFalse(gate.workflow_policy(self.root)['passed'])

    def test_signpath_secret_cannot_be_exposed_to_normal_ci_job(self):
        path = self.root / '.github/workflows/ci.yml'
        original = path.read_text('utf-8')
        path.write_text(original.replace('PYTHONIOENCODING: utf-8',
            "LEAK: ${{ secrets.SIGNPATH_API_TOKEN }}\n      PYTHONIOENCODING: utf-8", 1), 'utf-8')
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('ordinary jobs' in error for error in result['errors']))

    def test_signpath_artifact_template_cannot_sign_upstream_or_unknown_files(self):
        from xml.etree import ElementTree
        from scripts.release_security import FIRST_PARTY
        value = ElementTree.parse(gate.ROOT / 'security/signpath-artifact.xml')
        ns = {'s': 'http://signpath.io/artifact-configuration/v1'}
        files = value.findall('./s:zip-file/s:pe-file', ns)
        self.assertEqual({node.attrib['path'] for node in files}, set(FIRST_PARTY))
        self.assertEqual(len(files), len(FIRST_PARTY))
        for node in files:
            self.assertEqual(node.attrib['product-name'], 'MediaFlow')
            self.assertEqual(node.attrib['product-version'], '${version}')
            self.assertEqual([child.tag for child in node], ['{' + ns['s'] + '}authenticode-sign'])
