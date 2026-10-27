"""Host IPv4 configuration, including an optional owned foreground DHCP client."""
import ipaddress
from contextlib import contextmanager
import fcntl
import json
import logging
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import os
import uuid

from modules.network.routes import parse_routes


MAX_ROUTE_METRIC = (1 << 32) - 1


class NetworkCommandError(subprocess.CalledProcessError):
    """Retain subprocess details while making iproute2 diagnostics visible."""
    def __init__(self, returncode, cmd, output=None, stderr=None, context=None):
        super().__init__(returncode, cmd, output=output, stderr=stderr)
        self.context = context

    def __str__(self):
        message = super().__str__()
        detail = self.stderr.decode('utf-8', 'replace') if isinstance(self.stderr, bytes) else str(self.stderr or '')
        return ': '.join(part for part in (self.context, message, detail.strip()) if part)


class Network:
    def __init__(self, settings, timeout, stop, *, monitor_factory=None, clock=time.monotonic):
        self.settings, self.timeout, self.stop = settings, timeout, stop
        self.interface = None
        self.address = None
        self.route = None
        self.dns_backend = None
        self.dns_record = None
        self.dhcp = None
        self.lease_dir = None
        self.last_lease = None
        self.original = None
        self.raw_original = None
        self.original_mtu = None
        self.ifindex = None
        self.cleaning = False
        self.changed = lambda: None
        self.gateway = None
        self.dns_addresses = []
        self._monitor_factory, self._clock = monitor_factory, clock
        self._monitor = self._monitor_key = None
        self._monitor_error = None
        self._monitor_generation = 0
        self._dns_applied = None
        self._next_maintenance = 0
        self._last_maintenance_key = None
        self._last_connectivity_log = None
        self._last_connectivity_errors = (None, None)
        self.peer_routes = None
        # Routes to network.routes through this modem, as installed: one argument list each.
        self.extra_routes = []
        self._routes_error = None
        # The automatic route left to the operator because it overlaps another network.
        self._routes_conflict = None
        self._connectivity_applied = {'route_mode': 'disabled', 'preferred': False,
                                      'route_error': None, 'dns_error': None}

    def command(self, *args, input=None):
        try:
            return subprocess.run(args, input=input, text=True, capture_output=True, check=True,
                                  env=dict(os.environ, LC_ALL='C'),
                                  timeout=min(self.timeout, 3) if self.cleaning else self.timeout).stdout
        except subprocess.CalledProcessError as error:
            raise NetworkCommandError(error.returncode, error.cmd, output=error.output,
                                      stderr=error.stderr) from error

    @property
    def route_metric(self):
        """The metric of the route actually owned, including recovered state."""
        return int(self.route[self.route.index('metric') + 1]) if self.route else None

    @staticmethod
    def _gateway(route):
        return route[route.index('via') + 1] if route and 'via' in route else None

    @staticmethod
    def _matches_route(record, route):
        if not route or record.get('dst', 'default') not in {'default', '0.0.0.0/0'}:
            return False
        return (record.get('type', 'unicast') == 'unicast'
                and record.get('dev') == route[route.index('dev') + 1]
                and record.get('gateway') == Network._gateway(route)
                and record.get('metric', 0) == int(route[route.index('metric') + 1])
                and not record.get('nexthops'))

    def _default_routes(self):
        routes = json.loads(self.command('ip', '-j', '-4', 'route', 'show', 'table', 'main', 'default'))
        if not isinstance(routes, list) or any(not isinstance(route, dict) for route in routes):
            raise RuntimeError('Cannot inspect IPv4 default routes in table main')
        return routes

    def _select_metric(self, routes, *, policy=None, allow_no_route=False):
        policy = policy or getattr(self.settings, 'route_policy', 'fixed')
        configured = self.settings.route_metric
        if self.peer_routes is not None:
            from modules.runtime.ipc import read_json
            from modules.network.peers import coordinated_metric, current_owned_routes
            manifest = read_json(self.peer_routes)
            if not manifest or not isinstance(manifest.get('peers'), list):
                raise RuntimeError('Coordinated modem route settings are unavailable')
            peers = current_owned_routes(manifest['peers'], self.interface)
            return coordinated_metric(routes, peers, self.interface, policy, configured,
                                      self.route, self._matches_route, allow_no_route=allow_no_route)
        if policy == 'fixed':
            return configured
        if policy not in {'primary', 'backup'}:
            raise RuntimeError(f'Unknown route policy: {policy}')
        metrics = []
        for record in routes:
            # Only the exact tuple already recorded as ours may be excluded.
            if self._matches_route(record, self.route):
                continue
            metric = record.get('metric', 0)
            if type(metric) is not int or not 0 <= metric <= MAX_ROUTE_METRIC:
                raise RuntimeError('Cannot determine the metric of another IPv4 default route')
            metrics.append(metric)
        if not metrics:
            return configured
        if policy == 'primary':
            if min(metrics) == 0:
                raise RuntimeError('Cannot make WWAN primary: another IPv4 default route in table main '
                                   'has metric 0; no lower metric exists. Choose fixed/backup or change that route explicitly')
            return min(configured, min(metrics) - 1)
        if max(metrics) == MAX_ROUTE_METRIC:
            if allow_no_route:
                return None
            raise RuntimeError('Cannot make WWAN backup: another IPv4 default route in table main '
                               f'has metric {MAX_ROUTE_METRIC}; no higher metric exists')
        return max(configured, max(metrics) + 1)

    def _configure_route(self, gateway, routes=None, *, policy=None, allow_no_route=False):
        with self._route_lock():
            # A peer may have changed its route while this actor awaited the
            # lock. Read kernel state and ownership journals in the same turn.
            if self.peer_routes is not None:
                routes = None
            return self._configure_route_locked(gateway, routes, policy=policy, allow_no_route=allow_no_route)

    @contextmanager
    def _route_lock(self):
        if self.peer_routes is None:
            yield
            return
        path = Path(self.peer_routes).with_suffix('.lock')
        with path.open('a') as stream:
            deadline = time.monotonic() + (min(self.timeout, 3) if self.cleaning else self.timeout)
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline or (self.stop.is_set() and not self.cleaning):
                        raise TimeoutError('Timed out awaiting coordinated route ownership')
                    time.sleep(.01)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _configure_route_locked(self, gateway, routes=None, *, policy=None, allow_no_route=False):
        if not self.settings.default_route and not self.route:
            return
        routes = self._default_routes() if routes is None else routes
        for attempt in range(3):
            wanted = None
            if self.settings.default_route:
                metric = self._select_metric(routes, policy=policy, allow_no_route=allow_no_route)
                if metric is not None:
                    wanted = ['default']
                    if gateway:
                        wanted += ['via', str(ipaddress.IPv4Address(gateway)), 'onlink']
                    wanted += ['dev', self.interface, 'metric', str(metric)]
            present = self.route and any(self._matches_route(record, self.route) for record in routes)
            if self.route == wanted and (wanted is None or present):
                return
            if self.route:
                if present:
                    self.command('ip', 'route', 'del', *self.route)
                # One owned route at a time keeps the existing checkpoint exact.
                # The small del/add gap does not stop or restart the QMI bearer.
                self.route = None
                self.changed()
            if wanted is None:
                return
            try:
                self.command('ip', 'route', 'add', *wanted)
            except subprocess.CalledProcessError as error:
                detail = error.stderr.decode('utf-8', 'replace') if isinstance(error.stderr, bytes) else str(error.stderr or '')
                exists = error.returncode == 2 and 'file exists' in detail.lower()
                active_policy = policy or getattr(self.settings, 'route_policy', 'fixed')
                if not exists:
                    raise
                if active_policy == 'fixed':
                    context = (f'Cannot install fixed IPv4 default route with metric {self.settings.route_metric}: '
                               'a conflicting route already exists in table main; choose another metric or primary/backup policy')
                elif attempt == 2:
                    context = 'Cannot install the WWAN default route after 3 attempts; routes in table main kept changing'
                else:
                    routes = self._default_routes()
                    continue
                raise NetworkCommandError(error.returncode, error.cmd, output=error.output,
                                          stderr=error.stderr, context=context) from error
            self.route = wanted
            self.changed()
            return

    def _extra_route_specs(self, networks):
        """Routes through this modem, with the profile metric, so two modems can route one network."""
        specs = []
        for network in networks:
            spec = [network]
            if self.gateway:
                spec += ['via', str(ipaddress.IPv4Address(self.gateway)), 'onlink']
            specs.append(spec + ['dev', self.interface, 'metric', str(self.settings.route_metric)])
        return specs

    @staticmethod
    def _matches_extra(record, spec):
        destination = record.get('dst')
        try:
            # iproute2 prints host routes without their /32.
            destination = str(ipaddress.IPv4Network(destination)) if destination not in (None, 'default') else None
        except ValueError:
            return False
        return (destination == spec[0] and record.get('type', 'unicast') == 'unicast'
                and record.get('dev') == spec[spec.index('dev') + 1]
                and record.get('gateway') == Network._gateway(spec)
                and record.get('metric', 0) == int(spec[spec.index('metric') + 1]))

    def _auto_network(self, present):
        """The /24 around the modem address, or the network of another interface it would collide with.

        The core never announces its UE pool; the modem only reports a /30 around its
        own address. A /24 is the usual pool, and it is added only when it overlaps no
        route of another interface (LAN, containers, VPN), so the host never loses one.
        """
        own = ipaddress.IPv4Interface(self.address).network
        if own.prefixlen <= 24:
            return None, None
        network = own.supernet(new_prefix=24)
        for record in present:
            destination = record.get('dst')
            if record.get('dev') == self.interface or destination in (None, 'default'):
                continue
            try:
                other = ipaddress.IPv4Network(destination)
            except ValueError:
                continue
            if other.prefixlen and network.overlaps(other):
                return None, {'network': str(network), 'overlaps': str(other), 'dev': record.get('dev')}
        return str(network), None

    def _main_routes(self):
        routes = json.loads(self.command('ip', '-j', '-4', 'route', 'show', 'table', 'main'))
        if not isinstance(routes, list) or any(not isinstance(route, dict) for route in routes):
            raise RuntimeError('Cannot inspect IPv4 routes in table main')
        return routes

    def _maintain_extra_routes(self):
        """Keep only the owned routes the profile asks for, adding back any the kernel dropped.

        Like the default route, a route is owned only once this process added it, so
        a matching foreign route is never claimed or deleted.
        """
        networks = list(parse_routes(getattr(self.settings, 'routes', ''))) if self.interface and self.address else []
        auto = 'auto' in networks
        networks = [network for network in networks if network != 'auto']
        if not networks and not auto and not self.extra_routes:
            self._routes_conflict = None
            return
        present = self._main_routes()
        self._routes_conflict = None
        if auto:
            network, self._routes_conflict = self._auto_network(present)
            if network and network not in networks:
                networks.append(network)
        wanted = self._extra_route_specs(networks)
        for spec in [spec for spec in self.extra_routes if spec not in wanted]:
            if any(self._matches_extra(record, spec) for record in present):
                self.command('ip', 'route', 'del', *spec)
            self.extra_routes.remove(spec)
            self.changed()
        failures = []
        for spec in wanted:
            if any(self._matches_extra(record, spec) for record in present) and spec in self.extra_routes:
                continue
            try:
                self.command('ip', 'route', 'add', *spec)
            except subprocess.CalledProcessError as error:
                detail = error.stderr.decode('utf-8', 'replace') if isinstance(error.stderr, bytes) else str(error.stderr or '')
                if error.returncode == 2 and 'file exists' in detail.lower():
                    failures.append(f'another route to {spec[0]} already uses metric {self.settings.route_metric}')
                else:
                    failures.append(f'{spec[0]}: {detail.strip() or error}')
                continue
            if spec not in self.extra_routes:
                self.extra_routes.append(spec)
                self.changed()
        if failures:
            raise RuntimeError('Cannot route through the modem: ' + '; '.join(failures))

    def _check_enabled(self):
        return getattr(self.settings, 'connectivity_check', True)

    def _worker_key(self):
        return (self.interface, self.address, self.ifindex, self.gateway,
                *(getattr(self.settings, name, default) for name, default in (
                    ('connectivity_targets', '1.1.1.1:443,8.8.8.8:53'),
                    ('connectivity_interval', 3.0), ('connectivity_timeout', 1.0),
                    ('connectivity_failures', 2), ('connectivity_successes', 2))))

    def _stop_monitor(self):
        monitor, self._monitor = self._monitor, None
        self._monitor_key = None
        self._monitor_generation += 1
        self._last_maintenance_key = None
        self._monitor_error = None
        if monitor is not None:
            try:
                monitor.close()
            except Exception as error:
                # The discarded generation can never promote a new interface.
                self._monitor_error = f'Connectivity worker close failed: {error}'

    def _start_monitor(self):
        if (not self._check_enabled() or not self.settings.default_route
                or self._monitor is not None or self.stop.is_set()):
            return
        key = self._worker_key()
        try:
            if self._monitor_factory is None:
                from modules.network.connectivity import ConnectivityMonitor
                monitor = ConnectivityMonitor(self.settings, self.interface, self.address.split('/')[0],
                                              self.ifindex, clock=self._clock)
            else:
                monitor = self._monitor_factory(self.settings, self.interface,
                                                self.address.split('/')[0], self.ifindex)
            self._monitor, self._monitor_key = monitor, key
            monitor.start()
            self._monitor_error = None
        except Exception as error:
            self._stop_monitor()
            self._monitor_error = f'Connectivity worker could not start: {error}'

    @property
    def connectivity_status(self):
        """Cheap, copied observations; never probe or run a host command here."""
        enabled = self._check_enabled()
        report = {'enabled': enabled, 'state': 'checking', 'stale': False,
                  'checked_monotonic': None, 'last_success_monotonic': None,
                  'check_age_ms': None, 'target': None, 'error': self._monitor_error,
                  'sequence': 0, 'consecutive_successes': 0, 'consecutive_failures': 0}
        if not enabled:
            report.update(state='disabled', reason='Connectivity checking disabled')
        elif not self.settings.default_route:
            report.update(state='disabled', reason='No managed default route')
        elif not self.interface or not self.address:
            report.update(state='disabled', reason='No managed IPv4 interface')
        elif self._monitor is not None:
            monitor, generation = self._monitor, self._monitor_generation
            try:
                observed = monitor.snapshot()
                if monitor is self._monitor and generation == self._monitor_generation:
                    report.update(observed)
            except Exception as error:
                report.update(state='checking', error=f'Connectivity result unavailable: {error}')
            checked, now = report.get('checked_monotonic'), self._clock()
            valid = type(checked) in {int, float} and math.isfinite(checked) and checked <= now
            ttl = 2 * getattr(self.settings, 'connectivity_interval', 3.0) + getattr(self.settings, 'connectivity_timeout', 1.0)
            if valid:
                report['check_age_ms'] = max(0, now - checked) * 1000
            if not valid or now - checked > ttl:
                report['stale'] = checked is not None or report.get('state') in {'up', 'down'}
                report['state'] = 'checking'
                if report['stale']:
                    report['error'] = 'Connectivity result is stale or has an invalid timestamp'
            elif report.get('state') not in {'checking', 'up', 'down'}:
                report.update(state='checking', error='Connectivity result has an invalid state')
        report.update(self._connectivity_applied)
        report['effective_route_metric'] = self.route_metric
        report['routes'] = [spec[0] for spec in self.extra_routes]
        report['routes_error'] = self._routes_error
        report['routes_conflict'] = dict(self._routes_conflict) if self._routes_conflict else None
        return report

    def _preferred_route(self, routes):
        if not self.route:
            return False
        for record in routes:
            if self._matches_route(record, self.route):
                continue
            metric = record.get('metric', 0)
            if type(metric) is not int or not 0 <= metric <= MAX_ROUTE_METRIC:
                raise RuntimeError('Cannot determine whether WWAN is the preferred default route')
            if metric <= self.route_metric:
                return False
        return True

    def _revoke_dns(self):
        if self.dns_backend == 'resolvectl':
            self.command('resolvectl', 'revert', self.interface)
        elif self.dns_backend == 'resolvconf':
            self.command('resolvconf', '-d', self.dns_record or self.interface + '.qmi-supervisor')
        else:
            self._dns_applied = None
            return
        self.dns_backend = None
        self.dns_record = None
        self._dns_applied = None
        self.changed()

    def _log_connectivity(self, status):
        logger = logging.getLogger(__name__)
        summary = (status['state'], status['route_mode'], status['preferred'], status['effective_route_metric'])
        if summary != self._last_connectivity_log:
            reason = status.get('route_error') or status.get('error') or status.get('reason')
            reason = str(reason).replace('\n', ' ')[:180] if reason else ''
            logger.info('WWAN connectivity=%s route=%s preferred=%s metric=%s%s',
                        *summary, '; ' + reason if reason else '')
            self._last_connectivity_log = summary
        errors = (status.get('route_error'), status.get('dns_error'))
        for label, current, previous in zip(('route', 'DNS'), errors, self._last_connectivity_errors):
            if current and current != previous:
                logger.warning('WWAN %s policy: %s', label, str(current).replace('\n', ' ')[:400])
        self._last_connectivity_errors = errors

    def maintain_connectivity(self, *, force=False):
        """Apply worker snapshots without waiting for any reachability check.

        Only this owner mutates routes/DNS. Local IPv4 ownership and the QMI
        bearer remain intact when Internet is unavailable or policy conflicts.
        """
        enabled = self._check_enabled()
        if self._monitor is not None and (not enabled or not self.settings.default_route
                                         or self._monitor_key != self._worker_key()):
            self._stop_monitor()
        status = self.connectivity_status
        if not self.interface or not self.address:
            return status
        now = self._clock()
        key = (self._monitor_generation, status.get('sequence'), status['state'], status['stale'],
               enabled, self.settings.default_route, getattr(self.settings, 'route_policy', 'fixed'),
               self.settings.route_metric, self.settings.dns, tuple(self.dns_addresses), self._worker_key())
        if not force and key == self._last_maintenance_key and now < self._next_maintenance:
            return status
        self._last_maintenance_key = key
        self._next_maintenance = now + getattr(self.settings, 'connectivity_interval', 3.0)
        applied = {'route_mode': 'disabled', 'preferred': False, 'route_error': None, 'dns_error': None}
        verified = not enabled or status['state'] == 'up'
        local_ready = False
        try:
            links = json.loads(self.command('ip', '-j', 'link', 'show', 'dev', self.interface))
            if not links or links[0].get('ifindex') != self.ifindex:
                self._stop_monitor()
                raise RuntimeError('WWAN interface identity changed; routes and DNS were not modified')
            local_ready = True
            routes = self._default_routes() if self.settings.default_route or self.route else []
            before = list(self.route) if self.route else None
            policy = None if verified else 'backup'
            try:
                self._configure_route(self.gateway, routes, policy=policy, allow_no_route=enabled)
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                if not enabled:
                    raise
                applied['route_error'] = str(error)
                # A failed promotion must not discard a usable fallback route.
                self._configure_route(self.gateway, policy='backup', allow_no_route=True)
            if before != self.route:
                routes = self._default_routes()
            applied['preferred'] = self._preferred_route(routes)
            if not self.settings.default_route:
                applied['route_mode'] = 'disabled'
            elif not self.route:
                applied['route_mode'] = 'blocked'
                applied['route_error'] = applied['route_error'] or (
                    'No safe WWAN backup default route is possible: another route uses metric '
                    f'{MAX_ROUTE_METRIC}; the owned default route was withdrawn')
            else:
                applied['route_mode'] = 'requested' if verified and not applied['route_error'] else 'fallback'
            if verified and applied['preferred'] and self.settings.dns != 'none' and self.dns_addresses:
                try:
                    self.configure_dns(self.dns_addresses)
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                    applied['dns_error'] = str(error)
                    if not enabled:
                        raise
                    # TCP reachability alone cannot justify taking over the host
                    # when the requested DNS configuration did not complete.
                    applied['preferred'] = False
                    try:
                        self._configure_route(self.gateway, policy='backup', allow_no_route=True)
                        applied['route_mode'] = 'fallback' if self.route else 'blocked'
                        if not self.route:
                            applied['route_error'] = ('Cannot retain a WWAN backup default route '
                                                      'after the cellular DNS configuration failed')
                    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as route_error:
                        applied.update(route_mode='blocked', route_error=str(route_error))
                    # Demote first. Even if reverting DNS fails, Ethernet must
                    # regain route priority; keep both diagnostics for retry.
                    try:
                        self._revoke_dns()
                    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as revoke_error:
                        applied['dns_error'] += '; DNS rollback failed: ' + str(revoke_error)
            else:
                try:
                    self._revoke_dns()
                except (OSError, subprocess.SubprocessError) as error:
                    applied['dns_error'] = str(error)
                    if not enabled:
                        raise
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            if not enabled:
                raise
            applied.update(route_mode='blocked', preferred=False)
            applied['route_error'] = applied['route_error'] or str(error)
            if local_ready:
                try:
                    self._revoke_dns()
                except (OSError, subprocess.SubprocessError) as dns_error:
                    applied['dns_error'] = str(dns_error)
        self._connectivity_applied = applied
        if local_ready:
            # A route that cannot be installed is reported; it never touches the connection.
            conflict = self._routes_conflict
            try:
                self._maintain_extra_routes()
                error = None
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as failure:
                error = str(failure)
            if error and error != self._routes_error:
                logging.getLogger(__name__).warning('WWAN routes: %s', error.replace('\n', ' ')[:400])
            self._routes_error = error
            if self._routes_conflict and self._routes_conflict != conflict:
                logging.getLogger(__name__).warning(
                    'WWAN routes: automatic route to %(network)s not added, it overlaps %(overlaps)s on %(dev)s; other hosts '
                    'of the mobile network stay unreachable through the modem until network.routes lists them by hand',
                    self._routes_conflict)
        # Install the backup before starting a bound probe, even on the first round.
        if local_ready:
            self._start_monitor()
        result = self.connectivity_status
        self._log_connectivity(result)
        return result

    def attach(self, interface):
        self._stop_monitor()
        self.interface = interface
        link = json.loads(self.command("ip", "-j", "link", "show", "dev", interface))[0]
        self.original = "UP" in link.get("flags", [])
        self.original_mtu = link["mtu"]
        self.ifindex = link["ifindex"]
        addresses = json.loads(self.command("ip", "-j", "-4", "addr", "show", "dev", interface))
        if any(a.get("scope") == "global" for item in addresses for a in item.get("addr_info", [])):
            # Do not take over a connection owned by ModemManager, netifd or NetworkManager.
            self.interface = None
            raise RuntimeError(f"{interface} already has IPv4 configuration; use monitor mode or free the interface")
        self.changed()

    def prepare(self, raw):
        path = Path("/sys/class/net") / self.interface / "qmi/raw_ip"
        if path.exists():
            self.raw_original = path.read_text().strip()
            desired = "Y" if raw else "N"
            if self.raw_original != desired:
                self.command("ip", "link", "set", "dev", self.interface, "down")
                path.write_text(desired + "\n")
        self.command("ip", "link", "set", "dev", self.interface, "up")

    def configure(self, values):
        if not values.get("address"):
            raise RuntimeError("Modem did not provide an IPv4 address")
        prefix = ipaddress.IPv4Network("0.0.0.0/" + (values.get("netmask") or "255.255.255.255")).prefixlen
        address = f"{ipaddress.IPv4Address(values['address'])}/{prefix}"
        gateway = str(ipaddress.IPv4Address(values['gateway'])) if values.get('gateway') else None
        dns_addresses = [str(ipaddress.IPv4Address(value)) for value in values.get('dns', []) or []]
        links = json.loads(self.command('ip', '-j', 'link', 'show', 'dev', self.interface))
        if not links or links[0].get('ifindex') != self.ifindex:
            self._stop_monitor()
            raise RuntimeError('WWAN interface identity changed before IPv4 configuration')
        if self.address != address or self.gateway != gateway:
            self._stop_monitor()
        if self.address and self.address != address:
            self.command("ip", "addr", "del", self.address, "dev", self.interface)
            self.address = None
            self.changed()
        if self.address != address:
            self.command("ip", "addr", "add", address, "dev", self.interface)
            self.address = address
            self.changed()
        mtu = values.get("mtu")
        if mtu and 576 <= int(mtu) <= 65535:
            self.command("ip", "link", "set", "dev", self.interface, "mtu", str(mtu))
        if self.gateway != gateway or self.dns_addresses != dns_addresses:
            self.gateway, self.dns_addresses = gateway, dns_addresses
            self.changed()
        self.maintain_connectivity(force=True)

    def configure_dns(self, addresses):
        if self.settings.dns == "none" or not addresses:
            return
        addresses = [str(ipaddress.IPv4Address(a)) for a in addresses]
        if (self.dns_backend and self._dns_applied == tuple(addresses) and
                self.settings.dns in ('auto', self.dns_backend)):
            return
        candidates = [self.settings.dns] if self.settings.dns != "auto" else ["resolvectl", "resolvconf"]
        if self.dns_backend:
            if self.dns_backend in candidates:
                # Keep an active backend until its owned state is revoked.
                # Resolver availability can change without a configuration edit.
                candidates.remove(self.dns_backend)
                candidates.insert(0, self.dns_backend)
            else:
                self._revoke_dns()
        for candidate in candidates:
            if not shutil.which(candidate):
                continue
            try:
                if candidate == "resolvectl":
                    self.command(candidate, "dns", self.interface, *addresses)
                    self.dns_backend = candidate
                    self.changed()
                    if self.settings.default_route:
                        self.command(candidate, "domain", self.interface, "~.")
                    self.command(candidate, "default-route", self.interface,
                                 "yes" if self.settings.default_route else "no")
                else:
                    # A name may be reused after USB removal. Journal a private
                    # record before applying it so old cleanup cannot revoke a
                    # peer's DNS, including after a partial command or crash.
                    if self.dns_record is None:
                        self.dns_record = self.interface+'.qmi-supervisor-'+uuid.uuid4().hex
                    self.dns_backend = candidate
                    self.changed()
                    self.command(candidate, "-a", self.dns_record,
                                 input="".join(f"nameserver {a}\n" for a in addresses))
                self._dns_applied = tuple(addresses)
                return
            except (OSError, subprocess.SubprocessError):
                # A partial update must be undone through the recorded backend.
                # Only try an alternative if this backend did not modify anything.
                if self.settings.dns != "auto" or self.dns_backend == candidate:
                    raise
        raise RuntimeError("Cannot install DNS: install/enable resolvconf or systemd-resolved, or set network.dns=none")

    def start_dhcp(self):
        self.lease_dir = tempfile.TemporaryDirectory(prefix="qmi-supervisor-")
        env = dict(os.environ, QMI_SUPERVISOR_LEASE=str(Path(self.lease_dir.name) / "lease.json"))
        hook = Path(__file__).with_name("dhcp_hook.py")
        self.dhcp = subprocess.Popen(["udhcpc", "-f", "-n", "-t", "5", "-T", "1", "-i", self.interface,
                                      "-s", str(hook)], env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline and not self.stop.is_set():
            if self.refresh_dhcp():
                return
            if self.dhcp.poll() is not None:
                break
            self.stop.wait(0.2)
        raise RuntimeError("DHCP did not provide an IPv4 lease")

    def refresh_dhcp(self):
        if self.dhcp.poll() is not None:
            return False
        path = Path(self.lease_dir.name) / "lease.json"
        try:
            values = json.loads(path.read_text())
        except FileNotFoundError:
            return False
        if values != self.last_lease:
            self.configure(values)
            self.last_lease = values
        return True

    def healthy(self):
        if self.dhcp and not self.refresh_dhcp():
            return False
        links = json.loads(self.command("ip", "-j", "link", "show", "dev", self.interface))
        if not links or "UP" not in links[0].get("flags", []):
            return False
        if self.ifindex is not None and links[0].get('ifindex') != self.ifindex:
            return False
        addresses = json.loads(self.command("ip", "-j", "-4", "addr", "show", "dev", self.interface))
        wanted = self.address.split("/") if self.address else []
        if not any([a.get("local"), str(a.get("prefixlen"))] == wanted
                   for item in addresses for a in item.get("addr_info", [])):
            return False
        self.maintain_connectivity(force=not self._check_enabled())
        if self._check_enabled():
            # Reachability is an observation, not a failed local/QMI connection.
            return True
        if self.route:
            return True
        return not self.settings.default_route

    def close(self):
        self._stop_monitor()
        self.cleaning = True
        try:
            if self.dhcp:
                self.dhcp.terminate()
                try:
                    self.dhcp.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.dhcp.kill()
                    self.dhcp.wait(timeout=3)
                self.dhcp = None
            if self.lease_dir:
                self.lease_dir.cleanup()
                self.lease_dir = None
            if not self.interface:
                return
            try:
                links = json.loads(self.command('ip', '-j', 'link', 'show', 'dev', self.interface))
                same_interface = bool(links) and links[0].get('ifindex') == self.ifindex
            except (OSError, subprocess.SubprocessError) as inspection_error:
                # An unplug removes addresses/routes with the old ifindex. A failed
                # inspection alone is not evidence that our resources disappeared.
                links = json.loads(self.command('ip', '-j', 'link', 'show'))
                if any(link.get('ifindex') == self.ifindex for link in links):
                    raise inspection_error
                same_interface = False
            errors = []
            if not same_interface:
                # Never mutate a new device reusing the same interface name.
                self.route = self.address = self.original_mtu = self.original = None
                self.extra_routes = []
                if self.dns_backend == 'resolvectl':
                    self.dns_backend = None
            if self.dns_backend:
                try:
                    if self.dns_backend == 'resolvectl':
                        self.command('resolvectl', 'revert', self.interface)
                    elif self.dns_backend == 'resolvconf':
                        self.command('resolvconf', '-d', self.dns_record or self.interface + '.qmi-supervisor')
                    self.dns_backend = None
                    self.dns_record = None
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append(error)
            if same_interface and self.extra_routes:
                try:
                    present = self._main_routes()
                    for spec in list(self.extra_routes):
                        if any(self._matches_extra(record, spec) for record in present):
                            self.command('ip', 'route', 'del', *spec)
                        self.extra_routes.remove(spec)
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                    errors.append(error)
            if same_interface and self.route:
                try:
                    routes = self._default_routes()
                    if any(self._matches_route(record, self.route) for record in routes):
                        self.command('ip', 'route', 'del', *self.route)
                    self.route = None
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                    errors.append(error)
            if same_interface and self.address:
                try:
                    addresses = json.loads(self.command('ip', '-j', '-4', 'addr', 'show', 'dev', self.interface))
                    wanted = self.address.split('/')
                    if any([address.get('local'), str(address.get('prefixlen'))] == wanted
                           for record in addresses for address in record.get('addr_info', [])):
                        self.command('ip', 'addr', 'del', self.address, 'dev', self.interface)
                    self.address = None
                except (OSError, subprocess.SubprocessError, ValueError) as error:
                    errors.append(error)
            if same_interface and not self.route and not self.address and not self.extra_routes:
                if self.original_mtu:
                    try:
                        self.command('ip', 'link', 'set', 'dev', self.interface, 'mtu', str(self.original_mtu))
                        self.original_mtu = None
                    except (OSError, subprocess.SubprocessError) as error:
                        errors.append(error)
                if self.original is False:
                    try:
                        self.command('ip', 'link', 'set', 'dev', self.interface, 'down')
                        self.original = None
                    except (OSError, subprocess.SubprocessError) as error:
                        errors.append(error)
            if errors:
                # Retain the interface and each unconfirmed resource for retry and
                # Backend's cleanup-pending checkpoint; successful deletions stay cleared.
                raise errors[0]
            self.interface = self.address = self.route = self.dns_backend = self.last_lease = None
            self.extra_routes, self._routes_error, self._routes_conflict = [], None, None
            self.dns_record = None
            self.original = self.original_mtu = self.ifindex = None
            self.gateway, self.dns_addresses, self._dns_applied = None, [], None
            self._connectivity_applied = {'route_mode': 'disabled', 'preferred': False,
                                          'route_error': None, 'dns_error': None}
            # No changed() here: recover() has not yet readopted its saved WDS CID.
            # Keep driver/firmware framing in sync; do not restore only one side.
        finally:
            self.cleaning = False

    def checkpoint(self):
        return {key: getattr(self, key) for key in ("interface", "address", "route", "dns_backend", "dns_record",
                                                   "original", "original_mtu", "ifindex", 'gateway', 'dns_addresses',
                                                   'extra_routes')}

    def restore(self, values):
        self._stop_monitor()
        for key in ('interface', 'address', 'route', 'dns_backend', 'original', 'original_mtu', 'ifindex'):
            setattr(self, key, values.get(key))
        self.dns_record = values.get('dns_record')
        self.gateway = values.get('gateway', self._gateway(self.route))
        self.dns_addresses = list(values.get('dns_addresses') or [])
        self.extra_routes = [list(spec) for spec in values.get('extra_routes') or []]
        self._dns_applied = None
        self._next_maintenance = 0
        self._connectivity_applied = {'route_mode': 'disabled', 'preferred': False,
                                      'route_error': None, 'dns_error': None}
