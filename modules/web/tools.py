"""Bounded network tests for the web UI: one child process, validated input.

Ping and iperf3 are offered. iperf3 runs either as a server, so another device
measures against this one, or as a client against a given server. Every option
is validated and bounded, and only options the installed tool lists in its help
are passed. Nothing is executed through a shell. Options that write files, read
credentials, detach a daemon or flood the network are not offered. Output is
kept in a small ring buffer that the page polls.
"""
from collections import deque
import ipaddress
import math
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

MAX_LINES = 400
# Any kernel interface name (wwan0, wwx<mac>, wwp0s20u1, ...): at most 15 characters, never an option.
INTERFACE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,14}$')
HOSTNAME = re.compile(r'^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')
CONGESTION = re.compile(r'^[a-z0-9_]{1,16}$')
CONGESTION_LIST = Path('/proc/sys/net/ipv4/tcp_available_congestion_control')

# Ping: every value is optional except the target; the bounds keep a test short of flooding.
PING_FIELDS = {'target', 'count', 'interface', 'interval', 'size', 'family', 'fragmentation', 'ttl', 'tclass', 'timeout',
               'deadline', 'preload', 'pattern', 'sndbuf', 'mark', 'identifier', 'timestamp_option', 'flowlabel', 'timestamps',
               'outstanding', 'quiet', 'verbose', 'user_latency', 'precise', 'adaptive', 'record_route', 'sticky_source',
               'connect', 'no_loopback'}
PING_SWITCHES = {'timestamps': '-D', 'outstanding': '-O', 'quiet': '-q', 'verbose': '-v', 'user_latency': '-U', 'precise': '-3',
                 'adaptive': '-A', 'record_route': '-R', 'sticky_source': '-B', 'connect': '-C', 'no_loopback': '-L'}
IPERF_COMMON = {'mode', 'port', 'interface', 'family', 'interval', 'format', 'timestamps', 'verbose', 'debug', 'rcv_timeout',
                'snd_timeout', 'mptcp', 'cntl_ka'}
IPERF_CLIENT = IPERF_COMMON | {'target', 'direction', 'protocol', 'bitrate', 'burst', 'limit', 'duration', 'bytes', 'blocks',
                               'parallel', 'omit', 'length', 'window', 'mss', 'nodelay', 'congestion', 'zerocopy', 'nstreams',
                               'tos', 'flowlabel', 'cport', 'connect_timeout', 'pacing_timer', 'fq_rate', 'server_output',
                               'repeating_payload', 'skip_rx_copy', 'counters64', 'dont_fragment', 'gsro'}
IPERF_SERVER = IPERF_COMMON | {'one_off', 'server_bitrate_limit', 'server_rate_interval', 'idle_timeout', 'server_max_duration'}


class ToolsError(ValueError):
    """Invalid request or unavailable dependency."""


def host_target(value):
    text = str(value or '').strip()
    if not text or len(text) > 253:
        raise ToolsError('A ping target is required')
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    if not HOSTNAME.fullmatch(text):
        raise ToolsError('Use a numeric address or a hostname')
    return text


def integer(value, bounds, label):
    if isinstance(value, bool):
        raise ToolsError(f'{label} must be an integer')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolsError(f'{label} must be an integer') from None
    if isinstance(value, float) and value != number:
        raise ToolsError(f'{label} must be an integer')
    if not bounds[0] <= number <= bounds[1]:
        raise ToolsError(f'{label} must be between {bounds[0]} and {bounds[1]}')
    return number


def decimal(value, bounds, label):
    if isinstance(value, bool):
        raise ToolsError(f'{label} must be a number')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolsError(f'{label} must be a number') from None
    if not math.isfinite(number) or not bounds[0] <= number <= bounds[1]:
        raise ToolsError(f'{label} must be between {bounds[0]} and {bounds[1]}')
    return round(number, 3)


def flag(value, label):
    if not isinstance(value, bool):
        raise ToolsError(f'{label} must be true or false')
    return value


def choice(value, options, label):
    if value not in options:
        raise ToolsError(f'Unknown {label}')
    return value


