# QMI Supervisor frontend

React with a static Next export. Node 22+ builds and develops the frontend; the target Mochabin serves the export through Python and does not run Node or SSR.

## Local development

```sh
npm ci
npm run dev
```

Open http://127.0.0.1:3000. The existing supervisor web/API must already be running at http://127.0.0.1:8080. Development starts Next and a loopback proxy, never the modem or acquisition service. Override with `PORT=3001 QMI_API_URL=http://127.0.0.1:8081 npm run dev`.

QMI_API_URL accepts an HTTP loopback IP without credentials or additional URL components. For a remote Mocha, use a local SSH tunnel. The proxy validates local Host/Origin before forwarding to the API; session cookies and CSRF protection remain active. Unavailable upstreams produce a 502 response without starting another service.

Sign in with the installed account. Anonymous status/config access and browser token login are not supported. Reset an account on the target using `sudo qmi-supervisor-configure --set-web-password`. Automation signs in through the same account and session cookie API.

## UI behaviour

With one modem the home page is that modem's Summary. With two or more it is Fleet: one row per modem with state, network, current signal and its connection/monitoring switches, which act on that modem directly. The sidebar always lists the modems; each modem page has Summary, Metrics, Configuration, Diagnostics and Tools tabs. Links carry `?modem=<id>`, so reloads, new tabs and history keep the modem. Each modem keeps its own draft and resource history; selecting another modem never applies settings, and late responses from a previous selection are discarded. Profile-scoped APIs use `?modem=<id>` without adding polling timers or QMI queries.

Objective switches use saved configuration and preserve pending form edits. Configuration shows one section at a time from a grouped vertical index (keyboard traversal, compact selector on mobile). Rarely changed settings, and the Recovery section, appear only after "Show N advanced settings", a link at the foot of each section card (a browser preference shared by every section); a setting with an error, a pending change or a non-default use is never hidden. Dependent options are disabled with their reason, and invalid retained fields stay editable for repair. One save applies a modem profile (modem, network, recovery and telemetry), the shared web interface (System) or the user account (Profile); the web interface and the account share a section but are edited and saved apart, so pending edits of one never travel with the other. The sticky save bar names the target, the consequence of applying and, on request, every pending change. Expiry preserves drafts, while explicit sign-out clears them. System also has a read-only System log section: the last 200 journal lines of the supervisor unit, fetched only while that section is open and refreshed every five seconds while "Follow new lines" is on.

Summary and the two Metrics chart views (Signal and Performance) are boards of cards. "Customize", beside the refresh button of the modem header, opens an editor of rows: each row holds one, two, three or four cards, and its cards share the row width (fewer cards than places fill the row; a row whose cards would get narrower than they read wraps 4 → 2 → 1 or 3 → 2 → 1). Rows can be added, narrowed (the cards that no longer fit continue in a row below), moved or removed (their cards are hidden). Cards are dragged between rows and free places (mouse, touch with a press-and-hold, or the keyboard: Space to lift, arrows to move, Space to drop, Escape to cancel); a card dropped on a full row trades places with the card there, and the hidden tray keeps the cards out of the page. Step buttons offer the same moves without dragging. Arrangements saved with the earlier per-card widths are converted to rows. Done keeps the arrangement in this browser for that view; Cancel discards it; the default arrangement is never stored, so a view keeps following its own defaults. Drag and drop uses `@dnd-kit/core` and `@dnd-kit/sortable`, loaded only when the editor opens.

The Tools tab has two tools, Ping and iperf3, and runs one test at a time on the host; the test is shared by every modem page and its output is polled only while it runs. Coming back while a test runs opens its tool, mode and options, locked until it ends. Each card shows its basic options; the rest sit behind the same "Show N advanced options" link as the configuration cards (one preference for both), and an advanced row that holds a value of its own never hides.

