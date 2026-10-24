"""Audit the actual build environment with a separately installed pinned tool.

The inventory includes all installed distributions, including transitive and
build dependencies. ``--no-deps --disable-pip`` avoids resolving a second,
different environment; it does not omit transitive packages from this inventory.
No fixes, ignored advisories, raw process output, URLs or machine paths are
written to public reports. Every reported vulnerability, including an advisory
without a severity score, blocks the gate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z')
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.!+_-]{0,199}\Z')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z')
PIN = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]{0,199})==([A-Za-z0-9][A-Za-z0-9.!+_-]{0,199})\Z')
INVENTORY_CODE = '''import importlib.metadata as m, json, platform, sys
print(json.dumps({"prefix":sys.prefix,"python":platform.python_version(),
 "implementation":platform.python_implementation(),"system":platform.system(),
 "machine":platform.machine(),"distributions":[
  {"name":d.metadata.get("Name"),"version":d.version} for d in m.distributions()]},sort_keys=True))
'''
POLICY = {
    'schema_version': 1,
    'tool': {'name': 'pip-audit', 'version': '2.10.1',
             'official_source': 'https://github.com/pypa/pip-audit'},
    'vulnerability_service': 'pypi', 'block_all_reported_vulnerabilities': True,
    'block_unknown_severity': True, 'require_complete_environment_coverage': True,
    'require_distinct_tool_environment': True, 'ignored_vulnerabilities': [],
    'request_timeout_seconds': 20, 'process_timeout_seconds': 180,
}


class DependencyAuditError(RuntimeError):
    """A safe reason code suitable for a public report."""


def canonical(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise DependencyAuditError('invalid_distribution_name')
    return re.sub(r'[-_.]+', '-', name).lower()


def version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise DependencyAuditError('invalid_distribution_version')
    return value


def read_policy(path):
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError):
        raise DependencyAuditError('policy_unreadable_or_invalid_json') from None
    # This gate intentionally has no ignore/severity weakening switches.
    if json.dumps(value, sort_keys=True) != json.dumps(POLICY, sort_keys=True):
        raise DependencyAuditError('policy_does_not_match_reviewed_fail_closed_policy')
    return value, hashlib.sha256(raw).hexdigest()


def read_requirements(path):
    try:
        raw = path.read_bytes()
        lines = raw.decode('utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        raise DependencyAuditError('requirements_unreadable') from None
    result = {}
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise DependencyAuditError('requirements_must_use_plain_exact_pins')
        name, resolved = canonical(match[1]), version(match[2])
        if name in result:
            raise DependencyAuditError('duplicate_direct_requirement')
        result[name] = resolved
    if not result:
        raise DependencyAuditError('requirements_empty')
    return result, hashlib.sha256(raw).hexdigest()


def safe_environment():
    # Never allow ambient pip-audit flags to redirect output, select an alternate
    # vulnerability service, or suppress advisory IDs. No secrets are logged.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('PIP_AUDIT_', 'PYTHON'))}
    environment.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                       PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1')
    return environment


def run_process(arguments, *, timeout):
    try:
        result = subprocess.run(list(map(str, arguments)), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            env=safe_environment(), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise DependencyAuditError('process_timeout') from None
    except OSError:
        raise DependencyAuditError('process_start_failed') from None
    if len(result.stdout) > 16 * 1024 * 1024 or len(result.stderr) > 4 * 1024 * 1024:
        raise DependencyAuditError('process_output_limit_exceeded')
    return result


def inspect_environment(python, *, timeout):
    result = run_process([python, '-I', '-B', '-c', INVENTORY_CODE], timeout=timeout)
    if result.returncode != 0:
        raise DependencyAuditError('environment_inventory_failed')
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise DependencyAuditError('environment_inventory_invalid_json') from None
    fields = {'prefix', 'python', 'implementation', 'system', 'machine', 'distributions'}
    if (type(value) is not dict or set(value) != fields
            or not isinstance(value['prefix'], str) or not value['prefix']
            or type(value['distributions']) is not list or not value['distributions']
            or len(value['distributions']) > 10000):
        raise DependencyAuditError('environment_inventory_invalid_shape')
    packages = {}
    for item in value['distributions']:
        if type(item) is not dict or set(item) != {'name', 'version'}:
            raise DependencyAuditError('environment_inventory_invalid_distribution')
        name, resolved = canonical(item['name']), version(item['version'])
        if name in packages:
            raise DependencyAuditError('duplicate_environment_distribution')
        packages[name] = resolved
    for key in ('python', 'implementation', 'system', 'machine'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9 ._+-]{1,100}', value[key]):
            raise DependencyAuditError('environment_runtime_invalid')
    return value, dict(sorted(packages.items()))


def normalize_findings(document, packages):
    if (type(document) is not dict or type(document.get('dependencies')) is not list
            or document.get('fixes', []) != []):
        raise DependencyAuditError('audit_json_invalid_shape')
    found, records, skipped = {}, [], []
    for item in document['dependencies']:
        if type(item) is not dict:
            raise DependencyAuditError('audit_dependency_invalid_shape')
        name = canonical(item.get('name'))
        if name not in packages or name in found:
            raise DependencyAuditError('audit_duplicate_or_unexpected_dependency')
        is_skipped = 'skip_reason' in item
        resolved = version(item.get('version', packages[name] if is_skipped else None))
        if resolved != packages[name]:
            raise DependencyAuditError('audit_package_version_mismatch')
        found[name] = resolved
        if is_skipped:
            # Upstream reason text can include private indexes/URLs. Retain a
            # fixed reason and package identity rather than serializing it.
            skipped.append({'name': name, 'version': resolved, 'reason': 'vulnerability_lookup_skipped'})
        vulns = item.get('vulns', [] if is_skipped else None)
        if type(vulns) is not list:
            raise DependencyAuditError('audit_vulnerability_list_missing')
        normalized = []
        for vulnerability in vulns:
            if type(vulnerability) is not dict:
                raise DependencyAuditError('audit_vulnerability_invalid_shape')
            identifier = vulnerability.get('id')
            aliases, fixes = vulnerability.get('aliases', []), vulnerability.get('fix_versions', [])
            if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier)
                    or type(aliases) is not list or type(fixes) is not list
                    or any(not isinstance(alias, str) or not IDENTIFIER.fullmatch(alias) for alias in aliases)):
                raise DependencyAuditError('audit_vulnerability_identifier_invalid')
            severity = vulnerability.get('severity', 'UNKNOWN')
            if severity not in ('LOW', 'MODERATE', 'MEDIUM', 'HIGH', 'CRITICAL', 'UNKNOWN'):
                severity = 'UNKNOWN'
            normalized.append({'id': identifier, 'aliases': sorted(set(aliases)),
                'fix_versions': sorted(set(version(fix) for fix in fixes)),
                'severity': severity, 'blocking': True})
        records.append({'name': name, 'version': resolved,
                        'vulnerabilities': sorted(normalized, key=lambda item: item['id'])})
    missing = sorted(set(packages) - set(found))
    return sorted(records, key=lambda item: item['name']), skipped, missing


def _save_new(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2, allow_nan=False)
        stream.write('\n')


def audit(args):
    directory = Path(args.output)
    # A fresh directory preserves failed audit evidence and cannot overwrite a
    # previous passing receipt. Its absolute path is never included in reports.
    directory.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'kind': 'MediaFlow.dependency-audit', 'passed': False,
        'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'scope': 'all installed distributions in actual build environment; advisory database coverage only',
        'tool': dict(POLICY['tool']), 'vulnerability_service': POLICY['vulnerability_service'],
        'blocking_policy': 'all reported vulnerabilities, including unknown severity; no ignores',
        'failure_reasons': [], 'dependencies': [], 'skipped': [],
        'coverage': {'installed': 0, 'audited': 0, 'missing': [], 'complete': False}}
    stage = 'policy'
    try:
        policy, report['policy_sha256'] = read_policy(Path(args.policy))
        direct, report['requirements_sha256'] = read_requirements(Path(args.requirements))
        build_path = getattr(args, 'build_requirements', None)
        build = {}
        if build_path is not None:
            build, report['build_requirements_sha256'] = read_requirements(Path(build_path))
            if any(name in direct and direct[name] != resolved for name, resolved in build.items()):
                raise DependencyAuditError('application_and_build_pins_conflict')
        stage = 'environment_inventory'
        target, packages = inspect_environment(args.environment_python, timeout=30)
        tool, tools = inspect_environment(args.audit_python, timeout=30)
        if Path(target['prefix']).resolve() == Path(tool['prefix']).resolve():
            raise DependencyAuditError('audit_tool_and_build_environments_must_be_distinct')
        if 'pip-audit' in packages:
            raise DependencyAuditError('audit_tool_must_not_be_installed_in_build_environment')
        if tools.get('pip-audit') != policy['tool']['version']:
            raise DependencyAuditError('audit_tool_version_not_reviewed_pin')
        if any(packages.get(name) != resolved for name, resolved in direct.items()):
            raise DependencyAuditError('installed_direct_requirements_do_not_match_pins')
        if any(packages.get(name) != resolved for name, resolved in build.items()):
            raise DependencyAuditError('installed_build_requirements_do_not_match_pins')
        report['environment'] = {key: target[key] for key in ('python', 'implementation', 'system', 'machine')}
        report['coverage']['installed'] = len(packages)
        report['direct_requirements'] = [{'name': name, 'version': resolved}
                                         for name, resolved in sorted(direct.items())]
        report['build_requirements'] = [{'name': name, 'version': resolved}
                                        for name, resolved in sorted(build.items())]
        stage = 'environment_dependency_consistency'
        checked = run_process([args.environment_python, '-I', '-B', '-m', 'pip', 'check'], timeout=30)
        report['environment_dependency_consistency'] = checked.returncode == 0
        if checked.returncode != 0:
            raise DependencyAuditError('installed_environment_dependency_inconsistency')
        pins = ''.join(f'{name}=={resolved}\n' for name, resolved in packages.items())
        pinned = directory / 'resolved-requirements.txt'
        with pinned.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(pins)
        report['inventory_sha256'] = hashlib.sha256(pins.encode('utf-8')).hexdigest()
        stage = 'advisory_lookup'
        result = run_process([args.audit_python, '-I', '-B', '-m', 'pip_audit',
            '--requirement', pinned, '--strict', '--no-deps', '--disable-pip',
            '--format', 'json', '--desc', 'off', '--aliases', 'on',
            '--progress-spinner', 'off', '--vulnerability-service', 'pypi',
            '--timeout', str(policy['request_timeout_seconds'])],
            timeout=policy['process_timeout_seconds'])
        report['audit_process_exit_code'] = result.returncode
        report['audit_stderr_present'] = bool(result.stderr.strip())
        report['audit_stderr_sha256'] = hashlib.sha256(result.stderr).hexdigest()
        try:
            document = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise DependencyAuditError('audit_service_or_tool_failed_without_valid_json') from None
        stage = 'coverage_and_policy'
        records, skipped, missing = normalize_findings(document, packages)
        for item in records:
            item['role'] = ('direct_requirement' if item['name'] in direct else
                            'build_requirement' if item['name'] in build else 'transitive_or_build_environment')
        report['dependencies'], report['skipped'] = records, skipped
        report['coverage'].update(audited=len(records), missing=missing, complete=not skipped and not missing)
        count = sum(len(item['vulnerabilities']) for item in records)
        report['vulnerability_count'] = count
        if count:
            report['failure_reasons'].append('reported_vulnerabilities_blocked')
        if skipped or missing:
            report['failure_reasons'].append('incomplete_advisory_coverage')
        if result.returncode not in (0, 1) or (result.returncode == 1 and not count):
            report['failure_reasons'].append('audit_process_or_vulnerability_service_failed')
        if result.returncode == 0 and count:
            report['failure_reasons'].append('audit_exit_code_inconsistent_with_findings')
        stage = 'environment_stability'
        after, after_packages = inspect_environment(args.environment_python, timeout=30)
        _, after_requirements_sha256 = read_requirements(Path(args.requirements))
        _, after_policy_sha256 = read_policy(Path(args.policy))
        after_build_sha256 = read_requirements(Path(build_path))[1] if build_path is not None else None
        report['environment_unchanged'] = target == after and packages == after_packages
        report['inputs_unchanged'] = (after_requirements_sha256 == report['requirements_sha256']
                                     and after_policy_sha256 == report['policy_sha256']
                                     and after_build_sha256 == report.get('build_requirements_sha256'))
        if not report['environment_unchanged'] or not report['inputs_unchanged']:
            report['failure_reasons'].append('audited_environment_or_inputs_changed_during_scan')
        report['passed'] = not report['failure_reasons']
    except DependencyAuditError as error:
        report['failure_reasons'].append(str(error))
    except (OSError, ValueError):
        report['failure_reasons'].append('audit_input_or_output_error')
    report['last_stage'] = stage
    _save_new(directory / 'dependency-audit.json', report)
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--environment-python', required=True,
                        help='Actual installed build environment Python; never modified.')
    result.add_argument('--audit-python', required=True,
                        help='Separate environment containing the reviewed pinned pip-audit.')
    result.add_argument('--requirements', type=Path, default=ROOT / 'requirements.txt')
    result.add_argument('--build-requirements', type=Path, default=ROOT / 'requirements-build.txt',
                        help='Separate exact build/bootstrap pins; also checked and bound to the report.')
    result.add_argument('--policy', type=Path, default=ROOT / 'security/dependency-policy.json')
    result.add_argument('--output', type=Path, required=True, help='New report directory; must not exist.')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        report = audit(args)
    except OSError:
        print(json.dumps({'passed': False, 'failure_reasons': ['report_directory_not_fresh_or_not_writable']}))
        return 2
    print(json.dumps({'passed': report['passed'], 'coverage': report['coverage'],
                      'failure_reasons': report['failure_reasons']}, sort_keys=True))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
