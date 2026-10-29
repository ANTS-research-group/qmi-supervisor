"""Independent modem profiles sharing one web configuration and transaction."""
from configparser import ConfigParser
from dataclasses import asdict, dataclass, replace
from io import StringIO
import hashlib
import json
import re

from modules.runtime.ipc import revision, settings_values
from modules.runtime.settings import Settings, read_settings, validate_settings


def valid_profile(identifier):
    return isinstance(identifier, str) and bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', identifier))


@dataclass(frozen=True)
class Configuration:
    base: Settings
    profiles: dict

    @classmethod
    def load(cls, path=None):
        return cls(*read_settings(path))

    @property
    def revision(self):
        if not self.profiles:
            return revision(self.base)
        value = {'default': settings_values(self.base),
                 'profiles': {key: settings_values(value) for key, value in self.profiles.items()}}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]

    def select(self, identifier='default'):
        if identifier == 'default':
            return self.base
        if identifier not in self.profiles:
            raise ValueError(f'Unknown modem profile: {identifier}')
        return self.profiles[identifier]

    def update(self, identifier, settings, *, validate=validate_settings):
        validate(settings)
        profiles = dict(self.profiles)
        if identifier == 'default':
            base = settings
        else:
            if not valid_profile(identifier):
                raise ValueError('Invalid modem profile identifier')
            profiles[identifier] = settings
            base = replace(self.base, web=settings.web, system=settings.system)
        # Web, authentication and supervisor policy are platform-wide, never modem-specific.
        profiles = {key: replace(value, web=base.web, system=base.system) for key, value in profiles.items()}
        return Configuration(base, profiles)

    def remove(self, identifier):
        if identifier == 'default' or identifier not in self.profiles:
            raise ValueError('Only existing named modem profiles can be removed')
        return Configuration(self.base, {key: value for key, value in self.profiles.items() if key != identifier})

    def render(self, *, redact=False):
        parser = ConfigParser(interpolation=None)
        for identifier, settings in [(None, self.base), *sorted(self.profiles.items())]:
            for section, values in asdict(settings).items():
                if identifier is not None and section in ('web', 'system'):
                    continue
                name = section if identifier is None else f'{section}:{identifier}'
                parser[name] = {key: ('<redacted>' if value else '<not set>')
                    if redact and section == 'web' and key == 'password_hash'
                    else str(value).lower() if isinstance(value, bool) else str(value)
                    for key, value in values.items()}
        stream = StringIO()
        stream.write('# Managed by QMI Supervisor; web access is shared by all modem profiles.\n')
        parser.write(stream)
        return stream.getvalue()
