#!/usr/bin/env python3
"""Regenerate the published diagnosis-code contract files.

`modules/runtime/diagnosis.py` (CODES) is the single source of truth. This
generator writes doc/schemas/codes.json and patches the `code` enum inside
doc/schemas/diagnosis.schema.json. The output is deterministic: sorted keys,
two-space indentation and a trailing newline, so a test can compare the
committed files with a fresh run.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.runtime.diagnosis import CODES  # noqa: E402

SCHEMAS = ROOT / 'doc' / 'schemas'
CODES_NAME = 'codes.json'
DIAGNOSIS_NAME = 'diagnosis.schema.json'


def codes_document(codes=None):
    """The machine-readable catalogue: version plus code -> category/policy/since."""
    source = CODES if codes is None else codes
    return {
        'version': 1,
        'codes': {
            code: {
                'category': entry['category'],
                'policy': entry['policy'],
                'since': entry['since'],
            }
            for code, entry in source.items()
        },
    }


def dump(value):
    return json.dumps(value, indent=2, sort_keys=True) + '\n'


def render_codes(codes=None):
    return dump(codes_document(codes))


def render_diagnosis_schema(schema, codes=None):
    """Return the schema with the diagnosis code enum set from CODES."""
    source = CODES if codes is None else codes
    result = json.loads(json.dumps(schema))
    result.setdefault('properties', {}).setdefault('code', {})['enum'] = sorted(source)
    return dump(result)


def generate(directory=SCHEMAS, codes=None):
    """Write both generated artefacts into `directory` and return their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    codes_path = directory / CODES_NAME
    codes_path.write_text(render_codes(codes))
    schema_path = directory / DIAGNOSIS_NAME
    schema = json.loads(schema_path.read_text())
    schema_path.write_text(render_diagnosis_schema(schema, codes))
    return codes_path, schema_path


def main():
    codes_path, schema_path = generate()
    print(f'wrote {codes_path}')
    print(f'wrote {schema_path}')


if __name__ == '__main__':
    main()
