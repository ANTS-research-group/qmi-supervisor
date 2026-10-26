# Multimodem validation — 2026-10-22

## Scope

The production entry point now uses a fleet manager under the existing single systemd unit. Each physical UE receives its own modem/telemetry process pair, configuration snapshot, private IPC, ownership checkpoint, acquisition schedule, storage queues and histories. One web process and account are shared. Legacy unqualified INI sections and CSV columns are retained.

Automatic additional devices are materialised as independent profiles. Named selectors reserve hardware before automatic discovery. A physical USB lock supplements the inherited platform lock, preventing two workers from opening the same UE even if an assignment is duplicated. Route allocation excludes only exact recorded peer-owned routes and keeps management routes outside platform ownership.

## Automated evidence

- A complete Python suite recorded 586 passing tests. Subsequent deployment/API/fleet/route checks recorded 123 passing tests, with an additional physical-lock fault test and five coordinated-route cases passing.
- The 57 frontend unit tests and TypeScript check passed; the static Next export built successfully.
- Browser acceptance passed in an isolated Chrome instance. It covered two modem selections, independent APN drafts, scoped saves and objective switches, unchanged peer drafts, mobile width, existing themes/languages/authentication and navigation.
- Executable synthetic workers verified independent APNs, per-UE histories, telemetry-worker crash recovery, monitoring-off transitions, USB removal and preservation of the other modem and shared web process.
- Configuration/API tests cover named-section validation, round trips, global revision conflicts, preservation of credentials/other profiles, isolated histories and rejection of invalid profile paths.

## Sustained acquisition

A 180.90-second synthetic run exercised the actual fleet parent, executable workers, Unix IPC, writers and web publisher with two UEs at 1 Hz. One UE acquired/persisted 181 samples and the other 180 because their start times differ. There were no storage drops/errors or worker restarts. Both public histories retained 120 consecutive actual sample IDs; the per-UE CSV IDs were sequential.

Mean recorded periods were 999.998 ms for both UEs. The largest scheduling lag was 1.081 ms. The summed process CPU averaged 4.88% of one core; maximum summed RSS was 132.02 MiB. These are development-PC synthetic measurements: they exclude real QMI, registration and host-network latency, and summed RSS can count shared pages more than once. They do not establish resource use on a Mochabin or guarantee hard real-time acquisition.

See the [machine-readable audit](multimodem-soak.json), including runtime source hashes, per-modem statistics, IDs and timing. Reproduce with:

```bash
/usr/bin/python3 tools/telemetry/fleet_soak.py --seconds 180 --modems 2
```

The synthetic tool uses isolated temporary files, sockets and fake UEs; it does not open a real modem or alter host routes.

## Recovery and view hardening

The follow-up complete suite passed 610 Python tests. The 57 frontend unit tests, TypeScript check, static build and browser acceptance passed again. Browser acceptance now reproduces and rejects a late response from an earlier A → B → A modem selection, and shows errors/retry state while a requested disconnection still awaits owned cleanup.

New regressions cover profile-specific runtime write failures without stopping peers/web, continued supervision of the last valid configuration, concurrent hotplug/configuration edits, unreaped-owner retry backoff, shared runtime publication errors, invalid-INI validation cadence, worker incarnation checks independent of PID reuse, truthful inactive/removed states and scoped installer/DNS checks. A retained owned connection can run an explicitly requested cleanup worker in monitor mode; the real GLib actor loop with a hardware adapter double verifies that this does not prepare APN or registration.

A second 180.85-second run recorded 181/180 samples, all persisted, with zero skipped intervals both at startup and within capture. Both histories retained 120 consecutive points. Seventeen authenticated HTTP observations per UE verified device identity and unmixed, bounded histories; there were no API errors. Runtime/telemetry/network source hashes match the tree used for that run. Mean periods were 999.997 ms and maximum planning lag was 1.007 ms. Tests ran concurrently, so the measured CPU/RSS are workload-specific observations rather than an isolated comparison with the earlier run. See [follow-up audit](multimodem-hardening-soak.json).

## Physical ownership handoff

Recovery journals now follow canonical USB topology and modem identity under `ownership/<key>/`, independently of editable profile IDs. The parent migrates legacy connection/attach journal pairs before starting their owner. A small transaction retains both snapshots and proven source attribution, so a partial move can resume after a crash or profile rebind. Conflicting or ambiguous records remain intact and block adoption. The hardware backend also rejects an explicitly different USB topology or identity without retiring its journal; re-enumeration on the same UE still avoids reusing stale CIDs.

