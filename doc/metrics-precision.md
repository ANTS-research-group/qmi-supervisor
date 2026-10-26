# Telemetry timing, accuracy and cost

This document distinguishes acquisition scheduling, modem observation windows and asynchronous storage. See [CONFIGURATION.md](../CONFIGURATION.md) for configuration and [INSTALLATION.md](../INSTALLATION.md) for deployment and verification.

## Architecture and scheduling

A lightweight manager supervises the modem, telemetry and web processes under one systemd unit. Only the modem process owns QMI clients and data-session handles. The telemetry process schedules acquisitions through a private Unix socket and processes/stores observations independently. The web reads published status; it never requests a radio sample.

The default target is one sample per second. Acquisition deadlines use the host monotonic clock, not `sleep(interval)` after variable work. Expired ticks are skipped and counted; the next request is scheduled against the absolute calendar. Neither the IPC queue nor storage is an unbounded backlog.

Within an observation window, supported dynamic QMI requests run as a bounded asynchronous batch on the owner's GLib context. The normal total budget is 0.8 seconds, with a one-second per-request timeout additionally constrained by that budget. Control/cleanup retains modem ownership and its own deadlines. During registration, useful NAS observations can feed both policy and telemetry; WDS bearer queries do not run before the owned bearer is ready.

Late callbacks cannot mutate a closed acquisition. Unsupported and timed-out values remain unavailable. Static hardware information is cached; SIM identity has its own expiry. A new process/run or USB generation is not silently joined to an earlier trace.

USB modems answer QMI requests serially: each response waits for one poll of the control interface's interrupt endpoint (32 ms on the validated Fibocom and Quectel modules). Acquisition time therefore grows by about 32 ms per request in a sample. With `profile = custom`, each query group has its own `query_*` refresh period. Groups that are not due in a sample are not requested; their values are null in that sample rather than repeated from an earlier observation, and they are absent from the sample's request records. The `schedule` entry in the collection report and `query_timing_json` records each active group's period, whether it was queried in that sample and the time of its last successful response. Slow groups are staggered across samples. Registration still observes the serving system every sample. Use `tools/telemetry/query_timing_report.py` to summarise per-request timing from dynamic CSV captures.

The IPC response preserves the owner's actual acquisition start. Queue waiting is reported separately instead of attributing it to an earlier radio observation. Throughput and external GPS readings retain their respective observation times.

## Meaning of precision

- **Scheduling:** how closely observation starts follow the requested period, including lag and skipped intervals.
- **Observation:** the interval between each host QMI request and response. This bounds host observation; it is not the modem's internal radio measurement timestamp.
- **Measurement:** correct scale, sign, units and validity of values returned by the firmware. Extra timestamp decimal places do not improve RF accuracy.
- **Correlation:** UTC timestamps enable comparison between devices, subject to clock synchronisation. Monotonic time measures local durations without wall-clock jumps.

Firmware, USB transport, Linux scheduling and storage have variable latency. This implementation does not provide a hard real-time guarantee that every supported metric will arrive every second. Missing observations are visible rather than invented or replayed later.

## Storage and datasets

CSV and JSON use a separate telemetry writer thread and bounded queue inside the telemetry process. A blocked filesystem operation can require replacing that process; it does not require closing the modem's data connection. The old writer must be reaped before replacement.

The storage queue defaults to 64 queued samples, with an additional in-flight job. Full queues increment explicit drop counters. Errors and flush failures have separate counters. UDP forwarding uses an independent latest-sample slot and does not block radio acquisition.

Daily CSV files are opened with schema checking that does not scan the entire existing file before every append. Static CSV, dynamic v2 CSV and latest JSON retain their existing formats. Files are never silently truncated to accommodate a schema mismatch; select a new capture directory when required.

Acquisition history and writer history are distinct. A write attempt carries the sample ID and capture run to which it belongs. The latest acquired sample and latest written sample need not match. A writer update must not be presented as the write duration of a newly acquired sample.

**Job duration**, **queue-inclusive latency** and **buffer-flush duration** are different measurements. CSV flush is not `fsync` and does not guarantee physical-media durability. Normal shutdown drains within a bounded interval; incomplete drain is reported. Data files have no automatic retention policy.

## Timing and validity fields

The dynamic CSV v2 prefix preserves the existing timing columns. JSON also carries structured collection and pipeline details:

| Field | Meaning |
| --- | --- |
| `run_id` | Capture-process incarnation. |
| `scheduled_at_utc`, `scheduled_monotonic` | Intended observation start. |
| `acquired_start_utc`, `acquired_start_monotonic` | Actual owner observation start. |
| `acquired_end_utc`, `acquired_end_monotonic` | End of sample acquisition/host observations. |
| `acquisition_duration_ms` | Actual acquisition duration. |
| `schedule_lag_ms` | Delay from schedule to observation start. |
| `effective_interval_ms` | Difference between successive acquisition starts. |
| `skipped_intervals` | Explicit expired intervals; traces retain gaps. |
| `wall_clock_step_ms` | Difference between wall-time and monotonic progress. |
| `query_timing_json` | Per-request timestamps, result and duration, plus optional observations. |
| `collection.ipc_queue_wait_ms` | Owner dispatch wait, separate from radio query time. |
| `pipeline.last_write_sample_id` | Sample involved in the latest write attempt. |
| `pipeline.last_write_duration_ms` | Duration of that attempt. |
| `pipeline.last_write_latency_ms` | Acquisition-to-attempt-completion latency, including queue waiting. |

