# Installation and operations

This guide covers Debian/Ubuntu deployment, including Mochabin. See [README.md](README.md) for the quick start and [CONFIGURATION.md](CONFIGURATION.md) for every INI option, default and validation rule.

## Requirements

- Debian 12+ or Ubuntu 22.04+ with systemd and Python 3.10+.
- A compatible USB QMI function exposed by `qmi_wwan` as `/dev/cdc-wdm*` plus a network interface. MBIM, ECM and RNDIS-only devices are not managed by this implementation.
- A provisioned SIM, coverage and the operator APN. PIN-free SIMs need no unlock step. A required PIN/PUK is reported; recovery never guesses or consumes PIN attempts.
- Root for installation and modem/network management. Configuration validation and device enumeration can run without root, subject to device permissions.
- Node 22+ only on the machine building the optional frontend. Node, npm, React SSR and a Next server are not runtime requirements on the Mochabin.

Automatic discovery supports several USB modems simultaneously, assigning exclusive ownership and independent connection/capture processes. Additional devices receive saved profiles. A source-compatible modem may still expose fewer measurements or no QMI LOC GPS.

The modem firmware must also permit the network's radio technology. QMI Supervisor selects the configured PLMN/APN but does not automatically remove vendor RAT, SA/NSA or band restrictions. During the physical RM520N-GL validation, a saved LTE-only mode and disabled SA prevented NR registration even with a ready SIM. The Quectel-specific `AT+QNWPREFCFG="nr5g_disable_mode"` query returned `1`; enabling SA with value `0` restored its NR band list. Its supported UMTS/LTE/NR preference also had to be saved rather than applied only until the next radio power cycle. Apply vendor-specific changes only to the intended idle modem, then confirm registration and effective APN. See the [Quectel command reference](https://www.quectel.com/content/uploads/2024/05/Quectel_RG50xQRM5xxQ_Series_AT_Commands_Manual_V1.2.pdf) and the [physical acceptance report](doc/benchmarks/multimodem-validation.md).

## Build and transfer the application

On the development PC:

```bash
git clone https://github.com/ANTS-research-group/qmi-supervisor.git
cd qmi-supervisor
npm --prefix frontend ci
npm --prefix frontend run build
```

The build produces `frontend/out`. Transfer that export together with the Python application and deployment tools. A Git clone alone does not include the ignored export or unpublished local development changes.

Example transfer of the current working tree:

```bash
rsync -a --exclude=.git --exclude=.venv --exclude=node_modules \
  --exclude=.next --exclude=files --exclude=qmi-supervisor.ini \
  ./ user@MOCHA:/tmp/qmi-supervisor/
```

Exclude any additional local capture directories or private configuration/backups from your own working tree. Do not transfer a PC virtual environment or `node_modules` to an ARM device. The static frontend itself is architecture-independent.

For headless deployment, the frontend export and Node are unnecessary; pass `--web off` explicitly on a fresh installation.

## First installation through the web

On the target, from the transferred application directory:

```bash
sudo ./tools/deployment/install.sh --web on
```

The installer asks for an account password using hidden terminal input. No default password is installed. Choose at least 8 characters and at most 256 UTF-8 bytes. Plaintext passwords are not written into the INI or command arguments. For an unattended installation, pipe the password into `--web-password-stdin`.

A new installation listens on every interface (`web.host = 0.0.0.0`), including the modem's once it is connected, so the account password is what protects the web. Use `--web-host 127.0.0.1` for localhost only, or `--web-host auto` for localhost plus the management LAN without the cellular interface.

On a fresh machine the connection and monitoring objectives are both stopped. Open `http://ADDRESS:8080` with any address of the device, sign in, prepare the saved network/capture settings, and enable either objective from the modem's Summary (or from Fleet with several modems).

Defaults proposed for a new installation are device `auto`, APN `internet`, PLMN `auto`, IPv4 via QMI and sampling every second. Existing installations retain their settings and credentials. Empty PLMN in an existing INI preserves modem selection; it is not equivalent to `auto`.

### Preparing competing managers

QMI Supervisor must be the only writer managing the selected modem and its host interface. The default installer policy excludes QMI USB network interfaces from NetworkManager through a `qmi_wwan` udev rule; Ethernet remains managed. This rule covers QMI interfaces, not only one USB serial.