Regressions exercise profile-to-profile movement, replacement while the old handle is unconfirmed, duplicate serials, conflicting journal pairs, interrupted pair migration with changed bindings and recovery from the staged snapshot. A present, unassigned old UE runs a cleanup-only actor with monitoring disabled and no APN/PLMN provisioning. Cleanup is reaped before normal reassignment; unresolved journals and migration transactions prevent uninstall. The shared/scoped API retains `ownership_cleanup` status. These cases use real parent/state code and process or hardware adapter doubles; they do not establish physical dual-modem connectivity.

The complete ownership suite at that stage passed 622 Python tests in 161.0 seconds. Focused ownership/recovery/uninstall checks passed 62 tests, and five scoped HTTP API checks passed after adding cleanup-status propagation. The one-hour captures are reported below. A deliberately interrupted preliminary soak is not counted as a completed sustained test.

The subsequent port-relocation suite passed 634 Python tests in 161.9 seconds. Recovery regressions verify unique serials versus ambiguous identity, retirement of only old host resources without importing GI or issuing QMI, proof changes, failed cleanup, inherited/physical ownership leases, waiting for the old actor to be reaped, continued peer operation, migration of persistent attach intent only in connection mode, interrupted migration and legacy upgrade with known old topology. Legacy records with no topology are marked unverified; their CIDs are not reused even when the device number matches, and their retired evidence is archived. A follow-up 66-test deployment suite covers rollback compatibility: an old application is not restarted over journals it cannot recover.

A 180.90-second run of the updated runtime captured/persisted 181/180 samples with no skipped intervals, storage failures, drops or child restarts. Both histories retained 120 consecutive sample IDs. Seventeen authenticated scoped HTTP observations per UE reported no errors. Recorded mean periods were 999.9998/999.9980 ms, with maximum planning lag 1.049 ms. The [relocation runtime audit](multimodem-relocation-soak.json) includes source hashes matching the tested runtime/device/telemetry/network files. CPU/RSS were observed while other isolated tests and a longer capture ran concurrently, so they are not an isolated platform-resource comparison.

The [one-hour baseline](multimodem-hour-baseline.json) completed in 3601.03 seconds with 3601/3600 samples, all persisted. Independently rereading every CSV confirmed IDs 1..N, one run per UE, no duplicates/gaps and zero skipped intervals. Both acquisition and writer histories stayed within 120 points throughout 359 observations. All 718 authenticated scoped HTTP checks passed, with no modem/telemetry/web restarts. Mean periods were 999.99989/999.99994 ms and maximum planning lag was 1.257 ms.

That run started before relocation, automatic-profile reassignment and DNS changes; its source manifest records those differences and unchanged telemetry modules. Its duration is baseline acquisition evidence, not a one-hour validation of every later ownership implementation. The updated-runtime short captures above are distinct evidence.

The baseline hour's resource observer retained all 359 readings in a list inside the measured parent. The decoded list alone occupies about 3.7 MB; parent RSS includes this growing test instrumentation. Accordingly, the recorded 135–146 MiB summed RSS cannot isolate the platform's memory trend. The harness now streams observations to JSONL and materialises the result only after capture and observation stop; observer I/O failures fail the audit. A 60-second integration run passed with all 61/60 samples persisted, no gaps and five streamed observations.

The corrected [streamed-observer hour](multimodem-hour-streamed.json) completed in 3600.55 seconds. Both UEs persisted every sample (3601/3600), with sequential CSV IDs, zero skipped intervals, storage errors/drops or worker restarts. All 718 scoped HTTP observations passed; acquisition and writer histories remained bounded at 120. Mean periods were 999.99993/999.99990 ms; maximum planning lag was 1.237 ms. After 180 seconds, summed RSS ranged from 134.46 to 141.85 MiB. The parent's early/final steady medians were 34.65/37.51 MiB, so this run alone does not prove flat memory use. This capture began before later same-port identity/USB-generation changes; its manifest records that scope and unchanged acquisition modules.

