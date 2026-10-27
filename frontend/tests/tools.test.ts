import assert from 'node:assert/strict';
import test from 'node:test';
import { iperfDefaults, iperfErrors, iperfFromDetail, iperfPayload, iperfSummary, megabits, pingDefaults, pingErrors, pingFromDetail, pingPayload,
  pingSummary, reachableHosts } from '../lib/tools.ts';

test('ping is summarised while it runs and from its final statistics', () => {
  const running = ['PING 1.1.1.1 (1.1.1.1) from 10.64.12.2 wwan0: 56(84) bytes of data.',
    '64 bytes from 1.1.1.1: icmp_seq=1 ttl=56 time=31.2 ms', '64 bytes from 1.1.1.1: icmp_seq=2 ttl=56 time=29.8 ms'];
  assert.deepEqual(pingSummary(running), { sent: null, received: 2, loss: null, min: 29.8, avg: 30.5, max: 31.2, last: 29.8 });
  const done = [...running, '', '--- 1.1.1.1 ping statistics ---', '4 packets transmitted, 2 received, 50% packet loss, time 3004ms',
    'rtt min/avg/max/mdev = 29.812/30.501/31.204/0.512 ms'];
  assert.deepEqual(pingSummary(done), { sent: 4, received: 2, loss: 50, min: 29.812, avg: 30.501, max: 31.204, last: 29.8 });
  const busybox = ['4 packets transmitted, 4 packets received, 0% packet loss', 'round-trip min/avg/max = 1.1/2.2/3.3 ms'];
  assert.equal(pingSummary(busybox)?.avg, 2.2);
  assert.equal(pingSummary(['ping: connect: Network is unreachable']), null);
});

test('iperf3 is summarised from its interval, sender and receiver lines', () => {
  assert.equal(megabits('[  5]   0.00-1.00   sec   640 KBytes  5.24 Mbits/sec'), 5.24);
  assert.equal(megabits('[  5]   0.00-1.00   sec   1.1 GBytes  1.2 Gbits/sec'), 1200);
  assert.equal(megabits('[  5]   0.00-1.00   sec   10 KBytes  800 Kbits/sec'), 0.8);
  const tcp = ['Connecting to host 10.0.0.1, port 5201', '[ ID] Interval           Transfer     Bitrate         Retr  Cwnd',
    '[  5]   0.00-1.00   sec  5.12 MBytes  42.9 Mbits/sec    0    345 KBytes'];
  const none = { transmit: null, receive: null, currentTransmit: null, currentReceive: null };
  assert.deepEqual(iperfSummary(tcp), { current: 42.9, sender: null, receiver: null, jitter: null, loss: null, ...none });
  const parallel = [...tcp, '[  7]   0.00-10.00  sec  27.0 MBytes  22.7 Mbits/sec    0             sender',
    '[SUM]   0.00-10.00  sec  54.2 MBytes  45.5 Mbits/sec    0             sender',
    '[SUM]   0.00-10.04  sec  53.9 MBytes  45.0 Mbits/sec                  receiver', 'iperf Done.'];
  assert.deepEqual(iperfSummary(parallel), { current: 42.9, sender: 45.5, receiver: 45, jitter: null, loss: null, ...none });
  const udp = ['[  5]   0.00-10.00  sec  11.9 MBytes  10.0 Mbits/sec  0.000 ms  0/8633 (0%)  sender',
    '[  5]   0.00-10.04  sec  11.8 MBytes  9.86 Mbits/sec  0.050 ms  12/8633 (0.14%)  receiver'];
  assert.deepEqual(iperfSummary(udp), { current: null, sender: 10, receiver: 9.86, jitter: 0.05, loss: 0.14, ...none });
  assert.equal(iperfSummary(['iperf3: error - unable to connect to server: Connection refused']), null);
});

test('only addresses another device can reach are offered for the iperf3 server', () => {
  assert.deepEqual(reachableHosts(['http://127.0.0.1:8080/', 'http://10.205.52.160:8080/', 'http://[::1]:8080/', 'http://10.205.52.160:8080/x', 'nonsense']),
    ['10.205.52.160']);
});

