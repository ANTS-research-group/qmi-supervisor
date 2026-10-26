"""Validated user configuration; deliberately independent of GI and hardware."""
from configparser import ConfigParser, Error as IniError
from dataclasses import dataclass, field, replace
from pathlib import Path
import math
import ipaddress
import re

from modules.web.auth import is_password_hash
from modules.network.connectivity import parse_targets


@dataclass(frozen=True)
class ModemSettings:
    device: str = "auto"
    serial: str = ""
    apn: str = "internet"
    plmn: str = ""
    mode: str = "connect"


@dataclass(frozen=True)
class NetworkSettings:
    method: str = "qmi"
    route_metric: int = 700
    default_route: bool = True
    dns: str = "auto"
    route_policy: str = "fixed"
    connectivity_check: bool = True
    connectivity_targets: str = "1.1.1.1:443,8.8.8.8:53"
    connectivity_interval: float = 3.0
    connectivity_timeout: float = 1.0
    connectivity_failures: int = 2
    connectivity_successes: int = 2


@dataclass(frozen=True)
class RecoverySettings:
    retry_initial: float = 2
    retry_max: float = 60
    registration_timeout: float = 180
    operation_timeout: int = 20
    poll_interval: float = 10


@dataclass(frozen=True)
class TelemetrySettings:
    enabled: bool = True
    interval: float = 1.0
    directory: str = "files"
    csv: bool = True
    json: bool = True
    udp_host: str = ""
    udp_port: int = 5006
    collector_host: str = ""
    collector_port: int = 5007
    throughput: bool = False
    gps: bool = False
    gps_source: str = 'gpsd'
    queue_size: int = 64
    flush_interval: float = 1.0
    gps_max_age: float = 10.0
    request_timeout: int = 1
    collection_budget: float = 0.8
    profile: str = "full"
    # Refresh periods (seconds) for profile=custom; 0 disables the group.
    query_signal: float = 1.0
    query_cell: float = 1.0
    query_txrx: float = 1.0
    query_serving: float = 5.0
    query_bands: float = 10.0
    query_strength: float = 0.0
    query_home: float = 3600.0
    query_wds_stats: float = 1.0
    query_wds_status: float = 1.0
    query_wds_rates: float = 10.0
    # Modem-pushed values for supported groups: off, shadow (compare) or on.
    indications: str = "off"


@dataclass(frozen=True)
class WebSettings:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    history_size: int = 120
    username: str = "admin"
    password_hash: str = ""
    session_timeout: int = 28800
    # Platform-wide runtime limits of the web child and its browser client.
    max_workers: int = 8
    address_refresh: float = 10.0
    poll_interval: float = 2.0


@dataclass(frozen=True)
class SystemSettings:
    """Platform-wide supervisor behaviour, shared by every modem profile."""
    log_level: str = "INFO"


