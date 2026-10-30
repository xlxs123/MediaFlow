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

    def write_workflow(self, value):
        import yaml
        (self.root / '.github/workflows/ci.yml').write_text(yaml.safe_dump(value, sort_keys=False), 'utf-8')

    def credential_denied(self, value, location):
        self.write_workflow(value)
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any(location in error and 'credential' in error for error in result['errors']), result['errors'])

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

    def test_only_designated_boolean_presence_check_is_allowed(self):
        value = self.workflow()
        self.assertEqual(value['jobs']['signpath']['env']['SIGNPATH_TOKEN_PRESENT'],
                         "${{ secrets.SIGNPATH_API_TOKEN != '' }}")
        self.assertTrue(gate.workflow_policy(self.root)['passed'])
        value['jobs']['signpath']['env']['EXTRA_PRESENT'] = "${{ secrets.SIGNPATH_API_TOKEN != '' }}"
        self.credential_denied(value, '/jobs/signpath/env/EXTRA_PRESENT')

    def test_workflow_level_credential_reference_is_rejected(self):
        value = self.workflow()
        value['env'] = {'LEAK': '${{ secrets.SIGNPATH_API_TOKEN }}'}
        self.credential_denied(value, 'ci.yml/env/LEAK')

    def test_signing_job_level_credential_reference_is_rejected(self):
        value = self.workflow()
        value['jobs']['signpath']['env']['LEAK'] = '${{ secrets.SIGNPATH_API_TOKEN }}'
        self.credential_denied(value, '/jobs/signpath/env/LEAK')

    def test_step_fields_and_other_actions_cannot_receive_credential(self):
        original = self.workflow()
        for step_id, field in product(('preflight', 'prepare', 'submit', 'verify'), ('env', 'with', 'run')):
            with self.subTest(step=step_id, field=field):
                from copy import deepcopy
                value = deepcopy(original)
                step = next(item for item in value['jobs']['signpath']['steps'] if item.get('id') == step_id)
                if field == 'run':
                    step[field] = 'echo ${{ secrets.SIGNPATH_API_TOKEN }}'
                else:
                    step.setdefault(field, {})['LEAK'] = '${{ secrets.SIGNPATH_API_TOKEN }}'
                self.credential_denied(value, '/' + field)
        value = original
        step = value['jobs']['signpath']['steps'][0]
        self.assertTrue(step['uses'].startswith('actions/checkout@'))
        step['with']['LEAK'] = '${{ secrets.SIGNPATH_API_TOKEN }}'
        self.credential_denied(value, '/with/LEAK')

    def test_wrong_missing_or_computed_api_token_is_rejected(self):
        from copy import deepcopy
        original = self.workflow()
        for expression in (None, '${{ secrets.OTHER_TOKEN }}',
                "${{ format('{0}', secrets.SIGNPATH_API_TOKEN) }}",
                "prefix-${{ secrets.SIGNPATH_API_TOKEN }}",
                '${{ secrets.SIGNPATH_API_TOKEN',
                '${{ secrets.SIGNPATH_API_TOKEN }}${{ secrets.SIGNPATH_API_TOKEN }}'):
            with self.subTest(expression=expression):
                value = deepcopy(original)
                settings = next(step['with'] for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit')
                settings.pop('api-token')
                if expression is not None:
                    settings['api-token'] = expression
                self.credential_denied(value, '/with/api-token')

    def test_boolean_check_cannot_return_or_interpolate_the_actual_value(self):
        from copy import deepcopy
        original = self.workflow()
        for expression in (None, '${{ secrets.SIGNPATH_API_TOKEN }}',
                "${{ secrets.SIGNPATH_API_TOKEN != '' && secrets.SIGNPATH_API_TOKEN }}",
                "${{ secrets.SIGNPATH_API_TOKEN != '' }}-${{ secrets.SIGNPATH_API_TOKEN }}"):
            with self.subTest(expression=expression):
                value = deepcopy(original)
                value['jobs']['signpath']['env'].pop('SIGNPATH_TOKEN_PRESENT')
                if expression is not None:
                    value['jobs']['signpath']['env']['SIGNPATH_TOKEN_PRESENT'] = expression
                self.credential_denied(value, '/env/SIGNPATH_TOKEN_PRESENT')

    def test_literal_index_access_is_classified_at_both_approved_positions(self):
        value = self.workflow()
        value['jobs']['signpath']['env']['SIGNPATH_TOKEN_PRESENT'] = "${{ secrets['SIGNPATH_API_TOKEN'] != '' }}"
        submit = next(step for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit')
        submit['with']['api-token'] = "${{ secrets [ 'SIGNPATH_API_TOKEN' ] }}"
        self.write_workflow(value)
        self.assertTrue(gate.workflow_policy(self.root)['passed'])
        value['env'] = {'LEAK': "${{ secrets['SIGNPATH_API_TOKEN'] }}"}
        self.credential_denied(value, 'ci.yml/env/LEAK')

    def test_unicode_casefold_aliases_cannot_replace_the_reviewed_secret_name(self):
        from copy import deepcopy
        original = self.workflow()
        for name, use in product(('\u017fIGNPATH_API_TOKEN', 'SIGNPATH_API_TO\u212aEN'), ('presence', 'value')):
            with self.subTest(name=name, use=use):
                value = deepcopy(original)
                expression = "${{ secrets['" + name + "']" + (" != ''" if use == 'presence' else '') + ' }}'
                if use == 'presence':
                    value['jobs']['signpath']['env']['SIGNPATH_TOKEN_PRESENT'] = expression
                    location = '/env/SIGNPATH_TOKEN_PRESENT'
                else:
                    submit = next(step for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit')
                    submit['with']['api-token'] = expression
                    location = '/with/api-token'
                self.credential_denied(value, location)
        value = deepcopy(original)
        value['jobs']['signpath']['env']['SIGNPATH_TOKEN_PRESENT'] = "${{ SECRETS['signpath_api_token'] != '' }}"
        submit = next(step for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit')
        submit['with']['api-token'] = '${{ SECRETS.signpath_api_token }}'
        self.write_workflow(value)
        self.assertTrue(gate.workflow_policy(self.root)['passed'])

    def test_opaque_secret_access_fails_closed(self):
        original = self.workflow()
        for expression in ("${{ secrets[env.NAME] }}", "${{ toJSON(secrets) }}",
                "${{ secrets[format('{0}_{1}', 'SIGNPATH', 'API_TOKEN')] }}",
                '${{ secrets.* }}', '${{ SECRETS.signpath_api_token }}',
                '${{ secrets["SIGNPATH_API_TOKEN"] }}'):
            with self.subTest(expression=expression):
                value = dict(original, env={'LEAK': expression})
                self.credential_denied(value, 'ci.yml/env/LEAK')

    def test_recursive_fields_and_implicit_if_are_checked(self):
        from copy import deepcopy
        original = self.workflow()
        for where in ('run-name', 'defaults', 'strategy', 'matrix-steps', 'container', 'if', 'expression-key'):
            with self.subTest(where=where):
                value = deepcopy(original)
                windows = value['jobs']['windows']
                expression = "${{ secrets['SIGNPATH_API_TOKEN'] }}"
                if where == 'run-name':
                    value[where] = expression
                elif where == 'defaults':
                    value[where] = {'run': {'working-directory': expression}}
                elif where == 'strategy':
                    windows[where] = {'matrix': {'include': [{'nested': [expression]}]}}
                elif where == 'matrix-steps':
                    windows['strategy'] = {'matrix': {'steps': [expression]}}
                elif where == 'container':
                    windows[where] = {'credentials': {'password': expression}}
                elif where == 'if':
                    windows['steps'][0]['if'] = "secrets['SIGNPATH_API_TOKEN'] != ''"
                else:
                    value['env'] = {expression: 'harmless'}
                location = 'ci.yml/jobs/windows/strategy/matrix/steps/0' if where == 'matrix-steps' else 'ci.yml/'
                self.credential_denied(value, location)

    def test_other_workflows_cannot_use_the_signing_credential(self):
        self.extra('another.yaml', '  ordinary:\n    runs-on: ubuntu-24.04\n'
            "    env:\n      LEAK: ${{ secrets['SIGNPATH_API_TOKEN'] }}\n"
            '    steps:\n      - run: echo unit\n')
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('another.yaml/jobs/ordinary/env/LEAK' in error for error in result['errors']))

    def test_submit_step_must_use_the_reviewed_signpath_action(self):
        value = self.workflow()
        submit = next(step for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit')
        submit['uses'] = value['jobs']['signpath']['steps'][0]['uses']
        self.credential_denied(value, '/with/api-token')

    def test_mutating_both_action_and_pin_cannot_reauthorize_the_credential(self):
        import json
        value = self.workflow()
        submit = next(step for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit')
        action, _ = submit['uses'].split('@')
        submit['uses'] = action + '@' + 'a' * 40
        path = self.root / 'security/toolchain.json'
        pins = json.loads(path.read_text('utf-8'))
        pins['actions'][action]['commit'] = 'a' * 40
        path.write_text(json.dumps(pins), 'utf-8')
        self.credential_denied(value, '/with/api-token')

    def test_duplicate_submit_or_signpath_action_is_rejected(self):
        from copy import deepcopy
        original = self.workflow()
        for duplicate_id in ('submit', 'second-submit'):
            with self.subTest(id=duplicate_id):
                value = deepcopy(original)
                duplicate = deepcopy(next(step for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit'))
                duplicate['id'] = duplicate_id
                value['jobs']['signpath']['steps'].append(duplicate)
                self.credential_denied(value, '/with/api-token')

    def test_harmless_values_and_expression_literals_do_not_expand_secret_access(self):
        value = self.workflow()
        value['env'] = {'ORDINARY': 'utf-8', 'PUBLIC_VALUE': '${{ github.repository }}',
            'OTHER_CREDENTIAL': '${{ secrets.OTHER_TOKEN }}',
            'PLAIN_TEXT': 'secrets.SIGNPATH_API_TOKEN', 'PROPERTY': '${{ inputs.secrets }}',
            'LITERAL': "${{ 'secrets.SIGNPATH_API_TOKEN' }}",
            'ESCAPED_LITERAL': "${{ 'a''secrets.SIGNPATH_API_TOKEN' }}",
            'DELIMITER_LITERAL': "${{ format('}} {0}', 'secrets.SIGNPATH_API_TOKEN') }}"}
        self.write_workflow(value)
        self.assertTrue(gate.workflow_policy(self.root)['passed'])

    def test_embedded_delimiters_cannot_hide_a_later_credential_reference(self):
        value = self.workflow()
        value['env'] = {'LEAK': "${{ format('}} {0}', secrets['SIGNPATH_API_TOKEN']) }}"}
        self.credential_denied(value, 'ci.yml/env/LEAK')

    def test_unclosed_expression_is_rejected_without_echoing_its_contents(self):
        value = self.workflow()
        value['env'] = {'LEAK': '${{ secrets.SIGNPATH_API_TOKEN unit-marker-not-a-token'}
        self.write_workflow(value)
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('ci.yml/env/LEAK' in error for error in result['errors']))
        self.assertNotIn('unit-marker-not-a-token', str(result))

    def test_yaml_aliases_do_not_hide_additional_reference_locations(self):
        value = self.workflow()
        value['env'] = next(step['with'] for step in value['jobs']['signpath']['steps'] if step.get('id') == 'submit')
        self.credential_denied(value, 'ci.yml/env/api-token')

    def test_duplicate_yaml_keys_are_rejected_instead_of_discarding_a_reference(self):
        path = self.root / '.github/workflows/ci.yml'
        path.write_text(path.read_text('utf-8').replace('PYTHONIOENCODING: utf-8',
            'LEAK: ${{ secrets.SIGNPATH_API_TOKEN }}\n      LEAK: harmless\n      PYTHONIOENCODING: utf-8', 1), 'utf-8')
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('ci.yml: ambiguous YAML mapping at line' in error for error in result['errors']))

    def test_recursive_yaml_is_rejected_without_infinite_traversal(self):
        value = self.workflow()
        value['env'] = recursive = {}
        recursive['loop'] = recursive
        self.write_workflow(value)
        result = gate.workflow_policy(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('ci.yml:' in error and 'credential' in error for error in result['errors']))

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
