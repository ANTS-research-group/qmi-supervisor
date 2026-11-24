"""HTTP(S) client of the updater: stdlib only, never imports GI.

It is the single place that opens a network connection. The updater reuses it for
package and checksum downloads. Only a `https://` URL is opened; `http://` is
accepted for a 127.0.0.1 or [::1] test server only when
QMI_SUPERVISOR_UPDATE_TEST_SOURCE=1 is set, which no installed file ever does.

The Authorization header is added with `add_unredirected_header`: GitHub
redirects asset downloads to another host and the token must never follow.
"""
from collections import namedtuple
import http.client
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from modules.runtime import version as runtime_version
from modules.runtime.settings import check_update_url

TEST_SOURCE_ENV = 'QMI_SUPERVISOR_UPDATE_TEST_SOURCE'
GITHUB_API_VERSION = '2022-11-28'
DETAIL_MAX_BYTES = 256
MAX_REDIRECTS = 5

# English message IDs printed by the CLI and translated by the web interface.
ERROR_TEXT = {
    'network': 'The release server could not be reached',
    'tls': 'The secure connection to the release server failed (certificates or clock)',
    'unauthorized': 'The access token was rejected',
    'forbidden': 'The release server refused the request',
    'rate_limited': 'Too many requests to the release server; the check is retried later',
    'not_found': 'The release list was not found; check update.source or set a token for a private repository',
    'http_error': 'The release server answered with an error (HTTP {status})',
    'too_large': 'The release list is larger than 2 MiB',
    'bad_response': 'The release list could not be read',
    'bad_source': 'update.source is not an allowed address',
    'bad_redirect': 'The release server redirected to an address that is not allowed',
    'bad_token_file': 'The token file is not private or not valid; set it again with qmi-supervisor-update set-token',
}

HttpResponse = namedtuple('HttpResponse', 'status headers body')


class UpdateCheckError(Exception):
    """A bounded, machine-codable check failure (never carries a token)."""

    def __init__(self, code, detail, *, status=None):
        self.code = code
        self.detail = bounded(detail)
        self.status = status
        super().__init__(f'{code}: {self.detail}')


def bounded(text, limit=DETAIL_MAX_BYTES):
    """Strip control characters and cut the UTF-8 text at `limit` bytes without breaking it."""
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', str(text))
    return cleaned.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def test_sources_enabled():
    """True only for the exact value '1', so a stray 'yes' never enables http."""
    return os.environ.get(TEST_SOURCE_ENV) == '1'


def test_mode():
    """The updater name for the same switch: loopback HTTP and the test keyring."""
    return test_sources_enabled()


def check_url(url, *, code='bad_source'):
    """Validate a URL the updater may open, mapping a refusal to `code`."""
    try:
        return check_update_url(url, loopback_http=test_sources_enabled())
    except ValueError as error:
        raise UpdateCheckError(code, 'URL ' + str(error)) from None


def user_agent():
    """The only identification sent: qmi-supervisor/<version> with a safe version spelling."""
    value = runtime_version.installed_version() or 'unknown'
    return 'qmi-supervisor/' + re.sub(r'[^A-Za-z0-9.+~:-]', '_', value)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, code='bad_redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _host(url):
    return urllib.parse.urlsplit(url).hostname or url


def _error_code(status, headers):
    remaining = headers.get('X-RateLimit-Remaining')
    if status == 401:
        return 'unauthorized'
    if status == 429 or (status == 403 and (remaining == '0' or headers.get('Retry-After') is not None)):
        return 'rate_limited'
    if status == 403:
        return 'forbidden'
    if status == 404:
        return 'not_found'
    return 'http_error'


def open_url(url, *, accept=None, timeout=10.0, token=None):
    """Stream `url` (package downloads): the response exposes `.read(n)`, `.status`,
    `.headers` and is a context manager. Same URL rules and redirect refusal as
    `http_get`; the token reaches only the configured host, never a redirect target.
    The caller closes the response (it is returned inside its `with` block).
    """
    check_url(url)
    headers = {'User-Agent': user_agent()}
    if accept:
        headers['Accept'] = accept
    request = urllib.request.Request(url, headers=headers, method='GET')
    if token:
        request.add_unredirected_header('Authorization', 'Bearer ' + token)
    opener = urllib.request.build_opener(
        _RedirectHandler(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(request, timeout=timeout)


def http_get(url, *, headers=None, token=None, timeout=10.0, limit):
    """GET `url`, bounded by `limit` bytes, mapping every failure to UpdateCheckError.

    A 304 is returned as `HttpResponse(304, headers, b'')`; the caller decides
    whether asking without a conditional header was a protocol error.
    """
    check_url(url)
    host = _host(url)
    request = urllib.request.Request(
        url, headers={'User-Agent': user_agent(), **(headers or {})}, method='GET')
    if token:
        # Unredirected: the token reaches the configured URL only, never a redirect target.
        request.add_unredirected_header('Authorization', 'Bearer ' + token)
    opener = urllib.request.build_opener(
        _RedirectHandler(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        response = opener.open(request, timeout=timeout)
        try:
            status = response.status
            response_headers = response.headers
            declared = response_headers.get('Content-Length')
            if declared is not None:
                try:
                    declared_bytes = int(declared)
                except ValueError:
                    declared_bytes = None
                if declared_bytes is not None and declared_bytes > limit:
                    raise UpdateCheckError('too_large', f'{host}: response is larger than {limit} bytes')
            body = response.read(limit + 1)
        finally:
            response.close()
    except urllib.error.HTTPError as error:
        try:
            if error.code == 304:
                return HttpResponse(304, error.headers, b'')
            code = _error_code(error.code, error.headers)
            # http.client accepts 100..999; the published contract only 100..599.
            status = error.code if 100 <= error.code <= 599 else None
            raise UpdateCheckError(code, f'HTTP {error.code} from {host}', status=status) from None
        finally:
            error.close()
    except UpdateCheckError:
        raise
    except urllib.error.URLError as error:
        reason = error.reason
        code = 'tls' if isinstance(reason, ssl.SSLError) else 'network'
        raise UpdateCheckError(code, f'{host}: {reason}') from None
    except TimeoutError as error:
        raise UpdateCheckError('network', f'{host}: {error}') from None
    except ssl.SSLError as error:
        raise UpdateCheckError('tls', f'{host}: {error}') from None
    except http.client.HTTPException as error:
        raise UpdateCheckError('network', f'{host}: {error}') from None
    except OSError as error:
        raise UpdateCheckError('network', f'{host}: {error}') from None
    except ValueError as error:
        raise UpdateCheckError('bad_source', bounded(str(error))) from None
    if len(body) > limit:
        raise UpdateCheckError('too_large', f'{host}: response is larger than {limit} bytes')
    return HttpResponse(status, response_headers, body)
