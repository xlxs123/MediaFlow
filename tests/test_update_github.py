import unittest
from unittest.mock import patch
from core.update_github import release_asset, ReleaseRedirectPolicy, channel_redirect_policy
from core.update_transport import _SameOriginRedirect, _origin, TransportError

SOURCE = 'https://github.com/owner/MediaFlow-Releases/releases/latest/download/manifest.json'
CDN = 'https://release-assets.githubusercontent.com/github-production-release-asset/123/abcd-1234?sig=synthetic'


class GitHubRedirectTests(unittest.TestCase):
    def test_latest_to_exact_versioned_asset_then_official_cdn(self):
        policy = ReleaseRedirectPolicy(SOURCE, SOURCE)
        self.assertTrue(policy.accepts(SOURCE))
        self.assertTrue(policy.accepts(SOURCE.replace('latest/download', 'download/v2.1.0')))
        self.assertTrue(policy.accepts(CDN, record=True))
        self.assertTrue(policy.accepts(CDN))
        self.assertFalse(policy.accepts(SOURCE))
        self.assertFalse(policy.accepts(CDN.replace('abcd-1234', 'other-file')))

    def test_signed_package_in_pinned_repository_is_allowed(self):
        url = SOURCE.replace('latest/download/manifest.json', 'download/v2.1.0/MediaFlow-2.1.0.zip')
        policy = ReleaseRedirectPolicy(url, SOURCE)
        self.assertTrue(policy.enabled)
        self.assertTrue(policy.accepts(CDN, record=True))

    def test_remote_url_cannot_choose_another_repository_or_asset(self):
        for url in (SOURCE.replace('/owner/', '/foreign/'), SOURCE.replace('MediaFlow-Releases', 'Other'),
                    'https://github.com/owner/repo/issues', 'https://other.invalid/file'):
            with self.subTest(url=url):
                self.assertFalse(ReleaseRedirectPolicy(url, SOURCE).enabled)
        policy = ReleaseRedirectPolicy(SOURCE, SOURCE)
        self.assertFalse(policy.accepts(SOURCE.replace('manifest.json', 'unrelated.exe')))
        self.assertFalse(policy.accepts(SOURCE.replace('/owner/', '/foreign/')))

    def test_cdn_lookalikes_credentials_ports_and_paths_are_rejected(self):
        bad = (CDN.replace('release-assets.githubusercontent.com', 'evil.invalid'),
               CDN.replace('release-assets.githubusercontent.com', 'release-assets.githubusercontent.com.evil.invalid'),
               CDN.replace('https:', 'http:'), CDN.replace('https://', 'https://user@'),
               CDN.replace('.com/', '.com:8443/'), CDN.replace('/github-production-release-asset/', '/other/'),
               CDN.replace('/123/', '/../'), CDN + '#fragment')
        for url in bad:
            with self.subTest(url=url):
                self.assertFalse(ReleaseRedirectPolicy(SOURCE, SOURCE).accepts(url))

    def test_malformed_github_release_paths_do_not_enable_policy(self):
        bad = (SOURCE+'?redirect=1', SOURCE+'#part', SOURCE.replace('/owner/', '/%6fwner/'),
               SOURCE.replace('/owner/', '/../'), SOURCE.replace('/latest/', '/../../'),
               SOURCE.replace('https:', 'http:'), SOURCE.replace('github.com', 'github.com.evil.invalid'))
        for url in bad:
            with self.subTest(url=url):
                self.assertIsNone(release_asset(url))

    def test_only_embedded_approved_channel_enables_policy(self):
        for channel in ({'key_state':'pending','source_url':''},
                        {'key_state':'initialized','source_url':'https://publisher.invalid/manifest.json'}):
            with patch('core.update_channel.load_channel', return_value=channel):
                self.assertFalse(channel_redirect_policy(SOURCE).enabled)
        with patch('core.update_channel.load_channel',return_value={'key_state':'initialized','source_url':SOURCE}):
            self.assertTrue(channel_redirect_policy(SOURCE).enabled)

    def test_generic_sources_remain_same_origin_and_final_response_is_checked(self):
        generic = _SameOriginRedirect(_origin('https://publisher.invalid/file'), ReleaseRedirectPolicy(SOURCE,''))
        self.assertTrue(generic.accepts('https://publisher.invalid/other'))
        with self.assertRaises(TransportError):
            generic.redirect_request(None,None,302,'',{},CDN)
        pinned = _SameOriginRedirect(_origin(SOURCE), ReleaseRedirectPolicy(SOURCE,SOURCE))
        self.assertTrue(pinned.accepts(CDN,record=True))
        self.assertFalse(pinned.accepts('https://evil.invalid/file'))
        with self.assertRaises(TransportError):
            pinned.accepts('http://release-assets.githubusercontent.com/file')


if __name__ == '__main__':
    unittest.main()