test('bidirectional and timestamped iperf3 output keeps each direction apart', () => {
  const bidir = ['[ ID][Role] Interval           Transfer     Bitrate         Retr  Cwnd',
    '[  5][TX-C]   0.00-1.00   sec  3.75 MBytes  31.4 Mbits/sec   13   2.56 MBytes', '[  7][RX-C]   0.00-1.00   sec   385 KBytes  3.15 Mbits/sec',
    '[  5][TX-C]   0.00-2.00   sec  7.47 MBytes  31.3 Mbits/sec   13             sender', '[  5][TX-C]   0.00-2.04   sec  7.47 MBytes  30.7 Mbits/sec                  receiver',
    '[  7][RX-C]   0.00-2.00   sec   770 KBytes  3.15 Mbits/sec    1             sender', '[  7][RX-C]   0.00-2.04   sec   766 KBytes  3075 Kbits/sec                  receiver'];
  const result = iperfSummary(bidir);
  assert.equal(result?.transmit, 30.7, 'the receiving end measures what this device sent');
  assert.equal(result?.receive, 3.075);
  assert.equal(result?.currentTransmit, 31.4);
  assert.equal(result?.sender, null, 'tagged lines never mix into the one-way figures');
  const stamped = ['Tue Oct 27 02:12:54 2026 [  5]   0.00-2.00   sec  1.22 MBytes  5112 Kbits/sec  0.000 ms  0/39 (0%)  sender',
    'Tue Oct 27 02:12:54 2026 [  5]   0.00-2.04   sec  1.22 MBytes  1.2 Tbits/sec  0.015 ms  0/39 (0%)  receiver'];
  assert.deepEqual([iperfSummary(stamped)?.sender, iperfSummary(stamped)?.receiver], [5.112, 1200000]);
});

test('a test request carries only what differs from the defaults and fits the chosen protocol', () => {
  assert.deepEqual(pingPayload(pingDefaults, 'wwan0'), { kind: 'ping', target: '1.1.1.1', count: 4, interface: 'wwan0' });
  const ping = { ...pingDefaults, viaModem: false, size: 1472, fragmentation: 'do' as const, ttl: 64, flowlabel: 5, record_route: true, quiet: true,
    identifier: 9, precise: true };
  assert.deepEqual(pingPayload(ping, 'wwan0'), { kind: 'ping', target: '1.1.1.1', count: 4, interface: null, size: 1472, fragmentation: 'do',
    ttl: 64, record_route: true, quiet: true, identifier: 9, precise: true }, 'the IPv6 flow label stays out of an IPv4 test');
  assert.deepEqual(pingPayload({ ...ping, family: '6' }, ''), { kind: 'ping', target: '1.1.1.1', count: 4, interface: null, size: 1472,
    family: '6', fragmentation: 'do', ttl: 64, flowlabel: 5, quiet: true, identifier: 9, precise: true }, 'IPv4-only options stay out of an IPv6 test');
  const client = { ...iperfDefaults, host: ' 10.0.0.9 ' };
  assert.deepEqual(iperfPayload(client, 'wwan0'), { kind: 'iperf', mode: 'client', target: '10.0.0.9', port: 5201, interface: 'wwan0', duration: 10 });
  const udp = { ...client, protocol: 'udp' as const, direction: 'both' as const, bitrate: 20, burst: 5, limit: 'bytes' as const, bytes: 50, mss: 1400,
    nodelay: true, mptcp: true, nstreams: 3, counters64: true, gsro: true, parallel: 2, format: 'k' as const };
  assert.deepEqual(iperfPayload(udp, ''), { kind: 'iperf', mode: 'client', target: '10.0.0.9', port: 5201, interface: null, bitrate: 20, burst: 5,
    format: 'k', direction: 'both', protocol: 'udp', limit: 'bytes', bytes: 50, parallel: 2, counters64: true, gsro: true }, 'TCP and SCTP options stay out of UDP');
  assert.deepEqual(iperfPayload({ ...client, burst: 5, bitrate: 0 }, 'wwan0'), { ...iperfPayload(client, 'wwan0'), bitrate: 0 }, 'bursts need a target rate');
  assert.deepEqual(iperfPayload({ ...client, protocol: 'sctp', nstreams: 4, mptcp: true, gsro: true }, 'wwan0'),
    { ...iperfPayload(client, 'wwan0'), protocol: 'sctp', nstreams: 4 });
  assert.deepEqual(iperfPayload({ ...iperfDefaults, mode: 'server', one_off: false, listenModem: true, idle_timeout: 30, mss: 1400, server_rate_interval: 10,
    mptcp: true, rcv_timeout: 5000 }, 'wwan0'), { kind: 'iperf', mode: 'server', port: 5201, one_off: false, interface: 'wwan0', idle_timeout: 30,
    mptcp: true, rcv_timeout: 5000 }, 'the averaging time travels only with a rate limit');
  assert.deepEqual(iperfPayload({ ...iperfDefaults, mode: 'server', server_bitrate_limit: 50, server_rate_interval: 10 }, ''),
    { kind: 'iperf', mode: 'server', port: 5201, interface: null, server_bitrate_limit: 50, server_rate_interval: 10 });
});

