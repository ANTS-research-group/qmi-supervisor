"""Deterministic route priorities for independently managed cellular interfaces."""

MAX_METRIC = (1 << 32)-1


def current_owned_routes(peers, interface):
    """Read synchronous ownership evidence rather than delayed UI snapshots."""
    from modules.runtime.state import RuntimeState
    result = []
    for entry in peers:
        entry = dict(entry)
        if entry.get('enabled') and entry.get('interface') != interface and entry.get('checkpoint'):
            saved = RuntimeState(entry['checkpoint']).load()
            entry['route'] = None
            if saved is not None:
                if any(saved.get(key) != entry.get(key) for key in ('identity', 'usb_path', 'enumeration')):
                    raise RuntimeError('Coordinated route journal belongs to another modem transport')
                network = saved.get('network')
                if not isinstance(network, dict) or network.get('interface') != entry['interface']:
                    raise RuntimeError('Coordinated route journal has another interface')
                entry['route'] = network.get('route')
        result.append(entry)
    return result


def coordinated_metric(routes, peers, interface, policy, metric, owned_route, matches, *, allow_no_route=False):
    """Exclude only recorded owned tuples; never compete with a peer's metric."""
    entries = {entry['interface']: dict(entry) for entry in peers if entry.get('enabled')}
    if interface not in entries:
        raise RuntimeError('Awaiting coordinated route settings for this modem')
    entries[interface].update(policy=policy, metric=metric, route=owned_route, fallback=False)
    for name, entry in entries.items():
        if name != interface and entry.get('fallback'):
            entry['policy'] = 'backup'
    foreign = []
    for record in routes:
        if any(matches(record, entry.get('route')) for entry in entries.values()):
            continue
        value = record.get('metric', 0)
        if type(value) is not int or not 0 <= value <= MAX_METRIC:
            raise RuntimeError('Cannot determine the metric of another IPv4 default route')
        foreign.append(value)
    result, used = {}, set()
    ordered = sorted(entries, key=lambda name: (entries[name]['metric'], entries[name]['id']))
    for name in ordered:
        entry = entries[name]
        if entry['policy'] == 'fixed':
            value = entry['metric']
            while value in used and value <= MAX_METRIC:
                value += 1
            result[name] = value if value <= MAX_METRIC else None
            if result[name] is not None:
                used.add(value)
    ceiling = min(foreign)-1 if foreign else MAX_METRIC
    for name in reversed([name for name in ordered if entries[name]['policy'] == 'primary']):
        value = min(entries[name]['metric'], ceiling)
        while value in used and value >= 0:
            value -= 1
        result[name] = value if value >= 0 else None
        if value >= 0:
            used.add(value)
            ceiling = value-1
    floor = max([*foreign, *used], default=-1)+1
    for name in ordered:
        if entries[name]['policy'] == 'backup':
            value = max(entries[name]['metric'], floor)
            result[name] = value if value <= MAX_METRIC else None
            if result[name] is not None:
                floor = value+1
    value = result.get(interface)
    if value is None and (policy != 'backup' or not allow_no_route):
        raise RuntimeError('No distinct IPv4 default-route metric is available for this modem policy')
    return value
