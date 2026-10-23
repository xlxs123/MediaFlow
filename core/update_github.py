"""Narrow GitHub Releases CDN policy, rooted in the locally approved channel.

The signed manifest never chooses redirect hosts or changes this trust policy.
Other HTTPS sources retain the original same-origin-only behaviour.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')
_CDN = 'release-assets.githubusercontent.com'
_CDN_PATH = re.compile(r'/github-production-release-asset/[0-9]+/[A-Za-z0-9-]+\Z')


def release_asset(url):
    """Return (owner, repo, asset) only for unambiguous public release URLs."""
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or p.hostname != 'github.com' or p.port not in (None, 443)
                or p.username is not None or p.password is not None or p.query or p.fragment
                or '%' in p.path or '\\' in p.path):
            return None
        parts = p.path.split('/')
        if len(parts) != 7 or parts[3] != 'releases':
            return None
        owner, repo, mode, tag, asset = parts[1], parts[2], parts[4], parts[5], parts[6]
        if (not all(_NAME.fullmatch(v) and v not in ('.', '..') for v in (owner, repo, asset))
                or not (mode == 'latest' and tag == 'download' or mode == 'download'
                        and _NAME.fullmatch(tag) and tag not in ('.', '..'))):
            return None
        return owner.lower(), repo.lower(), asset
    except (ValueError, TypeError):
        return None


class ReleaseRedirectPolicy:
    def __init__(self, initial_url, approved_source):
        self.initial = release_asset(initial_url)
        source = release_asset(approved_source)
        self.enabled = bool(source and self.initial and source[:2] == self.initial[:2])
        self.cdn_path = None

    def accepts(self, url, *, record=False):
        if not self.enabled:
            return False
        asset = release_asset(url)
        if asset:
            # latest -> versioned URLs are allowed for this one exact asset.
            return self.cdn_path is None and asset == self.initial
        try:
            p = urlsplit(url)
            allowed = (p.scheme == 'https' and p.hostname == _CDN and p.port in (None, 443)
                and p.username is None and p.password is None and not p.fragment
                and _CDN_PATH.fullmatch(p.path) is not None)
        except (ValueError, TypeError):
            return False
        if not allowed or self.cdn_path is not None and p.path != self.cdn_path:
            return False
        if record:
            self.cdn_path = p.path
        return True


def channel_redirect_policy(url):
    # Imported only after update_transport is initialized (update_channel uses
    # its HTTPS validator). Frozen workers read the embedded approved resource.
    from core.update_channel import load_channel
    try:
        channel = load_channel()
        source = channel['source_url'] if channel['key_state'] == 'initialized' else ''
    except (OSError, ValueError, RuntimeError):
        source = ''
    return ReleaseRedirectPolicy(url, source)
