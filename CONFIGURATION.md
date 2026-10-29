# INI configuration reference

This reference documents every INI option in the sections below: its parser default, accepted values, meaning and when a change takes effect. Installation and host preparation are described in [INSTALLATION.md](INSTALLATION.md); normal operation is configured through the web interface.

## Syntax and validation

The installed file is `/etc/qmi-supervisor.ini`, mode `0600`. Use the six section names below, optionally adding named per-modem sections as described next. Unknown sections/options, duplicate entries, `[DEFAULT]` values, line breaks and NUL characters are rejected. Option names are case-insensitive; enum values are case-sensitive. Values are not shell-expanded. Do not add quotation marks unless they are intended characters in a string.

Booleans accept `true/false`, `yes/no`, `on/off` and `1/0`, case-insensitively. Use a decimal point for numbers. Blank strings are permitted only where documented. Retained inactive settings are still validated, so disabling a feature does not make an invalid port or number acceptable.

Tables show **parser defaults** when a setting is omitted. A **fresh installation** intentionally overrides these: `modem.mode=monitor`, `modem.plmn=auto`, `telemetry.enabled=false`, an absolute telemetry directory under `/var/lib/qmi-supervisor`, and `web.enabled=true`. The installer creates credentials before starting web access. Existing installations preserve saved values.

## Independent objectives

| Connection | Monitoring | INI values |
| --- | --- | --- |
| Enabled | Disabled | `modem.mode=connect`, `telemetry.enabled=false` |
| Disabled | Enabled | `modem.mode=monitor`, `telemetry.enabled=true` |
| Enabled | Enabled | `modem.mode=connect`, `telemetry.enabled=true` |
| Disabled | Disabled | `modem.mode=monitor`, `telemetry.enabled=false` |

Connection-off releases only owned resources; monitoring-only never selects an operator or opens a bearer. Both-off means no periodic QMI acquisition. Web is independent. Browser language and theme are local preferences, not INI options.

A retained ownership checkpoint after a crash can require a temporary cleanup worker even with both objectives off. This worker verifies and releases only the previous supervisor's resources, without applying APN/PLMN settings or starting dataset acquisition. Release stays pending until confirmed; an absent modem must be reconnected. This recovery exception does not adopt or stop an unrelated existing data session.

## Multiple modems

The unqualified sections describe the `default` modem and the template for newly discovered devices. A second detected modem is automatically saved as an `auto-<manufacturer>-<serial>` profile, falling back to the USB topology when the serial is missing or duplicated. This materialisation preserves its APN, objectives and other settings when the default modem is edited later. With both objectives disabled it remains idle until enabled explicitly.

Named profiles use `[modem:<id>]`, `[network:<id>]`, `[recovery:<id>]` and `[telemetry:<id>]`. IDs start with a lowercase ASCII letter followed by up to 47 lowercase letters, digits, underscores or hyphens. `default` is reserved for the unqualified sections. `[modem:<id>]` is required; missing options initially inherit unqualified settings. Saving configuration materialises the effective values so later edits remain independent. All profiles are validated, including profiles with absent hardware or disabled objectives. An absent modem keeps its profile and settings; the web interface lists only modems currently detected, so the fleet view shows the remaining modem alone until the other one is reconnected.

`[web]` and `[system]` are shared by the platform; `[web:<id>]` and `[system:<id>]` are invalid. Named modem selectors are reserved before automatic discovery. Duplicate selectors never share a modem: the conflicting profile reports a blocking condition. A duplicated serial requires an explicit device path; an automatically discovered device without a unique serial is pinned to its current control path. Prefer unique serials for stable identity across USB renumbering.

```ini
[modem]
device = auto
apn = internet
mode = connect
plmn = 99910

[telemetry]
enabled = true
directory = /var/lib/qmi-supervisor
interval = 1

[modem:private]
serial = second-modem-usb-serial
apn = tenten
plmn = 99910
mode = connect

[network:private]
method = qmi
route_policy = backup
route_metric = 700

[telemetry:private]
enabled = true
interval = 1
```