ModemManager is kept unless explicitly requested otherwise. On a dedicated cellular-controller Mochabin, review the plan and prepare exclusive management with:

```bash
sudo ./tools/deployment/install.sh --web on \
  --modem-manager disable --dry-run
sudo ./tools/deployment/install.sh --web on \
  --modem-manager disable
```

`--modem-manager disable` stops, disables and masks ModemManager globally, including D-Bus activation. Do not choose it for a host where another modem must remain managed by ModemManager. For passive observation alongside another manager, use `--network-manager keep` and leave autonomous connection disabled.

Detected host networking impacts are shown before mutation. Interactive installation asks before applying them. After reviewing the dry run, unattended deployment can use `--accept-network-changes`. This acknowledges the reported policy; it does not authorise arbitrary runtime host-wide changes.

The web does not install packages or silently stop other modem managers. An activation conflict is reported with diagnostics; resolve host preparation through the installer/configurator.

## Packages, files and processes

APT installs only missing packages and records the transaction for uninstall:

| Package | Purpose |
| --- | --- |
| `python3` | Python 3.10+ runtime. |
| `python3-gi`, `gir1.2-qmi-1.0` | GI bindings and libqmi introspection; required, the modem process uses libqmi through them. Where libqmi is built with QRTR support (Debian 12+, Ubuntu 24.04+; not Ubuntu 22.04), its typelib requires `gir1.2-qrtr-1.0` and `gir1.2-qmi-1.0` pulls it as a dependency. QMI Supervisor does not use QRTR directly, but without it the libqmi bindings fail to load. |
| `libqmi-utils`, `libqmi-proxy` | QMI utilities and shared transport proxy. |
| `iproute2`, `udev`, `kmod` | Host interface commands, discovery/rules and driver loading. |
| `udhcpc` | DHCP mode; also prepared for future web configuration. |
| `resolvconf` | Installed when required by DNS policy and no operational resolved backend is available. |
| `gpsd` | Optional external GPS source when selected; no need for modem QMI LOC. |

Dependency installation uses APT, not pip. `--skip-packages` requires dependencies to be provisioned already. The shell bootstrap can install Python on a supported system; dry runs and diagnostics do not install it.

| Location | Purpose |
| --- | --- |
| `/opt/qmi-supervisor` | Deployed application, static export, root reference documents and license. |
| `/etc/qmi-supervisor.ini` | Persistent settings and hashed/private credentials, mode 0600. |
| `/etc/systemd/system/qmi-supervisor.service` | The single public systemd unit. |
| `/usr/local/sbin/qmi-supervisor-configure` | Configuration/maintenance wrapper. |
| `/usr/local/sbin/qmi-supervisor-uninstall` | Removal wrapper. |
| `/var/lib/qmi-supervisor` | Default capture root. |
| `/var/lib/qmi-supervisor-install` | Installation receipt, package inventory and rollback information. |
| `/run/qmi-supervisor` | Private sockets, heartbeats, live status and session state. |
| `/run/qmi-supervisor/ownership/<key>/` | Per-physical-UE recovery journals and private cleanup configuration. Legacy `/run/qmi-supervisor-state*.json` journals are migrated automatically. |
| `/etc/udev/rules.d/85-qmi-supervisor.rules` | Optional QMI-only NetworkManager exclusion. |

The unit starts one lightweight process manager. It executes the modem, telemetry and web children only when required, watches heartbeats and restarts failed children independently. Only the modem child imports GI and owns QMI clients/bearers. `qmi-proxy` remains libqmi transport; there are no separate telemetry/web systemd units.

With both objectives stopped, the modem and telemetry children are absent. Disabling web removes its child and additional resource sampling. Normal service stop/restart affects all children but does not change saved objectives. A failed child is visible in component state even while the systemd unit remains active.

## CLI reference

Use web configuration for routine operation. CLI is retained for headless systems, recovery and provisioning:

```bash
sudo qmi-supervisor-configure --connection on --telemetry off \
  --plmn 99910 --apn tenten --method qmi --web off
sudo qmi-supervisor-configure --connection off --telemetry on --web off
sudo qmi-supervisor-configure --connection on --telemetry on
sudo qmi-supervisor-configure --connection off --telemetry off
```

