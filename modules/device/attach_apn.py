"""Synchronize only the APN of the modem's current default LTE attach profile.

Stored profile APNs are not evidence of the APN negotiated with the network.
The caller owns RF transitions: before_change lowers RF after the journal is
saved; mark_completed is called only after ONLINE and registration succeed.
"""
import gi
gi.require_version('Qmi', '1.0')
from gi.repository import GLib, Qmi
import os
from pathlib import Path
import uuid

from modules.runtime.state import RuntimeState


DEFAULT_STATE_PATH = '/run/qmi-supervisor-attach.json'


class _Fallback(Exception):
    def __init__(self, status):
        self.status = status


class AttachApn:
    def __init__(self, session, device, state_path=DEFAULT_STATE_PATH, *, enumeration=None):
        self.session, self.device = session, device
        self.state = RuntimeState(state_path)
        self.enumeration = (enumeration if callable(enumeration) else
                            (lambda: enumeration) if isinstance(enumeration, str) else self._enumeration)
        self._completion_token = None

    def _enumeration(self):
        try:
            return (Path(self.device.usb_path) / 'devnum').read_text().strip()
        except OSError:
            return ''

    @staticmethod
    def _known_fallback(error):
        if error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOTSUPPORTED):
            return 'fallback_unsupported'
        if error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.INFORMATIONUNAVAILABLE):
            return 'fallback_information_unavailable'
        if error.matches(Qmi.core_error_quark(), Qmi.CoreError.UNSUPPORTED):
            return 'fallback_unsupported'
        return None

    def _request(self, name, value=None):
        if not hasattr(Qmi.ClientWds, name):
            raise _Fallback('fallback_unsupported')
        try:
            return self.session.request(Qmi.Service.WDS, name, value)
        except GLib.Error as error:
            fallback = self._known_fallback(error)
            if fallback:
                raise _Fallback(fallback) from error
            raise

    @classmethod
    def _field(cls, output, name):
        getter = getattr(output, name, None)
        if getter is None:
            return None
        try:
            return getter()
        except GLib.Error as error:
            if any(error.matches(Qmi.core_error_quark(), code)
                   for code in (Qmi.CoreError.TLVNOTFOUND, Qmi.CoreError.TLVEMPTY)):
                return None
            fallback = cls._known_fallback(error)
            if fallback:
                raise _Fallback(fallback) from error
            raise

    @staticmethod
    def _profiles(value):
        if value is None:
            return None
        values = list(value)
        if any(isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 255
               for index in values):
            raise ValueError('Invalid LTE attach profile list')
        return values

    def _profile(self, profile_id):
        if not hasattr(Qmi, 'MessageWdsGetProfileSettingsInput'):
            raise _Fallback('fallback_unsupported')
        value = Qmi.MessageWdsGetProfileSettingsInput()
        # GI exposes the enum member as "3GPP", not a Python identifier.
        value.set_profile_id(Qmi.WdsProfileType(0), profile_id)
        return self._request('get_profile_settings', value)

    def _load(self):
        saved = self.state.load()
        if saved is None:
            return None
        if (saved.get('kind') != 'attach_apn' or not isinstance(saved.get('identity'), str)
                or not isinstance(saved.get('enumeration'), str) or not saved.get('token')
                or not isinstance(saved.get('requested_apn'), str)
                or type(saved.get('profile_id')) is not int or not 0 <= saved['profile_id'] <= 255):
            raise ValueError('Invalid attach APN checkpoint')
        return saved

    def _save(self, record):
        # This is setup work, outside 1 Hz capture. Finish the journal before
        # any RF/profile mutation; /run remains volatile across a machine reboot.
        self.state.save(record)
        with self.state.path.open('rb') as stream:
            os.fsync(stream.fileno())
        directory = os.open(self.state.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def ensure(self, requested_apn, *, mode='connect', before_change=None):
        self._completion_token = None
        report = dict(status='monitor' if mode != 'connect' else None, requested_apn=requested_apn,
                      profile_id=None, profile_apn=None, current_profiles=None, pending_profiles=None,
                      changed=False, needs_reregister=False, recovered_pending=False,
                      ignored_pending=False, replaced_pending=False)
        if mode != 'connect':
            return report
        if (not isinstance(requested_apn, str) or not requested_apn.strip()
                or any(char in requested_apn for char in ('\x00', '\n', '\r'))):
            raise ValueError('Attach APN must be a nonempty single-line string')
        requested_apn = requested_apn.strip()
        report['requested_apn'] = requested_apn
        identity, enumeration = self.device.identity, self.enumeration()
        pending = self._load()
        same_device = bool(pending and enumeration and pending['identity'] == identity
                           and (not pending.get('usb_path') or
                                pending['usb_path'] == str(Path(self.device.usb_path).resolve()))
                           and pending['enumeration'] == enumeration)
        report['ignored_pending'] = bool(pending and not same_device)
        def fallback(status):
            if same_device:
                raise RuntimeError('Pending attach APN transaction cannot be reconciled with current profile '
                                   f'information ({status}); registration checkpoint was retained')
            report['status'] = status
            return report
        try:
            output = self._request('get_lte_attach_pdn_list')
            current = self._profiles(self._field(output, 'get_current_list'))
            future = self._profiles(self._field(output, 'get_pending_list'))
            report.update(current_profiles=current, pending_profiles=future)
            if not current:
                return fallback('fallback_pending_only' if future else 'fallback_no_current_profile')
            if future and future != current:
                return fallback('fallback_pending_list_conflict')
            profile_id = current[0]
            report['profile_id'] = profile_id
            if same_device and pending['profile_id'] != profile_id:
                raise RuntimeError('Pending attach APN transaction refers to a different current profile; '
                                   'registration checkpoint was retained')
            profile = self._profile(profile_id)
            profile_apn = self._field(profile, 'get_apn_name')
            if profile_apn is not None and not isinstance(profile_apn, str):
                raise ValueError('Invalid APN in LTE attach profile')
            report['profile_apn'] = profile_apn
            mask = self._field(profile, 'get_apn_type_mask')
            if mask is None:
                return fallback('fallback_profile_type_unavailable')
            mask = int(mask)
            protected = int(Qmi.WdsApnTypeMask.IMS) | int(Qmi.WdsApnTypeMask.EMERGENCY)
            names = (profile_apn, self._field(profile, 'get_profile_name'))
            if (not mask & int(Qmi.WdsApnTypeMask.DEFAULT) or mask & protected
                    or any(isinstance(name, str) and name.strip().casefold() in {'ims', 'sos', 'emergency'}
                           for name in names)):
                return fallback('fallback_unsafe_profile')
            recovered = bool(same_device and pending['profile_id'] == profile_id)
            matches = isinstance(profile_apn, str) and profile_apn.casefold() == requested_apn.casefold()
            report['recovered_pending'] = recovered
            if matches and not recovered:
                report['status'] = 'matched'
                return report
            if not enumeration or not identity:
                raise RuntimeError('Cannot safely journal attach APN changes without USB identity and enumeration')
            if not matches and not hasattr(Qmi, 'MessageWdsModifyProfileInput'):
                raise _Fallback('fallback_unsupported')
            value = None
            if not matches:
                value = Qmi.MessageWdsModifyProfileInput()
                value.set_profile_identifier(Qmi.WdsProfileType(0), profile_id)
                value.set_apn_name(requested_apn)
            # Preserve an existing matching transaction across process crashes.
            record = (pending if recovered and pending['requested_apn'].casefold() == requested_apn.casefold()
                      else dict(kind='attach_apn', token=uuid.uuid4().hex, identity=identity,
                                enumeration=enumeration, usb_path=str(Path(self.device.usb_path).resolve()),
                                profile_id=profile_id, requested_apn=requested_apn))
            report['replaced_pending'] = bool(pending and record is not pending)
            self._save(record)
            report['needs_reregister'] = True
            self._completion_token = record['token']
            if before_change is not None:
                before_change()
            if not matches:
                self._request('modify_profile', value)
                report.update(changed=True, profile_apn=None)
                # Read back only this APN. We did not alter PDP/auth/type/list.
                verified = self._field(self._profile(profile_id), 'get_apn_name')
                if not isinstance(verified, str) or verified.casefold() != requested_apn.casefold():
                    raise RuntimeError('LTE attach profile APN readback did not match the requested APN')
                report['profile_apn'] = verified
            report.update(status='synchronized' if report['changed'] else 'pending_reregister',
                          needs_reregister=True)
            return report
        except _Fallback as error:
            self._completion_token = None
            if report['needs_reregister'] or same_device:
                raise RuntimeError('Attach APN transaction could not be confirmed after journaling; '
                                   'pending registration checkpoint was retained') from error
            report['status'] = error.status
            return report
        except Exception:
            self._completion_token = None
            raise

    def mark_completed(self):
        """Caller confirmed RF restoration and network registration, not APN negotiation."""
        if self._completion_token is None:
            return False
        pending = self._load()
        if (not pending or pending['token'] != self._completion_token
                or (pending.get('usb_path') and pending['usb_path'] != str(Path(self.device.usb_path).resolve()))
                or pending['identity'] != self.device.identity or pending['enumeration'] != self.enumeration()):
            return False
        self.state.clear()
        self._completion_token = None
        return True
