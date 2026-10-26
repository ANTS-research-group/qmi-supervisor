#!/usr/bin/env python3
"""QMI supervisor entry point. CLI/configuration validation needs no modem or GI."""
import argparse
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading

from modules.device.device import scan_devices
from modules.runtime.settings import load_settings
from modules.runtime.fleet_config import Configuration


def main():
    parser = argparse.ArgumentParser(description="QMI USB modem supervisor")
    parser.add_argument("--config", "-c", help="INI configuration file")
    parser.add_argument("--device", "-d", help="QMI device path (default: auto discovery)")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration and exit without touching hardware")
    parser.add_argument("--list-devices", action="store_true", help="Show detected QMI modems and exit")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None,
                        help="Override system.log_level from the configuration")
    args = parser.parse_args()
    if args.list_devices:
        for device in scan_devices():
            print(json.dumps(asdict(device)))
        return 0
    path = args.config
    if path is None:
        path = next((str(p) for p in (Path("qmi-supervisor.ini"), Path("/etc/qmi-supervisor.ini")) if p.is_file()), None)
    try:
        settings = load_settings(path, args.device)
        configuration = Configuration.load(path)
        if args.device and configuration.profiles:
            raise ValueError('--device cannot override a configuration containing named modem profiles')
    except ValueError as error:
        parser.error(str(error))
    if args.check_config:
        public = asdict(settings)
        for key in ('password_hash',):
            public['web'][key] = '[hidden]' if public['web'][key] else ''
        if configuration.profiles:
            public['profiles'] = {key: {section: values for section, values in asdict(profile).items() if section != 'web'}
                                  for key, profile in configuration.profiles.items()}
        print(json.dumps(public, indent=2))
        return 0
    if os.geteuid() != 0:
        parser.error("Root permissions are required to manage the modem and host interface")
    log_level = args.log_level or settings.system.log_level
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    shutdown_requested = threading.Event()
    def shutdown(signum, frame):
        shutdown_requested.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGHUP, shutdown)
    from modules.runtime.fleet import FleetManager
    if path is None:
        from modules.deployment.provisioning import render_config, write_atomic
        path = "/run/qmi-supervisor/default.ini"
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(Path(path), render_config(settings))
    return FleetManager(path, shutdown_requested, device_override=args.device, log_level=log_level,
                        log_level_override=args.log_level is not None).run()


if __name__ == "__main__":
    sys.exit(main())
