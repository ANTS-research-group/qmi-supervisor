# Published contract (version 1)

QMI Supervisor publishes a small, versioned part of its output so third parties
and future extensions can rely on it. This document is the compatibility policy
for contract version 1 (application release v1.3.0).

## What is covered

The contract is exactly these files and the objects they describe:

| Schema | Describes |
|---|---|
| [`doc/schemas/diagnosis.schema.json`](schemas/diagnosis.schema.json) | The `diagnosis` object published in `/api/status` and `health.json`. |
| [`doc/schemas/health.schema.json`](schemas/health.schema.json) | `health.json` in the profile's telemetry directory. |
| [`doc/schemas/event.schema.json`](schemas/event.schema.json) | One line of the append-only `events.jsonl` stream. |
| [`doc/schemas/modem-status.schema.json`](schemas/modem-status.schema.json) | The stable subset of one modem record (`/api/status?modem=<id>` → `snapshot`). |
| [`doc/schemas/update-state.schema.json`](schemas/update-state.schema.json) | `/var/lib/qmi-supervisor/update/state.json`, written by `qmi-supervisor-update` (Debian package installations). |
| [`doc/schemas/update-history.schema.json`](schemas/update-history.schema.json) | One line of `/var/lib/qmi-supervisor/update/history.jsonl`, one per update transaction. |
| [`doc/schemas/codes.json`](schemas/codes.json) | The machine-readable diagnosis-code catalogue. |

**Updater state** (`update-state.schema.json`): `/var/lib/qmi-supervisor/update/state.json`, written by
`qmi-supervisor-update` on Debian package installations and readable without root. `schema_version` 1; changes
are additive; consumers ignore unknown fields and unknown `reason` or `check_error.code` values. It never contains
an access token or a device identifier. `in_progress` describes the running transaction, `last_result` the last
one; `repair` reports a package left half-installed by an interruption and `auto` the last automatic-mode decision.

**Update history** (`update-history.schema.json`): one line per install, rollback, repair or detected
interruption in `/var/lib/qmi-supervisor/update/history.jsonl`. It carries versions, step and result
names, the history code, a bounded `detail` and the signing-key fingerprint only: never a URL, a token,
a password hash or modem identity data. Lines are appended atomically, one writer at a time; the last
line describes the last transaction.


The HTTP envelope of `/api/status` carries `contract_version` (currently `1`)
and `supervisor_version` (the installed package version, or `unknown`) next to
`snapshot`; `supervisor_version` is available only to authenticated requests and
is deliberately absent from `/api/session`. The static frontend bundle does
carry the version it was built with (the account menu shows it), so this is a
scope decision, not a way to hide the release from an unauthenticated visitor.
`health.json` carries `supervisor_version` too, and the modem status record
includes it. `health.json` and event lines carry `schema_version` (currently
`1`).

## What is not covered

Everything else is internal and may change in any release without notice: the
rest of the `/api/status` snapshot (samples, radio, history, processes,
resources, configuration), `/api/config`, `/api/tools`, `/api/logs`, the web UI,
file names and directory layout not named above, and the journal text. Fields
outside the stable subsets are not part of the contract even when they appear
in the same object.

## Compatibility rules

- **Additive changes only within a version.** A new field, a new event type or
  a new diagnosis code does not change the version. Consumers must ignore fields
  and codes they do not know.
- **Breaking changes bump the version.** A removed or renamed field, a changed
  type or meaning, or an incompatible structural change increments
  `schema_version` and `contract_version`. When possible the old shape is kept
  for one minor release alongside the new one, and the change is documented here.
- **Codes are never renamed or reused.** A code keeps its meaning forever. When
  a code stops being produced it stays in `codes.json` with
  `"retired": "<version>"`; it is never removed and its spelling is never
  reused for something else.
- **Unknown codes are not an error.** Treat an unknown diagnosis code as
  `unknown` while keeping the severity the supervisor published for it.

## Privacy guarantees

Contract objects never carry IMSI, ICCID or IMEI, and never carry raw error text
outside the bounded `detail` of the diagnosis object. The modem is identified by
its serial number: `log_label` and `health.json`'s `modem` hold the
USB serial, or `sn-` plus the first eight hex characters of its SHA-256 when the
serial is IMEI-shaped (exactly 15 decimal digits).

## Regenerating the code catalogue

`modules/runtime/diagnosis.py` (`CODES`) is the single source of truth. After
changing it run:

```sh
python3 tools/contract/generate_codes.py
```

This rewrites `doc/schemas/codes.json` and the `code` enum in
`doc/schemas/diagnosis.schema.json`. A test fails when the committed files
differ from a fresh deterministic run of the generator.