Each UE has private QMI clients, bearer ownership, connection checkpoints, IPC, scheduling, queues and bounded histories. Named profile files are stored beneath `<telemetry.directory>/profiles/<id>/modem_<identity>/`; default-profile paths and CSV columns stay unchanged. JSON and full UDP samples retain device/interface/run identifiers. The legacy collector envelope remains compatible and carries its existing static IMEI; use separate collector destinations if the collector cannot distinguish UEs with missing/duplicated identity. A GPSd source is shared host location; `gps_source=modem` uses that UE's own supported QMI LOC receiver.

Recovery journals follow physical USB topology and modem identity, independently of profile IDs. Rebinding a profile cannot apply its previous UE's bearer CID/handle to the replacement. A present, unassigned UE with retained owned resources can temporarily run a cleanup-only modem worker; no network selection, APN provisioning or acquisition is performed. Missing hardware retains its journal. See [runtime recovery](INSTALLATION.md#multiple-modems-and-independent-apns) for deployment context.

A port change after a failure requires a unique serial to confirm physical identity. Old host IP resources are retired without QMI before starting the new transport; its previous CID is never reused. Persistent attach-profile intent can migrate with connection enabled and is checked against current firmware information. Monitoring-only preserves that intent without applying it. Missing or duplicate serials do not trigger automatic ownership relocation.

Route allocation reserves distinct metrics for managed peers without changing saved requests. With equal configured metrics the profile ID provides deterministic ordering. Primary/backup placement is calculated against unrelated host routes, excluding only exact recorded peer-owned routes. Exhausted metric ranges produce an explicit error or withdraw an unsafe fallback. DNS is applied only by an actually preferred, verified connection. These are priority/failover routes, not traffic bonding or load balancing.

## [modem]

| Option | Parser default | Type / accepted values | Meaning and prerequisites | Applying changes |
| --- | --- | --- | --- | --- |
| `device` | `auto` | `auto` or an absolute device path | QMI USB control port. The platform assigns separate physical modems to independent workers. Explicit profile selectors must identify one physical UE. | Reopens only the selected modem; affects both of its objectives. |
| `serial` | (empty) | String; empty means unrestricted | Optional stable USB serial selector. Combined with device when both are set. Pinning restricts acceptable replacements; it is not a SIM identifier. | Reopens modem access; affects both objectives. |
| `apn` | `internet` | Single-line string; nonempty in connect mode | Operator APN, commonly internet. It is not discovered from a cell broadcast. Connection setup synchronises the supported initial attach profile and requests/verifies the data APN where firmware supports reporting. IMS/SOS profiles are retained. | Reconnects an enabled connection; observation can have radio gaps. |
| `plmn` | (empty) | Empty, `auto`, or 5/6 ASCII digits | Empty preserves modem network selection; auto explicitly requests automatic selection. Digits are MCC+MNC: retain a two- or three-digit MNC, including leading zeros. Example: 99910. Selection remains subject to SIM permissions and coverage. | Reconnects an enabled connection; never applied by passive monitoring. |
| `mode` | `connect` | `connect` or `monitor` | Persistent connection objective. connect enables owned data/IP management; monitor disables it and leaves monitoring controlled independently by telemetry.enabled. This is not a radio SA/NSA selector. | Enables/disables owned connectivity without stopping enabled monitoring. |

## [network]