A separate [parent heap diagnostic](multimodem-heartbeat-memory.json) used full-GC snapshots at 180 and 480 seconds, without HTTP clients or an accumulating observer list. Before heartbeat-path caching, live traced allocations increased from 3164971 to 5112551 bytes; after caching, from 3165958 to 3541303 bytes. The earlier single 1310744-byte increase attributed to pathlib string interning was absent after caching. Residual JSON allocation growth remains; these two-point synthetic diagnostics are not proof of a long-term memory bound or target-device capacity.

## Assignment and DNS ownership follow-up

A three-device regression reproduced an automatically named profile being rebound to another UE while its original hardware remained present. The old identifier collision previously left the original device unassigned. Automatic allocation now reserves saved intent and gives the displaced UE a distinct stable identifier; a runtime regression verifies materialisation, preserved physical ownership journals and an unchanged peer process. The resulting complete suite passed 638 tests.

A [91.19-second three-UE run](multimodem-three-ue-soak.json) captured/persisted 91/91/90 samples, with sequential IDs, zero skipped intervals, no persistence failures/drops and no actor restarts. All scoped HTTP checks passed. This was run before the subsequent DNS change; it does not exercise real QMI, routing or DNS.

An additional regression reproduced `resolvconf` DNS loss after another modem reused an old interface name. New DNS records have private per-connection identifiers, checkpointed before application and retained across partial failures. Recovery deletes only the recorded identifier; old checkpoints retain their legacy tag. Automatic DNS keeps its active backend rather than leaking a former record when resolver availability changes, and explicit backend changes revoke the old record first. All 87 isolated network tests passed after this change. The final full suite passed **643 Python tests in 162.2 seconds**, including the latest assignment, rollback and DNS changes.

## Completion evidence by feature

| Requirement | Current automated evidence | Hardware scope |
|---|---|---|
| Independent per-modem APN/settings/objectives | Fleet/configuration/API tests; executable per-UE actors; two/three-UE captures | Simultaneous effective `tenten`/`internet` bearers; four objective modes on the Quectel |
| Discovery, replacement and exclusive ownership | Assignment, ownership, relocation, modem actor and recovery regressions | Both physical UEs discovered; owner crash recovery. Physical USB removal/relocation remains covered by adapter/process regressions |
| Per-UE acquisition, files, forwarding, GPS/traffic context | Sampling/pipeline/history/resource tests and synthetic executable captures | Real NAS/WDS, separate CSVs, WWAN traffic and timing; no satellite fix claimed |
| Shared authenticated web and scoped views/drafts | Fleet API tests; frontend unit tests and isolated browser acceptance | Real scoped API/configuration and static HTTP access; live UI automation timed out |
| Separate routes/DNS and management preservation | Peer-route tests; isolated TCP/route network namespaces; DNS ownership regressions | Both interface-bound TCP paths and Ethernet retained; route-race correction described below |
| Installation, rollback and removal | Temporary-host deployment suites; read-only real installer preview | Authorised deployment completed; destructive uninstall/rollback tests remain isolated |
| Bounded 1 Hz capture over time | Completed short captures and both one-hour captures; bounded histories; heap diagnostic with documented residual growth | Three-minute physical observations; target-Mochabin capacity and a physical hour are not claimed |

The real installer's read-only preview retained APN `tenten`, QMI IPv4, backup policy/metric 700, 1 Hz capture, existing web credentials and Ethernet policy. It reported the existing supervisor-owned cellular IPv4/default route as interrupted by deployment. No production files or service state were changed by the preview.

## Fleet resource comparison

`platform_profile.py` now launches the actual fleet parent and accepts `--modems`. Its external `/proc` observer retains every child instead of overwriting peers sharing a role. Per-role CPU totals use actual, interval-weighted process readings; disabled or failed observations remain unavailable. Seven focused tests cover child enumeration, role aggregation, PID-specific baselines, missing data and irregular intervals. All eight objective/web combinations completed with the expected process counts for two synthetic UEs. The final complete acceptance pass, including these observer tests, passed **650 tests in 161.2 seconds**.

The [resource audit](multimodem-objective-resources.json) uses 15 measured seconds per case after two seconds of warmup. CPU is percent of one core; RSS is the maximum observed sum in MiB, including the parent and all children. These short development-PC cases ran while a separate synthetic capture was active. There was no real QMI/proxy/network work or HTTP client, and histories had not reached their full two-minute extent. They are a mode comparison, not a target-Mochabin capacity or long-term memory bound.

