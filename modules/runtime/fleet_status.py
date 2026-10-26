"""Merge observed per-UE state with current platform assignments and intent."""
import math


def modem_snapshot(snapshot, entry, *, manager_heartbeat=None, boot_id=None):
    """Do not label another or a removed modem's observations as current data."""
    snapshot = snapshot or {}
    expected = entry.get('modem_worker_id')
    owner = (snapshot.get('processes') or {}).get('modem') or {}
    wrong_owner = 'modem_worker_id' in entry and (
        (expected is None and snapshot.get('state') != 'IDLE') or owner.get('worker_id') != expected)
    wrong_owner = wrong_owner or ('profile_instance_id' in entry and (
        entry['profile_instance_id'] is None or snapshot.get('profile_instance_id') != entry['profile_instance_id']))
    if not snapshot or not entry.get('detected') or entry.get('assignment_pending') or wrong_owner:
        return dict(state=entry.get('state', 'WAIT_DEVICE'), device=entry.get('device'),
            modem_readable=False, features=entry.get('features', {}), error=entry.get('error'),
            sample=None, history=[], pipeline_history=[], network={},
            heartbeat_monotonic=manager_heartbeat, boot_id=boot_id)
    result = dict(snapshot, device=entry.get('device'),
                  features=entry.get('features', snapshot.get('features', {})))
    result['error'] = entry.get('error') or snapshot.get('error')
    if entry.get('modem_alive') is False:
        result.update(state=entry.get('state', 'BACKOFF'), modem_readable=False, sample=None,
                      radio={}, radio_summary={}, network={})
        if isinstance(result.get('connection'), dict):
            result['connection'] = dict(result['connection'], phase=result['state'])
    elif result.get('state') == 'IDLE':
        result.update(modem_readable=False, sample=None)
    return result


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def signal_summary(record, *, now, interval=1.0, alive=True):
    """Latest radio reading of one modem for the fleet list, or None.

    Only an owner-checked record of a running modem worker is used, and only
    while its newest sample is recent (the dashboard's own freshness rule), so
    the fleet never shows an old reading as current.
    """
    if not alive or not isinstance(record, dict):
        return None
    history = record.get('history')
    point = history[-1] if isinstance(history, list) and history and isinstance(history[-1], dict) else None
    observed = _number(point.get('monotonic')) if point else None
    if observed is None or now - observed > max(10.0, 3.0 * (_number(interval) or 1.0)):
        return None
    radio = record.get('radio') if isinstance(record.get('radio'), dict) else {}
    sample = record.get('sample') if isinstance(record.get('sample'), dict) else {}
    dynamic = sample.get('dynamic') if isinstance(sample.get('dynamic'), dict) else {}
    operator = dynamic.get('nas_value_current_plmn_description')
    if not isinstance(operator, str) or not operator.strip():
        operator = next((cell['plmn'] for cell in radio.get('cells') or []
                         if isinstance(cell, dict) and isinstance(cell.get('plmn'), str) and cell['plmn']), None)
    band = next((str(item.get('label') or item.get('band')) for item in radio.get('bands') or []
                 if isinstance(item, dict) and (item.get('label') or item.get('band'))), None)
    summary = dict(rat=point.get('rat') if isinstance(point.get('rat'), str) else None, band=band,
                   operator=operator.strip() if isinstance(operator, str) else None,
                   rsrp=_number(point.get('rsrp')), snr=_number(point.get('snr')))
    return summary if any(value is not None for value in summary.values()) else None