| Option | Parser default | Type / accepted values | Meaning and prerequisites | Applying changes |
| --- | --- | --- | --- | --- |
| `method` | `qmi` | `qmi` or `dhcp` | qmi obtains address, prefix, gateway, DNS and MTU from QMI. dhcp uses udhcpc and requires that package and a modem supporting DHCP. Both methods configure IPv4. | Reconnects an enabled connection. |
| `route_metric` | `700` | Integer, 0..4294967295 | For fixed: exact owned main-table default metric. For primary: ceiling when selecting below foreign defaults. For backup: floor when selecting above foreign defaults. An impossible priority (for example primary below metric 0) fails explicitly; foreign routes are retained. | Reconfigures an enabled connection and its owned route. |
| `default_route` | `true` | Boolean | Whether to add a cellular IPv4 default route. false still allows an addressed cellular interface and reachable on-link traffic. Default-route policies/checks and cellular default DNS management are inactive when false. | Reconfigures an enabled connection. |
| `dns` | `auto` | `auto`, `none`, `resolvectl`, `resolvconf` | auto selects operational systemd-resolved, otherwise resolvconf. Explicit backends require their executable/service. none preserves existing DNS. Cellular DNS is applied only when the owned default is preferred and connectivity policy permits it. | Reconfigures an enabled connection; may change active DNS. |
| `route_policy` | `fixed` | `fixed`, `primary`, `backup` | Policy for the owned default after verification. primary selects below foreign IPv4 defaults in table main; backup selects above them. Other routes/policy tables are not rewritten. Requires default_route=true to have effect. | Reconfigures an enabled connection. |
| `routes` | `auto` | Empty, or up to 8 comma-separated items: `auto` and IPv4 networks in CIDR form, such as `192.168.4.0/24` | Networks always reached through this modem, whatever the default route, such as the other UEs of a private core. The core never announces its UE pool and the modem reports only a /30, so auto adds the /24 around the modem address (nothing when the modem network is already that wide). When that /24 overlaps a route of another interface (LAN, containers, VPN) it is not added: a warning in the log and on the modem Summary asks to check the conflict and list the network by hand. Empty routes nothing beyond the modem network. Each one gets an owned main-table route via the modem with route_metric, added after the IPv4 configuration, restored if the kernel drops it and removed when the connection ends; a matching foreign route is never claimed. No 0.0.0.0/0 (use default_route), loopback, multicast, 0/8 or host bits. A route that cannot be added is logged and reported without stopping the connection. | Reconnects an enabled connection. |
| `connectivity_check` | `true` | Boolean | Enable separate TCP checks bound to the WWAN interface and source address. During checking/down the owned default is demoted behind foreign defaults. ONLINE alone describes local session/IP configuration, not Internet reachability. | Reconfigures an enabled connection. |
| `connectivity_targets` | `1.1.1.1:443,8.8.8.8:53` | 1..4 comma-separated unique numeric `IPv4:port` targets | Ports 1..65535. No hostnames, duplicate targets, loopback, multicast, 0/8 or limited broadcast. Success to any target verifies a round. For private networks use services reachable inside that network. Validation applies even while checking is disabled. | Reconfigures enabled connection checks. |
| `connectivity_interval` | `3.0` | Finite number, 1..60 seconds | Time between independent TCP check rounds. This does not change telemetry sampling. | Reconfigures enabled connection checks. |
| `connectivity_timeout` | `1.0` | Finite number, 0.1..5 seconds and <= connectivity_interval | Maximum duration of one TCP verification round. | Reconfigures enabled connection checks. |
| `connectivity_failures` | `2` | Integer, 1..10 rounds | Consecutive failed rounds before falling back. Checking initially stays conservative until verified. | Reconfigures enabled connection checks. |
| `connectivity_successes` | `2` | Integer, 1..10 rounds | Consecutive successful rounds before restoring requested priority. | Reconfigures enabled connection checks. |

## [recovery]

| Option | Parser default | Type / accepted values | Meaning and prerequisites | Applying changes |
| --- | --- | --- | --- | --- |
| `retry_initial` | `2` | Positive finite number, seconds | Initial connection retry delay; exponential backoff is capped by retry_max. Worker-crash recovery has its own independent 2..60-second backoff. | Updates connection recovery; an enabled connection may be reconfigured. |
| `retry_max` | `60` | Positive finite number, seconds; >= retry_initial | Maximum connection retry delay. Missing hardware, registration or data-session failure does not disable the saved objective. | Updates connection recovery; an enabled connection may be reconfigured. |
| `registration_timeout` | `180` | Positive finite number, seconds | Maximum registration wait before connection recovery. Passive monitoring does not select or wait for registration. | Updates enabled connection setup. |
| `operation_timeout` | `20` | Positive integer, seconds | Timeout passed to normal QMI/network operations. Cleanup uses shorter bounded deadlines. Firmware/library numeric limits still apply; normal installations should retain the default. | Updates modem operation deadlines and enabled connection setup. |
| `poll_interval` | `10` | Positive finite number, seconds | Connection/device health-check period. Valid fresh observations can be reused; it is independent of the telemetry interval. | Updates connection supervision. |

## [telemetry]

