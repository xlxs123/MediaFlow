"""Security-tool environment tests of real workflow policy mutations."""
from pathlib import Path
from itertools import product
import re
import shutil
import tempfile
import unittest

from scripts import security_gate as gate


def expression_value(template, context):
    """Evaluate this workflow's small documented expression subset, no eval.

    This is a local semantic model, not a replacement for GitHub/actionlint.
    Inputs are typed booleans; strings compare case-insensitively as on GitHub.
    """
    def truthy(value):
        return value is not None and value is not False and value != 0 and value != ''

    def evaluate(source):
        pattern = r"\s*('(?:[^']|'')*'|&&|\|\||==|!=|[!(),]|[A-Za-z_][A-Za-z_0-9.]*)"
        tokens = []; offset = 0
        while offset < len(source.rstrip()):
            match = re.match(pattern, source[offset:])
            if not match:
                raise AssertionError('Unsupported expression token in semantic fixture')
            tokens.append(match[1]); offset += match.end()
        index = 0

        def take(expected=None):
            nonlocal index
            token = tokens[index]; index += 1
            if expected is not None and token != expected:
                raise AssertionError('Unexpected expression structure')
            return token

        def parse(minimum=0):
            token = take()
            if token == '!':
                left = not truthy(parse(4))
            elif token == '(':
                left = parse(); take(')')
            elif token.startswith("'"):
                left = token[1:-1].replace("''", "'")
            elif token in ('true', 'false', 'null'):
                left = {'true': True, 'false': False, 'null': None}[token]
            elif token == 'format':
                take('('); arguments = [parse()]
                while tokens[index] == ',':
                    take(','); arguments.append(parse())
                take(')'); left = arguments[0].format(*arguments[1:])
            else:
                left = context
                for part in token.split('.'):
                    left = left.get(part) if isinstance(left, dict) else None
            priority = {'||': 1, '&&': 2, '==': 3, '!=': 3}
            while index < len(tokens) and priority.get(tokens[index], -1) >= minimum:
                operator = take(); right = parse(priority[operator] + 1)
                if operator == '&&': left = right if truthy(left) else left
                elif operator == '||': left = left if truthy(left) else right
                else:
                    equal = left.casefold() == right.casefold() if isinstance(left, str) and isinstance(right, str) else left == right
                    left = equal if operator == '==' else not equal
            return left

        result = parse()
        if index != len(tokens):
            raise AssertionError('Unconsumed workflow expression')
        return result

    matches = list(re.finditer(r'\$\{\{(.*?)\}\}', template))
    if len(matches) == 1 and matches[0].span() == (0, len(template)):
        return evaluate(matches[0][1])
    return re.sub(r'\$\{\{(.*?)\}\}', lambda m: str(evaluate(m[1])), template)


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

    def workflow(self):
        import yaml
        return yaml.load((self.root / '.github/workflows/ci.yml').read_text('utf-8'), Loader=yaml.BaseLoader)

    def context(self, event='workflow_dispatch', request=True, repository='xlxs123/MediaFlow', ref='refs/heads/main', **extra):
        return {'github': dict(event_name=event, repository=repository, ref=ref,
                              workflow=self.workflow()['name'], **extra),
                'inputs': {} if request is None else {'signpath_request': request}}

    def effective(self, context):
        value = self.workflow()
        group = expression_value(value['concurrency']['group'], context)
        cancel = expression_value(value['concurrency']['cancel-in-progress'], context)
        # A job `if` coerces a missing/null optional input to false.
        allowed = bool(expression_value(value['jobs']['signpath']['if'], context))
        self.assertIsInstance(group, str)
        self.assertIsInstance(cancel, bool)
        self.assertIsInstance(allowed, bool)
        return group, cancel, allowed

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

    def test_ordinary_main_push_keeps_original_group_and_does_not_share_signing(self):
        signing = self.effective(self.context())
        for request in (None, False, True):
            context = self.context(event='push', request=request)
            group, cancel, allowed = self.effective(context)
            self.assertEqual(group, 'mediaflow-ci-' + self.workflow()['name'] + '-refs/heads/main')
            self.assertNotEqual(group.casefold(), signing[0].casefold())
            self.assertTrue(cancel)
            self.assertFalse(allowed)

    def test_actual_config_selects_signing_only_for_authorized_main_manual_opt_in(self):
        for event, request, repository, ref in product(
                ('push', 'pull_request', 'workflow_dispatch'), (None, False, True),
                ('xlxs123/MediaFlow', 'untrusted/MediaFlow'), ('refs/heads/main', 'refs/heads/feature')):
            with self.subTest(event=event, request=request, repository=repository, ref=ref):
                expected = event == 'workflow_dispatch' and request is True and repository == 'xlxs123/MediaFlow' and ref == 'refs/heads/main'
                group, cancel, allowed = self.effective(self.context(event, request, repository, ref))
                self.assertEqual(allowed, expected)
                self.assertEqual(cancel, not expected)
                self.assertEqual(group == 'mediaflow-signpath-main', expected)

    def test_signing_group_is_stable_across_dispatches_and_source_commits(self):
        first = self.effective(self.context(run_id='101', sha='a' * 40))
        second = self.effective(self.context(run_id='102', sha='b' * 40))
        self.assertEqual(first, ('mediaflow-signpath-main', False, True))
        self.assertEqual(second, first)

    def test_single_pending_model_protects_active_signing_but_does_not_claim_fifo(self):
        # Default GitHub concurrency: one active + one pending, newer pending
        # replaces the old pending even with cancel-in-progress disabled.
        active = {}; pending = {}; cancelled = []
        def enqueue(name, context):
            group, cancel, _ = self.effective(context)
            group = group.casefold()
            if group not in active:
                active[group] = name
            elif cancel:
                cancelled.append(active[group]); active[group] = name
                if group in pending: cancelled.append(pending.pop(group))
            else:
                if group in pending: cancelled.append(pending[group])
                pending[group] = name
        enqueue('sign-A', self.context())
        enqueue('push-A', self.context(event='push', request=None))
        enqueue('push-B', self.context(event='push', request=None))
        enqueue('sign-B', self.context())
        enqueue('sign-C', self.context())
        self.assertEqual(active['mediaflow-signpath-main'], 'sign-A')
        self.assertEqual(pending['mediaflow-signpath-main'], 'sign-C')
        self.assertEqual(cancelled, ['push-A', 'sign-B'])
        self.assertNotIn('sign-A', cancelled)

    def test_concurrency_policy_rejects_shared_group_cancellation_or_unreviewed_queue(self):
        path = self.root / '.github/workflows/ci.yml'
        original = path.read_text('utf-8')
        changes = ((gate.SIGNPATH_CONCURRENCY_GROUP, 'mediaflow-ci-${{ github.workflow }}-${{ github.ref }}'),
            (gate.SIGNPATH_CANCEL_CONDITION, 'true'), (gate.SIGNPATH_CANCEL_CONDITION, 'false'),
            (gate.SIGNPATH_CONCURRENCY_GROUP, gate.SIGNPATH_CONCURRENCY_GROUP.replace("'xlxs123/MediaFlow'", "'untrusted/MediaFlow'")),
            ('concurrency:\n', 'concurrency:\n  queue: max\n'),
            ('    environment: signpath-foundation', '    concurrency: mediaflow-signpath-main\n    environment: signpath-foundation'))
        for before, after in changes:
            with self.subTest(change=before):
                self.assertIn(before, original)
                path.write_text(original.replace(before, after, 1), 'utf-8')
                self.assertFalse(gate.workflow_policy(self.root)['passed'])
        start = original.index('concurrency:\n'); end = original.index('\njobs:', start)
        path.write_text(original[:start] + original[end:], 'utf-8')
        self.assertFalse(gate.workflow_policy(self.root)['passed'])

    def test_security_workflow_cannot_cancel_signing_or_reuse_its_group_in_any_case(self):
        import yaml
        path = self.root / '.github/workflows/security.yml'
        original = path.read_text('utf-8')
        security = yaml.load(original, Loader=yaml.BaseLoader)
        normal_group = expression_value(security['concurrency']['group'], self.context(event='push', request=None))
        self.assertNotEqual(normal_group.casefold(), self.effective(self.context())[0].casefold())
        for replacement in ('MEDIAFLOW-SIGNPATH-MAIN', "${{ format('mediaflow-signpath-{0}', github.ref_name) }}"):
            with self.subTest(group=replacement):
                path.write_text(original.replace(security['concurrency']['group'], replacement, 1), 'utf-8')
                self.assertFalse(gate.workflow_policy(self.root)['passed'])
        path.write_text(original, 'utf-8')
        self.extra('collision.yml', '  accidental:\n    runs-on: ubuntu-24.04\n'
            '    concurrency:\n      group: MediaFlow-SignPath-Main\n'
            '      cancel-in-progress: true\n    steps:\n      - run: echo unit\n')
        self.assertFalse(gate.workflow_policy(self.root)['passed'])
