"""Publisher state transitions with synthetic assets and a controlled gh process.

No GitHub requests, production keys, credentials, or real release writes occur.
Manifest authentication still uses a real, ephemeral in-memory Ed25519 key.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.update_manifest import REQUIRED_FILES, UpdateValidationError, canonical_bytes
from scripts import publish_github_release as publisher
from scripts.accept_release_transaction import load_resume


class SyntheticGitHub:
    """Only the fixed gh commands used by the publisher are accepted."""
    def __init__(self, directory, repository, tag, *, on_publish=None):
        self.directory, self.repository, self.tag = directory, repository, tag
        self.on_publish = on_publish
        self.current = None
        self.commands = []
        self.tamper_download = False
        self.fail_publish = False

    def draft(self):
        return {'id': 241, 'tag_name': self.tag, 'draft': True,
            'prerelease': False, 'published_at': None, 'assets': [],
            'html_url': 'https://github.com/' + self.repository + '/releases/tag/untagged-unit-draft'}

    def __call__(self, arguments, **options):
        command = arguments[1:]
        self.commands.append(command)
        if command == ['api', 'user', '--jq', '.login']:
            value = self.repository.split('/')[0]
        elif command == ['api', 'repos/' + self.repository]:
            value = json.dumps({'owner': {'login': self.repository.split('/')[0]},
                'private': False, 'permissions': {'admin': True}, 'default_branch': 'main'})
        elif command == ['api', 'repos/' + self.repository + '/releases?per_page=100']:
            value = json.dumps([self.current] if self.current else [])
        elif command[:2] == ['release', 'create']:
            if self.current is not None:
                raise AssertionError('A fixture release must never be replaced.')
            self.current = self.draft()
            self.current['prerelease'] = '--prerelease' in command
            value = ''
        elif command[:2] == ['release', 'upload']:
            path = Path(command[3])
            if '--clobber' in command or any(r['name'] == path.name for r in self.current['assets']):
                raise AssertionError('The test server does not permit asset overwrites.')
            data = path.read_bytes()
            self.current['assets'].append({'name': path.name, 'size': len(data),
                'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                'browser_download_url': 'https://github.com/' + self.repository
                    + '/releases/download/untagged-unit-draft/' + path.name})
            value = ''
        elif command[:2] == ['release', 'download']:
            output = Path(command[command.index('--dir') + 1])
            patterns = [command[index + 1] for index, value in enumerate(command) if value == '--pattern']
            for name in patterns:
                data = (self.directory / name).read_bytes()
                if self.tamper_download and name == 'manifest.json':
                    data += b' altered synthetic bytes'
                (output / name).write_bytes(data)
            value = ''
        elif command[:2] == ['release', 'edit']:
            if self.fail_publish:
                return subprocess.CompletedProcess(arguments, 1, '', 'unit-sensitive-diagnostic')
            self.current.update(draft=False, published_at='2026-10-23T09:00:00Z',
                prerelease='--prerelease' in command,
                html_url='https://github.com/' + self.repository + '/releases/tag/' + self.tag)
            for asset in self.current['assets']:
                asset['browser_download_url'] = ('https://github.com/' + self.repository
                    + '/releases/download/' + self.tag + '/' + asset['name'])
            if self.on_publish:
                self.on_publish(self.current)
            value = ''
        else:
            raise AssertionError('Unexpected gh command in isolated publisher test: ' + repr(command))
        return subprocess.CompletedProcess(arguments, 0, value, '')


class GitHubPublisherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-github-publisher-unit-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.artifacts = self.directory / 'synthetic-assets'
        self.artifacts.mkdir()
        self.repository = 'publisher-unit/MediaFlow-Releases'
        self.tag = 'v2.1.0'
        self.package_name = 'MediaFlow-unit-fixture.zip'
        # The fixture exercises publisher byte binding, not deployment or PE/ZIP
        # acceptance, which are tested by their own real-file integration suites.
        package = b'SYNTHETIC PUBLISHER INPUT; NOT A MEDIAFLOW RELEASE'
        (self.artifacts / self.package_name).write_bytes(package)
        key = Ed25519PrivateKey.generate()
        key_id = 'unit-publisher-key-never-production'
        self.channel = {'key_state': 'initialized', 'trusted_keys': {key_id: {
            'algorithm': 'ed25519',
            'public_key': base64.b64encode(key.public_key().public_bytes_raw()).decode('ascii')}}}
        self.manifest = {'schema_version': 2, 'product': 'MediaFlow', 'version': '2.1.0',
            'build_id': 'unit-publisher-fixture', 'platform': 'windows', 'architecture': 'x86_64',
            'published_at': '2026-10-23T08:00:00Z', 'release_notes': 'Synthetic unit fixture',
            'release_page': '', 'minimum_app_version': '2.0.0', 'data_migration': 'separate',
            'files': [{'path': name, 'size': 1, 'sha256': hashlib.sha256(name.encode()).hexdigest()}
                      for name in REQUIRED_FILES],
            'signing': {'algorithm': 'ed25519', 'key_id': key_id},
            'package': {'format': 'zip', 'url': 'https://github.com/' + self.repository
                + '/releases/download/' + self.tag + '/' + self.package_name,
                'size': len(package), 'sha256': hashlib.sha256(package).hexdigest()}}
        self.manifest['signature'] = {'algorithm': 'ed25519', 'key_id': key_id,
            'value': base64.b64encode(key.sign(canonical_bytes(self.manifest))).decode('ascii')}
        self.write_json(self.artifacts / 'release.json', self.manifest)
        self.write_json(self.artifacts / 'signature.json', {
            'signature': self.manifest['signature'],
            'manifest_sha256': hashlib.sha256(canonical_bytes(self.manifest)).hexdigest()})
        self.notes = self.directory / 'notes.md'
        self.notes.write_text('Synthetic release notes', encoding='utf-8')
        self.extra = self.directory / 'source-notice.txt'
        self.extra.write_text('Synthetic additional reviewed asset', encoding='utf-8')
        self.gh = self.directory / 'gh-fixture.exe'
        self.gh.write_bytes(b'Never executed; subprocess.run is controlled by this test.')

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def server(self, **kwargs):
        return SyntheticGitHub(self.artifacts, self.repository, self.tag, **kwargs)

    def invoke(self, server, *, publish=True, report='publication.json'):
        self.report = self.directory / report
        args = argparse.Namespace(repository=self.repository, gh=str(self.gh),
            artifacts=str(self.artifacts), asset=[str(self.extra)], notes=str(self.notes),
            report=str(self.report), publish=publish)
        with patch.object(publisher, 'load_channel', return_value=deepcopy(self.channel)), \
                patch.object(publisher.subprocess, 'run', side_effect=server):
            return publisher.publish(args)

    def recorded(self):
        return json.loads(self.report.read_text(encoding='utf-8'))

    def mirror_server(self):
        return SyntheticGitHub(self.artifacts, 'publisher-unit/MediaFlow-Release-Acceptance',
            'mediaflow-pipeline-test-unit-only')

    def invoke_test(self, server, *, repository=None, tag=None, expected_version='2.1.0', create_only=False):
        self.report = self.directory / 'test-publication.json'
        channel = deepcopy(self.channel)
        channel['source_url'] = 'https://github.com/' + self.repository + '/releases/latest/download/manifest.json'
        args = argparse.Namespace(repository=repository or server.repository, gh=str(self.gh),
            artifacts=str(self.artifacts), asset=[], notes=str(self.notes), report=str(self.report),
            publish=not create_only, test_release=True, test_tag=tag or server.tag, expected_version=expected_version,
            test_create_only=create_only)
        with patch.object(publisher, 'load_channel', return_value=channel), \
                patch.object(publisher.subprocess, 'run', side_effect=server):
            return publisher.publish(args)

    def test_publication_refreshes_all_final_urls_and_never_claims_anonymous_acceptance(self):
        server = self.server()
        result = self.invoke(server)
        self.assertEqual(result, self.recorded())
        self.assertTrue(result['passed'])
        self.assertTrue(result['publication_requested'])
        self.assertTrue(result['published'])
        self.assertTrue(result['published_assets_verified'])
        self.assertTrue(result['draft_verified'])
        self.assertFalse(result['public_online_verified'])
        self.assertEqual(result['stage'], 'published_pending_anonymous_client_acceptance')
        self.assertEqual(result['release_url'], 'https://github.com/' + self.repository + '/releases/tag/' + self.tag)
        self.assertEqual(len(result['assets']), 6)
        for asset in result['assets']:
            self.assertEqual(asset['url'], 'https://github.com/' + self.repository
                + '/releases/download/' + self.tag + '/' + asset['name'])
        self.assertNotIn('untagged-', json.dumps(result))
        self.assertEqual(sum(command[:2] == ['release', 'edit'] for command in server.commands), 1)

    def test_verified_draft_stays_a_draft_without_publish_request(self):
        server = self.server()
        result = self.invoke(server, publish=False)
        self.assertEqual(result['stage'], 'verified_draft')
        self.assertTrue(result['draft_verified'])
        for name in ('publication_requested', 'published', 'published_assets_verified', 'public_online_verified'):
            self.assertFalse(result[name])
        self.assertTrue(server.current['draft'])
        self.assertFalse(any(command[:2] == ['release', 'edit'] for command in server.commands))

    def test_wrong_final_tag_identity_or_public_state_is_rejected(self):
        changes = [{'tag_name': 'v2.1.1'}, {'id': 999}, {'draft': True},
            {'prerelease': True}, {'published_at': None},
            {'html_url': 'https://github.com/' + self.repository + '/releases/tag/untagged-stale'}]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                server = self.server(on_publish=lambda current, change=change: current.update(change))
                with self.assertRaisesRegex(publisher.PublicationError, 'Publication state not confirmed'):
                    self.invoke(server, report=f'bad-public-state-{index}.json')
                report = self.recorded()
                self.assertEqual(report['stage'], 'public_release_inventory')
                self.assertTrue(report['publication_requested'])
                self.assertFalse(report['passed'])
                self.assertFalse(report['published_assets_verified'])

    def test_missing_changed_or_duplicate_final_assets_cannot_pass(self):
        changes = [lambda assets: assets.pop(),
            lambda assets: assets[0].update(size=assets[0]['size'] + 1),
            lambda assets: assets[0].update(digest='sha256:' + '0' * 64),
            lambda assets: assets[0].pop('digest'),
            lambda assets: assets.append(deepcopy(assets[0]))]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                server = self.server(on_publish=lambda current, change=change: change(current['assets']))
                with self.assertRaises(publisher.PublicationError):
                    self.invoke(server, report=f'bad-final-assets-{index}.json')
                report = self.recorded()
                self.assertTrue(report['published'], 'The already published release must not be falsely reported as a retained draft.')
                self.assertFalse(report['passed'])
                self.assertFalse(report['published_assets_verified'])
                self.assertEqual(report['stage'], 'public_release_inventory')
                self.assertNotIn('untagged-', json.dumps(report))
                self.assertEqual(len(server.current['assets']), 5 if index == 0 else 7 if index == 4 else 6)

    def test_draft_or_foreign_final_asset_urls_are_rejected(self):
        addresses = ['https://github.com/' + self.repository + '/releases/download/untagged-stale/a.zip',
            'https://github.com/another-owner/other/releases/download/' + self.tag + '/' + self.package_name,
            'https://another-host.unit-fixture.org/' + self.package_name,
            'https://github.com/' + self.repository + '/releases/download/' + self.tag + '/' + self.package_name + '?extra=1']
        for index, address in enumerate(addresses):
            with self.subTest(address=address):
                server = self.server(on_publish=lambda current, address=address:
                    current['assets'][0].update(browser_download_url=address))
                with self.assertRaisesRegex(publisher.PublicationError, 'Published asset URL'):
                    self.invoke(server, report=f'bad-final-url-{index}.json')
                report = self.recorded()
                self.assertTrue(report['published'])
                self.assertFalse(report['passed'])
                self.assertEqual(report['stage'], 'public_release_inventory')
                self.assertTrue(all('url' not in item for item in report['assets']))

    def test_existing_published_release_is_never_edited_or_uploaded(self):
        server = self.server()
        server.current = dict(server.draft(), draft=False, published_at='2026-10-23T08:00:00Z')
        before = deepcopy(server.current)
        with self.assertRaisesRegex(publisher.PublicationError, 'already published'):
            self.invoke(server)
        self.assertEqual(server.current, before)
        self.assertFalse(any(command[0] == 'release' for command in server.commands))
        self.assertFalse(self.recorded()['publication_requested'])

    def test_conflicting_draft_asset_is_preserved_without_clobber(self):
        server = self.server()
        server.current = server.draft()
        server.current['assets'] = [{'name': self.package_name, 'size': 1,
            'digest': 'sha256:' + '0' * 64, 'browser_download_url': 'unchanged'}]
        before = deepcopy(server.current)
        with self.assertRaisesRegex(publisher.PublicationError, 'Existing asset differs'):
            self.invoke(server)
        self.assertEqual(server.current, before)
        self.assertFalse(any(command[:2] in (['release', 'upload'], ['release', 'edit']) for command in server.commands))
        self.assertEqual(self.recorded()['stage'], 'upload')

    def test_tampered_local_manifest_is_rejected_before_any_gh_process(self):
        changed = dict(self.manifest, release_notes='Tampered without a new signature')
        self.write_json(self.artifacts / 'release.json', changed)
        server = self.server()
        with self.assertRaises(UpdateValidationError):
            self.invoke(server)
        self.assertEqual(server.commands, [])
        self.assertFalse(self.report.exists())

    def test_changed_draft_download_stops_before_publication(self):
        server = self.server()
        server.tamper_download = True
        with self.assertRaisesRegex(publisher.PublicationError, 'Re-downloaded draft bytes differ'):
            self.invoke(server)
        report = self.recorded()
        self.assertEqual(report['stage'], 'authenticated_draft_download')
        self.assertFalse(report['draft_verified'])
        self.assertFalse(report['publication_requested'])
        self.assertTrue(server.current['draft'])

    def test_failed_publish_preserves_request_stage_and_redacts_cli_diagnostics(self):
        server = self.server()
        server.fail_publish = True
        with self.assertRaises(publisher.PublicationError):
            self.invoke(server)
        report = self.recorded()
        self.assertEqual(report['stage'], 'publish')
        self.assertTrue(report['publication_requested'])
        self.assertFalse(report['published'])
        self.assertNotIn('unit-sensitive-diagnostic', json.dumps(report))

    def test_existing_evidence_report_is_preserved_without_gh_operations(self):
        report = self.directory / 'publication.json'
        report.write_bytes(b'Existing evidence must remain byte-for-byte intact.')
        server = self.server()
        with self.assertRaisesRegex(publisher.PublicationError, 'new report path'):
            self.invoke(server)
        self.assertEqual(report.read_bytes(), b'Existing evidence must remain byte-for-byte intact.')
        self.assertEqual(server.commands, [])

    def test_isolated_mirror_keeps_formal_content_and_uses_prerelease_never_latest(self):
        server = self.mirror_server()
        before = (self.artifacts / 'release.json').read_bytes()
        result = self.invoke_test(server)
        self.assertTrue(result['passed'])
        self.assertTrue(result['test_release'])
        self.assertTrue(result['published_assets_verified'])
        self.assertEqual(result['formal_content_repository'], self.repository)
        self.assertEqual((self.artifacts / 'release.json').read_bytes(), before)
        self.assertTrue(server.current['prerelease'])
        self.assertFalse(result['production_chain_verified'])
        self.assertFalse(result['public_online_verified'])
        self.assertFalse(result['mirror_public_verified'])
        writes = [c for c in server.commands if c[0] == 'release' and c[1] in ('create','edit')]
        self.assertEqual(len(writes), 2)
        for command in writes:
            self.assertIn('--prerelease', command)
            self.assertIn('--latest=false', command)
            self.assertNotIn('--latest', command)
            self.assertEqual(command[command.index('--repo')+1], server.repository)
        self.assertTrue(all('/'+server.tag+'/' in a['url'] for a in result['assets']))

    def test_test_release_cannot_target_formal_or_other_repository(self):
        for repository in (self.repository, 'publisher-unit/unrelated-project',
                'other-owner/MediaFlow-Release-Acceptance'):
            with self.subTest(repository=repository):
                server = self.mirror_server()
                with self.assertRaisesRegex(publisher.PublicationError, 'separate owner'):
                    self.invoke_test(server, repository=repository)
                self.assertEqual(server.commands, [])

    def test_test_tag_is_bounded_and_cannot_be_a_product_version(self):
        for tag in ('v2.1.0','latest','mediaflow-pipeline-test-../stable','mediaflow-pipeline-test-'+('x'*92)):
            with self.subTest(tag=tag):
                server = self.mirror_server()
                with self.assertRaisesRegex(publisher.PublicationError, 'bounded'):
                    self.invoke_test(server, tag=tag)
                self.assertEqual(server.commands, [])

    def test_test_version_and_signature_are_verified_before_any_github_call(self):
        server = self.mirror_server()
        with self.assertRaisesRegex(publisher.PublicationError, 'Requested test version'):
            self.invoke_test(server, expected_version='2.1.1')
        self.assertEqual(server.commands, [])
        changed = dict(self.manifest, release_notes='Tampered signed field')
        self.write_json(self.artifacts / 'release.json', changed)
        with self.assertRaises(UpdateValidationError):
            self.invoke_test(server)
        self.assertEqual(server.commands, [])

    def test_test_mode_never_promotes_an_existing_non_prerelease_draft(self):
        server = self.mirror_server()
        server.current = server.draft()
        before = deepcopy(server.current)
        with self.assertRaisesRegex(publisher.PublicationError, 'prerelease state'):
            self.invoke_test(server)
        self.assertEqual(server.current, before)
        self.assertFalse(any(c[0] == 'release' for c in server.commands))

    def test_test_published_repeat_is_read_only_refusal(self):
        server = self.mirror_server()
        server.current = dict(server.draft(), draft=False, prerelease=True,
            published_at='2026-10-24T00:00:00Z')
        before = deepcopy(server.current)
        with self.assertRaisesRegex(publisher.PublicationError, 'already published'):
            self.invoke_test(server)
        self.assertEqual(server.current, before)
        self.assertFalse(any(c[0] == 'release' for c in server.commands))

    def test_test_tag_does_not_override_a_production_call(self):
        args = argparse.Namespace(artifacts=str(self.artifacts), repository=self.repository,
            asset=[],test_tag='mediaflow-pipeline-test-no-mode')
        with self.assertRaisesRegex(publisher.PublicationError, 'explicit TestRelease'):
            publisher.publication_inputs(args)

    def test_isolated_create_only_is_an_empty_unverified_draft(self):
        server = self.mirror_server()
        result = self.invoke_test(server, create_only=True)
        self.assertTrue(result['passed'])
        self.assertEqual(result['stage'],'isolated_test_draft_created_no_upload')
        self.assertFalse(result['draft_verified'])
        self.assertFalse(result['published'])
        self.assertFalse(result['publication_requested'])
        self.assertEqual(server.current['assets'],[])
        self.assertFalse(any(c[:2] in (['release','upload'],['release','edit']) for c in server.commands))

    def test_create_only_cannot_bypass_production_or_request_publication(self):
        for testing,publishing in ((False,False),(True,True)):
            with self.subTest(testing=testing,publishing=publishing):
                args=argparse.Namespace(test_release=testing,test_create_only=True,publish=publishing)
                with self.assertRaisesRegex(publisher.PublicationError,'non-publishing TestRelease'):
                    publisher.publication_inputs(args)

    def test_published_read_checkpoint_requires_exact_repository_and_failed_phase(self):
        valid = {'schema_version':1,'scope':'real_github_isolated_publication_transaction',
            'repository':'publisher-unit/MediaFlow-Release-Acceptance','passed':False,
            'stage':'anonymous-public-download-signature-and-zip-verification',
            'faults':{'partial_assets':{'passed':True},'publication_failure':{'passed':True}},
            'release_id':123,'test_release_url':'https://github.com/publisher-unit/fixture'}
        path = self.directory/'unit-checkpoint.json'
        self.write_json(path,valid)
        before = path.read_bytes()
        self.assertEqual(load_resume(path,valid['repository']),valid)
        self.assertEqual(path.read_bytes(),before)
        for change in ({'passed':True},{'stage':'upload'},{'scope':'unit_process_fixture'},
                {'release_id':None},{'faults':{}},{'repository':'publisher-unit/MediaFlow-Releases'}):
            with self.subTest(change=change):
                self.write_json(path,dict(valid,**change))
                with self.assertRaisesRegex(publisher.PublicationError,'checkpoint can resume'):
                    load_resume(path,valid['repository'])


if __name__ == '__main__':
    unittest.main()
