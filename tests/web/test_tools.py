"""Diagnostic runner: bounds, a single process and no lock held by its own status."""
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from modules.web.tools import DiagnosticRunner, ToolsError, host_target, integer


class ToolsTests(unittest.TestCase):
    def test_targets_and_integers_are_validated(self):
        self.assertEqual(host_target(' 1.1.1.1 '), '1.1.1.1')
        self.assertEqual(host_target('example.org'), 'example.org')
        for value in ('', 'two words', 'a' * 300, 'host..name'):
            with self.subTest(value=value):
                with self.assertRaises(ToolsError):
                    host_target(value)
        self.assertEqual(integer('4', (1, 60), 'count'), 4)
        for value in ('0', '61', 'x', None):
            with self.subTest(value=value):
                with self.assertRaises(ToolsError):
                    integer(value, (1, 60), 'count')

    def test_start_reports_status_without_deadlocking_on_its_own_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory)/'fake-ping'
            script.write_text('#!/bin/sh\necho "PING one"\nsleep 0.2\necho "PING two"\nexit 0\n')
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            runner = DiagnosticRunner()
            with patch('modules.web.tools.shutil.which', return_value=str(script)):
                state = runner.start('ping', target='127.0.0.1', count=2)
                self.assertTrue(state['running'])
                self.assertEqual(state['kind'], 'ping')
                self.assertEqual(runner.status()['detail']['target'], '127.0.0.1')
                stopped = runner.stop()
            self.assertFalse(stopped['running'])

    def test_only_one_process_runs_and_missing_tools_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory)/'fake-server'
            script.write_text('#!/bin/sh\nsleep 5\n')
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            runner = DiagnosticRunner()
            with patch('modules.web.tools.shutil.which', return_value=str(script)):
                runner.start('iperf', port=5201)
                with self.assertRaises(ToolsError):
                    runner.start('iperf', port=5202)
                runner.stop()
            with patch('modules.web.tools.shutil.which', return_value=None):
                with self.assertRaises(ToolsError):
                    runner.start('ping', target='127.0.0.1')

    IPERF_HELP = ('  -p, --port  -f, --format  -i, --interval  -V, --verbose  --forceflush  --timestamps  -d, --debug\n'
                  '  -s, --server  -1, --one-off  --server-bitrate-limit  -c, --client  --sctp  -u, --udp  --connect-timeout\n'
                  '  -b, --bitrate  --pacing-timer  --fq-rate  -t, --time  -n, --bytes  -k, --blockcount  -l, --length  --cport\n'
                  '  -P, --parallel  -R, --reverse  --bidir  -w, --window  -C, --congestion  -M, --set-mss  -N, --no-delay\n'
                  '  -4, --version4  -6, --version6  -S, --tos  -L, --flowlabel  -Z, --zerocopy  -O, --omit  --nstreams\n'
                  '  --get-server-output  --udp-counters-64bit  --repeating-payload\n')
    # Options of later releases (3.10 to 3.19) that the 3.9 text above lacks.
    IPERF_NEWER = ('  --bind-dev  --idle-timeout  --dont-fragment  --rcv-timeout  --snd-timeout  -m, --mptcp  --cntl-ka\n'
                   '  --skip-rx-copy  --gsro  --server-max-duration\n')

    def fake_tool(self, directory, name, help_text):
        """A stand-in that prints its arguments, and the given text for --help or -h."""
        script = Path(directory)/name
        script.write_text('#!/bin/sh\nif [ "$1" = "--help" ] || [ "$1" = "-h" ]; then cat <<\'EOF\'\n'
                          f'{help_text}EOF\nexit 0\nfi\nprintf \'%s\\n\' "$*"\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def run_tool(self, runner, kind, **options):
        state = runner.start(kind, **options)
        runner.reader.join(timeout=5)
        return state, ' '.join(runner.status()['lines'])

    def test_iperf_client_and_server_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            script = self.fake_tool(directory, 'fake-iperf3', self.IPERF_HELP + self.IPERF_NEWER)
            runner = DiagnosticRunner()
            with patch('modules.web.tools.shutil.which', return_value=str(script)), \
                    patch('modules.web.tools.congestion_algorithms', return_value=['reno', 'cubic', 'bbr']):
                state, line = self.run_tool(runner, 'iperf', mode='client', target='10.0.0.1', port=5201, direction='both',
                                            protocol='tcp', bitrate=50, burst=10, limit='bytes', bytes=200, parallel=4, omit=2,
                                            length=65536, window=512, mss=1400, nodelay=True, congestion='bbr', zerocopy=True,
                                            mptcp=True, tos=184, cport=6000, connect_timeout=3000, pacing_timer=500, fq_rate=80.5,
                                            server_output=True, repeating_payload=True, skip_rx_copy=True, family='4', interval=0.5,
                                            format='k', timestamps=True, verbose=True, debug=False, cntl_ka=True, rcv_timeout=5000,
                                            snd_timeout=2000, interface='wwan0')
                self.assertEqual(line, '--client 10.0.0.1 --port 5201 --version4 --interval 0.5 --timestamps --verbose --cntl-ka '
                                       '--rcv-timeout 5000 --snd-timeout 2000 --bind-dev wwan0 --bidir --bitrate 50M/10 --bytes 200M '
                                       '--parallel 4 --omit 2 --length 65536 --window 512K --set-mss 1400 --no-delay --congestion bbr '
                                       '--zerocopy --mptcp --tos 184 --cport 6000 --connect-timeout 3000 --pacing-timer 500 '
                                       '--fq-rate 80.5M --get-server-output --repeating-payload --skip-rx-copy --forceflush --format k')
                self.assertEqual(state['detail']['direction'], 'both')
                self.assertEqual(state['detail']['bytes'], 200)
                self.assertEqual(state['detail']['burst'], 10)
                _, line = self.run_tool(runner, 'iperf', mode='client', target='example.org')
                self.assertEqual(line, '--client example.org --port 5201 --time 10 --forceflush --format m',
                                 'TCP upload for 10 s through the default route by default')
                _, line = self.run_tool(runner, 'iperf', mode='client', target='example.org', duration=0)
                self.assertEqual(line, '--client example.org --port 5201 --time 0 --forceflush --format m', '0 runs until stopped')
                _, line = self.run_tool(runner, 'iperf', mode='client', target='10.0.0.1', protocol='udp', direction='download',
                                        bitrate=20, limit='blocks', blocks=1000, counters64=True, dont_fragment=True, gsro=True,
                                        length=1400, family='6', flowlabel=7)
                self.assertEqual(line, '--client 10.0.0.1 --port 5201 --version6 --udp --reverse --bitrate 20M --blockcount 1000 '
                                       '--length 1400 --flowlabel 7 --udp-counters-64bit --dont-fragment --gsro --forceflush --format m')
                _, line = self.run_tool(runner, 'iperf', mode='client', target='10.0.0.1', protocol='sctp', mss=1200, nstreams=4)
                self.assertEqual(line, '--client 10.0.0.1 --port 5201 --sctp --time 10 --set-mss 1200 --nstreams 4 --forceflush --format m')
                state, line = self.run_tool(runner, 'iperf', port=5202)
                self.assertEqual(line, '--server --port 5202 --one-off --forceflush --format m')
                self.assertEqual(state['detail'], {'mode': 'server', 'port': 5202, 'one_off': True})
                _, line = self.run_tool(runner, 'iperf', mode='server', port=5202, one_off=False, server_bitrate_limit=100,
                                        server_rate_interval=10, idle_timeout=60, server_max_duration=300, mptcp=True,
                                        rcv_timeout=1000, interface='wwan1', format='g')
                self.assertEqual(line, '--server --port 5202 --rcv-timeout 1000 --mptcp --bind-dev wwan1 --server-bitrate-limit 100M/10 '
                                       '--idle-timeout 60 --server-max-duration 300 --forceflush --format g')
                capabilities = runner.capabilities()
                self.assertTrue(capabilities['iperf3_bind_dev'])
                self.assertIn('--bidir', capabilities['iperf3_options'])
                self.assertEqual(capabilities['congestion'], ['reno', 'cubic', 'bbr'])

    def test_iperf_options_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DiagnosticRunner()
            with patch('modules.web.tools.shutil.which', return_value=str(self.fake_tool(directory, 'fake-iperf3', self.IPERF_HELP))), \
                    patch('modules.web.tools.congestion_algorithms', return_value=['reno', 'cubic']):
                client = {'mode': 'client', 'target': '1.1.1.1'}
                for options in ({'mode': 'both'}, {'mode': 'client'}, {**client, 'target': 'bad host'}, {**client, 'duration': 3601}, {**client, 'duration': -1},
                                {**client, 'parallel': 33}, {**client, 'direction': 'sideways'}, {**client, 'protocol': 'quic'},
                                {**client, 'bitrate': -1}, {**client, 'port': True}, {'mode': 'server', 'port': 80},
                                {**client, 'interface': 'eth0;reboot'}, {**client, 'limit': 'bytes'}, {**client, 'protocol': 'udp', 'length': 70000},
                                {**client, 'protocol': 'udp', 'mss': 1400}, {**client, 'congestion': 'bbr'}, {**client, 'congestion': 'cubic', 'protocol': 'udp'},
                                {**client, 'zerocopy': 'yes'}, {**client, 'counters64': True}, {**client, 'flowlabel': 3},
                                {**client, 'reverse': True}, {'mode': 'server', 'bitrate': 10}, {**client, 'format': 'M'},
                                {**client, 'burst': 10}, {**client, 'bitrate': 0, 'burst': 5}, {**client, 'bitrate': 10, 'burst': 1001},
                                {**client, 'nstreams': 2}, {**client, 'protocol': 'udp', 'mptcp': True}, {**client, 'gsro': True},
                                {'mode': 'server', 'server_rate_interval': 5}, {**client, 'rcv_timeout': 50}, {'mode': 'server', 'burst': 3},
                                {**client, 'interval': 0.05}, {**client, 'window': 524289},
                                {'mode': 'server', 'server_bitrate_limit': 10, 'server_rate_interval': 61}):
                    with self.subTest(options=options):
                        with self.assertRaises(ToolsError):
                            runner.start('iperf', **options)
                # An older iperf3 lacks these options, so the test refuses instead of silently dropping them.
                with self.assertRaisesRegex(ToolsError, '3.10'):
                    runner.start('iperf', **client, interface='wwan0')
                with self.assertRaisesRegex(ToolsError, '--idle-timeout'):
                    runner.start('iperf', mode='server', idle_timeout=30)
                with self.assertRaisesRegex(ToolsError, '--dont-fragment'):
                    runner.start('iperf', **client, protocol='udp', dont_fragment=True)
                with self.assertRaisesRegex(ToolsError, '--mptcp'):
                    runner.start('iperf', **client, mptcp=True)
                self.assertFalse(runner.capabilities()['iperf3_bind_dev'])
                self.assertIsNone(runner.process, 'nothing was started')

    def test_ping_options_become_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            letters = 'ABCDFILOQRSTUWceilmnpqstvw346'
            script = self.fake_tool(directory, 'fake-ping', ''.join(f'  -{letter}   option\n' for letter in letters)
                                    + '  -M <pmtud opt>  define path MTU discovery, can be one of <do|dont|want|probe>\n')
            runner = DiagnosticRunner()
            with patch('modules.web.tools.shutil.which', return_value=str(script)):
                state, line = self.run_tool(runner, 'ping', target='1.1.1.1', count=10, interval=0.5, size=1472, interface='wwan0',
                                            family='4', fragmentation='do', ttl=64, tclass=184, timeout=2, deadline=30, preload=3,
                                            pattern='ff00', sndbuf=65536, mark=7, identifier=77, timestamp_option='tsonly',
                                            timestamps=True, outstanding=True, quiet=False, verbose=True, user_latency=True,
                                            precise=True, adaptive=True, record_route=True, sticky_source=True, connect=True,
                                            no_loopback=True)
                self.assertEqual(line, '-n -c 10 -i 0.5 -s 1472 -I wwan0 -4 -M do -t 64 -Q 184 -W 2 -w 30 -l 3 -S 65536 -m 7 '
                                       '-e 77 -p ff00 -T tsonly -D -O -v -U -3 -A -R -B -C -L 1.1.1.1')
                self.assertEqual(state['detail']['size'], 1472)
                _, line = self.run_tool(runner, 'ping', target='2001:db8::1', family='6', flowlabel=255)
                self.assertEqual(line, '-n -c 4 -6 -F ff 2001:db8::1', 'the flow label is passed in hexadecimal')
                _, line = self.run_tool(runner, 'ping', target='1.1.1.1', size=1500, fragmentation='probe')
                self.assertEqual(line, '-n -c 4 -s 1500 -M probe 1.1.1.1')
                capabilities = runner.capabilities()
                self.assertEqual(capabilities['ping_options'], sorted('-' + letter for letter in letters + 'M'))
                self.assertTrue(capabilities['ping_pmtu_probe'])

    def test_ping_options_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DiagnosticRunner()
            with patch('modules.web.tools.shutil.which', return_value=str(self.fake_tool(directory, 'fake-ping', '  -c  -s  -I  -4  -6\n'))):
                ping = {'target': '1.1.1.1'}
                for options in ({**ping, 'count': 1001}, {**ping, 'interval': 0.1}, {**ping, 'size': 65508}, {**ping, 'ttl': 0},
                                {**ping, 'family': '5'}, {**ping, 'fragmentation': 'maybe'}, {**ping, 'pattern': 'xyz'},
                                {**ping, 'preload': 4}, {**ping, 'flowlabel': 1}, {**ping, 'family': '6', 'record_route': True},
                                {**ping, 'flood': True}, {**ping, 'quiet': 'yes'}):
                    with self.subTest(options=options):
                        with self.assertRaises(ToolsError):
                            runner.start('ping', **options)
                # This ping lists no -t: the option is refused rather than ignored.
                with self.assertRaisesRegex(ToolsError, '-t'):
                    runner.start('ping', **ping, ttl=5)
                with self.assertRaisesRegex(ToolsError, 'probe'):
                    runner.start('ping', **ping, fragmentation='probe')
                for options in ({**ping, 'identifier': 65536}, {**ping, 'connect': 1}):
                    with self.subTest(options=options):
                        with self.assertRaises(ToolsError):
                            runner.start('ping', **options)
                self.assertFalse(runner.capabilities()['ping_pmtu_probe'])
                self.assertIsNone(runner.process)

if __name__ == '__main__':
    unittest.main()