Bounded acquisition/writer histories default to 120 points. Live status freshness derives from each actor's actual heartbeat. HTTP responses, writer updates and `/proc` reads cannot renew the modem heartbeat. CPU is measured against actual elapsed monotonic time; 100% means one CPU core. RSS includes shared pages and is not private memory. The first CPU reading and missing-process readings are unavailable, not zero; RSS is available after the first valid process read. Additional process-resource sampling runs only with web enabled.

While web is enabled, the acquisition worker retains a compact window of every actual sample. Heartbeats transport that window independently of their own publication cadence. The manager merges and deduplicates points by capture run, modem generation and sample ID, so a slower status-file publication does not omit measurements from charts. History resizing does not restart acquisition; disabling web clears this additional buffer. Unchanged actor files are not repeatedly parsed. Neither HTTP polling nor this history transport adds QMI requests.

Verification must compare history sample IDs with acquired/written sample IDs, as well as checking bounded size. A full 120-point buffer alone does not prove that it contains 120 consecutive samples. Actual acquisition gaps, unavailable values and RAT changes retain their original times and validity; chart rendering does not interpolate them away.

## RF and traffic interpretation

NR and LTE sources are retained per signal magnitude. A current NR RSRP does not prove that every other field comes from NR. Signal gaps are retained when values, RAT, run, USB generation or observation continuity changes.

In QMI cell-location information, NR RSRP/RSRQ/SNR values use tenths. Signal Info RSRP is already dBm and extended RSRQ is dB; SNR requires the documented scale. Invalid sentinels such as `-32768` are not measurements. EV-DO SINR enumerations are not NR SINR. Tx/Rx powers require their documented scales and tuning/availability indicators.

RF band numbers survive older libqmi versions without an enum label. A dedicated or extended NR-ARFCN takes precedence over a truncated legacy field. NR-ARFCN 629952 corresponds to 3449.28 MHz; legacy 40128 is not its full-channel equivalent. Band n78 is reported only with supporting band information, and LTE presence/absence alone does not establish SA/NSA.

Frequency calculations follow [3GPP NR through ETSI](https://www.etsi.org/deliver/etsi_TS/138100_138199/138104/17.14.00_60/ts_138104v171400p.pdf) and [LTE through ETSI](https://www.etsi.org/deliver/etsi_ts/136100_136199/136101/18.05.00_60/ts_136101v180500p.pdf). The implementation's parsers follow [official qmicli NAS definitions](https://raw.githubusercontent.com/linux-mobile-broadband/libqmi/main/src/qmicli/qmicli-nas.c).

Actual interface throughput uses sysfs byte-counter differences and elapsed monotonic time. Legacy names containing `kb`/`kbps` retain their compatibility, but the units are KiB and Kibit/s (divisor 1024). QMI channel rates are not actual traffic. First readings and reset counters produce unavailable rates. GPS solution timestamps and host receipt timestamps remain distinct; a stale solution is not promoted to a fresh fix.

## Reproducible software checks

```bash
python3 tools/telemetry/benchmark.py --samples 10000 --interval 1 \
  --query-delay 0.2 --write-delay 0.4
python3 tools/telemetry/benchmark.py --help
python3 tools/telemetry/analyze_capture.py --help
/usr/bin/python3 tools/telemetry/platform_soak.py --seconds 3600
```

The default benchmark is a deterministic virtual-clock model. Its realtime option uses the existing scheduler/writer with injected delays. The platform soak exercises real manager/child processes, IPC, storage, publication and web with a synthetic UE. These are software measurements; they do not characterise modem firmware, RF timing or Mochabin flash.

Historical synthetic outputs remain in [synthetic-1hz.json](benchmarks/synthetic-1hz.json) and [synthetic-bounded-memory.json](benchmarks/synthetic-bounded-memory.json). In the historical 12-sample 1 Hz run, an injected 20 ms acquisition and 1200 ms write with queue capacity 3 produced a 1000.009 ms mean observation period, 1.060 ms maximum scheduling lag, 12 persisted samples and no drops. Its 14.437-second total included final draining. These are historical host-specific results, not a new platform benchmark.

## Hardware evidence and remaining target validation

On 20 October 2026 a Fibocom USB modem on Ubuntu 22.04 reported a ready SIM. Correcting borrowed UIM output lifetime, WDS ownership/APN handling and initial attach-profile selection allowed an owned session on authorised PLMN 99910/APN tenten with IPv4 through QMI. Short forced-WWAN ping and HTTP tests confirmed actual traffic. Those observations do not establish sustained sampling accuracy for every modem or core.

Verify the final deployed platform on each target: actual QMI request windows, partial responses, signal scaling, RF-channel source, disk pressure, CPU/RSS, UTC synchronisation and loss/recovery of USB/coverage. Test the management path separately from the cellular default. Retain capture IDs and error/gap counters when comparing datasets.
