"""Read-only Quectel AT companion: SIM detection, active slot and insertion.

It never writes modem state: only the query commands AT+QSIMDET?, AT+QUIMSLOT?
and AT+QSIMSTAT? are sent, under an exclusive lock, within a short budget. A
busy port or a missing answer leaves the companion unavailable until the next
open of the modem.
"""
import errno
import fcntl
import os
from pathlib import Path
import select
import termios
import time

BUDGET_SECONDS = 2.0
QUERIES = ('AT+QSIMDET?', 'AT+QUIMSLOT?', 'AT+QSIMSTAT?')


def _candidates(device):
    usb_path = Path(getattr(device, 'usb_path', '')).resolve()
    ports = []
    for prefix in ('ttyUSB', 'ttyACM'):
        for entry in sorted(Path('/sys/class/tty').glob(prefix + '*')):
            try:
                resolved = (entry/'device').resolve()
            except OSError:
                continue
            if usb_path and str(resolved).startswith(str(usb_path)):
                ports.append('/dev/' + entry.name)
    return ports


def _open_port(path):
    descriptor = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    attributes = termios.tcgetattr(descriptor)
    attributes[0] = 0
    attributes[1] = 0
    attributes[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attributes[3] = 0
    attributes[4] = attributes[5] = termios.B115200
    termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
    termios.tcflush(descriptor, termios.TCIOFLUSH)
    return descriptor


def _command(descriptor, command, deadline):
    os.write(descriptor, (command + '\r').encode())
    buffer = b''
    while time.monotonic() < deadline:
        ready, _, _ = select.select([descriptor], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        try:
            chunk = os.read(descriptor, 1024)
        except OSError as error:
            if error.errno == errno.EAGAIN:
                time.sleep(0.02)
                continue
            raise
        buffer += chunk
        if b'OK' in buffer or b'ERROR' in buffer:
            break
    return buffer.decode('ascii', 'replace')


def _numbers(line):
    body = line.split(':', 1)[1] if ':' in line else ''
    values = []
    for part in body.replace(',', ' ').split():
        try:
            values.append(int(part))
        except ValueError:
            return []
    return values


class QuectelAtCompanion:
    name = 'quectel_at'

    def __init__(self):
        self.descriptor = None
        self.port = None

    @staticmethod
    def applies_to(device):
        text = ' '.join(str(getattr(device, field, '') or '')
                        for field in ('product', 'manufacturer'))
        return 'quectel' in text.lower() or 'rm520' in text.lower()

    def open(self, device):
        for path in _candidates(device):
            try:
                descriptor = _open_port(path)
            except OSError:
                continue
            deadline = time.monotonic() + 0.5
            try:
                if 'OK' in _command(descriptor, 'AT', deadline):
                    self.descriptor, self.port = descriptor, path
                    return
            except OSError:
                pass
            os.close(descriptor)
        raise RuntimeError('No free Quectel AT port found')

    def observe(self, reason):
        if self.descriptor is None:
            return {}
        deadline = time.monotonic() + BUDGET_SECONDS
        answers = {}
        for query in QUERIES:
            if time.monotonic() >= deadline:
                break
            try:
                answers[query] = _command(self.descriptor, query, deadline)
            except OSError:
                self.close()
                return {}
        result = {'port': self.port, 'reason': reason}
        for line in answers.get('AT+QSIMDET?', '').splitlines():
            if line.startswith('+QSIMDET:'):
                values = _numbers(line)
                if values:
                    result['config'] = {'enabled': bool(values[0]),
                                        'level': values[1] if len(values) > 1 else None}
        for line in answers.get('AT+QUIMSLOT?', '').splitlines():
            if line.startswith('+QUIMSLOT:'):
                values = _numbers(line)
                if values:
                    result['slot'] = values[0]
        for line in answers.get('AT+QSIMSTAT?', '').splitlines():
            if line.startswith('+QSIMSTAT:'):
                values = _numbers(line)
                if values:
                    result['inserted'] = bool(values[1]) if len(values) > 1 else None
        return result if any(key in result for key in ('config', 'slot', 'inserted')) else {}

    def close(self):
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = None
        self.port = None