| Option | Parser default | Type / accepted values | Meaning and prerequisites | Applying changes |
| --- | --- | --- | --- | --- |
| `enabled` | `true` | Boolean | Persistent monitoring objective. Starts/stops acquisition, optional GPS, storage and forwarding independently of connection. false is not merely a stop-recording switch. | Starts/stops the telemetry worker; retains owned data connectivity. |
| `interval` | `1.0` | Positive finite number, seconds | Target time between observation windows. 1 means 1 Hz. Absolute monotonic scheduling skips expired ticks instead of accumulating drift. | Restarts telemetry only; starts a new capture run. |
| `directory` | `files` | Nonempty single-line path | Storage root. Relative paths resolve under the service working directory (/opt/qmi-supervisor when installed). Installer uses /var/lib/qmi-supervisor. The process needs write permission and free space. No automatic retention/deletion is performed. | Restarts telemetry only; existing captures are retained. |
| `csv` | `true` | Boolean | Write daily static_stats_YYYYMMDD.csv and dynamic_stats_v2_YYYYMMDD.csv below modem_<identity>. Existing CSV column/schema checks remain enforced. | Restarts telemetry only. |
| `json` | `true` | Boolean | Atomically replace stats.json with the latest complete sample. It is not an append-only dataset; use CSV for sample history. | Restarts telemetry only. |
| `udp_host` | (empty) | Single-line destination string; empty disables sink | Optional UDP full-sample destination, resolved by the socket layer. Broadcast is supported. Network/recipient delivery is not guaranteed by UDP. | Restarts telemetry only. |
| `udp_port` | `5006` | Integer, 1..65535 | UDP port for the full-sample destination; inactive without udp_host. | Restarts telemetry only. |
| `collector_host` | (empty) | Single-line destination string; empty disables sink | Optional UDP collector destination for the static/dynamic metric envelope. This is forwarding, not a local Prometheus HTTP endpoint. | Restarts telemetry only. |
| `collector_port` | `5007` | Integer, 1..65535 | UDP collector port; inactive without collector_host. | Restarts telemetry only. |
| `throughput` | `false` | Boolean | Read actual selected-interface byte counters from sysfs and compute KiB/Kibit/s using actual elapsed monotonic time. First readings, device changes and counter resets do not fabricate a rate. QMI channel rates are not traffic. | Restarts telemetry only. |
| `gps` | `false` | Boolean | Enable optional positioning while monitoring is enabled. Missing/unsupported GPS is reported separately and does not supply fabricated coordinates. | Restarts telemetry; starts/stops only owned GPS activity. |
| `gps_source` | `gpsd` | `gpsd` or `modem` | gpsd reads an external GPSd service (requires gpsd and a configured receiver). modem uses supported QMI LOC indications on the owned QMI session. It does not require or start a data bearer. | Restarts telemetry and adjusts owned GPS activity. |
| `queue_size` | `64` | Integer, 1..4096 samples | Maximum queued storage samples, plus an in-flight operation. A full queue drops explicitly; acquisition never waits for disk. UDP uses a separate latest-sample slot. | Restarts telemetry only. |
| `flush_interval` | `1.0` | Positive finite number, seconds | Maximum normal interval between CSV buffer flush attempts. Flush is not fsync and does not guarantee physical-media persistence. Relevant to storage, not radio sampling. | Restarts telemetry only. |
| `gps_max_age` | `10.0` | Positive finite number, seconds | Maximum accepted positioning age. Stale/no-fix coordinates are not represented as a current fix. | Restarts telemetry only. |
| `request_timeout` | `1` | Integer, 1..60 seconds | Per-QMI-request timeout, additionally bounded by the collection budget. Some firmware calls do not complete before the budget and are marked partial. | Restarts telemetry only; modem uses updated read deadlines. |
| `collection_budget` | `0.8` | Positive finite number, seconds; <= interval | Total acquisition window. Each IPC acquisition is capped at 60 seconds; normal operation uses 0.8 seconds. Expired work is discarded. Transport queue wait and individual query windows are recorded separately. | Restarts telemetry only; updates modem collection budget. |
| `profile` | `full` | `fast`, `standard`, `full`, `custom` | fast requests signal info/strength and WDS status/statistics. standard adds serving/cell/RF-band information and channel rates. full also requests home-network and supported Tx/Rx information. Fixed profiles request their groups in every sample. custom requests each group at its own query_* period. Each USB QMI response costs roughly one 32 ms interrupt poll, so fewer requests per sample shorten acquisition. During registration, necessary NAS observations replace unavailable bearer requests. Firmware support still limits availability. | Restarts telemetry only; updates modem query profile. |
| `query_signal` | `1.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. NAS Get Signal Info: LTE RSSI/RSRP/RSRQ/SNR and NR RSRP/SNR/RSRQ. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_cell` | `1.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. NAS Get Cell Location Info: serving NR cell (PCI, global ID, ARFCN, TAC, RSRP/RSRQ/SNR) and LTE intra/inter-frequency cells. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_txrx` | `1.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. NAS Get Tx/Rx Info for the current radio: per-chain Rx power/RSRP/ECIO/phase and Tx power. Skipped where firmware reports it unsupported. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_serving` | `5.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. NAS Get Serving System: registration and attach states, current PLMN, roaming and radio interfaces. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_bands` | `10.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. NAS Get RF Band Information: active bands, extended bands and bandwidths. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_strength` | `0.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. NAS Get Signal Strength: legacy strength, IO and EV-DO SINR. LTE/NR values come from query_signal. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_home` | `3600.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. NAS Get Home Network: home MCC/MNC and network name. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_wds_stats` | `1.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. WDS packet statistics: packet, byte, error, overflow and drop counters. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_wds_status` | `1.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. WDS packet service status. Connection health treats it as fresh for twice this period. Cannot be 0 in connect mode. Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `query_wds_rates` | `10.0` | Number, 0..86400 seconds; 0 disables | Used only with profile=custom. WDS channel rates reported by the modem (not measured traffic). Periods shorter than interval mean every sample; others are rounded to the nearest whole number of samples and staggered across samples. A group not requested in a sample is null there, never a repeated value; the sample's schedule records its period and last observation. | Restarts telemetry only; updates the modem query schedule. |
| `indications` | `off` | `off`, `shadow`, `on` | Modem-pushed QMI indications for WDS packet statistics, WDS bearer status and the NAS serving system (libqmi provides no complete indication for bands, channel rates, signal or cell information, which stay polled). on serves those groups from the latest indication instead of a request; an unsupported registration or a stale indication falls back to polling in that sample, and on-change values are confirmed by a request at least every 60 seconds. Served values keep their own receipt time and age in the collection report. shadow requests every group and records the age and differing fields of the latest indication, to validate firmware before enabling on. Registration always polls. | Restarts telemetry only; registers indications on the next sample. |

## [system]

| Option | Parser default | Type / accepted values | Meaning and prerequisites | Applying changes |
| --- | --- | --- | --- | --- |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | Detail of the supervisor and worker logs. The systemd unit starts without a flag, so this value applies; `--log-level` on the command line overrides it for one run. | Restarts the supervisor workers; no data session is reopened. |
| `log_file` | (empty) | Empty or an absolute path | Optional rotating file sink (2 MB, three backups) for the supervisor process log; the modem and web workers keep logging to the systemd journal. Empty keeps the journal only. The service user must be able to create the file. | Applies on the next configuration reconcile; no session is reopened. |

## [web]

| Option | Parser default | Type / accepted values | Meaning and prerequisites | Applying changes |
| --- | --- | --- | --- | --- |
| `enabled` | `false` | Boolean | Start/stop the web child. Turning it off removes browser access but preserves other objectives. Re-enable with the configurator CLI. | Starts/stops web only. |
| `host` | `127.0.0.1` | `auto` or a numeric IPv4/IPv6 listen address | 127.0.0.1/::1 restrict access to localhost. auto keeps localhost plus an eligible physical management LAN IPv4 (including a physical bridge), rechecks the address at `address_refresh` and excludes WWAN/Docker/veth. 0.0.0.0/:: explicitly request wildcard binding, cellular interface included; new installations write 0.0.0.0. Non-loopback access requires an account password. | Restarts web only; changes access addresses. |
| `port` | `8080` | Integer, 1..65535 | HTTP listen port. All auto-mode listeners share it. The port must be available; port 0 is not a configuration option. | Restarts web only; changes access URL. |
| `history_size` | `120` | Integer, 1..1000 samples | Bounded acquisition and writer histories. 120 samples is approximately two minutes at 1 Hz. Resource history is also bounded; its five-second period differs from acquisition. | Resizes histories and restarts web only. |
| `max_workers` | `8` | Integer, 1..32 | Concurrent HTTP requests (API and static assets) served by the web child. Lower values bound memory, higher values help several open dashboards. | Restarts web only. |
| `address_refresh` | `10.0` | Number, 5..600 seconds | Only with host=auto: how often the current management LAN address is rechecked and a listener is moved to a new address. | Restarts web only. |
| `poll_interval` | `2.0` | Number, 1..30 seconds | How often an open page asks for a new status snapshot. The footer states the effective cadence. | Restarts web only; open pages follow after their next reload. |
| `username` | `admin` | 1..64 ASCII letters/digits or `_ . @ -` | Web account name, case-sensitive. Use the installer/configurator to create or reset account credentials. | Restarts web; credential changes invalidate old sessions. |
| `password_hash` | (empty) | Empty or supported `pbkdf2_sha256$iterations$salt$digest` | Generated hash, never a plaintext password. Iterations 600000..2000000, 32 lowercase hexadecimal salt characters and 64 lowercase hexadecimal digest characters. Account passwords require at least 8 characters, at most 256 UTF-8 bytes and no control characters. An empty hash disables password login. | Restarts web; invalidates old account sessions. |
| `session_timeout` | `28800` | Integer, 300..604800 seconds | Authenticated session lifetime: 5 minutes to 7 days. The default 28800 is 8 hours. Sessions use monotonic expiry, HttpOnly/SameSite cookies and CSRF protection. | Restarts web; updates authentication lifetime. |

## Saving, applying and recovery

Use the web forms or `sudo qmi-supervisor-configure` for normal changes. The API sends partial patches, preserves hidden credentials and reports requested/applied revisions. CLI and API use the same configuration transaction lock. Invalid updates retain the last valid running configuration. Changes that affect connectivity can briefly interrupt the owned data session; telemetry-only and web-only changes do not stop that session.

The single systemd unit supervises a modem/telemetry pair per UE and one shared web child. Each child has its own heartbeat and restart counter. A failed writer cannot block another UE or its connection process. A modem restart can interrupt that UE's observations and data while its peers and the web remain available. Restarting/stopping the whole service affects every child but does not change saved objectives.

Connectivity fallback requires another working route to provide alternate access. It does not preserve established TCP sessions, alter foreign routes, or automatically change host-wide reverse-path filtering. See the routing diagnostics in INSTALLATION.md.

## Complete standalone examples

These examples disable web so they can be validated without embedding credentials. For a web installation, retain the account and secrets generated by the installer rather than replacing the entire installed file with an example.

### Connection only

```ini
# Managed by qmi-supervisor-configure; no hardware identity is required.
[modem]
device = auto
serial = 
apn = internet
plmn = auto
mode = connect

