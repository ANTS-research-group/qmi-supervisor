#!/usr/bin/env python3
"""Post diagnosis events to a URL with an HMAC-SHA256 signature."""
from collections import deque
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request

from qmi_supervisor_sdk import PluginError, connect

QUEUE_SIZE = 100
RETRIES = 5


def send(url, secret, body, attempts=RETRIES):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'X-QMI-Signature': 'sha256=' + digest})
    delay = 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.HTTPError):
            if attempt + 1 == attempts:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return False


def main():
    bus = connect()
    config = bus.settings()
    url = config.get('url', '').strip()
    secret = config.get('secret', '')
    if not url.startswith(('https://', 'http://')):
        bus.log('webhook: settings.url must be an http(s) URL')
        return 2
    queue = deque(maxlen=QUEUE_SIZE)
    wakeup = threading.Event()
    stats = {'sent': 0, 'dropped': 0}

    def worker():
        last_report = 0.0
        while True:
            wakeup.wait(1)
            wakeup.clear()
            while queue:
                body = queue.popleft()
                if not send(url, secret, body):
                    stats['dropped'] += 1
            now = time.monotonic()
            if stats['dropped'] and now - last_report >= 60:
                bus.log(f"webhook: {stats['dropped']} events dropped after retries")
                last_report = now

    threading.Thread(target=worker, daemon=True).start()
    bus.subscribe(topics=('events',))
    for message in bus.messages():
        if message.get('op') == 'dropped':
            stats['dropped'] += int(message.get('count') or 0)
            continue
        if message.get('op') != 'event':
            continue
        queue.append(json.dumps(message['data'], ensure_ascii=False).encode())
        wakeup.set()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PluginError as error:
        raise SystemExit(f'webhook: {error.code}')
