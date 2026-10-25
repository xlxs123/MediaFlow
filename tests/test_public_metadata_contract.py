"""Actual public-key contracts underlying the exact CodeQL metadata reviews."""
from copy import deepcopy
import json
import re
import unittest

from core.update_channel import load_channel
from core.update_manifest import UpdateValidationError, validate_trusted_keys
from utils.build_info import runtime_identity


class PublicMetadataContractTests(unittest.TestCase):
    def test_confirmed_public_validation_material_is_accepted(self):
        public_records = load_channel()['trusted_keys']
        self.assertTrue(public_records)
        self.assertEqual(validate_trusted_keys(deepcopy(public_records)), public_records)
        for record in public_records.values():
            self.assertEqual(set(record), {'algorithm', 'public_key'})
            self.assertEqual(record['algorithm'], 'ed25519')

    def test_private_or_credential_fields_are_not_permitted_in_public_records(self):
        public_records = load_channel()['trusted_keys']
        for extra in ('private_key', 'password', 'Cookie'):
            changed = deepcopy(public_records)
            changed[next(iter(changed))][extra] = 'unit disallowed field'
            with self.subTest(extra=extra), self.assertRaises(UpdateValidationError):
                validate_trusted_keys(changed)

    def test_build_identity_exports_only_public_channel_summary(self):
        channel = runtime_identity()['update_channel']
        self.assertEqual(set(channel), {'key_state', 'source_sha256', 'key_fingerprints'})
        self.assertTrue(re.fullmatch('[0-9a-f]{64}', channel['source_sha256']))
        self.assertTrue(all(re.fullmatch('[0-9a-f]{64}', value)
            for value in channel['key_fingerprints'].values()))
        serialized = json.dumps(channel)
        for record in load_channel()['trusted_keys'].values():
            self.assertNotIn(record['public_key'], serialized)
        self.assertNotIn('private_key', serialized)