[network]
method = qmi
route_metric = 700
default_route = true
dns = auto
route_policy = fixed
connectivity_check = true
connectivity_targets = 1.1.1.1:443,8.8.8.8:53
connectivity_interval = 3.0
connectivity_timeout = 1.0
connectivity_failures = 2
connectivity_successes = 2

[recovery]
retry_initial = 2
retry_max = 60
registration_timeout = 180
operation_timeout = 20
poll_interval = 10

[telemetry]
enabled = false
interval = 1.0
directory = /var/lib/qmi-supervisor
csv = true
json = true
udp_host = 
udp_port = 5006
collector_host = 
collector_port = 5007
throughput = false
gps = false
gps_source = gpsd
queue_size = 64
flush_interval = 1.0
gps_max_age = 10.0
request_timeout = 1
collection_budget = 0.8
profile = full

[system]
log_level = INFO

[web]
enabled = false
host = 127.0.0.1
port = 8080
history_size = 120
username = admin
password_hash = 
session_timeout = 28800
max_workers = 8
address_refresh = 10.0
poll_interval = 2.0

```

### Monitoring only

```ini
# Managed by qmi-supervisor-configure; no hardware identity is required.
[modem]
device = auto
serial = 
apn = internet
plmn = auto
mode = monitor

[network]
method = qmi
route_metric = 700
default_route = true
dns = auto
route_policy = fixed
connectivity_check = true
connectivity_targets = 1.1.1.1:443,8.8.8.8:53
connectivity_interval = 3.0
connectivity_timeout = 1.0
connectivity_failures = 2
connectivity_successes = 2

