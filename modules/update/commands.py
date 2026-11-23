"""Subprocess helpers of the updater: a fixed PATH, a fixed locale, no prompt.

`run_command()` never raises for a non-zero exit and never kills apt-get with
SIGKILL at the timeout; it sends SIGTERM, waits a minute, then kills.
"""
import os
import subprocess

SAFE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
APT_ENV = {'DEBIAN_FRONTEND': 'noninteractive', 'LC_ALL': 'C', 'APT_LISTCHANGES_FRONTEND': 'none',
           'NEEDRESTART_SUSPEND': '1'}   # the last one only silences needrestart where installed


def command_env(extra=None):
    env = dict(os.environ, PATH=SAFE_PATH, LC_ALL='C')
    env.update(extra or {})
    return env


def run_command(args, *, timeout, env=None, cwd=None):
    """Never raises for a non-zero exit. On timeout: SIGTERM, 60 s grace, SIGKILL."""
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, errors='replace', env=env, cwd=cwd)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def dpkg_status(runner):
    """`(status abbrev, version)` of qmi-supervisor, or None when it is not installed."""
    result = runner(['dpkg-query', '-W', '-f=${db:Status-Abbrev}|${Version}\\n', 'qmi-supervisor'],
                    timeout=30)
    if result.returncode != 0:
        return None
    line = (result.stdout or '').splitlines()
    if not line or '|' not in line[0]:
        return None
    abbrev, _, version = line[0].partition('|')
    return abbrev.strip(), version.strip()