test('forms are validated with the backend bounds, and blank optional numbers are allowed', () => {
  assert.deepEqual(pingErrors(pingDefaults), {});
  assert.deepEqual(Object.keys(pingErrors({ ...pingDefaults, target: 'bad host', interval: 0.1, size: 70000, pattern: 'xyz', preload: 4, identifier: 70000 })).sort(),
    ['identifier', 'interval', 'pattern', 'preload', 'size', 'target']);
  assert.deepEqual(Object.keys(iperfErrors(iperfDefaults)), ['host'], 'a client needs its server');
  assert.deepEqual(iperfErrors({ ...iperfDefaults, host: 'iperf.example.org' }), {});
  assert.deepEqual(Object.keys(iperfErrors({ ...iperfDefaults, host: 'x', protocol: 'udp', length: 70000, parallel: 40, duration: 1.5 })).sort(),
    ['duration', 'length', 'parallel']);
  assert.deepEqual(Object.keys(iperfErrors({ ...iperfDefaults, host: 'x', bitrate: 10, burst: 1001, protocol: 'sctp', nstreams: 2000, rcv_timeout: 50 })).sort(),
    ['burst', 'nstreams', 'rcv_timeout']);
  assert.deepEqual(iperfErrors({ ...iperfDefaults, host: 'x', burst: 5000, nstreams: 0 }), {}, 'options the test leaves out are not checked');
  assert.deepEqual(iperfErrors({ ...iperfDefaults, host: 'x', interval: 0 }), {}, '0 turns the reports off');
  assert.equal(iperfDefaults.duration, 10, 'a test only runs until stopped when asked to');
  assert.deepEqual(iperfErrors({ ...iperfDefaults, host: 'x', duration: 0 }), {}, '0 runs until stopped');
  assert.deepEqual(Object.keys(iperfErrors({ ...iperfDefaults, host: 'x', duration: -1 })), ['duration']);
  assert.deepEqual(Object.keys(iperfErrors({ ...iperfDefaults, host: 'x', interval: 0.05, window: 524289 })).sort(), ['interval', 'window']);
  assert.deepEqual(Object.keys(iperfErrors({ ...iperfDefaults, mode: 'server', port: 80 })), ['port']);
  assert.deepEqual(Object.keys(iperfErrors({ ...iperfDefaults, mode: 'server', server_bitrate_limit: 10, server_rate_interval: 0, server_max_duration: 0 })).sort(),
    ['server_max_duration', 'server_rate_interval']);
});

test('a running test fills its form back in', () => {
  const ping = pingFromDetail({ target: '9.9.9.9', count: 20, interface: 'wwan1', ttl: 12, quiet: true });
  assert.deepEqual([ping.target, ping.count, ping.viaModem, ping.ttl, ping.quiet, ping.size], ['9.9.9.9', 20, true, 12, true, 56]);
  const client = iperfFromDetail({ mode: 'client', target: '10.0.0.9', port: 5300, direction: 'download', protocol: 'udp', limit: 'bytes', bytes: 70 });
  assert.deepEqual([client.mode, client.host, client.port, client.direction, client.protocol, client.limit, client.bytes, client.viaModem],
    ['client', '10.0.0.9', 5300, 'download', 'udp', 'bytes', 70, false]);
  const server = iperfFromDetail({ mode: 'server', port: 5400, one_off: false, interface: 'wwan0', server_bitrate_limit: 50, server_rate_interval: 10, mptcp: true });
  assert.deepEqual([server.mode, server.port, server.one_off, server.listenModem, server.viaModem, server.server_rate_interval, server.mptcp],
    ['server', 5400, false, true, false, 10, true]);
});