Connection-off closes owned data resources. Monitoring-only does not select/register a network or configure IPv4. The connection can remain up with monitoring disabled, without generating datasets.

| Option | Meaning |
| --- | --- |
| `--connection on/off` | Persistent autonomous-connection objective. |
| `--mode connect/monitor` | Compatible legacy alternative; mutually exclusive with `--connection`. |
| `--telemetry on/off` | Persistent acquisition/storage/forwarding objective. |
| `--apn APN`, `--plmn auto/NUMERIC` | Saved network settings; quote `''` to preserve modem PLMN selection. |
| `--auto-modem` | Clear device/serial restrictions. |
| `--device /dev/cdc-wdmN`, `--serial SERIAL` | Explicit selection; mutually exclusive with each other and `--auto-modem` in the CLI. |
| `--method qmi/dhcp` | IPv4 configuration method. |
| `--network-profile primary/backup/no-default` | Dynamic cellular priority or no cellular default. |
| `--route-metric N` | Fixed metric, or primary ceiling/backup floor with those profiles. |
| `--dns auto/none/resolvectl/resolvconf` | DNS policy. |
| `--connectivity-check on/off` | Enable/explicitly bypass WWAN reachability verification. |
| `--connectivity-targets IPv4:port,...` | One to four reachable TCP targets; private-network targets may be required. |
| `--connectivity-interval N`, `--connectivity-timeout N` | Check period and deadline in seconds. |
| `--connectivity-failures N`, `--connectivity-successes N` | Consecutive rounds for fallback/recovery. |
| `--sample-interval N`, `--telemetry-profile fast/standard/full/custom` | Acquisition period and queries. |
| `--telemetry-indications off/shadow/on` | Modem-pushed WDS statistics/status and NAS serving system; shadow validates them against polling. |
| `--telemetry-query-<group> SECONDS` | Per-group refresh period for the custom profile (`signal`, `cell`, `txrx`, `serving`, `bands`, `strength`, `home`, `wds-stats`, `wds-status`, `wds-rates`; 0 disables). |
| `--collection-budget N` | Acquisition deadline, in seconds, no longer than the interval. |
| `--throughput on/off` | Actual selected-interface traffic counters/rates. |
| `--gps on/off`, `--gps-source gpsd/modem` | Optional external or QMI LOC positioning. |
| `--collector-host HOST` | Optional UDP collector destination. |
| `--web on/off`, `--web-host ADDRESS`, `--web-port N` | Optional web process and binding; a new installation listens on `0.0.0.0`. |
| `--web-user NAME` | Account name. |
| `--set-web-password` | Set/reset a password with two hidden prompts. |
| `--web-password-stdin` | Read one password line from stdin; do not use plaintext shell arguments. The new hash is saved only when the run ends with `Web password updated`; after `Setup failed` the previous password remains. |
| `--modem-manager keep/disable` | Explicit host-wide ModemManager policy. |
| `--network-manager keep/exclude-qmi` | Retain management or exclude QMI network functions. |
| `--accept-network-changes` | Acknowledge reported networking impacts after review. |
| `--interactive` | Terminal configuration wizard. |
| `--dry-run` | Plan without applying changes or installing packages. |
| `--doctor` | Read-only dependency, USB, manager, resolver and network diagnostics. |
| `--skip-packages` | Offline/manually prepared dependencies. |
| `--start`, `--no-start` | Explicit service startup policy. Install normally starts; configure preserves whether the unit was active. Ordinary configuration applies live to the required components; explicit startup, host-policy changes and legacy migrations use the full service transaction. |

Some advanced parameters have no dedicated CLI flag. Use the web forms or edit the INI against CONFIGURATION.md and validate it before applying. Unknown INI keys are rejected.

### Headless and offline examples

```bash
sudo ./tools/deployment/install.sh --web off --connection on --telemetry on \
  --plmn auto --apn internet --modem-manager disable
sudo ./tools/deployment/install.sh --skip-packages --web off \
  --connection off --telemetry on --network-manager keep
```

Offline installation does not fetch Python, QMI, DHCP, resolver or GPS packages. Prepare the required Debian/Ubuntu packages for the target architecture in advance. A web install also requires an already built static export.

## Routing and management access

Keep management on Ethernet/LAN. The default `web.host=0.0.0.0` also answers on the cellular interface; `web.host=auto` follows a physical management address independently of cellular default-route preference and keeps localhost available. Theme and English/Spanish choices are browser-local and work before login.