| Objectives | Web off: CPU / RSS | Web on: CPU / RSS |
|---|---:|---:|
| Idle | 1.47% / 22.5 MiB | 2.33% / 44.5 MiB |
| Connection only | 2.40% / 57.9 MiB | 3.07% / 79.7 MiB |
| Monitoring only | 3.20% / 95.2 MiB | 4.26% / 118.4 MiB |
| Both | 3.00% / 94.8 MiB | 3.93% / 117.6 MiB |

The small difference between the monitoring-only and combined cases is within this synthetic measurement's workload variation; the fake modem performs no registration/data-plane work. Do not infer that enabling a real connection reduces resource use. Web-off starts no web child; connection-only starts no telemetry children; idle starts neither modem nor telemetry children.

## USB generation and client teardown

The complete USB-generation suite passed **663 Python tests in 161.3 seconds** before the final heartbeat-path cache and selective client teardown refinements. Regressions cover replacing a UE at the same USB port/control path, restoring its predecessor, and restarting the worker pair after the same UE re-enumerates. Checkpoints retain their opened transport generation; post-open discovery rejects a replacement before CID adoption or state-changing preparation. Relocation cleanup uses a separate journal lock, allowing an unrelated replacement at the former port to continue under its own physical lock.

The [two-minute generation capture](multimodem-usb-generation-soak.json) persisted 121/120 sequential samples, with no skipped intervals, storage failures/drops or worker restarts. It predates the last teardown/cache refinements and is explicitly synthetic. Closing a stale transport now unregisters local clients without firmware CID release or former bearer/GPS stop. An uncertain stop on a current transport preserves only the firmware WDS CID and journal for recovery; successful close retains normal release behaviour. A repeated close does not send the old handle or replace the original failure diagnostic. The focused final recovery/QMI suite passed 53 tests.

The final complete suite passed **666 Python tests in 161.7 seconds** after all changes above. It includes executable actors, network namespaces, scoped API/configuration, fault injection and selective teardown. The isolated WWAN TCP check verified the cellular source interface and refusal to validate through a different interface. Tests requiring local sockets/namespaces ran outside the restricted sandbox; no production deployment or physical-modem mutation was performed. `git diff --check` and Python compilation passed.

The [final short capture](multimodem-final-close-soak.json) completed in 60.62 seconds with 61/60 samples acquired and persisted, sequential CSV IDs, no skipped intervals, storage errors/drops or worker restarts. All ten scoped HTTP observations passed. It uses the final runtime source manifest, but its synthetic hardware does not exercise firmware QMI teardown. Mean sample periods were 999.993/1000.005 ms; maximum planning lag was 1.240 ms.

## Deployment gate before authorisation

Linux detected a Fibocom QMI modem on `wwan0` and a Quectel RM520N-GL on `wwan1`. The second modem's UIM report confirmed a present, ready USIM with PIN1 disabled. Before deployment it was registered-denied on a public PLMN and had no data session. This does not establish a SIM fault or a successful connection to the authorised private network.

The existing installed service remained active. A final read-only host check found both physical devices, Ethernet default metric 100 and the existing `wwan0` default metric 700; no `wwan1` IPv4 route was present. Deployment of the new version was not executed: automatic approval review required explicit authorisation to update `/opt`, `/etc` and systemd and briefly interrupt the cellular connections. Passwordless sudo was separately verified, which establishes capability rather than deployment authorisation. Simultaneous registration, effective APNs, independent IPv4/routes, bound traffic and capture timing on the two physical modems must be checked after that deployment. No synthetic result is presented as physical-modem verification.

## Authorised physical deployment and findings

The user subsequently authorised installation and checks on both physical modems. The current fleet was installed through `tools/deployment/install.sh --skip-packages --accept-network-changes`, retaining account credentials and saved network settings. Subsequent corrections used that same installer. The Fibocom uses `wwan0` with APN `tenten`; the RM520N-GL uses `wwan1` with APN `internet`, both on the authorised PLMN `99910`. WDS current settings independently reported matching effective APNs. TCP connections bound with `SO_BINDTODEVICE` succeeded through both interfaces, with source IPv4 `192.168.3.2` and `192.168.4.6` respectively. Ethernet retained its metric-100 management route.

