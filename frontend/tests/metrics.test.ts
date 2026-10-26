import assert from 'node:assert/strict';
import test from 'node:test';
import { apnPresentation, finite, flatten, gpsPresentation, imsiValue, lineConnects, metricLabel, nearestPoint, number,
  orderedPoints, pointKey, pointTime, radio, rfValue, signalQuality, signalSegments, signalValue,
  text, trafficPresentation, valueScale } from '../lib/metrics.ts';

test('finite values retain zero while missing, boolean and string values stay unavailable', () => {
  assert.equal(finite(0), true);
  assert.equal(number(0), '0');
  assert.equal(text(0), '0');
  for (const value of [null, undefined, '', false, '0', NaN, Infinity, -Infinity]) {
    assert.equal(finite(value), false);
    assert.equal(number(value), 'Not available');
  }
  assert.equal(text(null), 'Not available');
});

test('current signal uses the history GetSignalInfo fields without substituting cell metrics', () => {
  const dynamic = { nas_value_5g_signal_strength_rsrp: -95, nas_value_lte_rsrp: -105,
    nas_value_5g_signal_strength_extended: -14, nas_value_lte_rsrq: -17,
    nas_value_5g_signal_strength_snr: 0, nas_value_lte_snr: 8,
    nas_value_nr5g_cell_information_rsrp: -60, nas_value_nr5g_cell_information_snr: 30 };
  assert.deepEqual(signalValue(dynamic, 'rsrp'), { value: -95, rat: 'NR' });
  assert.deepEqual(signalValue(dynamic, 'rsrq'), { value: -14, rat: 'NR' });
  assert.deepEqual(signalValue(dynamic, 'snr'), { value: 0, rat: 'NR' });
  assert.deepEqual(signalValue({ ...dynamic, nas_value_5g_signal_strength_rsrp: null }, 'rsrp'), { value: -105, rat: 'LTE' });
  assert.deepEqual(signalValue({ nas_value_nr5g_cell_information_rsrp: -60 }, 'rsrp'), { value: null, rat: null });
  assert.deepEqual(signalValue(null, 'snr'), { value: null, rat: null });
});

test('signal colors use the exact documented threshold boundaries', () => {
  for (const [metric, boundaries] of [['rsrp', [-100, -90, -80]], ['rsrq', [-20, -15, -10]], ['snr', [0, 13, 20]]] as const) {
    const [low, middle, high] = boundaries;
    assert.equal(signalQuality(metric, high)?.name, 'Excellent');
    assert.equal(signalQuality(metric, middle)?.name, 'Good');
    assert.equal(signalQuality(metric, low + .1)?.name, 'Fair');
    assert.equal(signalQuality(metric, low)?.name, 'Cell edge');
    assert.equal(signalQuality(metric, null), null);
  }
});

test('a signal line changes color exactly where each threshold is crossed', () => {
  const segments = signalSegments('rsrp', -110, -70);
  assert.deepEqual(segments.map(segment => [segment.from, segment.to]), [[0, .25], [.25, .5], [.5, .75], [.75, 1]]);
  assert.deepEqual(segments.map(segment => segment.color), ['#d82323', '#ef8b00', '#c7a600', '#77b82a']);
  assert.equal(signalSegments('snr', 0, 0)[0].color, '#d82323');
  assert.deepEqual(signalSegments('rsrp', NaN, -90), []);
});

test('traces break at missing samples, RAT changes, runs, device generations and omitted ticks', () => {
  const first = { monotonic: 10, rsrp: -100, rsrp_rat: 'LTE', run_id: 'run', device_generation: 1, skipped_intervals: 0 };
  const next = { ...first, monotonic: 11, rsrp: -90 };
  assert.equal(lineConnects(first, next, 'rsrp'), true);
  for (const changes of [{ rsrp: null }, { rsrp: NaN }, { rsrp_rat: 'NR' }, { run_id: 'new-run' },
    { device_generation: 2 }, { skipped_intervals: 1 }, { monotonic: 13 }, { monotonic: 10 }]) {
    assert.equal(lineConnects(first, { ...next, ...changes }, 'rsrp'), false, JSON.stringify(changes));
  }
  // This field describes each sample's skipped ticks, rather than a cumulative counter.
  assert.equal(lineConnects({ ...first, skipped_intervals: 1 }, { ...next, skipped_intervals: 1 }, 'rsrp'), false);
});

