"""Private installation receipt and per-transaction package ownership."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import tempfile


PACKAGE = re.compile(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?\Z')
INVENTORY_FORMAT = '-f=${binary:Package}\\t${Status}\\n'
MAX_RECEIPT_BYTES = 4 * 1024 * 1024


def _private_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Installation receipt paths must be absolute')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f'Refusing a symlink in installation receipt path: {component}')
    return path


def _read_private(path):
    path = _private_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f'Installation receipt must be a regular file: {path}')
        text = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(text.encode('utf-8')) > MAX_RECEIPT_BYTES:
        raise ValueError(f'Installation receipt is too large: {path}')
    return text


def _absolute(value):
    return (isinstance(value, str) and '\x00' not in value and Path(value).is_absolute()
            and '..' not in Path(value).parts)


def _validate(state):
    try:
        if not isinstance(state, dict) or type(state['version']) is not int or state['version'] != 1:
            raise ValueError('unsupported version')
        packages = state['packages']
        if not isinstance(packages, list) or any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in packages):
            raise ValueError('invalid package ownership')
        mm = state['modem_manager']
        if not isinstance(mm, dict) or any(type(mm[k]) is not bool for k in ('active', 'enabled', 'masked', 'changed')):
            raise ValueError('invalid ModemManager baseline')
        rule = state['network_rule']
        if (not isinstance(rule, dict) or type(rule['existed']) is not bool or type(rule['changed']) is not bool
                or type(rule['mode']) is not int or not 0 <= rule['mode'] <= 0o7777
                or (rule['existed'] and not isinstance(rule['content'], str))
                or (not rule['existed'] and rule['content'] is not None)):
            raise ValueError('invalid network rule baseline')
        artifacts = state['artifacts']
        if not isinstance(artifacts, dict):
            raise ValueError('invalid artifacts')
        for key in ('app_backups', 'config_backups'):
            if not isinstance(artifacts[key], list) or any(not _absolute(p) for p in artifacts[key]):
                raise ValueError('invalid artifact paths')
        directories = state['data_directories']
        if (not isinstance(directories, list)
                or any(not isinstance(d, dict) or not _absolute(d['path']) or type(d['existed']) is not bool
                       for d in directories)):
            raise ValueError('invalid capture directory ownership')
        if type(state['completed']) is not bool:
            raise ValueError('invalid completion flag')
        if 'resolver_config' in state:
            resolver = state['resolver_config']
            if (not isinstance(resolver, dict) or type(resolver['existed']) is not bool
                    or type(resolver['changed']) is not bool or resolver['kind'] not in {'absent', 'file', 'symlink'}
                    or type(resolver['mode']) is not int or not 0 <= resolver['mode'] <= 0o7777
                    or (resolver['kind'] == 'file' and not isinstance(resolver['content'], str))
                    or (resolver['kind'] == 'symlink' and not isinstance(resolver['target'], str))):
                raise ValueError('invalid resolver baseline')
    except (KeyError, TypeError) as error:
        raise ValueError('Invalid installation receipt schema') from error
    return state


def load(paths):
    """Return a validated receipt, refusing links rather than following them."""
    try:
        text = _read_private(paths.manifest)
    except FileNotFoundError:
        return None
    try:
        return _validate(json.loads(text))
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'Invalid installation receipt {paths.manifest}: {error}') from error


def save(paths, state):
    """Atomically persist the receipt in a private directory."""
    _validate(state)
    path = _private_path(paths.manifest)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_path(path)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.manifest-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _inventory(text, *, include_config_files=False):
    present = {'installed', 'unpacked', 'half-installed', 'half-configured',
               'triggers-awaited', 'triggers-pending'}
    if include_config_files:
        present.add('config-files')
    packages = set()
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split('\t', 1)
        if len(parts) != 2:
            raise ValueError('Cannot parse installed package inventory')
        package, status = parts
        if not PACKAGE.fullmatch(package):
            raise ValueError(f'Invalid installed package name: {package!r}')
        fields = status.split()
        if len(fields) != 3:
            raise ValueError('Cannot parse installed package status')
        if fields[-1] in present:
            packages.add(package)
    return packages


def _query_inventory(runner, *, include_config_files=False):
    result = runner(['dpkg-query', '-W', INVENTORY_FORMAT])
    if result.returncode:
        raise ValueError('Cannot read installed package inventory')
    return _inventory(result.stdout, include_config_files=include_config_files)


def installed_packages(runner):
    """Read present packages, including interrupted installs and multiarch names."""
    return _query_inventory(runner)


def new_state(modem_manager, network_rule, resolver_config=None):
    state = dict(version=1, packages=[], modem_manager=dict(modem_manager),
                 network_rule=dict(network_rule),
                 artifacts=dict(app_backups=[], config_backups=[]),
                 data_directories=[], completed=False)
    if resolver_config is not None:
        state['resolver_config'] = dict(resolver_config)
    return _validate(state)


def add_data_directory(state, path):
    path = Path(os.path.abspath(path))
    if not any(item['path'] == str(path) for item in state['data_directories']):
        state['data_directories'].append({'path': str(path), 'existed': path.exists() or path.is_symlink()})


def add_artifact(state, kind, path):
    value = str(Path(os.path.abspath(path)))
    if value not in state['artifacts'][kind]:
        state['artifacts'][kind].append(value)


def consume_bootstrap(paths, state):
    """Use only the immediate bootstrap before/after pair, never a later host diff."""
    before = paths.manifest.parent / 'bootstrap-packages.before'
    after = paths.manifest.parent / 'bootstrap-packages.after'
    if not (before.exists() or before.is_symlink() or after.exists() or after.is_symlink()):
        return False
    if not before.exists() or not after.exists():
        raise ValueError('Incomplete Python bootstrap inventory; retain the installation receipt directory for recovery')
    introduced = (_inventory(_read_private(after), include_config_files=True)
                  - _inventory(_read_private(before), include_config_files=True))
    state['packages'] = sorted(set(state['packages']) | introduced)
    save(paths, state)
    before.unlink()
    after.unlink()
    return True


@contextmanager
def record_packages(paths, state, runner):
    """Record this APT transaction, including a partially failed installation."""
    # An old rc/config-files entry is preexisting too: do not acquire its ownership.
    before = _query_inventory(runner, include_config_files=True)
    save(paths, state)
    try:
        yield
    finally:
        after = _query_inventory(runner, include_config_files=True)
        state['packages'] = sorted(set(state['packages']) | (after - before))
        save(paths, state)
