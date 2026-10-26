#!/usr/bin/env python3
"""Optional UI process: no GI imports, QMI clients or sampling work."""
import argparse
import logging
import signal
import threading
from modules.runtime.settings import load_settings
from modules.web.server import serve


def main():
    parser = argparse.ArgumentParser(description='Optional QMI-Supervisor web UI')
    parser.add_argument('--config', '-c', required=True)
    parser.add_argument('--status', default='/run/qmi-supervisor/status.json')
    args = parser.parse_args()
    settings = load_settings(args.config)
    if not settings.web.enabled:
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *unused: stop.set())
    signal.signal(signal.SIGINT, lambda *unused: stop.set())
    logging.basicConfig(level=logging.INFO)
    logging.info('Web bind mode=%s, port=%s', settings.web.host, settings.web.port)
    try:
        serve(settings, args.config, args.status, stop)
    except (OSError, ValueError) as error:
        logging.error('Web interface unavailable: %s', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
