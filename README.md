# QMI Supervisor

QMI Supervisor provides two independent functions for each compatible USB cellular modem:

- **Autonomous connection:** discover the modem, register with the selected/automatic operator, maintain a data session and configure owned IPv4, routes and DNS.
- **UE monitoring:** acquire available radio, cell, identity, traffic and optional GPS observations for live inspection and timestamped datasets, normally at 1 Hz.

Enable connection, monitoring, both or neither from each modem's Summary, or from Fleet when several modems are present. Settings and objectives survive restarts. Monitoring-only observes without changing network selection or opening a bearer.

The platform targets Debian/Ubuntu with systemd, including Mochabin. One `qmi-supervisor.service` manages an independent modem/telemetry process pair per UE and one shared web process, restarting a failed child independently. Each modem can use its own APN and objectives. The optional React/Next frontend is a static export served by Python: Node is needed only to build it. The interface supports light/dark themes and English/Spanish, with browser-local preferences.

## Quick start

Build on the development PC and transfer the application with `frontend/out` to the target as described in [INSTALLATION.md](INSTALLATION.md):

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

On the target, from the application directory:

```bash
sudo ./tools/deployment/install.sh --web on
```

Create the requested account (8 characters or more), open any address of the device on port 8080 (the web listens on every interface by default), sign in and configure the desired functions. On a **fresh installation both objectives start disabled**. Proposed network settings are APN `internet` and PLMN `auto`; use the operator's actual APN when different. A PIN-free provisioned SIM requires no unlock input.

Prepare competing modem managers before enabling connection. On a dedicated controller, the installer offers `--modem-manager disable`; it is a global host policy, so review its effect in the installation guide. Ethernet management is retained. Automatic discovery supports several modems simultaneously. Additional devices receive saved profiles. With two or more modems present the home page becomes Fleet, and the sidebar lists every connected modem so each UE is inspected and configured on its own. An unplugged modem keeps its profile but is not listed until it is connected again. See [multimodem setup](INSTALLATION.md#multiple-modems-and-independent-apns).

```bash
systemctl status qmi-supervisor --no-pager -l
journalctl -u qmi-supervisor -f
sudo qmi-supervisor-configure --doctor
```

For headless deployment use `--web off` and explicitly choose objectives, for example `--connection on --telemetry on`. The existing INI and CLI remain supported.

## Documentation

- [INSTALLATION.md](INSTALLATION.md): complete deployment, packages/files, host policies, CLI, routing, recovery, upgrades and removal.
- [CONFIGURATION.md](CONFIGURATION.md): every INI option, parser/installer defaults, constraints, application effects and complete examples.
- [Frontend development](frontend/README.md): local API bridge, build and UI verification.
- [Measurement timing](doc/metrics-precision.md): scheduling, observation windows, storage semantics and reproducible benchmarks.

`ONLINE` describes an operational local data session and IPv4 configuration. Separate TCP verification assesses target reachability. Unsupported/late measurements remain unavailable; one-second acquisition is a target with explicit gaps, not a hard real-time radio guarantee.

## Project and attribution

Originally developed for the University of Murcia ANTS [GAIALAB](https://ants.inf.um.es/en/gaialab) 5G testbed, QMI Supervisor can also be used as a general modem controller and telemetry platform. Optional UDP collector integration is available with [ue-collector](https://github.com/ANTS-research-group/ue-collector).

If you are using this development in your integration / testbed / application, we'd love to hear about it and you can cite us using Zenodos's DOI: [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.12528538.svg)](https://doi.org/10.5281/zenodo.12528538) and the general testbed description and architecture is described in [this paper](https://doi.org/10.1007/978-3-031-20936-9_29)

```
    QMI-Supervisor - a 5G QMI modem controller
    Copyright (C) 2024 Anthony Joel Pogo Medina - Universidad de Murcia

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

