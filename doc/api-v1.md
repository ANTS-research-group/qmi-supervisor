# Machine API v1

Read-only HTTP endpoints for other systems (Grafana, Prometheus, a NOC script, another
team's service), authenticated with scoped API tokens. The interface is **off by default**
(`[web] api = off`) and every path answers `404` while it is off.

This is research and testing software: the machine API exposes the identifiers the supervisor
knows (raw modem serial, IMEI, IMSI and ICCID when available), so enable it only on trusted
networks and grant tokens to the systems that need them.

## Enabling it

1. Enable the web interface and the machine API in the configuration (`[web] enabled = true`,
   `api = true`) and save the web settings.
2. Create a token as root. The secret is printed once and only its digest is stored:

   ```bash
   sudo qmi-supervisor-configure --api-token-create grafana \
        --api-token-scopes read:status,read:metrics --api-token-expires 365
   ```

   List or revoke tokens without exposing secrets:

   ```bash
   sudo qmi-supervisor-configure --api-token-list
   sudo qmi-supervisor-configure --api-token-revoke 1a2b3c4d
   ```

   The same operations are available in **System → API access**; a created token is shown once.

Tokens live in `/etc/qmi-supervisor/api-tokens.json` (root, mode 0600), never in the INI.

## Scopes

| Scope | Grants |
|---|---|
| `read:status` | `/api/v1/modems`, `/api/v1/modems/<id>`, `/api/v1/modems/<id>/health`, `/api/v1/system` |
| `read:events` | `/api/v1/modems/<id>/events` |
| `read:metrics` | `/metrics` |

## Endpoints

Every JSON response uses the envelope `{"contract_version": 1, "data": …}`; errors use
`{"contract_version": 1, "error": {"code": "…", "message": "…"}}`. All responses are
`Cache-Control: no-store`.

| Method and path | Scope | `data` |
|---|---|---|
| `GET /api/v1/modems` | `read:status` | One entry per profile: `id`, `serial_label`, `detected`, `state`, `diagnosis` (`code`, `severity`, `active`, `since_utc`) and `identity` (`serial`, `imei`, `meid`, `esn`, `imsi`, `iccid`). |
| `GET /api/v1/modems/<id>` | `read:status` | The modem record of the stable contract subset, plus `identity`. |
| `GET /api/v1/modems/<id>/health` | `read:status` | That profile's `health.json`, or `null`. |
| `GET /api/v1/modems/<id>/events?instance=<i>&after=<seq>&limit=<n>` | `read:events` | `{instance, events}` from `events.jsonl` (and its `.1` rotation). `limit` 1..500, default 100. When `instance` differs from the current one, the oldest available lines of that instance are returned. |
| `GET /api/v1/system` | `read:status` | `supervisor_version` and the updater summary `{update_available, newest_installable, checked_utc, last_result}` (`null` fields when updates are not installed). |
| `GET /metrics` | `read:metrics` | Prometheus text exposition (below). |

Examples:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/modems
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://127.0.0.1:8080/api/v1/modems/default/events?limit=50"
```

The payloads follow the versioned contract in [contract.md](contract.md): diagnosis, health,
events and the modem status subset. `serial_label` and the metrics `modem` label use the
privacy label (the serial, or `sn-` plus a hash for an IMEI-shaped serial); the `identity`
block carries the raw values.

## Prometheus

```yaml
scrape_configs:
  - job_name: qmi-supervisor
    authorization:
      credentials: qms_1a2b3c4d_…
    static_configs:
      - targets: ['192.0.2.10:8080']
```

Metrics use the `qmi_supervisor_` prefix and labels `modem` (privacy label) and `profile`:

```
qmi_supervisor_info{version="1.3.0"} 1
qmi_supervisor_modem_up{modem,profile} 1|0
qmi_supervisor_modem_state{modem,profile,state} 1
qmi_supervisor_diagnosis_active{modem,profile,code,severity} 1
qmi_supervisor_diagnosis_attempts{modem,profile}
qmi_supervisor_diagnosis_failing_seconds{modem,profile}
qmi_supervisor_telemetry_stalled{modem,profile} 1|0
qmi_supervisor_last_sample_age_seconds{modem,profile}
qmi_supervisor_signal_rsrp_dbm{modem,profile,rat}
qmi_supervisor_signal_snr_db{modem,profile,rat}
```

Signal values appear only while the fleet summary considers them fresh.

## Limits and authentication rules

- `Authorization: Bearer qms_<id>_<secret>` is required; cookies, query tokens and browser
  sessions never authenticate these paths, and bearer tokens never authenticate the browser
  endpoints.
- Each token is limited to 10 requests per second (`429` with `Retry-After`).
- Invalid tokens are throttled per client: five failures in a minute answer `429`.
- A revoked or expired token stops working on the next request.
- The Host header must be an allowed listen address, like the browser interface.

## Privacy

`health.json`, event lines and the diagnosis objects never carry IMSI, ICCID or IMEI. The
machine API deliberately adds an `identity` block because this deployment is used for
research and testing; the privacy label remains the only modem identifier in metrics and
event lines so Prometheus cardinality and log readability do not depend on SIM contents.
