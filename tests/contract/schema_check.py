"""Minimal JSON Schema validator for the keyword subset used by doc/schemas.

No third-party dependency is required. When `jsonschema` is importable the
contract tests additionally run the document through it (Draft 7 understands
every keyword this project uses).
"""
import re

ALLOWED_KEYWORDS = {
    '$schema', '$id', 'title', 'description', 'type', 'properties', 'required',
    'additionalProperties', 'items', 'enum', 'const', 'minimum', 'maximum',
    'maxLength', 'maxItems', 'pattern',
}

_PRIMITIVES = {'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'}


def _is_bool(value):
    return isinstance(value, bool)


def _matches_type(value, name):
    if name == 'null':
        return value is None
    if name == 'boolean':
        return _is_bool(value)
    if name == 'integer':
        return isinstance(value, int) and not _is_bool(value)
    if name == 'number':
        return isinstance(value, (int, float)) and not _is_bool(value)
    if name == 'string':
        return isinstance(value, str)
    if name == 'array':
        return isinstance(value, list)
    if name == 'object':
        return isinstance(value, dict)
    return True


def _json_equal(first, second):
    """Compare with JSON types: True is not 1, and 1 is not 1.0."""
    if _is_bool(first) or _is_bool(second):
        return _is_bool(first) and _is_bool(second) and first is second
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return type(first) is type(second) and first == second
    if isinstance(first, dict) and isinstance(second, dict):
        return (set(first) == set(second)
                and all(_json_equal(first[key], second[key]) for key in first))
    if isinstance(first, list) and isinstance(second, list):
        return (len(first) == len(second)
                and all(_json_equal(a, b) for a, b in zip(first, second)))
    return type(first) is type(second) and first == second


def _join(path, key):
    return f'{path}.{key}'


def validate(instance, schema, path='$'):
    """Return a list of human-readable errors (empty when the value is valid)."""
    if not isinstance(schema, dict):
        return [f'{path}: schema is not an object']
    errors = []
    unknown = sorted(set(schema) - ALLOWED_KEYWORDS)
    if unknown:
        errors.append(f'{path}: unsupported schema keyword(s): {", ".join(unknown)}')

    if 'const' in schema and not _json_equal(instance, schema['const']):
        errors.append(f'{path}: expected const {schema["const"]!r}, got {instance!r}')

    if 'enum' in schema and not any(_json_equal(instance, option) for option in schema['enum']):
        errors.append(f'{path}: {instance!r} is not one of {schema["enum"]!r}')

    declared = schema.get('type')
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_matches_type(instance, name) for name in names):
            errors.append(f'{path}: expected type {declared!r}, got {type(instance).__name__}')
            return errors
        for name in names:
            if name not in _PRIMITIVES:
                errors.append(f'{path}: unknown type name {name!r}')

    if isinstance(instance, dict):
        properties = schema.get('properties') or {}
        for name in schema.get('required') or []:
            if name not in instance:
                errors.append(f'{path}: missing required property {name!r}')
        for name, subschema in properties.items():
            if name in instance:
                errors.extend(validate(instance[name], subschema, _join(path, name)))
        additional = schema.get('additionalProperties', True)
        for name, value in instance.items():
            if name in properties:
                continue
            if additional is False:
                errors.append(f'{path}: unexpected property {name!r}')
            elif isinstance(additional, dict):
                errors.extend(validate(value, additional, _join(path, name)))

    if isinstance(instance, list):
        if 'maxItems' in schema and len(instance) > schema['maxItems']:
            errors.append(f'{path}: {len(instance)} items exceeds maxItems {schema["maxItems"]}')
        if 'items' in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema['items'], f'{path}[{index}]'))

    if isinstance(instance, str):
        if 'maxLength' in schema and len(instance) > schema['maxLength']:
            errors.append(f'{path}: length {len(instance)} exceeds maxLength {schema["maxLength"]}')
        if 'pattern' in schema and re.search(schema['pattern'], instance) is None:
            errors.append(f'{path}: {instance!r} does not match pattern {schema["pattern"]!r}')

    if isinstance(instance, (int, float)) and not _is_bool(instance):
        if 'minimum' in schema and instance < schema['minimum']:
            errors.append(f'{path}: {instance!r} is below minimum {schema["minimum"]}')
        if 'maximum' in schema and instance > schema['maximum']:
            errors.append(f'{path}: {instance!r} is above maximum {schema["maximum"]}')

    return errors


def assert_valid(testcase, instance, schema, label='document'):
    """Fail the unittest case when the instance does not match the schema."""
    errors = validate(instance, schema)
    testcase.assertEqual(errors, [], f'{label} does not match its schema')
    try:
        import jsonschema
    except ImportError:
        return
    # Use Draft 7 explicitly: its vocabulary contains every keyword above and
    # older `jsonschema` releases may not know the 2020-12 meta-schema.
    jsonschema.Draft7Validator(schema).validate(instance)
