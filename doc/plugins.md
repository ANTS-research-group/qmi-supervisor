# Plugins

A plugin is a separate program plus a manifest. The supervisor starts enabled plugins as
unprivileged, supervised processes and feeds them status snapshots and events over a local bus.
Plugins never run inside the supervisor's processes and never talk to a modem.

## Install a plugin

1. Put the program in a root-owned directory, for example
   `/usr/lib/qmi-supervisor/plugins/<name>/plugin.py`, mode 0755. The program and its directory
   must not be writable by group or others.
2. Create `/etc/qmi-supervisor/plugins.d/<name>.ini` (mode 0644, root):

   ```ini
   [plugin]
   name = telegram-alerts
   description = Sends a Telegram message when a modem needs attention
   version = 1.0.0
   exec = /usr/lib/qmi-supervisor/plugins/telegram-alerts/plugin.py
   args =
   enabled = false
   capabilities = read:events

   [settings]
   chat = 123456
   token = 000000:example
   ```

   `name` must match the file name. `capabilities` is a comma-separated subset of the table below.
   `[settings]` is delivered to the plugin as a JSON object of strings (at most 16 KiB). The
   optional `os_capabilities` key accepts `net_raw`, `net_admin` and `net_bind_service`; that plugin
   runs as a transient systemd unit with only those Linux capabilities, `NoNewPrivileges`,
   `ProtectSystem=strict` and `PrivateTmp`.
3. Enable it in **System → Plugins** (or set `enabled = true`). No service restart is needed.

Two disabled examples ship with the package: a webhook exporter and an MQTT exporter. Their
manifests are in `/usr/share/doc/qmi-supervisor/examples/plugins.d/`; copy one to
`/etc/qmi-supervisor/plugins.d/` and fill in `[settings]`.

## Writing a plugin (Python)

The SDK is installed at `/opt/qmi-supervisor/sdk` and placed on `PYTHONPATH` by the plugin host.

```python
from qmi_supervisor_sdk import connect

bus = connect()
bus.subscribe(topics=('events',))
for message in bus.messages():
    if message['op'] == 'event':
        event = message['data']
        bus.log(f"{event['modem']}: {event['type']}")
```

| Member | Meaning |
| --- | --- |
| `connect(timeout=10.0)` | Connects with the environment prepared by the host. Raises `PluginError` with `unauthorized`, `unavailable`, `protocol` or `capability`. |
| `bus.name`, `bus.capabilities` | Plugin name and the effective capabilities from the welcome message. |
| `bus.settings()` | The manifest `[settings]` as a dict of strings. |
| `bus.subscribe(topics=('status', 'events'), interval=5)` | `interval` 5–300 seconds; `status` needs `read:status`, `events` needs `read:events`. |
| `bus.messages()` | Yields `status`, `event`, `dropped` and `reconnected` messages; answers pings and reconnects with backoff, restoring subscriptions. |
| `bus.log(text)` | One stderr line, shown in the journal as `plugin[<name>]`. |
| `bus.request/card/issue` | Reserved for plugins with action or UI capabilities; they raise `PluginError('capability')` otherwise. |
| `bus.close()` | Closes the connection. |

`status` messages carry the stable contract modem records plus their profile `id`; `event`
messages carry one `events.jsonl` line. `dropped` tells how many messages were lost because the
plugin was too slow. Contract objects never contain IMSI, ICCID or IMEI.

Testing helper:

```python
from qmi_supervisor_sdk.testing import FakeBus
with FakeBus(capabilities=['read:events'], settings={'url': 'http://127.0.0.1:9000/'}) as fake:
    fake.push_event({'seq': 1, 'type': 'diagnosis.opened', 'data': {}})
```

## Capabilities

| Capability | Allows |
| --- | --- |
| `read:status` | Topic `status` |
| `read:events` | Topic `events` |
| `act:objective` | `request('objective', ...)` to turn connection or monitoring on/off |
| `act:reconnect` | `request('reconnect', ...)`, refused during recovery or outside connect mode |
| `ui:cards` | `card(...)` widgets on the summary board, hidden until the operator adds them |
| `ui:issues` | `issue(...)` entries in the attention panel |

Read scopes are active when the manifest declares them. `act:*` and `ui:*` must also be confirmed by
the administrator in System → Plugins; the confirmation is stored in
`/etc/qmi-supervisor/plugins.d/<name>.grants.json` (root 0600). Every action, accepted or refused, is
audited as an `extension.action` event and a journal line. Actions are limited to six per minute per
plugin and one reconnect per modem every five minutes.

Linux capabilities in `os_capabilities` are separate from these data scopes and are confirmed by
the administrator when the manifest is installed.

## Security model

- The plugin runs as the system user `qmi-supervisor-plugin` (group `qmi-supervisor-ext`) with
  `RLIMIT_NOFILE=256`, `RLIMIT_AS=512 MiB` and `nice 10`. It cannot read
  `/etc/qmi-supervisor.ini` or `/run/qmi-supervisor/`.
- The program and its directory must be root-owned and not writable by group or others; the host
  re-validates the manifest on every start.
- The bus accepts a plugin only with the per-start secret written by the supervisor
  (`/run/qmi-supervisor/bus-secrets.json`, root 0600). The socket is
  `/run/qmi-supervisor-ext/bus.sock` (0660, group `qmi-supervisor-ext`).
- Each client has a bounded queue (100 messages); old messages are dropped and counted.
- A plugin that exits is restarted with the standard backoff (up to 60 seconds). State, restarts
  and the last error appear in System → Plugins; the journal keeps its output as
  `plugin[<name>]` lines, linked from that page.
- Disabling a plugin stops it. Editing the manifest restarts it.

## Protocol (other languages)

Newline-delimited JSON over the UNIX socket; each message is at most 64 KiB.

| Direction | Message |
| --- | --- |
| plugin → bus | `{"op":"hello","protocol":1,"plugin":"<name>","token":"<secret>"}` |
| bus → plugin | `{"op":"welcome","capabilities":[...],"contract_version":1}` or `{"op":"error","code":"unauthorized"}` |
| plugin → bus | `{"op":"subscribe","topics":["status","events"],"interval":5}` |
| bus → plugin | `{"op":"subscribed",...}`, `{"op":"status","data":{"modems":[...]}}`, `{"op":"event","data":{...}}`, `{"op":"dropped","count":n}` |
| both | `{"op":"ping"}` / `{"op":"pong"}` |