The Quectel initially had a saved LTE-only RAT preference and SA disabled. After a temporary RAT change, attach-APN renewal restored that saved restriction. With its platform objectives idle and its physical lease held exclusively, the supported UMTS/LTE/NR preference was saved and Quectel's SA-disable flag changed from 1 to 0. Its NR band list then became available, including n78, and registration succeeded. These were authorised hardware provisioning steps, not an automatic vendor reset implemented in the supervisor. An existing optional GPS error handler also referred to the nonexistent `ProtocolError.INVALIDSERVICE`; it now uses the installed `INVALIDSERVICETYPE` enum. A regression covers unsupported protocol/core errors and a genuine modem error without allowing GPS to abort connection setup. The resulting full suite passed 667 tests before the subsequent route fix.

The [initial physical capture](multimodem-physical-before-route-fix.json) showed no missing RSRP/RSRQ/SNR observations, storage loss or worker restart, with mean acquisition durations of 314/279 ms. Its final per-UE observation windows differ because large existing CSVs were read between final snapshots; the audit states that limitation. The [initial transition/crash checks](multimodem-physical-transitions-before-route-fix.json) passed connection-only, monitoring-only, idle and combined modes, plus scoped telemetry/modem crashes and a shared web crash. The Fibocom's modem/telemetry PIDs and capture run remained unchanged throughout.

Those physical checks exposed route-metric feedback despite successful traffic: a peer's delayed snapshot could make its newly changed route look foreign. The fix publishes physical checkpoint references and validates identity, USB topology, enumeration and interface before reading the current owned route. A shared bounded lock serialises kernel inspection, route application and checkpoint publication. Regression tests cover a deliberately stale snapshot, preservation of another route on the same interface, concurrent writers paused between kernel add and journal publication, wrong transport evidence and lock timeout. All 24 focused route/fleet tests passed before deployment. No new QMI requests or INI fields were added.

The final source passed **670 Python tests in 161.4 seconds**, **57 frontend unit tests**, TypeScript checking and whitespace validation. A comparison of 115 installed runtime/static files found no source/export mismatch before the final route change; the final installed source comparison is recorded with final acceptance. The deployed static page returned HTTP 200 on localhost and the management address; uncredentialed status returned HTTP 401 on both. Live in-app browser inspection timed out, so no authenticated live-browser screenshot is claimed. Earlier isolated browser acceptance remains the UI evidence.

The [final physical capture](multimodem-physical-final.json) used a common 180.05-second monotonic window and froze both final snapshots before reading existing CSVs. Each UE produced **180 consecutive observations**, independently matched to CSV sample/run IDs. All 178 scoped API observations passed. There were zero skipped intervals, missing RSRP/RSRQ/SNR values, storage failures/drops or worker restarts. Both acquisition and writer histories stayed within 120 points. TCP bound to each WWAN succeeded before and after capture. The route metrics remained exactly 701/700 throughout, with Ethernet at 100.

| Physical UE | Effective APN | IPv4 | Mean / maximum acquisition | Mean actual period | Maximum scheduling delay | Write p95 |
|---|---|---|---|---|---|---|
| Fibocom (`wwan0`) | `tenten` | `192.168.3.2/30` | 313.86 / 422.07 ms | 999.99999 ms | 7.62 ms | 1.83 ms |
| Quectel (`wwan1`) | `internet` | `192.168.4.6/30` | 281.29 / 387.17 ms | 999.99999 ms | 11.49 ms | 1.62 ms |

These are observed development-PC values, not timing guarantees. The GPS source reported no valid fix (Fibocom: no fix; Quectel: start error). No satellite position was invented, and those optional statuses did not prevent independent connectivity or radio capture. Physical USB relocation and firmware-specific failures beyond these two devices are covered by isolated regressions rather than represented as physically exercised.

The [final transition and fault audit](multimodem-physical-transitions-final.json) repeated all four objective combinations and deliberate telemetry/modem/web worker crashes on the final deployed source. Each recovered, interface-bound TCP succeeded after reconnection, and the Fibocom's modem/telemetry PIDs and capture run stayed unchanged. Monitoring-only removed only the Quectel's owned IP/route while capture continued; idle stopped its worker pair. Both connection and monitoring objectives were restored at the end.

The [final installed-state check](multimodem-physical-final-state.json) confirmed both UEs ONLINE without a connection error, both objectives enabled, route metrics 701/700, an active single systemd service and the preserved Ethernet route. All 115 runtime/static files matched the repository export and the complete web configuration matched its pre-deployment backup, including credentials. There is no remaining deployment approval gate for this authorised run.
