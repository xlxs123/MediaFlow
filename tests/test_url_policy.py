import unittest
from core.request_context import RequestContext
from core.url_policy import UrlResolutionPolicy as Policy, resolve_media_url


class UrlPolicyTests(unittest.TestCase):
    def test_standard_does_not_inherit_query(self):
        self.assertEqual(resolve_media_url('https://cdn.test/a/index.png?token=a%2Fb&t=2', 'seg.png'),
                         'https://cdn.test/a/seg.png')

    def test_scoped_raw_query_inheritance(self):
        ctx = RequestContext('https://cdn.test/a/index.png?token=a%2Fb&t=2&t=3',
                             url_resolution_policy=Policy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING,
                             playlist_decoder='rou')
        child = ctx.derive('seg.png')
        self.assertEqual(child.url, 'https://cdn.test/a/seg.png?token=a%2Fb&t=2&t=3')
        self.assertEqual(child.playlist_decoder, 'rou')

    def test_resource_query_wins(self):
        self.assertEqual(resolve_media_url('https://cdn.test/a/index?token=secret', 'seg?own=1', Policy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING),
                         'https://cdn.test/a/seg?own=1')

    def test_never_inherit_to_absolute_or_foreign_url(self):
        for uri in ('https://other.test/seg', '//other.test/seg', 'https://cdn.test/a/seg'):
            self.assertNotIn('secret', resolve_media_url('https://cdn.test/a/index?token=secret', uri, Policy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING))

    def test_explicit_empty_query_and_fragment(self):
        self.assertNotIn('secret', resolve_media_url('https://cdn.test/index?token=secret', 'seg?', Policy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING))
        self.assertEqual(resolve_media_url('https://cdn.test/index?token=x', 'seg#x', Policy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING),
                         'https://cdn.test/seg?token=x#x')
