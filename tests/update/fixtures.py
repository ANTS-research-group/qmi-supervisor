"""GitHub-shaped release fixtures shared by the updater tests (not a test module)."""
import hashlib

GITHUB_V120 = {
    'tag_name': 'v1.2.0',
    'name': 'v1.2.0',
    'draft': False,
    'prerelease': False,
    'published_at': '2026-11-05T12:22:41Z',
    'body': '',
    'html_url': 'https://github.com/ANTS-research-group/qmi-supervisor/releases/tag/v1.2.0',
    'assets': [{
        'name': 'qmi-supervisor_1.2.0-1_all.deb',
        'label': '',
        'size': 624080,
        'state': 'uploaded',
        'content_type': 'application/x-debian-package',
        'browser_download_url': 'https://github.com/ANTS-research-group/qmi-supervisor/releases/download/v1.2.0/qmi-supervisor_1.2.0-1_all.deb',
        'url': 'https://api.github.com/repos/ANTS-research-group/qmi-supervisor/releases/assets/713322546',
        'digest': 'sha256:4c04b0bf88ea7c87ef80293ef7173f356250e475ff3b78b26c1bd1b4c9ba23c0',
    }],
}


def asset_item(name, *, size=1024, state='uploaded', content_type='application/octet-stream'):
    asset_id = int(hashlib.sha256(name.encode('utf-8')).hexdigest()[:8], 16)
    return {
        'name': name,
        'label': '',
        'size': size,
        'state': state,
        'content_type': content_type,
        'browser_download_url': f'https://github.com/ANTS-research-group/qmi-supervisor/releases/download/x/{name}',
        'url': f'https://api.github.com/repos/ANTS-research-group/qmi-supervisor/releases/assets/{asset_id}',
        'digest': 'sha256:' + hashlib.sha256(name.encode('utf-8')).hexdigest(),
    }


def release_item(tag, *, deb=None, sums=True, signature=True, prerelease=False, draft=False,
                 body='', published_at='2026-11-20T10:00:00Z', state='uploaded', size=1024):
    version = tag[1:] if tag.startswith('v') else tag
    if deb is None:
        deb_names = [f'qmi-supervisor_{version}-1_all.deb']
    elif deb is False:
        deb_names = []
    elif isinstance(deb, str):
        deb_names = [deb]
    else:
        deb_names = list(deb)
    assets = [asset_item(name, size=size, state=state) for name in deb_names]
    if sums:
        assets.append(asset_item(sums if isinstance(sums, str) else 'SHA256SUMS', size=80, state=state))
    if signature:
        assets.append(asset_item(signature if isinstance(signature, str) else 'SHA256SUMS.asc', size=100, state=state))
    return {
        'tag_name': tag,
        'name': tag,
        'draft': draft,
        'prerelease': prerelease,
        'published_at': published_at,
        'body': body,
        'html_url': f'https://github.com/ANTS-research-group/qmi-supervisor/releases/tag/{tag}',
        'assets': assets,
    }
