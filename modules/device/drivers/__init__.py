"""Driver registry: only listed drivers can be loaded."""
from importlib import import_module

DRIVERS = {'qmi': 'modules.device.backend:QmiBackend'}
ENTRY_POINT_GROUP = 'qmi_supervisor.drivers'


def trusted_names(settings):
    raw = getattr(getattr(settings, 'system', None), 'trusted_drivers', '') or ''
    return [name.strip() for name in str(raw).split(',') if name.strip()]


def available_drivers(settings=None):
    """Registry names plus trusted entry points the administrator allowed."""
    names = set(DRIVERS)
    if settings is None:
        return sorted(names)
    allowed = set(trusted_names(settings))
    if allowed:
        try:
            from importlib.metadata import entry_points
            for entry in entry_points(group=ENTRY_POINT_GROUP):
                if entry.name in allowed:
                    names.add(entry.name)
        except Exception:
            pass
    return sorted(names)


def load_driver(name, settings, stop, state_path):
    """Import the driver class; trusted entry points only when allow-listed."""
    if name in DRIVERS:
        module, attribute = DRIVERS[name].split(':')
        return getattr(import_module(module), attribute)
    if name in trusted_names(settings):
        try:
            from importlib.metadata import entry_points
            for entry in entry_points(group=ENTRY_POINT_GROUP):
                if entry.name == name:
                    return entry.load()
        except Exception:
            pass
    raise ValueError('Unknown or untrusted modem driver: ' + str(name))