test('monotonic time survives UTC adjustments, resource cadence and process restarts are independent', () => {
  const first = { time: '2026-10-20T12:00:00Z', observed_monotonic: 10, pending: 1, supervisor_cpu: 0 };
  const next = { time: '2026-10-20T11:59:00Z', observed_monotonic: 11, pending: 0, supervisor_cpu: 2 };
  assert.equal(pointTime(next), 11000);
  assert.equal(lineConnects(first, next, 'pending', 1), true);
  assert.equal(lineConnects(first, { ...next, observed_monotonic: 15 }, 'pending', 1), false);
  assert.equal(lineConnects(first, { ...next, observed_monotonic: 15 }, 'supervisor_cpu', 5), true);
  assert.equal(lineConnects(first, { ...next, supervisor_cpu_run_id: 'new-process' }, 'supervisor_cpu', 5), false);
});

test('a cursor in a real gap does not imply an interpolated measurement', () => {
  const points = [{ monotonic: 10, rsrp: -100, rsrp_rat: 'LTE' }, { monotonic: 20, rsrp: -80, rsrp_rat: 'LTE' }];
  const series = [{ key: 'rsrp', label: 'RSRP' }];
  assert.deepEqual(nearestPoint(points, 15000, series), { point: null, index: null, time: 15000, gap: true });
  assert.equal(nearestPoint(points, 10000, series)?.point?.rsrp, -100);
  assert.equal(nearestPoint([], 10000, series), null);
  const zero = [{ monotonic: 10, pending: 0 }];
  assert.equal(nearestPoint(zero, 10000, [{ key: 'pending', label: 'Pending' }])?.point?.pending, 0);
});

test('history work remains bounded and selection keys include the run and monotonic instant', () => {
  const points = Array.from({ length: 1300 }, (_, index) => ({ monotonic: index, rsrp: -90 }));
  const ordered = orderedPoints(points);
  assert.equal(ordered.length, 1000);
  assert.equal(ordered[0].monotonic, 300);
  assert.equal(points.length, 1300);
  assert.notEqual(pointKey({ monotonic: 1, run_id: 'one' }), pointKey({ monotonic: 1, run_id: 'two' }));
  assert.deepEqual(orderedPoints([{ time: 'invalid' }, { monotonic: 2 }, { monotonic: 1 }]).map(point => point.monotonic), [1, 2]);
});

test('automatic signal scales never clip observed outliers or fabricate an observed zero', () => {
  const scale = valueScale([-150, -120], { metric: 'rsrp', min: -140, max: -50 });
  assert.ok(scale.min <= -150);
  assert.ok(scale.max >= -120);
  assert.equal(scale.observedMin, -150);
  const empty = valueScale([], { metric: 'rsrp', min: -140, max: -50 });
  assert.equal(empty.observedMin, null);
  assert.equal(empty.observedMax, null);
  const zero = valueScale([0]);
  assert.equal(zero.observedMin, 0);
  assert.ok(zero.max > zero.min);
});

test('diagnostics flatten nested records into one field/value row each', () => {
  assert.deepEqual(flatten({ sampled_at_utc: 'then', supervisor: { state: 'ok', pid: 7 }, none: null, empty: {} }),
    { sampled_at_utc: 'then', 'supervisor.state': 'ok', 'supervisor.pid': 7, none: null, empty: {} });
  assert.deepEqual(flatten({ requests: { 'nas.get_serving_system': { status: 'ok', duration_ms: 12 } } }),
    { 'requests.nas.get_serving_system.status': 'ok', 'requests.nas.get_serving_system.duration_ms': 12 });
  // Arrays stay whole: the raw QMI list is one value, never expanded into invented rows.
  const bands = [{ radio_interface: '5gnr', active_band_class: 'nr5g-78' }];
  assert.deepEqual(flatten({ nas_value_rf_bands: bands }), { nas_value_rf_bands: bands });
  assert.equal(metricLabel('supervisor.cpu_percent'), 'cpu percent');
  assert.equal(metricLabel('nas.get_serving_system.duration_ms'), 'duration ms');
  assert.equal(metricLabel('nas_value_lte_rsrp'), 'RSRP LTE');
  assert.deepEqual(flatten(null), {});
});

test('radio display uses only published observations without deriving bands or frequencies from ARFCN', () => {
  assert.deepEqual(radio({ sample: { dynamic: { nas_value_nr5g_arfcn: 629952 } } }), {});
  const observed = { cells: [{ rat: 'nr', channel: 629952 }], bands: [{ rat: 'nr', raw: 9999, channel: 629952 }] };
  const result = radio({ radio: observed });
  assert.equal(result.cells[0].frequency_mhz, undefined);
  assert.equal(result.bands[0].band, undefined);
  assert.equal(rfValue(result.bands[0]), 'QMI code 9999');
  assert.equal(rfValue({ rat: 'nr', raw: 9 }, 'bandwidth'), 'QMI code 9');
  assert.equal(rfValue({ raw: 0 }, 'bandwidth'), 'QMI code 0');
  assert.equal(rfValue({ band: 'n78', raw: 269 }), 'n78');
  assert.equal(rfValue({ bandwidth_mhz: 100, raw: 16 }, 'bandwidth'), '100 MHz');
  assert.deepEqual(radio({ radio_summary: observed }), observed);
});