[recovery]
retry_initial = 2
retry_max = 60
registration_timeout = 180
operation_timeout = 20
poll_interval = 10

[telemetry]
enabled = true
interval = 1.0
directory = /var/lib/qmi-supervisor
csv = true
json = true
udp_host = 
udp_port = 5006
collector_host = 
collector_port = 5007
throughput = false
gps = false
gps_source = gpsd
queue_size = 64
flush_interval = 1.0
gps_max_age = 10.0
request_timeout = 1
collection_budget = 0.8
profile = full

[system]
log_level = INFO

[web]
enabled = false
host = 127.0.0.1
port = 8080
history_size = 120
username = admin
password_hash = 
session_timeout = 28800
max_workers = 8
address_refresh = 10.0
poll_interval = 2.0

```

### Connection and monitoring

```ini
# Managed by qmi-supervisor-configure; no hardware identity is required.
[modem]
device = auto
serial = 
apn = internet
plmn = auto
mode = connect

[network]
method = qmi
route_metric = 700
default_route = true
dns = auto
route_policy = fixed
connectivity_check = true
connectivity_targets = 1.1.1.1:443,8.8.8.8:53
connectivity_interval = 3.0
connectivity_timeout = 1.0
connectivity_failures = 2
connectivity_successes = 2

[recovery]
retry_initial = 2
retry_max = 60
registration_timeout = 180
operation_timeout = 20
poll_interval = 10