- Ping: target, packets and interval (from 0.2 s), data size and the modem interface; advanced: IP version, fragmentation (`-M do|want|dont`, and `probe` when the installed ping lists it), reply timeout and deadline, TTL and service class, preload and send buffer, padding pattern, ICMP identifier and fwmark, IPv4 timestamps and record route, IPv6 flow label, adaptive interval, sticky source, connected socket, no multicast loopback, and output switches (timestamps, missing replies, summary only, verbose, user-to-user latency, unrounded times).
- iperf3 client: server and port, upload, download or both ways, TCP, UDP or SCTP, target rate, an end after a time, an amount of data or a number of blocks, parallel streams and the modem interface (`--bind-dev`, iperf3 3.10 or later); advanced: IP version, report interval and omitted seconds, buffer length and socket buffer, MSS and congestion control, no delay, zero copy and MPTCP, SCTP streams, TOS and IPv6 flow label, client port and connect timeout, receive and TCP send timeouts, control keepalive, pacing timer, fair-queuing rate and bursts, UDP 64-bit counters, don't fragment and GSO/GRO, repeating payload, server results, discarded received data, units and output switches.
- iperf3 server: port, one test or until stopped, and the command to run on the other device; advanced: IP version, listening only on the modem, report interval, a rate limit with its averaging time, idle timeout and maximum test duration, MPTCP, timeouts, control keepalive, units and output switches.

The backend validates every option with the same bounds as the page and passes only options the installed tool lists in its own help; the page disables the rest with "Not available in the installed ping/iperf3". Options that flood or broadcast (`ping -f`, `-b`), read or write files (`iperf3 -F`, `--logfile`, `-I`), detach a daemon (`-D`), handle credentials (`--username`, RSA keys), change the output format the page reads (`-J`, `--json-stream`), bind to a source address (`-B`, `-X`; `--bind-dev` is the reliable way through the modem) or add nothing here (`ping -a` audible bell, `-d` SO_DEBUG, `-H` reverse names, which the always-on `-n` overrides, `-N` node information queries; `iperf3 -A` CPU affinity, `-T` line titles, `--extra-data` for JSON, and `--dscp`, which TOS covers) are not offered. A summary (loss and latency, or sender and receiver throughput, jitter and datagram loss, or each direction of a bidirectional test) sits above the raw output.

The account menu at the bottom of the sidebar holds light/dark, English/Spanish, the Profile page and sign out; the Profile page (`/profile/`) holds the account: username, password and session lifetime. The login page keeps its own theme and language selectors. Preferences live in browser storage, not the INI or server account. The default is English/light. Storage failures fall back to memory. Semantic CSS colours include Canvas axes, cursors, series and tooltips. Inter and JetBrains Mono are bundled (Latin subsets, from `@fontsource-variable`), because the CSP only allows same-origin fonts. Translation text lives in `lib/messages.ts`; other maintained source text is English.

Public routes: `/dashboard/`, `/modem/`, `/metrics/charts/`, `/metrics/data/`, `/configuration/{modem,telemetry,web}/`, `/tools/` and `/profile/`. `/configuration/telemetry/` opens the capture section of the modem settings, and the legacy `/metrics/signal/` and `/metrics/performance/` routes redirect to Charts. Language changes do not translate public identifiers or dataset keys.

A single authenticated status poll runs every two seconds and pauses while the document is hidden. Theme, language, routes and charts do not add QMI queries. Source timestamps remain monotonic/UTC; display formatting follows the selected language.

## Build and verification

```sh
npm test
npm run typecheck
npm run build
npm run test:browser          # full matrix: all viewports, both languages and themes
npm run test:browser:quick    # same flows on two viewports, one language and theme
```

Output is `frontend/out`. Deploy it together with the Python application using the existing installer. Do not run `next start` or the development proxy on the target. Rebuild after frontend changes.

Unit checks use Node's native test runner; browser acceptance uses an isolated API fixture and Chrome/Chromium with local sockets. It does not query the real modem or alter host networking. The quick profile skips the screenshot artifacts and the extra viewport/language/theme combinations, which is enough while iterating; run the full profile before a release. See [INSTALLATION.md](../INSTALLATION.md) for deployment and the full project checks.
