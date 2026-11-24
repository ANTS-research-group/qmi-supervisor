"""Plugin manifests: validation, enable/disable and the capability allow-lists.

A plugin is a manifest plus a program. Manifests live in one INI file per
plugin and are re-read on every start, so editing or enabling one takes effect
without restarting the service.
"""
from configparser import ConfigParser, Error as ConfigError
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import tempfile

MANIFEST_DIR = '/etc/qmi-supervisor/plugins.d'
NAME = re.compile(r'[a-z0-9-]{1,32}')
CAPABILITIES = ('read:status', 'read:events', 'act:objective', 'act:reconnect',
                'ui:cards', 'ui:issues')
READ_CAPABILITIES = ('read:status', 'read:events')
GRANTED_CAPABILITIES = ('act:objective', 'act:reconnect', 'ui:cards', 'ui:issues')
# OS capabilities a plugin may declare; the administrator confirms the manifest.
OS_CAPABILITIES = {
    'net_raw': 'CAP_NET_RAW',
    'net_admin': 'CAP_NET_ADMIN',
    'net_bind_service': 'CAP_NET_BIND_SERVICE',
}
MAX_SETTINGS_BYTES = 16 * 1024
MAX_ARGS = 16


@dataclass
class Manifest:
    name: str
    path: Path
    description: str = ''
    version: str = ''
    exec: str = ''
    args: tuple = ()
    enabled: bool = False
    capabilities: tuple = ()
    os_capabilities: tuple = ()
    settings: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def valid(self):
        return self.error is None

    def public(self):
        return {'name': self.name, 'description': self.description, 'version': self.version,
                'enabled': self.enabled and self.valid, 'capabilities': list(self.capabilities),
                'os_capabilities': list(self.os_capabilities),
                'valid': self.valid, 'error': self.error}


def _root_owned_file(path):
    """A plugin program is root-owned, not writable by group/others, and so is its directory."""
    try:
        metadata = os.stat(path)
        directory = os.stat(Path(path).parent)
    except OSError:
        return False
    return (stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0
            and not metadata.st_mode & 0o022
            and directory.st_uid == 0 and not directory.st_mode & 0o022)


def _parse_args(raw):
    if not raw:
        return ()
    args = tuple(part for part in raw.split() if part)
    if len(args) > MAX_ARGS or any(len(part) > 256 for part in args):
        raise ValueError('args must be at most 16 short arguments')
    return args


def load_manifest(path, *, require_root=True):
    path = Path(path)
    name = path.name[:-4] if path.name.endswith('.ini') else path.name
    manifest = Manifest(name=name, path=path)
    try:
        parser = ConfigParser(interpolation=None)
        parser.read_string(path.read_text(encoding='utf-8'))
        if not parser.has_section('plugin'):
            raise ValueError('missing [plugin] section')
        plugin = parser['plugin']
        declared = plugin.get('name', '').strip()
        if declared != name:
            raise ValueError('name must equal the file name')
        if not NAME.fullmatch(name):
            raise ValueError('name must match [a-z0-9-]{1,32}')
        manifest.description = plugin.get('description', '').strip()
        if not manifest.description or len(manifest.description) > 200:
            raise ValueError('description is required and must be at most 200 characters')
        manifest.version = plugin.get('version', '').strip()
        if len(manifest.version) > 32:
            raise ValueError('version must be at most 32 characters')
        raw_exec = plugin.get('exec', '').strip()
        if not raw_exec or not os.path.isabs(raw_exec):
            raise ValueError('exec must be an absolute path')
        manifest.exec = raw_exec
        if require_root and not _root_owned_file(raw_exec):
            raise ValueError('exec must be a root-owned file, not writable by group or others')
        if not os.path.isfile(raw_exec):
            raise ValueError('exec does not exist')
        manifest.args = _parse_args(plugin.get('args', ''))
        enabled = plugin.get('enabled', '').strip().lower()
        if enabled not in {'true', 'false'}:
            raise ValueError('enabled must be true or false')
        manifest.enabled = enabled == 'true'
        raw_capabilities = [item.strip() for item in plugin.get('capabilities', '').split(',')]
        manifest.capabilities = tuple(dict.fromkeys(item for item in raw_capabilities if item))
        unknown = [item for item in manifest.capabilities if item not in CAPABILITIES]
        if unknown:
            raise ValueError('unknown capability: ' + ', '.join(unknown))
        raw_os = [item.strip() for item in plugin.get('os_capabilities', '').split(',')]
        manifest.os_capabilities = tuple(dict.fromkeys(item for item in raw_os if item))
        unknown = [item for item in manifest.os_capabilities if item not in OS_CAPABILITIES]
        if unknown:
            raise ValueError('unknown system capability: ' + ', '.join(unknown))
        if parser.has_section('settings'):
            size = 0
            for key, value in parser.items('settings'):
                size += len(key.encode()) + len(value.encode())
                manifest.settings[key] = value
            if size > MAX_SETTINGS_BYTES:
                raise ValueError('the [settings] section is larger than 16 KiB')
    except (OSError, ConfigError, ValueError) as error:
        manifest.error = str(error)[:200]
    return manifest


def load_manifests(directory=MANIFEST_DIR, *, require_root=True):
    """Every manifest, valid or not; invalid ones are listed but never started."""
    directory = Path(directory)
    try:
        paths = sorted(directory.glob('*.ini'))
    except OSError:
        return []
    return [load_manifest(path, require_root=require_root) for path in paths]


def set_enabled(path, enabled):
    """Rewrite only the enabled value; the rest of the manifest stays byte-identical."""
    path = Path(path)
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError as error:
        raise ValueError('cannot read the manifest: ' + str(error)) from None
    lines, inside = [], False
    changed = False
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            inside = stripped == '[plugin]'
        if inside and re.match(r'\s*enabled\s*=', line):
            ending = '\n' if line.endswith('\n') else ''
            line = f'enabled = {"true" if enabled else "false"}{ending}'
            changed = True
        lines.append(line)
    if not changed:
        raise ValueError('the manifest has no enabled key')
    content = ''.join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def grants_path(name, directory=MANIFEST_DIR):
    return Path(directory)/(name + '.grants.json')


def load_grants(name, directory=MANIFEST_DIR):
    """Capabilities the administrator confirmed; only action/UI ones need this file."""
    try:
        value = json.loads(grants_path(name, directory).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ()
    if not isinstance(value, dict) or value.get('version') != 1:
        return ()
    granted = value.get('capabilities')
    if not isinstance(granted, list):
        return ()
    return tuple(item for item in granted if item in GRANTED_CAPABILITIES)


def set_grant(name, capability, granted, directory=MANIFEST_DIR):
    if capability not in GRANTED_CAPABILITIES:
        raise ValueError('unknown granted capability: ' + str(capability))
    current = set(load_grants(name, directory))
    if granted:
        current.add(capability)
    else:
        current.discard(capability)
    path = grants_path(name, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps({'version': 1, 'capabilities': sorted(current)},
                                    sort_keys=True), encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return tuple(sorted(current))


def effective_capabilities(manifest, directory=MANIFEST_DIR):
    """Read scopes from the manifest plus the action/UI scopes the administrator granted."""
    granted = set(load_grants(manifest.name, directory))
    return tuple(capability for capability in manifest.capabilities
                 if capability in READ_CAPABILITIES or capability in granted)