`fixed` uses an exact owned metric (default 700). `primary` selects a lower metric than foreign main-table IPv4 defaults, using 100 as the CLI ceiling. `backup` selects higher, using 700 as the CLI floor. Explicit `--route-metric` customises the reference. A foreign metric 0 can make strict primary selection impossible; numeric overflow for backup is also reported. Foreign defaults are not deleted or replaced.

With verification enabled, cellular priority/DNS are conservative while checking, failed or stale. The default targets are `1.1.1.1:443,8.8.8.8:53`, checked every 3 seconds with a 1-second round deadline and two consecutive successes/failures. Either TCP target succeeding verifies a round. For an isolated core, configure a reachable service in that network. `ONLINE` confirms local bearer/IP readiness; use verification state to assess actual target reachability.

The modem reports a /30 around its own address, so other UEs of the same core (for example `192.168.4.6` seen from `192.168.4.10`) follow the host default route, usually the LAN, unless the modem is primary. The default `network.routes = auto` adds the /24 around the modem address through the modem, unless it overlaps another interface's network, which is then reported as a warning to resolve by hand; list other pools explicitly, for example `network.routes = 10.45.0.0/16`. Every UE host needs it, or replies leave through that host's LAN.

Fallback requires an alternate working route. It cannot preserve existing TCP sessions or repair upstream core/N6 routing. Strict `rp_filter=1` can discard replies arriving over a backup interface; the application does not modify global sysctls. Diagnose host policy before changing it.

## Captures, traffic and GPS

The telemetry worker plans requests with a monotonic clock, skips expired ticks, and writes through bounded queues. UTC timestamps correlate recordings; per-query request/response windows describe host observation, not the modem's internal radio measurement time.

Default acquisition is 1 Hz with a 0.8-second QMI budget. Unsupported, missing and late values remain unavailable. CSV grows daily; JSON stores the latest sample. There is no retention policy or automatic capture deletion. Preserve adequate storage and use a new directory when an existing CSV schema differs.

Traffic uses selected-interface sysfs counters and actual elapsed time, in KiB/Kibit/s despite legacy field-name suffixes. First readings and resets produce gaps. QMI channel rates are capacity observations, not measured interface throughput.

GPS `modem` requires QMI LOC support, suitable firmware/antenna and a valid satellite fix. It can run without a data bearer. GPS `gpsd` requires an externally configured GPSd receiver. Unsupported/no-fix/stale states remain distinct. Enabling GPS does not guarantee that a particular modem supports it.

## Verification and diagnostics

```bash
systemctl status qmi-supervisor --no-pager -l
journalctl -u qmi-supervisor -f
sudo qmi-supervisor-configure --doctor
/usr/bin/python3 qmi_supervisor.py --list-devices
/usr/bin/python3 qmi_supervisor.py --config /path/to/settings.ini --check-config
ip -4 address show dev wwan0
ip -4 route show
```

Replace example device/interface names with discovery results. The UI exposes requested/applied objectives, individual heartbeats, retry/error states and bounded acquisition/writer histories. Opening pages does not query the modem. A web response or CPU reading does not refresh modem liveness.

For a data-path test, bind traffic to the cellular interface and use an authorised reachable target. A successful unbound request may have used Ethernet. PIN errors require deliberate operator action; no repeated automatic unlock attempts occur.

An inherited WDS session is reused only when its reported APN can be verified. An unknown/mismatched existing APN is rejected rather than claimed as a successful configuration. Do not run mutating qmicli commands concurrently with the supervisor; foreign data sessions are not stopped automatically.

Recover a lost web password with `sudo qmi-supervisor-configure --set-web-password`. Enable a disabled web process with `sudo qmi-supervisor-configure --web on`. Changing credentials invalidates existing account sessions. Browser and API access use the same account login.

## Multiple modems and independent APNs

Connect the modems and enumerate them without reading private credentials:

```bash
python3 qmi_supervisor.py --list-devices
```