[telemetry]
enabled = true
interval = 1.0
directory = /var/lib/qmi-supervisor
csv = true
json = true
udp_host = 
udp_port = 5006
collector_host = 
collector_port = 5007
throughput = false
gps = false
gps_source = gpsd
queue_size = 64
flush_interval = 1.0
gps_max_age = 10.0
request_timeout = 1
collection_budget = 0.8
profile = full

[system]
log_level = INFO

[web]
enabled = false
host = 127.0.0.1
port = 8080
history_size = 120
username = admin
password_hash = 
session_timeout = 28800
max_workers = 8
address_refresh = 10.0
poll_interval = 2.0

```

### Idle

```ini
# Managed by qmi-supervisor-configure; no hardware identity is required.
[modem]
device = auto
serial = 
apn = internet
plmn = auto
mode = monitor

[network]
method = qmi
route_metric = 700
default_route = true
dns = auto
route_policy = fixed
connectivity_check = true
connectivity_targets = 1.1.1.1:443,8.8.8.8:53
connectivity_interval = 3.0
connectivity_timeout = 1.0
connectivity_failures = 2
connectivity_successes = 2

[recovery]
retry_initial = 2
retry_max = 60
registration_timeout = 180
operation_timeout = 20
poll_interval = 10

[telemetry]
enabled = false
interval = 1.0
directory = /var/lib/qmi-supervisor
csv = true
json = true
udp_host = 
udp_port = 5006
collector_host = 
collector_port = 5007
throughput = false
gps = false
gps_source = gpsd
queue_size = 64
flush_interval = 1.0
gps_max_age = 10.0
request_timeout = 1
collection_budget = 0.8
profile = full

[system]
log_level = INFO

[web]
enabled = false
host = 127.0.0.1
port = 8080
history_size = 120
username = admin
password_hash = 
session_timeout = 28800
max_workers = 8
address_refresh = 10.0
poll_interval = 2.0

```

## Validate without touching hardware

```bash
/usr/bin/python3 qmi_supervisor.py --config /path/to/settings.ini --check-config
```

The command requires neither a modem nor root and redacts stored secrets. A syntactically valid configuration still requires compatible QMI firmware, correct SIM provisioning, usable coverage, operational network dependencies and writable storage.