def interface_name(value):
    if not INTERFACE.fullmatch(str(value)):
        raise ToolsError('Unknown interface')
    return str(value)


def text_number(value):
    """A command-line number: 0.5 stays 0.5, 2.0 becomes 2."""
    return f'{value:g}'


def congestion_algorithms():
    try:
        return CONGESTION_LIST.read_text().split()
    except OSError:
        return []


class DiagnosticRunner:
    """One diagnostic process at a time; the web child owns it and stops it on exit."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        # Reentrant: start() reports its own status while holding the lock.
        self.lock = threading.RLock()
        self.process = None
        self.kind = None
        self.detail = {}
        self.started = None
        self.finished = None
        self.returncode = None
        self.lines = deque(maxlen=MAX_LINES)
        self.reader = None
        self.help = {}

    def _guard_binary(self, name):
        path = shutil.which(name)
        if not path:
            raise ToolsError(f'{name} is not installed on this host')
        return path

    def _help(self, path):
        """The tool's own option list, read once: releases differ in what they offer."""
        if path not in self.help:
            try:
                result = subprocess.run([path, '--help'] if 'iperf' in Path(path).name else [path, '-h'],
                                        capture_output=True, text=True, timeout=3, check=False)
                self.help[path] = (result.stdout or '') + (result.stderr or '')
            except (OSError, subprocess.SubprocessError):
                self.help[path] = ''
        return self.help[path]

    def _flags(self, path):
        text = self._help(path)
        found = set(re.findall(r'(?<![\w-])(--?[A-Za-z0-9][A-Za-z0-9-]*)', text))
        # Older iputils print "Usage: ping [-aAbBdDfhLnOqrRUvV64] [-c count] ...".
        for cluster in re.findall(r'\[-([A-Za-z0-9]+)\]', text):
            found.update('-' + letter for letter in cluster)
        return found

    def _require(self, path, option):
        flags = self._flags(path)
        # A help text that cannot be read does not block the test: the tool reports its own error.
        if flags and option not in flags:
            raise ToolsError(f'The installed {Path(path).name} does not support {option}')

    def capabilities(self):
        """What this host can run, so the page explains a missing tool or option before Start."""
        ping, iperf = shutil.which('ping'), shutil.which('iperf3')
        ping_options = sorted(option for option in self._flags(ping) if re.fullmatch(r'-[A-Za-z0-9]', option)) if ping else []
        iperf_options = sorted(option for option in self._flags(iperf) if option.startswith('--')) if iperf else []
        # Only newer iputils take "-M probe", and only their help says so.
        probe = bool(ping) and (not ping_options or 'probe' in self._help(ping))
        return {'ping': bool(ping), 'iperf3': bool(iperf), 'iperf3_bind_dev': '--bind-dev' in iperf_options,
                'ping_options': ping_options, 'ping_pmtu_probe': probe, 'iperf3_options': iperf_options,
                'congestion': congestion_algorithms()}

    def status(self):
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            if self.process and not running:
                self.returncode = self.process.returncode
            return {
                'running': running, 'kind': self.kind, 'detail': dict(self.detail),
                'started': self.started, 'finished': self.finished,
                'returncode': self.returncode, 'lines': list(self.lines),
            }

    def _ping(self, options):
        unknown = sorted(set(options) - PING_FIELDS)
        if unknown:
            raise ToolsError(f'Unknown option: {unknown[0]}')
        binary = self._guard_binary('ping')
        values = {key: value for key, value in options.items() if value is not None}
        target = host_target(values.get('target'))
        detail, args = {'target': target}, []

        def add(name, value, option, *arguments):
            self._require(binary, option)
            detail[name] = value
            args.extend([option, *arguments])

        count = integer(values.get('count', 4), (1, 1000), 'count')
        add('count', count, '-c', str(count))
        if 'interval' in values:
            interval = decimal(values['interval'], (0.2, 60), 'interval')
            add('interval', interval, '-i', text_number(interval))
        if 'size' in values:
            size = integer(values['size'], (0, 65507), 'size')
            add('size', size, '-s', str(size))
        if values.get('interface'):
            device = interface_name(values['interface'])
            add('interface', device, '-I', device)
        family = choice(values.get('family', 'auto'), ('auto', '4', '6'), 'IP version')
        if family != 'auto':
            add('family', family, '-' + family)
        if 'fragmentation' in values:
            mode = choice(values['fragmentation'], ('do', 'want', 'dont', 'probe'), 'fragmentation mode')
            if mode == 'probe' and self._flags(binary) and 'probe' not in self._help(binary):
                raise ToolsError('The installed ping does not support -M probe')
            add('fragmentation', mode, '-M', mode)
        for name, option, bounds in (('ttl', '-t', (1, 255)), ('tclass', '-Q', (0, 255)), ('timeout', '-W', (1, 60)),
                                     ('deadline', '-w', (1, 3600)), ('preload', '-l', (1, 3)), ('sndbuf', '-S', (1, 1048576)),
                                     ('mark', '-m', (0, 4294967295)), ('identifier', '-e', (0, 65535))):
            if name in values:
                value = integer(values[name], bounds, name)
                add(name, value, option, str(value))
        if 'pattern' in values:
            pattern = str(values['pattern'])
            if not re.fullmatch(r'[0-9A-Fa-f]{1,32}', pattern):
                raise ToolsError('Use up to 32 hexadecimal digits')
            add('pattern', pattern.lower(), '-p', pattern.lower())
        if 'timestamp_option' in values:
            stamp = choice(values['timestamp_option'], ('tsonly', 'tsandaddr'), 'timestamp option')
            add('timestamp_option', stamp, '-T', stamp)
        if 'flowlabel' in values:
            if family != '6':
                raise ToolsError('The flow label needs IPv6')
            label = integer(values['flowlabel'], (0, 1048575), 'flow label')
            # iputils reads the flow label in hexadecimal.
            add('flowlabel', label, '-F', format(label, 'x'))
        for name, option in PING_SWITCHES.items():
            if name in values and flag(values[name], name):
                add(name, True, option)
        if family == '6' and (detail.get('record_route') or 'timestamp_option' in detail):
            raise ToolsError('Record route and IP timestamps need IPv4')
        return [binary, '-n', *args, target], detail

    def _iperf(self, options):
        mode = choice(options.get('mode', 'server'), ('server', 'client'), 'iperf3 mode')
        fields = IPERF_CLIENT if mode == 'client' else IPERF_SERVER
        unknown = sorted(set(options) - fields)
        if unknown:
            raise ToolsError(f'Unknown option: {unknown[0]}')
        binary = self._guard_binary('iperf3')
        values = {key: value for key, value in options.items() if value is not None}
        detail, args = {'mode': mode}, []

        def add(name, value, option, *arguments):
            self._require(binary, option)
            detail[name] = value
            args.extend([option, *arguments])

        units = choice(values.get('format', 'm'), ('k', 'm', 'g', 't'), 'unit')
        # Each interval is flushed as it ends, and bit units keep the page's summary readable.
        common = ['--forceflush', '--format', units]
        if units != 'm':
            detail['format'] = units
        family = choice(values.get('family', 'auto'), ('auto', '4', '6'), 'IP version')
        if family != 'auto':
            add('family', family, '--version' + family)
        if 'interval' in values:
            interval = decimal(values['interval'], (0, 60), 'interval')
            # iperf3 takes 0 (no reports) or 0.1 to 60 seconds.
            if 0 < interval < 0.1:
                raise ToolsError('interval must be 0 or between 0.1 and 60')
            add('interval', interval, '--interval', text_number(interval))
        for name, option in (('timestamps', '--timestamps'), ('verbose', '--verbose'), ('debug', '--debug'),
                             ('cntl_ka', '--cntl-ka')):
            if name in values and flag(values[name], name):
                add(name, True, option)
        for name, option, bounds in (('rcv_timeout', '--rcv-timeout', (100, 3600000)), ('snd_timeout', '--snd-timeout', (1, 3600000))):
            if name in values:
                value = integer(values[name], bounds, name)
                add(name, value, option, str(value))
        mptcp = 'mptcp' in values and flag(values['mptcp'], 'mptcp')
        if mptcp and mode == 'server':
            add('mptcp', True, '--mptcp')
        if values.get('interface'):
            device = interface_name(values['interface'])
            # Without SO_BINDTODEVICE the test would silently follow the default route.
            if '--bind-dev' not in self._flags(binary):
                raise ToolsError('This iperf3 cannot send through a chosen interface; it needs version 3.10 or later')
            add('interface', device, '--bind-dev', device)

        if mode == 'server':
            port = integer(values.get('port', 5201), (1024, 65535), 'port')
            detail['port'] = port
            one_off = flag(values.get('one_off', True), 'one_off')
            detail['one_off'] = one_off
            if one_off:
                args.append('--one-off')
            if 'server_rate_interval' in values and 'server_bitrate_limit' not in values:
                raise ToolsError('The averaging time needs a server rate limit')
            if 'server_bitrate_limit' in values:
                limit = decimal(values['server_bitrate_limit'], (0.001, 100000), 'server bitrate limit')
                argument = text_number(limit) + 'M'
                if 'server_rate_interval' in values:
                    seconds = integer(values['server_rate_interval'], (1, 60), 'averaging time')
                    detail['server_rate_interval'] = seconds
                    argument += f'/{seconds}'
                add('server_bitrate_limit', limit, '--server-bitrate-limit', argument)
            for name, option, bounds in (('idle_timeout', '--idle-timeout', (1, 86400)), ('server_max_duration', '--server-max-duration', (1, 86400))):
                if name in values:
                    value = integer(values[name], bounds, name)
                    add(name, value, option, str(value))
            return [binary, '--server', '--port', str(port), *args, *common], detail

        host = host_target(values.get('target'))
        port = integer(values.get('port', 5201), (1, 65535), 'port')
        detail.update(target=host, port=port)
        protocol = choice(values.get('protocol', 'tcp'), ('tcp', 'udp', 'sctp'), 'protocol')
        detail['protocol'] = protocol
        if protocol != 'tcp':
            add('protocol', protocol, '--' + protocol)
        direction = choice(values.get('direction', 'upload'), ('upload', 'download', 'both'), 'direction')
        detail['direction'] = direction
        if direction != 'upload':
            add('direction', direction, '--reverse' if direction == 'download' else '--bidir')
        if 'burst' in values and not values.get('bitrate'):
            raise ToolsError('Bursts need a target rate')
        if 'bitrate' in values:
            rate = decimal(values['bitrate'], (0, 100000), 'bitrate')
            argument = text_number(rate) + 'M'
            if 'burst' in values:
                burst = integer(values['burst'], (1, 1000), 'burst')
                detail['burst'] = burst
                argument += f'/{burst}'
            add('bitrate', rate, '--bitrate', argument)
        limit = choice(values.get('limit', 'time'), ('time', 'bytes', 'blocks'), 'test limit')
        detail['limit'] = limit
        if limit == 'time':
            seconds = integer(values.get('duration', 10), (1, 3600), 'duration')
            add('duration', seconds, '--time', str(seconds))
        elif limit == 'bytes':
            megabytes = integer(values.get('bytes'), (1, 1000000), 'data volume')
            add('bytes', megabytes, '--bytes', f'{megabytes}M')
        else:
            blocks = integer(values.get('blocks'), (1, 100000000), 'block count')
            add('blocks', blocks, '--blockcount', str(blocks))
        streams = integer(values.get('parallel', 1), (1, 32), 'parallel')
        detail['parallel'] = streams
        if streams != 1:
            add('parallel', streams, '--parallel', str(streams))
        if 'omit' in values:
            omit = integer(values['omit'], (0, 60), 'omit')
            add('omit', omit, '--omit', str(omit))
        if 'length' in values:
            length = integer(values['length'], (16, 65507 if protocol == 'udp' else 1048576), 'length')
            add('length', length, '--length', str(length))
        if 'window' in values:
            window = integer(values['window'], (1, 524288), 'window')
            add('window', window, '--window', f'{window}K')
        if 'mss' in values:
            if protocol == 'udp':
                raise ToolsError('The MSS applies to TCP and SCTP')
            mss = integer(values['mss'], (88, 9216), 'MSS')
            add('mss', mss, '--set-mss', str(mss))
        if values.get('nodelay') is not None and flag(values['nodelay'], 'nodelay'):
            if protocol == 'udp':
                raise ToolsError('No delay applies to TCP and SCTP')
            add('nodelay', True, '--no-delay')
        if 'congestion' in values:
            algorithm = str(values['congestion'])
            available = congestion_algorithms()
            if protocol != 'tcp' or not CONGESTION.fullmatch(algorithm) or (available and algorithm not in available):
                raise ToolsError('Choose a congestion control algorithm this host offers, with TCP')
            add('congestion', algorithm, '--congestion', algorithm)
        if values.get('zerocopy') is not None and flag(values['zerocopy'], 'zerocopy'):
            if protocol != 'tcp':
                raise ToolsError('Zero copy applies to TCP')
            add('zerocopy', True, '--zerocopy')
        if mptcp:
            if protocol != 'tcp':
                raise ToolsError('MPTCP applies to TCP')
            add('mptcp', True, '--mptcp')
        if 'nstreams' in values:
            if protocol != 'sctp':
                raise ToolsError('SCTP streams apply to SCTP')
            streams = integer(values['nstreams'], (1, 1024), 'SCTP streams')
            add('nstreams', streams, '--nstreams', str(streams))
        if 'tos' in values:
            tos = integer(values['tos'], (0, 255), 'TOS')
            add('tos', tos, '--tos', str(tos))
        if 'flowlabel' in values:
            if family != '6':
                raise ToolsError('The flow label needs IPv6')
            label = integer(values['flowlabel'], (0, 1048575), 'flow label')
            add('flowlabel', label, '--flowlabel', str(label))
        for name, option, bounds in (('cport', '--cport', (1, 65535)), ('connect_timeout', '--connect-timeout', (1, 60000)),
                                     ('pacing_timer', '--pacing-timer', (1, 1000000))):
            if name in values:
                value = integer(values[name], bounds, name)
                add(name, value, option, str(value))
        if 'fq_rate' in values:
            rate = decimal(values['fq_rate'], (0.001, 100000), 'fair-queuing rate')
            add('fq_rate', rate, '--fq-rate', text_number(rate) + 'M')
        for name, option, udp_only in (('server_output', '--get-server-output', False), ('repeating_payload', '--repeating-payload', False),
                                       ('skip_rx_copy', '--skip-rx-copy', False), ('counters64', '--udp-counters-64bit', True),
                                       ('dont_fragment', '--dont-fragment', True), ('gsro', '--gsro', True)):
            if name in values and flag(values[name], name):
                if udp_only and protocol != 'udp':
                    raise ToolsError(f'{option} applies to UDP')
                add(name, True, option)
        return [binary, '--client', host, '--port', str(port), *args, *common], detail

    def start(self, kind, **options):
        with self.lock:
            if self.process and self.process.poll() is None:
                raise ToolsError('A diagnostic is already running')
            if kind == 'ping':
                command, detail = self._ping(options)
            elif kind == 'iperf':
                command, detail = self._iperf(options)
            else:
                raise ToolsError('Unknown diagnostic')
            try:
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                text=True, bufsize=1, start_new_session=True)
            except OSError as error:
                raise ToolsError(f'Could not start {kind}: {error}') from error
            self.kind, self.detail, self.started, self.finished, self.returncode = kind, detail, self.clock(), None, None
            self.lines.clear()
            self.reader = threading.Thread(target=self._drain, args=(self.process,), daemon=True)
            self.reader.start()
            return self.status()

    def _drain(self, process):
        with process.stdout:
            for line in process.stdout:
                with self.lock:
                    self.lines.append(line.rstrip('\n')[:1000])
        code = process.wait()
        with self.lock:
            self.returncode = code
            self.finished = self.clock()

    def stop(self):
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            with self.lock:
                # The reader thread records the end as well; a test that had already ended keeps its own time.
                self.returncode = process.returncode
                if self.finished is None:
                    self.finished = self.clock()
        return self.status()