Automatic mode manages all compatible physical QMI modems. The first modem uses the legacy default settings; additional modems receive persistent `auto-...` profiles and initially use the proposed settings and objectives. Saved additional profiles have independent APNs, PLMNs, connection/monitoring switches, network policy and telemetry. Choose the UE in the sidebar modem list before changing its configuration; Fleet switches the objectives of any modem directly. An unplugged modem keeps its saved profile but leaves the fleet and the sidebar until its hardware returns; with a single modem left the home page goes back to that modem's summary. Drafts and histories are kept per modem; web settings and login are shared.

For a fresh headless installation with explicitly named profiles, prepare both selectors before starting discovery. Use the USB serial printed by enumeration:

```bash
sudo ./tools/deployment/install.sh --no-start --web off --serial first-usb-serial --apn internet --plmn 99910 --connection on --telemetry on
sudo qmi-supervisor-configure --no-start --modem-profile private --serial second-usb-serial --apn tenten --plmn 99910 --connection on --telemetry on
sudo systemctl start qmi-supervisor
```

Replace the serials, network and APNs with values authorised by your operator. A modem profile is not an additional bearer on the same physical modem: this implementation uses one independently managed data APN per physical UE. IMS/emergency bearers remain firmware-owned. The platform does not bond links or split application traffic automatically.

If automatic discovery has already saved a device as `auto-...`, edit that existing profile in the web interface or use its ID with `--modem-profile`. `sudo qmi-supervisor-configure --doctor` reports the configured IDs. Creating a second selector for the same physical modem is a conflict; it does not rename or transfer the existing profile.

The installer retains every profile during updates and prepares the union of their package dependencies. `--modem-profile <id>` scopes ordinary CLI edits; web account/listen settings remain platform-wide. `--device` on the supervisor executable cannot override an INI containing multiple named profiles. Prefer USB serial selection over control-port numbering. Duplicated or missing serials require a path selector and may need correction after USB renumbering.

There is still one service. Its parent owns the global instance/legacy lock, each modem worker additionally locks its physical USB device, and every profile has private sockets, heartbeats and status files under `/run/qmi-supervisor/modems/<id>/`. Recovery journals live under `/run/qmi-supervisor/ownership/<key>/`, keyed by physical USB topology and modem identity rather than the editable profile name. Legacy default and named-profile journals migrate before a worker starts. A duplicate legacy identity without enough topology information blocks adoption instead of guessing. The optional web worker stays at the platform root. Do not edit the generated runtime INIs: use the main INI, CLI or web.

If a previous worker died while owning a data session, turning connection off keeps a temporary modem worker for ownership-checked cleanup. It never prepares a new APN or selects a network. With monitoring also off it stops after the owned checkpoint is cleared. If the assigned modem is absent or cleanup cannot be confirmed, the requested connection remains off while its release is reported as pending; other modems continue independently.

Changing a selector or moving a UE between profiles does not transfer its old bearer handle to another physical modem. A stopped owner's journal remains attached to the original UE. If that UE is still present but no longer assigned, an internal cleanup-only process releases its owned resources; it does not capture data or change APN/PLMN settings. Cleanup and normal workers are mutually exclusive, including while an old process cannot be reaped. The API exposes these retired-owner operations in `ownership_cleanup`. Missing original hardware leaves its journal intact for a later reconnect. Uninstall refuses to erase unresolved physical ownership journals.

If a unique serial identifies the same UE on a different USB port after a failure, an internal worker retires only the previous host IP resources. It does not issue QMI requests or reuse the old transport's CID/handle. The new modem worker waits for successful cleanup and process reaping. Missing or duplicate serials do not prove relocation and leave ownership intact. A pending persistent attach-profile transaction can move to the new port only with connection management enabled; current USB enumeration and firmware profile checks are required before registration is completed. Monitoring-only never applies that transaction.

Very old recovery files may lack USB-topology provenance. After migration their old CIDs are not readopted on the basis of a coincidentally equal USB device number. Host cleanup is ownership checked; the retired legacy evidence is retained as `legacy-context.json` in that UE's private ownership directory. Fresh activation still verifies its effective APN where firmware exposes it; an unrelated bearer is not stopped.

When using `resolvconf`, each connection owns a private DNS record identifier saved in its recovery journal. Cleaning up an old `wwan0` therefore cannot delete another connection's DNS record after Linux reuses that interface name. Legacy journals retain their original tag for recovery. Automatic DNS keeps an active resolver backend; an explicit backend change revokes its owned record first.

