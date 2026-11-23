"""Fetch and parse the release list into bounded, installable-or-explained records.

Every stored string is bounded, the response is limited to 2 MiB and JSON is
parsed as untrusted input (the updater runs as root in its own unit). Only the
fields a Gitea-style mirror would also provide are required, so a mirror parses.
"""
from collections import namedtuple
from datetime import datetime, timezone
import functools
import json
import re

from modules.runtime import version as runtime_version
from modules.update.transport import (
    GITHUB_API_VERSION, UpdateCheckError, bounded, check_url, http_get,
)

MAX_RELEASES = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
NOTES_MAX_BYTES = 8192
NAME_MAX_CHARS = 128
ASSET_NAME_MAX_CHARS = 255
DEB_VERSION_MAX_CHARS = 128
TAG_MAX_CHARS = 64
SUMS_NAME = 'SHA256SUMS'
SIGNATURE_NAME = 'SHA256SUMS.asc'

# English message IDs printed by the CLI and translated by the web interface (U4).
REASON_TEXT = {
    'bad_tag': 'The release tag is not a version number',
    'no_package': 'The release has no qmi-supervisor package',
    'several_packages': 'The release has more than one qmi-supervisor package',
    'bad_package_name': 'The package file name does not contain a package version',
    'version_mismatch': 'The package version does not match the release tag',
    'asset_not_ready': 'A release file is empty or still uploading',
    'bad_url': 'A release file has an address that is not allowed',
    'package_too_large': 'The package is larger than 32 MiB',
    'no_checksums': 'The release has no SHA256SUMS file',
    'unsigned': 'The release has no signature (SHA256SUMS.asc)',
}

# Reasons whose package version is still known: eligible for "newest available".
VERSION_KNOWN_REASONS = {None, 'asset_not_ready', 'bad_url', 'package_too_large', 'no_checksums', 'unsigned'}

DEB_ANY = re.compile(r'qmi-supervisor_.*_all\.deb\Z')
DEB_NAME = re.compile(
    r'qmi-supervisor_(?P<upstream>[0-9][A-Za-z0-9.+~-]*)-(?P<revision>[0-9][A-Za-z0-9.+~]*)_all\.deb\Z')
UPSTREAM = re.compile(r'[0-9][A-Za-z0-9.+~]*\Z')

FetchResult = namedtuple('FetchResult', 'items etag not_modified')


def clean_etag(value):
    """A printable ASCII ETag of 1..256 characters, else None."""
    if isinstance(value, str) and 1 <= len(value) <= 256 and all(32 <= ord(c) < 127 for c in value):
        return value
    return None


def fetch_releases(source, *, token=None, etag=None, timeout=10.0):
    """Read the first page of the release list, honouring a stored ETag."""
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': GITHUB_API_VERSION}
    if etag:
        headers['If-None-Match'] = etag
    response = http_get(source, headers=headers, token=token, timeout=timeout, limit=MAX_RESPONSE_BYTES)
    if response.status == 304:
        if not etag:
            raise UpdateCheckError('bad_response', 'The release server answered 304 without a stored ETag')
        return FetchResult(None, etag, True)
    try:
        items = json.loads(response.body.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise UpdateCheckError('bad_response', 'The release list is not valid JSON') from None
    if not isinstance(items, list):
        raise UpdateCheckError('bad_response', 'The release list is not a JSON array')
    return FetchResult(items, clean_etag(response.headers.get('ETag')), False)


def _clean_text(value):
    """JSON may carry lone surrogates; make a string safe to store and encode."""
    return value.encode('utf-8', errors='replace').decode('utf-8', errors='replace')


def _printable_tag(value):
    if not isinstance(value, str) or not 1 <= len(value) <= TAG_MAX_CHARS:
        return None
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        return None
    return value


def _safe_url(value):
    try:
        return check_url(value, code='bad_url')
    except UpdateCheckError:
        return None


def _size(asset):
    value = asset.get('size')
    return value if type(value) is int and value >= 0 else 0


def _digest(asset):
    value = asset.get('digest')
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'sha256:([0-9a-f]{64})', value)
    return match.group(1) if match else None


def _record(asset):
    """The public asset shape stored in state.json (GitHub state is not kept)."""
    return {'name': asset['name'], 'size': asset['size'], 'url': asset['url'], 'api_url': asset['api_url']}


def _ready(asset):
    state = asset.get('state')
    return state is None or state == 'uploaded'


def _notes(item):
    body = item.get('body')
    if not isinstance(body, str):
        body = ''
    body = _clean_text(body)
    notes = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', body.replace('\r\n', '\n').replace('\r', '\n'))
    truncated = False
    encoded = notes.encode('utf-8', errors='replace')
    if len(encoded) > NOTES_MAX_BYTES:
        notes = encoded[:NOTES_MAX_BYTES].decode('utf-8', errors='ignore')
        truncated = True
    return notes, truncated


def _published(item):
    value = item.get('published_at')
    if not isinstance(value, str):
        return None
    try:
        # Python 3.10's fromisoformat rejects the trailing 'Z'.
        moment = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc).isoformat()


