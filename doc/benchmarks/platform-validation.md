# Process isolation and resource validation — 2026-10-21

These are development-PC results. Synthetic tests replace the physical modem only; the process manager, private IPC, telemetry scheduler, storage and web worker are real executable processes. They do not measure QMI latency or predict Mochabin CPU use. No Node server runs in deployment.

## Objective resource matrix

Thirty measured seconds per case, after five seconds of warmup, with external `/proc` sampling. CPU uses 100% for one core. RSS is summed per process and can double-count shared pages; each case below sums per-process maxima. No browser was polling the synthetic web server during this matrix.

| Connection | Monitoring | Web | Mean total CPU | Sum of maximum RSS |
| --- | --- | --- | --- | --- |
| Off | Off | Off | 0.20% | 18.4 MiB |
| Off | Off | On | 0.53% | 39.8 MiB |
| Off | On | Off | 1.07% | 54.1 MiB |
| Off | On | On | 1.47% | 76.1 MiB |
| On | Off | Off | 0.73% | 35.9 MiB |
| On | Off | On | 1.03% | 57.1 MiB |
| On | On | Off | 1.03% | 54.0 MiB |
| On | On | On | 1.47% | 76.3 MiB |

The idle/headless case starts only the manager. Monitoring starts the modem actor and telemetry actor; connection-only has no telemetry actor. Web adds its own process and five-second resource observation. Synthetic modem CPU excludes actual modem/library work. [Machine-readable observations](platform-objectives-profile.json).

Reproduce from the repository with:

```bash
/usr/bin/python3 tools/telemetry/platform_profile.py --seconds 30
/usr/bin/python3 tools/telemetry/platform_soak.py --seconds 3600
```

## Extended acquisition

The initial architecture run completed 3,600 seconds with 3,600 acquired and persisted samples, zero storage drops/errors and zero child restarts. Acquisition and writer histories both remained at 120 entries. This run preceded the event-driven IPC wakeup refinement; its planning-delay distribution includes the earlier polling delay. [Initial full-hour report](platform-initial-hour.json).

An additional event-driven follow-up recorded 1,896 CSV samples with sequential IDs and zero storage drops/errors before the interactive execution session was stopped. This follow-up was not a completed one-hour run. It includes 39 explicitly skipped scheduling intervals and 2 periods over 1.5 seconds (maximum 30.36 seconds); the audit does not represent those gaps as a continuous 1 Hz capture. Its synthetic workers were then stopped explicitly. [Follow-up audit](platform-event-followup.json). A flush remains a buffer-flush operation, without an fsync/physical-disk guarantee.

## Fault and UI checks

Executable fault injection verifies full/slow/blocked storage, saturated queues, telemetry recovery, modem-worker replacement and an unresponsive web worker. A broken acquisition socket is repaired without reopening the QMI owner. Unit/integration checks cover all objective transitions, missing hardware, ownership recovery, stale heartbeats and configuration revision conflicts. Host networking tests use temporary models or isolated namespaces.

Browser acceptance covers the login and main sections in English/Spanish and light/dark, widths from 320 to 2,560 pixels, keyboard operation, drafts, selected text, chart inspection and dependent fields. Config reference checks validate every documented option and load the complete examples through the real parser.

## Hardware observations

The installed single systemd unit was checked with the Fibocom USB modem on PLMN 99910. Both `internet` and `tenten` were independently reported as matched effective APNs. `internet` initially passed bound traffic, but a later reconnection did not pass target reachability despite local ONLINE; the fallback kept Ethernet management available. This is recorded as an unsuccessful data-path observation rather than a successful Internet check.

`tenten` subsequently passed three interface-bound pings to 1.1.1.1 with zero loss. Monitoring-off retained the modem PID and IPv4 session while stopping capture; both-off removed modem/telemetry workers and owned routes while keeping the web reachable. A web-only history change replaced the web child while retaining the modem and telemetry PIDs, IPv4 and data reachability. Connection and monitoring were restored together on the authorised test APN, retaining the saved backup route policy.

Hardware identifiers, credentials, SIM numbers and GPS coordinates are deliberately absent from these reports. Recheck throughput, QMI support, storage capacity and timing on the target Mochabin before collecting a production dataset.

## Web history regression and correction

A later hardware audit found that a 120-point web history omitted valid acquired sample IDs even while storage persisted every acquired sample. The heartbeat interval and acquisition interval were independently rate-limited; consuming only the latest full sample from each heartbeat therefore produced false visual gaps. A bounded buffer alone did not detect this omission.

The telemetry actor now retains a compact point for every acquired sample while web is enabled. The manager merges that actual window by run, modem generation and sample ID, preserving missing values and real timing gaps. It avoids reparsing unchanged actor files. Web history settings apply without restarting capture, and disabling web clears the additional buffer. The cumulative skipped-interval display now comes from the telemetry scheduler.

A three-minute isolated process test acquired and persisted 180 samples; its final web window contained exactly IDs 61 through 180. There were no storage drops/errors. A separate executable test samples faster than heartbeats and checks every ID, including history resize and web disable. [Measured baseline and corrected-run audit](history-publication-fix.json). These checks supplement the earlier run; the earlier full-hour test did not verify history ID continuity and must not be interpreted as having covered this regression.
