# Plug-and-play architecture review

The platform has two independent objectives: maintaining owned cellular connectivity and recording available UE observations. Normal configuration is web-first; the INI and CLI remain available for provisioning, automation and recovery.

## Current ownership and isolation

One systemd unit runs a lightweight parent and separate modem, telemetry and optional web processes. Only the modem child imports GI or writes modem/network state. Telemetry schedules bounded read requests over private Unix IPC and owns file/UDP sinks. The parent performs lifecycle supervision, not acquisition. libqmi's proxy remains transport rather than a second modem manager.

A writer/web crash does not close an owned bearer. A modem-worker restart can interrupt both objectives because the physical device is shared, while web access remains available. A parent failure restarts the complete unit. Component health and desired/applied configuration are exposed independently.

## Device and network policy

Automatic sysfs discovery accepts replacement QMI USB devices but rejects ambiguous matches. Explicit serial/path selection remains available. Unsupported USB personalities require appropriate firmware/driver configuration outside this application.

Connection setup checks SIM readiness without automatic PIN attempts, requests the saved APN/PLMN, matches host/modem framing, verifies data activation and configures owned IPv4 resources. Initial attach-profile changes preserve IMS/SOS provisioning. A stored initial profile is distinct from an independently reported effective session APN.

Inherited data sessions are not blindly claimed. Stop and cleanup use owned handle/CID, USB identity/enumeration and exact host-resource checkpoints. Foreign bearers, Ethernet configuration and unrelated routes are retained.

TCP verification is separate from local ONLINE state. Route priority and cellular DNS are conservative during checking/down/stale conditions. The management web bind follows a physical LAN independently of WWAN default preference.

## Acquisition and persistence

Scheduling is monotonic with explicit gaps, bounded parallel QMI batches and asynchronous storage. Slow writers do not create unlimited acquisition backlog. Static information is cached and SIM identity expires independently. Histories are bounded and writer attempts retain their own sample/run identities.

Precision includes scheduling, query windows, scales and validity. Missing or unsupported values are unavailable; repeated historical values do not become current observations. Interface traffic and GNSS carry their own sources/timestamps. The platform remains a lightweight Python runtime with a static React/Next export.

## Installation and operator workflow

Fresh installations leave connection and monitoring disabled until selected through the web. Saved objectives resume after reboot. Both-off is idle; monitoring-only does not select a network or create a bearer. Configuration can be prepared before activation.

Host manager changes require explicit installer policy. Dependency checks precede activation, and the HTTP API never runs a package installer. Migration, backups, rollback and uninstall retain their existing receipts and ownership boundaries.

See [INSTALLATION.md](../INSTALLATION.md), [CONFIGURATION.md](../CONFIGURATION.md) and [measurement timing](metrics-precision.md) for the maintained operational references and reproducible verification. Captured diagnostic data retains its original content.