def parse_release(item, *, require_signature=True):
    """One GitHub-shaped release dict, or None when it must not be listed."""
    if not isinstance(item, dict) or item.get('draft') is True:
        return None
    tag = _printable_tag(item.get('tag_name'))
    if tag is None:
        return None

    try:
        version = runtime_version.tag_to_upstream(tag)
    except (ValueError, TypeError):
        version = None
    if version is not None and not UPSTREAM.fullmatch(version):
        version = None

    raw_name = item.get('name')
    if isinstance(raw_name, str) and raw_name:
        name = re.sub(r'[\x00-\x1f\x7f]', '', _clean_text(raw_name))[:NAME_MAX_CHARS]
    else:
        name = tag

    published_utc = _published(item)
    prerelease = item.get('prerelease') is True or (version is not None and '~' in version)
    html_url = _safe_url(item.get('html_url'))
    notes, notes_truncated = _notes(item)

    assets = []
    raw_assets = item.get('assets')
    for asset in raw_assets if isinstance(raw_assets, list) else []:
        if not isinstance(asset, dict) or not isinstance(asset.get('name'), str):
            continue
        asset_name = _clean_text(asset['name'])
        # GitHub caps asset names at 255 characters; anything longer cannot be a
        # legitimate package and would violate the published state schema.
        if not asset_name or len(asset_name) > ASSET_NAME_MAX_CHARS:
            continue
        assets.append({
            'name': asset_name,
            'size': _size(asset),
            'url': _safe_url(asset.get('browser_download_url')),
            'api_url': _safe_url(asset.get('url')),
            'state': asset.get('state'),
            'digest': _digest(asset),
        })

    matches = [asset for asset in assets if DEB_ANY.fullmatch(asset['name'])]
    deb = None
    deb_version = None
    name_match = None
    mismatch = False
    if len(matches) == 1:
        source_asset = matches[0]
        deb = _record(source_asset)
        name_match = DEB_NAME.fullmatch(source_asset['name'])
        if name_match is None:
            deb['version'] = None
        else:
            upstream, revision = name_match['upstream'], name_match['revision']
            named = f'{upstream}-{revision}'
            if version is None or upstream == version:
                deb_version = named
            elif re.sub(r'[~+]', '.', upstream) == re.sub(r'[~+]', '.', version):
                # GitHub replaced ~ or + by '.' in the uploaded file name.
                deb_version = f'{version}-{revision}'
            else:
                deb_version = named
                mismatch = True
            if len(deb_version) > DEB_VERSION_MAX_CHARS:
                # Too long to be a real package version (and the contract bounds it).
                name_match, deb_version = None, None
            deb['version'] = deb_version
        deb['sha256'] = source_asset['digest']

    sums = next((asset for asset in assets if asset['name'] == SUMS_NAME), None)
    signature = next((asset for asset in assets if asset['name'] == SIGNATURE_NAME), None)

    if version is None:
        reason = 'bad_tag'
    elif not matches:
        reason = 'no_package'
    elif len(matches) > 1:
        reason = 'several_packages'
    elif name_match is None:
        reason = 'bad_package_name'
    elif mismatch:
        reason = 'version_mismatch'
    elif (not _ready(matches[0]) or matches[0]['size'] == 0
          or (sums is not None and not _ready(sums))
          or (signature is not None and not _ready(signature))):
        reason = 'asset_not_ready'
    elif (deb['url'] is None
          or (sums is not None and sums['url'] is None)
          or (signature is not None and signature['url'] is None)):
        reason = 'bad_url'
    elif deb['size'] > MAX_PACKAGE_BYTES:
        reason = 'package_too_large'
    elif sums is None:
        reason = 'no_checksums'
    elif signature is None and require_signature:
        reason = 'unsigned'
    else:
        reason = None

    return {
        'tag': tag,
        'version': version,
        'name': name,
        'published_utc': published_utc,
        'prerelease': prerelease,
        'html_url': html_url,
        'notes': notes,
        'notes_truncated': notes_truncated,
        'deb': deb,
        'sums': _record(sums) if sums is not None else None,
        'signature': _record(signature) if signature is not None else None,
        'installable': reason is None,
        'reason': reason,
    }


def parse_releases(items, *, require_signature=True):
    """Parse every item, drop the non-listable ones and keep the newest 30."""
    parsed = [parse_release(item, require_signature=require_signature) for item in items]
    parsed = [release for release in parsed if release is not None]
    # Descending by publication, unknown dates last; the sort is stable for ties.
    parsed.sort(key=lambda release: (release['published_utc'] is not None, release['published_utc'] or ''),
                reverse=True)
    return parsed[:MAX_RELEASES]


def apply_signature_policy(release, require_signature):
    """Re-decide only the 'unsigned' rule after a require_signature change."""
    if release['reason'] in (None, 'unsigned'):
        reason = 'unsigned' if require_signature and release['signature'] is None else None
        return {**release, 'reason': reason, 'installable': reason is None}
    return release


def eligible(release, channel):
    return channel == 'prerelease' or not release['prerelease']


def newest(releases, channel, *, installable_only):
    """The greatest eligible package version, or None."""
    candidates = [release for release in releases
                  if eligible(release, channel)
                  and release['reason'] in VERSION_KNOWN_REASONS
                  and release['deb'] is not None
                  and release['deb'].get('version')
                  and (release['installable'] if installable_only else True)]
    if not candidates:
        return None

    def compare(first, second):
        return runtime_version.compare_versions(first['deb']['version'], second['deb']['version'])

    best = max(candidates, key=functools.cmp_to_key(compare))
    return {'tag': best['tag'], 'version': best['deb']['version']}


def is_newer(version, current):
    """Debian comparison, False for unknown values and unparseable versions."""
    if not version or not current or version == 'unknown' or current == 'unknown':
        return False
    try:
        return runtime_version.compare_versions(version, current) > 0
    except ValueError:
        return False
