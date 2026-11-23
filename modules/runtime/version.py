"""The installed package version and Debian version comparison.

This module is deliberately free of GI and modem imports: the manager, the web
server and the telemetry worker all need it, and none of them may load the QMI
stack just to print a version.

`installed_version()` answers the only question the rest of the application
asks: which package generated this tree? The package ships a `VERSION` file next
to `qmi_supervisor.py`; older installs are read through `dpkg-query`, and a
development checkout is described by git exactly as `tools/packaging/build-deb.sh`
derives it.
"""
from pathlib import Path
import subprocess

# The directory that holds qmi_supervisor.py: modules/runtime/version.py -> root.
APPLICATION_ROOT = Path(__file__).resolve().parents[2]
DPKG_ROOT = Path('/opt/qmi-supervisor')

_cached = None


def reset_cache():
    """Forget the detected version; tests use it between synthetic installs."""
    global _cached
    _cached = None


def _read_version_file(root):
    try:
        value = (Path(root) / 'VERSION').read_text(encoding='utf-8').strip()
    except OSError:
        return None
    return value or None


def _dpkg_query():
    """Package version from the dpkg database; None when dpkg cannot answer."""
    try:
        result = subprocess.run(
            ['dpkg-query', '-W', '-f=${Version}', 'qmi-supervisor'],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _git_describe(root):
    """`git describe --tags --long --dirty`, or None outside a describable tree."""
    try:
        result = subprocess.run(
            ['git', '-C', str(root), 'describe', '--tags', '--long', '--dirty'],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _detect(root):
    root = Path(root)
    value = _read_version_file(root)
    if value:
        return value
    if root == DPKG_ROOT:
        value = _dpkg_query()
        if value:
            return value
    raw = _git_describe(root)
    if raw:
        try:
            return describe_to_version(raw)
        except ValueError:
            pass
    return 'unknown'


def installed_version():
    """The package version of the running tree, cached for the process lifetime."""
    global _cached
    if _cached is None:
        _cached = _detect(APPLICATION_ROOT)
    return _cached


def tag_to_upstream(tag):
    """A release tag as a Debian upstream version.

    `v1.2.0` -> `1.2.0`, `v1.3.0-rc1` -> `1.3.0~rc1`: every hyphen of a tag
    becomes a tilde so a release candidate sorts before the release itself.
    Raises ValueError for something that is not a release tag.
    """
    if not isinstance(tag, str):
        raise TypeError('tag must be a string')
    value = tag[1:] if tag.startswith('v') else tag
    if not value or value[0] not in '0123456789' or value != value.strip():
        raise ValueError(f'not a release tag: {tag!r}')
    if not all(char.isascii() and (char.isalnum() or char in '.+~-') for char in value):
        raise ValueError(f'not a release tag: {tag!r}')
    return value.replace('-', '~')


def describe_to_version(raw, revision='1'):
    """Map one `git describe --tags --long --dirty` line like build-deb.sh.

    `v1.2.0-0-g1bfa441` -> `1.2.0-1`; one commit later ->
    `1.2.0+1.g1bfa441-1`; `v1.3.0-rc1-2-gabc-dirty` ->
    `1.3.0~rc1+2.gabc.dirty-1`. The revision is the packaging revision.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('empty git describe output')
    text = raw.strip()
    dirty = text.endswith('-dirty')
    if dirty:
        text = text[:-len('-dirty')]
    tagged, separator, described = text.rpartition('-')
    if not separator or len(described) < 2 or not described.startswith('g'):
        raise ValueError(f'not a git describe line: {raw!r}')
    tag, separator, count = tagged.rpartition('-')
    if not separator or not count.isdigit():
        raise ValueError(f'not a git describe line: {raw!r}')
    base = tag_to_upstream(tag)
    if count != '0':
        base = f'{base}+{count}.g{described[1:]}'
    if dirty:
        base += '.dirty'
    return f'{base}-{revision}'


def _order(char):
    """Sort weight of one version character, exactly as dpkg's order()."""
    if char == '~':
        return -1
    if char == '' or char.isdigit():
        return 0
    if ('a' <= char <= 'z') or ('A' <= char <= 'Z'):
        return ord(char)
    return ord(char) + 256


def _verrevcmp(first, second):
    """The dpkg string comparison of 5.6.12: ~ first, letters before others."""
    left = right = 0
    while left < len(first) or right < len(second):
        difference = 0
        while ((left < len(first) and not first[left].isdigit())
               or (right < len(second) and not second[right].isdigit())):
            a = _order(first[left] if left < len(first) else '')
            b = _order(second[right] if right < len(second) else '')
            if a != b:
                return -1 if a < b else 1
            left += 1
            right += 1
        while left < len(first) and first[left] == '0':
            left += 1
        while right < len(second) and second[right] == '0':
            right += 1
        while (left < len(first) and right < len(second)
               and first[left].isdigit() and second[right].isdigit()):
            if not difference:
                difference = ord(first[left]) - ord(second[right])
            left += 1
            right += 1
        if left < len(first) and first[left].isdigit():
            return 1
        if right < len(second) and second[right].isdigit():
            return -1
        if difference:
            return -1 if difference < 0 else 1
    return 0


def _parse(version):
    if not isinstance(version, str):
        raise TypeError('version must be a string')
    if not version or version != version.strip() or any(char.isspace() for char in version):
        raise ValueError(f'not a Debian version: {version!r}')
    epoch = 0
    rest = version
    if ':' in rest:
        head, rest = rest.split(':', 1)
        if not head.isdigit():
            raise ValueError(f'not a Debian version: {version!r}')
        epoch = int(head)
    if not rest:
        raise ValueError(f'not a Debian version: {version!r}')
    if '-' in rest:
        upstream, revision = rest.rsplit('-', 1)
        if not revision:
            raise ValueError(f'not a Debian version: {version!r}')
    else:
        upstream, revision = rest, ''
    if not upstream:
        raise ValueError(f'not a Debian version: {version!r}')
    return epoch, upstream, revision


def compare_versions(first, second):
    """-1, 0 or 1 with Debian policy 5.6.12 ordering (dpkg --compare-versions)."""
    epoch_a, upstream_a, revision_a = _parse(first)
    epoch_b, upstream_b, revision_b = _parse(second)
    if epoch_a != epoch_b:
        return -1 if epoch_a < epoch_b else 1
    order = _verrevcmp(upstream_a, upstream_b)
    if order:
        return order
    return _verrevcmp(revision_a, revision_b)
