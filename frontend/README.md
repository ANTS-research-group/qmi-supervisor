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

With one modem the home page is that modem's Summary. With two or more it is Fleet: one row per modem with state, network, current signal and its connection/monitoring switches, which act on that modem directly. The sidebar always lists the modems; each modem page has Summary, Metrics, Configuration and Diagnostics tabs. Links carry `?modem=<id>`, so reloads, new tabs and history keep the modem. Each modem keeps its own draft and resource history; selecting another modem never applies settings, and late responses from a previous selection are discarded. Profile-scoped APIs use `?modem=<id>` without adding polling timers or QMI queries.

Objective switches use saved configuration and preserve pending form edits. Configuration shows one section at a time from a grouped vertical index (keyboard traversal, compact selector on mobile). Rarely changed settings, and the Recovery section, appear only with "Show advanced options" (a browser preference); a setting with an error, a pending change or a non-default use is never hidden. Dependent options are disabled with their reason, and invalid retained fields stay editable for repair. One save applies a modem profile (modem, network, recovery and telemetry), the shared web interface (System configuration) or the user account (Profile); the web interface and the account share a section but are edited and saved apart, so pending edits of one never travel with the other. The sticky save bar names the target, the consequence of applying and, on request, every pending change. Expiry preserves drafts, while explicit sign-out clears them.

The account menu at the bottom of the sidebar holds light/dark, English/Spanish, the Profile page and sign out; the Profile page (`/profile/`) holds the account: username, password and session lifetime. The login page keeps its own theme and language selectors. Preferences live in browser storage, not the INI or server account. The default is English/light. Storage failures fall back to memory. Semantic CSS colours include Canvas axes, cursors and tooltips. Translation text lives in `lib/messages.ts`; other maintained source text is English.

Public routes: `/dashboard/`, `/modem/`, `/metrics/charts/`, `/metrics/data/`, `/configuration/{modem,telemetry,web}/` and `/profile/`. `/configuration/telemetry/` opens the capture section of the modem settings, and the legacy `/metrics/signal/` and `/metrics/performance/` routes redirect to Charts. Language changes do not translate public identifiers or dataset keys.

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