@dataclass(frozen=True)
class Settings:
    system: SystemSettings = field(default_factory=SystemSettings)
    modem: ModemSettings = field(default_factory=ModemSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    recovery: RecoverySettings = field(default_factory=RecoverySettings)
    telemetry: TelemetrySettings = field(default_factory=TelemetrySettings)
    web: WebSettings = field(default_factory=WebSettings)


def read_settings(path=None):
    """Read legacy defaults and named modem profiles through the same validator."""
    parser = ConfigParser(interpolation=None)
    if path is not None:
        try:
            with open(path, encoding="utf-8") as stream:
                parser.read_file(stream)
        except (OSError, IniError) as error:
            raise ValueError(f"Cannot read configuration {path}: {error}") from error
    defaults = Settings()
    if parser.defaults():
        raise ValueError("Use explicit configuration sections instead of [DEFAULT]")
    groups = {None: {}}
    for section in parser.sections():
        name, separator, profile = section.partition(':')
        if (name not in defaults.__dataclass_fields__ or
                (separator and (name in ('web', 'system') or profile == 'default' or
                                not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', profile)))):
            raise ValueError(f"Unknown configuration section [{section}]")
        groups.setdefault(profile if separator else None, {})[name] = section

    def read_group(group, settings):
        sections = {}
        for name, section in group.items():
            base = getattr(settings, name)
            values = {}
            for key in parser[section]:
                if key not in base.__dataclass_fields__:
                    raise ValueError(f"Unknown option [{section}] {key}")
                exemplar = getattr(base, key)
                try:
                    if isinstance(exemplar, bool):
                        value = parser.getboolean(section, key)
                    else:
                        value = base.__dataclass_fields__[key].type(parser[section][key].strip())
                except ValueError as error:
                    raise ValueError(f"Invalid value for [{section}] {key}") from error
                values[key] = value
            sections[name] = replace(base, **values)
        return validate_settings(replace(settings, **sections))

    settings = read_group(groups.pop(None), defaults)
    profiles = {}
    for identifier, group in groups.items():
        if 'modem' not in group:
            raise ValueError(f"Profile {identifier} requires [modem:{identifier}]")
        profiles[identifier] = read_group(group, settings)
    return settings, profiles


def load_settings(path=None, device=None, *, profile=None):
    settings, profiles = read_settings(path)
    if profile not in {None, 'default'}:
        try:
            settings = profiles[profile]
        except KeyError:
            raise ValueError(f"Unknown modem profile: {profile}") from None
    if device:
        settings = replace(settings, modem=replace(settings.modem, device=device))
    return validate_settings(settings)


def validate_settings(settings):
    for section in vars(settings).values():
        for name, value in vars(section).items():
            if isinstance(value, str) and any(char in value for char in ("\n", "\r", "\x00")):
                raise ValueError(f"{name} must fit on one line")
    for value, allowed, name in [
        (settings.modem.mode, {"connect", "monitor"}, "modem.mode"),
        (settings.network.method, {"qmi", "dhcp"}, "network.method"),
        (settings.network.dns, {"auto", "none", "resolvectl", "resolvconf"}, "network.dns"),
        (settings.network.route_policy, {'fixed', 'primary', 'backup'}, 'network.route_policy'),
        (settings.telemetry.profile, {"fast", "standard", "full", "custom"}, "telemetry.profile"),
        (settings.telemetry.gps_source, {'gpsd', 'modem'}, 'telemetry.gps_source'),
        (settings.telemetry.indications, {'off', 'shadow', 'on'}, 'telemetry.indications'),
        (settings.system.log_level, {'DEBUG', 'INFO', 'WARNING', 'ERROR'}, 'system.log_level'),
    ]:
        if value not in allowed:
            raise ValueError(f"{name} must be one of {', '.join(sorted(allowed))}")
    if not settings.modem.device or (settings.modem.device != "auto" and not Path(settings.modem.device).is_absolute()):
        raise ValueError("modem.device must be auto or an absolute device path")
    if settings.modem.mode == "connect" and not settings.modem.apn:
        raise ValueError("modem.apn is required in connect mode")
    plmn = settings.modem.plmn
    if plmn not in {'', 'auto'} and not (len(plmn) in {5, 6} and plmn.isascii() and plmn.isdigit()):
        raise ValueError('modem.plmn must be empty, auto, or 5/6 ASCII digits (MCC+MNC)')
    if type(settings.network.route_metric) is not int or not 0 <= settings.network.route_metric <= 4294967295:
        raise ValueError("network.route_metric must be an integer between 0 and 4294967295")
    if type(settings.network.connectivity_check) is not bool:
        raise ValueError("network.connectivity_check must be a boolean")
    parse_targets(settings.network.connectivity_targets)
    for key, minimum, maximum in (("connectivity_interval", 1, 60), ("connectivity_timeout", 0.1, 5)):
        value = getattr(settings.network, key)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"network.{key} must be between {minimum} and {maximum} seconds")
    if settings.network.connectivity_timeout > settings.network.connectivity_interval:
        raise ValueError("network.connectivity_timeout must be <= connectivity_interval")
    for key in ("connectivity_failures", "connectivity_successes"):
        value = getattr(settings.network, key)
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError(f"network.{key} must be an integer between 1 and 10")
    for key, value in vars(settings.recovery).items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"recovery.{key} must be positive and finite")
    if settings.recovery.retry_max < settings.recovery.retry_initial:
        raise ValueError("recovery.retry_max must be >= retry_initial")
    if not math.isfinite(settings.telemetry.interval) or settings.telemetry.interval <= 0:
        raise ValueError("telemetry.interval must be positive and finite")
    for key in ("flush_interval", "gps_max_age", "collection_budget"):
        value = getattr(settings.telemetry, key)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"telemetry.{key} must be positive and finite")
    if not 1 <= settings.telemetry.queue_size <= 4096:
        raise ValueError("telemetry.queue_size must be between 1 and 4096")
    if not 1 <= settings.telemetry.request_timeout <= 60:
        raise ValueError("telemetry.request_timeout must be between 1 and 60")
    if settings.telemetry.collection_budget > settings.telemetry.interval:
        raise ValueError("telemetry.collection_budget must be <= telemetry.interval")
    for key, value in vars(settings.telemetry).items():
        if key.startswith('query_') and (not math.isfinite(value) or not 0 <= value <= 86400):
            raise ValueError(f"telemetry.{key} must be between 0 (disabled) and 86400 seconds")
    if (settings.telemetry.profile == 'custom' and settings.modem.mode == 'connect'
            and settings.telemetry.query_wds_status == 0):
        # Connection health relies on the sampled bearer status.
        raise ValueError("telemetry.query_wds_status cannot be 0 in connect mode")
    if not settings.telemetry.directory:
        raise ValueError("telemetry.directory cannot be empty")
    for port in (settings.telemetry.udp_port, settings.telemetry.collector_port, settings.web.port):
        if not 1 <= port <= 65535:
            raise ValueError("Telemetry ports must be between 1 and 65535")
    if not 1 <= settings.web.history_size <= 1000:
        raise ValueError("web.history_size must be between 1 and 1000")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", settings.web.username):
        raise ValueError("web.username must contain 1 to 64 letters, digits or _.@-")
    if settings.web.password_hash and not is_password_hash(settings.web.password_hash):
        raise ValueError("web.password_hash is not a supported password hash")
    if not 300 <= settings.web.session_timeout <= 604800:
        raise ValueError("web.session_timeout must be between 300 and 604800 seconds")
    if type(settings.web.max_workers) is not int or not 1 <= settings.web.max_workers <= 32:
        raise ValueError("web.max_workers must be an integer between 1 and 32")
    for key, minimum, maximum in (("address_refresh", 5, 600), ("poll_interval", 1, 30)):
        value = getattr(settings.web, key)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"web.{key} must be between {minimum} and {maximum} seconds")
    try:
        loopback = settings.web.host != 'auto' and ipaddress.ip_address(settings.web.host).is_loopback
    except ValueError:
        raise ValueError("web.host must be auto or a numeric IP address") from None
    if (settings.web.enabled and not loopback and not settings.web.password_hash):
        raise ValueError("LAN access requires a web password")
    return settings