Replacing a modem at the same USB port, or re-enumerating the same modem, invalidates the previous worker assignment. Both workers are reaped before replacement. The backend pins its opened USB enumeration and checks discovery again after opening; checkpoints never relabel an old bearer with a new enumeration. A changed transport is closed without sending its former bearer/GPS stop or firmware CID releases. Local clients are still unregistered. If bearer stop is unconfirmed on the original transport, its firmware WDS CID and recovery journal are retained while other service clients are released. Repeated close calls do not resend the old handle.

Named-profile datasets are written under `<directory>/profiles/<id>/modem_<identity>/`; default dataset paths and CSV schemas are preserved. Queues, sample/write IDs, receiver counters and one-second schedules are independent. Account for CPU, USB bandwidth and storage capacity per active modem. A slow or disconnected UE must not be interpreted as a gap in its peer's dataset.

Route metrics are coordinated across cellular peers. Equal requested metrics get stable distinct effective metrics, visible in the network state. Verification failure demotes only the affected modem; unrelated Ethernet routes are retained. This does not guarantee that every private network accepts the same APN or that an ONLINE bearer reaches your chosen TCP targets.

Route selection reads each peer's current ownership checkpoint rather than its delayed web/heartbeat snapshot. A shared, bounded route-update lock covers kernel inspection, route application and checkpoint publication. Only exact journaled tuples are excluded from foreign routes; another route on a managed interface remains protected. If coordination cannot be confirmed, diagnostics report the failure instead of guessing ownership. This coordination adds no modem queries.

Authenticated API clients can use `/api/status?modem=<id>` and `/api/config?modem=<id>`. Status includes the modem inventory and the selected UE's bounded histories. Configuration writes remain partial and use the platform-wide revision in `If-Match`, preventing a concurrent edit to any profile from being overwritten. Unqualified endpoints refer to the default modem and remain compatible with existing clients.

## Update, rollback and removal

Build the new frontend export on the PC, transfer the complete application, then run:

```bash
sudo ./tools/deployment/install.sh --skip-packages
```

Existing configuration, private credentials, datasets and saved objectives are retained. Remove `--skip-packages` when dependencies must change. Deployment validates the staged application/export, saves previous application/configuration, stops the old service and starts the new architecture under the same unit. A whole-service update briefly interrupts all components; later telemetry/web configuration changes are isolated.

On deployment failure the installer restores tracked application/configuration/unit/host changes. Review the retained backup locations printed by the installer. Runtime child restarts do not roll back saved user intent.

If a failed update created physical ownership journals that the restored application cannot understand, rollback restores files but leaves the service stopped, preserving recovery data and the failed application backup. Reinstall a version supporting physical ownership recovery before starting the service; an older binary must not silently ignore these journals. With no unresolved journals, normal automatic restart after rollback is retained.

```bash
sudo qmi-supervisor-uninstall --dry-run
sudo qmi-supervisor-uninstall
sudo qmi-supervisor-uninstall --purge
```

Normal removal preserves datasets, backups and APT packages. Purge additionally removes tracked installer-owned data and introduced packages, without broad `autoremove`. Only recorded host policy changes are restored. Modem provisioning profiles may persist; removal does not erase SIM or arbitrary modem configuration.

## Development checks

```bash
/usr/bin/python3 -m unittest discover -s tests -q
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:browser
/usr/bin/python3 tools/telemetry/fleet_soak.py --seconds 3600 --modems 2
/usr/bin/python3 tools/telemetry/platform_profile.py --seconds 30 --modems 2
# Optional compatibility probe for one isolated worker pair:
/usr/bin/python3 tools/telemetry/platform_soak.py --seconds 180
```

Browser checks require Chrome/Chromium and local sockets. The fleet soak and objective resource matrix use synthetic UEs with the actual fleet parent, executable processes, IPC and writers; they do not touch a modem or host routes. The resource matrix includes every child and reports per-role CPU/RSS totals, with absent/failed readings unavailable instead of invented zeros. Its web cases enable the server without browser requests; QMI/proxy work and frontend rendering are excluded. `platform_soak.py` is a narrower single-worker-pair compatibility probe. Hardware timing/storage capacity must additionally be verified on the target Mochabin. See [timing details](doc/metrics-precision.md) and the [multi-modem validation report](doc/benchmarks/multimodem-validation.md).