test('SIM identity preserves leading zeroes and never falls back to the modem IMEI', () => {
  const synthetic = '000000000000001';
  assert.equal(imsiValue({ static: { dms_value_imsi: synthetic } }), synthetic);
  for (const sample of [null, undefined, {}, { static: {} }, { static: { dms_value_imsi: '' } },
    { static: { dms_value_imsi: 123 } }, { static: { dms_value_imei: 'synthetic-modem' } }]) assert.equal(imsiValue(sample), null);
});

test('matching APN strings alone cannot verify the effective session APN', () => {
  assert.equal(apnPresentation().verification, 'Unverified');
  assert.equal(apnPresentation({ requested_apn: 'internet', reported_apn: 'internet' }).verification, 'Unverified');
  const unavailable = apnPresentation({ requested_apn: 'internet', apn_verification: 'unavailable' });
  assert.equal(unavailable.reported, null);
  assert.match(unavailable.warning!, /does not report the effective APN/);
  assert.equal(apnPresentation({ requested_apn: 'internet', reported_apn: 'internet', apn_verification: 'matched' }).verification, 'Matched');
});

test('WWAN rates use explicit sysfs status, retain zero and avoid QMI channel-rate substitutes', () => {
  const sample = { dynamic: { throughput_download_speed_kbps: 0, throughput_upload_speed_kbps: 25.5,
    wds_value_channel_rates_channel_tx_rate_bps: 100000000 }, collection: { interface_traffic: {
    enabled: true, status: 'initializing', rate_unit: 'Kibit/s', interface: 'wwan0', rx_bytes: 0, tx_bytes: 2048 } } };
  assert.equal(trafficPresentation(sample).rx, null);
  sample.collection.interface_traffic.status = 'ok';
  const result = trafficPresentation(sample);
  assert.equal(result.rx, 0);
  assert.equal(result.tx, 25.5);
  assert.equal(result.rxBytes, 0);
  assert.equal(result.unit, 'Kibit/s');
  sample.collection.interface_traffic.status = 'counter_reset';
  assert.equal(trafficPresentation(sample).tx, null);
  assert.equal(trafficPresentation({ dynamic: sample.dynamic }).rx, null);
  assert.equal(trafficPresentation({ dynamic: { wds_value_channel_rates_channel_tx_rate_bps: 100000000 } }, true).tx, null);
});

test('GNSS presentation preserves full coordinate precision, source time, zero and MSL reference', () => {
  const sample = { collection: { gps: { enabled: true, source: 'qmi_loc', status: 'fixed', age_ms: 0 } }, dynamic: {
    gnss_latitude: 0, gnss_longitude: -1.123456789, gnss_altitude_msl_m: 76.321,
    gnss_timestamp_utc: '2026-10-21T12:00:00.123Z', gnss_horizontal_uncertainty_m: 2.5, gpsd_tpv_lon: 20 } };
  const result = gpsPresentation(sample);
  assert.equal(String(result.longitude), '-1.123456789');
  assert.equal(result.latitude, 0);
  assert.equal(result.timestamp, '2026-10-21T12:00:00.123Z');
  assert.equal(result.altitude, 76.321);
  assert.equal(result.altitudeReference, 'MSL');
  assert.equal(result.age, 0);
  assert.equal(gpsPresentation({ ...sample, dynamic: { gpsd_tpv_lon: 20 } }).longitude, null);
});

test('GPS stale fixes stay labelled stale and invalid or missing positions do not become new fixes', () => {
  const sample = { collection: { gps: { enabled: true, source: 'gpsd', status: 'stale', age_ms: 11000 } },
    dynamic: { gpsd_tpv_lat: 38.123456789, gpsd_tpv_lon: -1.123456789, gpsd_tpv_datetime: '2026-10-21T12:00:00Z' } };
  assert.equal(gpsPresentation(sample).status, 'stale');
  assert.equal(gpsPresentation(sample).latitude, 38.123456789);
  assert.equal(gpsPresentation(sample).age, 11000);
  assert.equal(gpsPresentation({ ...sample, dynamic: { gpsd_tpv_lat: NaN, gpsd_tpv_lon: Infinity } }).latitude, null);
  sample.collection.gps.status = 'no_fix';
  assert.equal(gpsPresentation(sample).latitude, null);
  assert.equal(gpsPresentation(sample).timestamp, null);
  assert.equal(gpsPresentation(null).status, 'disabled');
});
