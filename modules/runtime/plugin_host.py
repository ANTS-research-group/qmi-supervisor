"""Launch one plugin: validate its manifest, drop privileges and exec the program.

This process is started by the plugin supervisor. It never interprets plugin
code: it only prepares limits, environment and credentials, then replaces
itself with the plugin program. The settings file is written by the supervisor
as root and is readable only by the plugin user.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import resource
import sys

from modules.runtime.plugins import load_manifest

RLIMIT_NOFILE = 256
RLIMIT_AS = 512 * 1024 * 1024
NICE = 10
SDK_PATH = '/opt/qmi-supervisor/sdk'


def _identity(user, group):
    import grp
    import pwd
    entry = pwd.getpwnam(user)
    gid = grp.getgrnam(group).gr_gid if group else entry.pw_gid
    return entry.pw_uid, gid


def prepare(user='qmi-supervisor-plugin', group='qmi-supervisor-ext'):
    """Apply limits and drop to the unprivileged plugin user (idempotent)."""
    resource.setrlimit(resource.RLIMIT_NOFILE, (RLIMIT_NOFILE, RLIMIT_NOFILE))
    resource.setrlimit(resource.RLIMIT_AS, (RLIMIT_AS, RLIMIT_AS))
    os.nice(NICE)
    if os.geteuid() != 0:
        return
    uid, gid = _identity(user, group)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def environment(name, bus, secret, settings_path):
    return {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'PYTHONPATH': SDK_PATH,
        'QMI_SUPERVISOR_BUS': str(bus),
        'QMI_SUPERVISOR_PLUGIN': name,
        'QMI_SUPERVISOR_PLUGIN_SECRET': secret,
        'QMI_SUPERVISOR_PLUGIN_SETTINGS': str(settings_path),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='QMI Supervisor plugin host')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--bus', required=True)
    parser.add_argument('--secret', required=True)
    parser.add_argument('--user', default='qmi-supervisor-plugin')
    parser.add_argument('--group', default='qmi-supervisor-ext')
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest, require_root=os.geteuid() == 0)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s plugin[' + manifest.name + '] %(levelname)s %(message)s')
    if not manifest.valid:
        logging.error('Invalid manifest: %s', manifest.error)
        return 2
    try:
        settings = json.loads(Path(args.settings).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        logging.error('The plugin settings file is not readable')
        return 2
    if not isinstance(settings, dict) or any(type(value) is not str for value in settings.values()):
        logging.error('The plugin settings must be a JSON object of strings')
        return 2
    try:
        prepare(args.user, args.group)
    except (KeyError, OSError) as error:
        logging.error('Cannot drop privileges: %s', error)
        return 2
    env = environment(manifest.name, args.bus, args.secret, args.settings)
    try:
        os.execve(manifest.exec, [manifest.exec, *manifest.args], env)
    except OSError as error:
        logging.error('Cannot start the plugin program: %s', error)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
