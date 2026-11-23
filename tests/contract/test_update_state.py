"""The updater state the code really writes must match the published schema."""
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from modules.update import releases, transport
from modules.update.state import new_state, refresh_derived
from tests.contract.schema_check import assert_valid
from tests.update.fixtures import GITHUB_V120, release_item

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / 'doc' / 'schemas' / 'update-state.schema.json').read_text())
MOMENT = datetime(2026, 11, 21, 3, 7, 41, tzinfo=timezone.utc)


class UpdateStateSchemaTests(unittest.TestCase):
    def test_a_fresh_state_is_valid(self):
        assert_valid(self, new_state('1.2.0-1', MOMENT), SCHEMA, 'new updater state')

    def test_success_and_failure_states_are_valid(self):
        document = new_state('1.2.0-1', MOMENT)
        document['source'] = 'https://api.github.com/repos/ANTS-research-group/qmi-supervisor/releases'
        document['checked_utc'] = document['last_success_utc'] = document['updated_utc']
        document['etag'] = 'W/"abc"'
        document['releases'] = releases.parse_releases([
            GITHUB_V120,
            release_item('v9.9.0', published_at='2026-11-20T10:00:00Z'),
            release_item('v1.3.0-rc1', deb='qmi-supervisor_1.3.0~rc1-1_all.deb'),
            release_item('v1.3.0-rc1', deb='qmi-supervisor_1.3.0-1_all.deb'),
            release_item('v0.9.0', draft=True),
        ], require_signature=True)
        refresh_derived(document, current_version='1.2.0-1', channel='stable', require_signature=True)
        assert_valid(self, document, SCHEMA, 'state after a successful check')

        document['check_error'] = {'code': 'network', 'detail': 'x', 'status': None,
                                   'since_utc': document['updated_utc'], 'attempts': 1}
        assert_valid(self, document, SCHEMA, 'state after a failed check')

    def test_reason_and_error_enums_match_the_code_tables(self):
        reason_schema = SCHEMA['properties']['releases']['items']['properties']['reason']
        self.assertEqual(set(reason_schema['enum']) - {None}, set(releases.REASON_TEXT))
        error_schema = SCHEMA['properties']['check_error']['properties']['code']
        self.assertEqual(set(error_schema['enum']), set(transport.ERROR_TEXT))


if __name__ == '__main__':
    unittest.main()
