"""Associate recovery journals with physical UEs, independently of profile names.

Only the platform parent migrates legacy journals, before starting their owner.
Workers write their own journals; a different profile uses the same physical
location after the previous worker has been reaped.
"""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from modules.runtime.state import RuntimeState
from modules.runtime.relocation import relocated_device


def usb_key(device):
    return str(Path(device.usb_path).resolve())


class OwnershipStore:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.root = self.runtime/'ownership'

    def directory(self, device):
        key = hashlib.sha256((usb_key(device)+'\0'+device.identity).encode()).hexdigest()
        return self.root/key

    def checkpoint(self, device):
        return self.directory(device)/'connection-state.json'

    @staticmethod
    def attach_path(path):
        return path.with_name(path.stem+'-attach.json')

    def legacy(self):
        yield 'default', self.runtime.parent/'qmi-supervisor-state.json'
        for path in sorted((self.runtime/'modems').glob('*/connection-state*.json')):
            if path.name == 'connection-state.json':
                yield path.parent.name, path
            elif path.name == 'connection-state-attach.json' and not path.with_name('connection-state.json').exists():
                yield path.parent.name, path.with_name('connection-state.json')

    @staticmethod
    def _move(source, target):
        if not source.exists():
            return
        if target.exists():
            if RuntimeState(source).load() != RuntimeState(target).load():
                raise ValueError('Conflicting recovery journals for one physical modem; ownership was retained')
            source.unlink()
        else:
            source.replace(target)

    def resume_migration(self, directory):
        """Finish a journaled pair move before any worker may use its target."""
        transaction = RuntimeState(directory/'migration.json')
        pending = transaction.load()
        if pending is None:
            return
        files = pending.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= 3:
            raise ValueError('Invalid ownership migration journal')
        allowed_targets = {'connection-state.json', 'connection-state-attach.json'}
        for entry in files:
            if (not isinstance(entry, dict) or not isinstance(entry.get('source'), str) or
                    not isinstance(entry.get('target'), str) or not isinstance(entry.get('record'), dict) or
                    entry['record'].get('version') != 1 or
                    any(not isinstance(entry['record'].get(key), str) or not entry['record'][key]
                        for key in ('identity', 'usb_path'))):
                raise ValueError('Invalid ownership migration entry')
            source = Path(entry['source'])
            target = directory/entry['target']
            named_source = (source.parent.parent == self.runtime/'modems' and
                            source.name in allowed_targets)
            relocated_attach = (source.parent.parent == self.root and
                                source.name == 'connection-state-attach.json' and source.parent != directory)
            default_source = source in (self.runtime.parent/'qmi-supervisor-state.json',
                self.runtime.parent/'qmi-supervisor-state-attach.json', self.runtime.parent/'qmi-supervisor-attach.json')
            record = entry['record']
            if entry['target'] not in allowed_targets or not (named_source or default_source or relocated_attach):
                raise ValueError('Invalid ownership migration paths')
            for existing in (source, target):
                if existing.exists():
                    current = RuntimeState(existing).load()
                    if existing == source and entry.get('source_record') is not None:
                        if current not in (entry['source_record'], record):
                            raise ValueError('Recovery journal changed during migration; ownership was retained')
                        continue
                    if dict(current, usb_path=current.get('usb_path') or record['usb_path']) != record:
                        raise ValueError('Recovery journal changed during migration; ownership was retained')
        for entry in files:
            source, target = Path(entry['source']), directory/entry['target']
            if source.exists():
                RuntimeState(source).save(entry['record'])
                if target.exists() and not RuntimeState(target).load().get('usb_path'):
                    RuntimeState(target).save(entry['record'])
                self._move(source, target)
            elif not target.exists():
                # The transaction holds the full ownership snapshot, not just
                # a filename. No worker starts until this journal is removed.
                RuntimeState(target).save(entry['record'])
        transaction.clear()

    def prepare(self, profile, device, settings, devices, bindings):
        """Claim only this UE's legacy state, never another profile's live state."""
        from modules.deployment.provisioning import render_config, write_atomic
        directory = self.directory(device)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.resume_migration(directory)
        self.import_relocated_legacy(device, settings, devices, bindings)
        for owner, previous in self.records():
            relocated = relocated_device(owner, devices)
            if previous.exists() and relocated is not None and usb_key(relocated) == usb_key(device):
                raise RuntimeError('Previous USB transport cleanup is pending for this modem')
        target = self.checkpoint(device)
        owner = dict(version=1, profile=profile, identity=device.identity, usb_path=usb_key(device))
        owner_path = directory/'owner.json'
        encoded = json.dumps(owner, sort_keys=True)
        if not owner_path.exists() or owner_path.read_text() != encoded:
            write_atomic(owner_path, encoded)
        if settings.modem.mode == 'connect':
            self.relocate_attach(device, devices, directory)
        for identifier, source in self.legacy():
            origin = bindings.get(identifier)
            pairs = [(source, target), (self.attach_path(source), self.attach_path(target))]
            # An older release used this spelling for the default attach journal.
            if identifier == 'default':
                pairs.append((self.runtime.parent/'qmi-supervisor-attach.json', self.attach_path(target)))
            existing = [(old, new, RuntimeState(old).load()) for old, new in pairs if old.exists()]
            if not existing:
                continue
            def matches(record):
                if record.get('identity') != device.identity:
                    return False
                topology = record.get('usb_path') or origin
                if topology:
                    return str(Path(topology).resolve()) == usb_key(device)
                # A legacy serial without topology is safe only when unique.
                if sum(candidate.identity == device.identity for candidate in devices) != 1:
                    raise ValueError('Legacy recovery identity is ambiguous; physical ownership was retained')
                return True
            matched = [matches(record) for _, _, record in existing]
            if any(matched) and not all(matched):
                raise ValueError('Inconsistent paired recovery identities; ownership journals were retained')
            if not all(matched):
                continue
            canonical = [(old, new, dict(record, usb_path=usb_key(device),
                **({'legacy_transport_unverified': True} if record.get('kind') != 'attach_apn' and
                   not (record.get('usb_path') or origin) else {}))) for old, new, record in existing]
            # Preflight the entire pair before moving either file. Partial
            # migration after a crash is retried idempotently next time.
            for old, new, record in canonical:
                if new.exists():
                    current = RuntimeState(new).load()
                    current = dict(current, usb_path=current.get('usb_path') or usb_key(device))
                    if current != record:
                        raise ValueError('Conflicting recovery journals for one physical modem; ownership was retained')
            # Persist proven origins and both snapshots before moving either
            # half. A profile rebind cannot alter attribution after a crash.
            RuntimeState(directory/'migration.json').save(dict(files=[dict(source=str(old),
                target=new.name, record=record,
                source_record=next(saved for path, _, saved in existing if path == old))
                for old, new, record in canonical]))
            self.resume_migration(directory)
        content = render_config(settings)
        path = directory/'settings.ini'
        if not path.exists() or path.read_text() != content:
            write_atomic(path, content)
        return target

    def import_relocated_legacy(self, device, settings, devices, bindings):
        """Keep a known legacy USB origin when upgrading after a port change."""
        from modules.deployment.provisioning import render_config, write_atomic
        if not device.serial or len({usb_key(value) for value in devices if value.serial == device.serial}) != 1:
            return
        for identifier, source in list(self.legacy()):
            paths = [source, self.attach_path(source)]
            if identifier == 'default':
                paths.append(self.runtime.parent/'qmi-supervisor-attach.json')
            saved = [(path, RuntimeState(path).load()) for path in paths if path.exists()]
            matching = [(path, record) for path, record in saved if record.get('identity') == device.identity]
            if not matching:
                continue
            origin = matching[0][1].get('usb_path') or bindings.get(identifier)
            if not origin or str(Path(origin).resolve()) == usb_key(device):
                continue
            origin = str(Path(origin).resolve())
            if len(matching) != len(saved) or any(
                    str(Path(record.get('usb_path') or bindings.get(identifier) or '').resolve()) != origin
                    for _, record in saved):
                raise ValueError('Inconsistent paired recovery identities; ownership journals were retained')
            original = replace(device, usb_path=origin)
            directory = self.directory(original)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.resume_migration(directory)
            write_atomic(directory/'owner.json', json.dumps(dict(version=1, profile=identifier,
                identity=device.identity, usb_path=origin), sort_keys=True))
            if not (directory/'settings.ini').exists():
                write_atomic(directory/'settings.ini', render_config(settings))
            entries = [dict(source=str(path), target='connection-state.json' if path == source else
                'connection-state-attach.json', record=dict(record, usb_path=origin)) for path, record in saved]
            RuntimeState(directory/'migration.json').save(dict(files=entries))
            self.resume_migration(directory)

    def relocate_attach(self, device, devices, directory):
        """Transfer persistent profile intent, never transport-scoped bearer IDs."""
        for owner, previous in self.records():
            relocated = relocated_device(owner, devices)
            source = self.attach_path(previous)
            if relocated is None or usb_key(relocated) != usb_key(device) or not source.exists():
                continue
            saved = RuntimeState(source).load()
            if saved.get('kind') != 'attach_apn' or saved.get('identity') != device.identity:
                raise ValueError('Invalid relocated attach journal; ownership was retained')
            try:
                enumeration = (Path(device.usb_path)/'devnum').read_text().strip()
            except OSError as error:
                raise RuntimeError('Current USB enumeration is required to recover the attach journal') from error
            if not enumeration:
                raise RuntimeError('Current USB enumeration is required to recover the attach journal')
            record = dict(saved, usb_path=usb_key(device), enumeration=enumeration,
                          relocated_from_usb=owner['usb_path'], relocated_from_enumeration=saved.get('enumeration'))
            RuntimeState(directory/'migration.json').save(dict(files=[dict(source=str(source),
                target='connection-state-attach.json', source_record=saved, record=record)]))
            self.resume_migration(directory)

    def records(self):
        for path in sorted(self.root.glob('*/owner.json')):
            record = json.loads(path.read_text())
            if (not isinstance(record, dict) or record.get('version') != 1 or
                    any(not isinstance(record.get(key), str) or not record[key]
                        for key in ('profile', 'identity', 'usb_path'))):
                raise ValueError(f'Invalid physical ownership record: {path}')
            checkpoint = path.with_name('connection-state.json')
            yield record, checkpoint

    def pending_for(self, profile):
        return any(record.get('profile') == profile and (checkpoint.exists() or checkpoint.with_name('migration.json').exists())
                   for record, checkpoint in self.records())

    def migrate_unassigned(self, devices, reserved, bindings, configuration):
        """Import old journals even if their former profile no longer selects UE."""
        from modules.runtime.settings import load_settings
        for transaction in sorted(self.root.glob('*/migration.json')):
            self.resume_migration(transaction.parent)
        for device in devices:
            if usb_key(device) not in reserved:
                self.import_relocated_legacy(device, configuration.base, devices, bindings)
        for identifier, source in list(self.legacy()):
            existing = next((path for path in (source, self.attach_path(source),
                self.runtime.parent/'qmi-supervisor-attach.json' if identifier == 'default' else source)
                if path.exists()), None)
            if existing is None:
                continue
            record = RuntimeState(existing).load()
            topology = record.get('usb_path') or bindings.get(identifier)
            matches = [device for device in devices if device.identity == record.get('identity') and
                       (not topology or usb_key(device) == str(Path(topology).resolve()))]
            if len(matches) != 1 or usb_key(matches[0]) in reserved:
                continue
            previous_config = self.runtime/'modems'/identifier/'settings.ini'
            settings = (load_settings(previous_config) if previous_config.exists() else
                        configuration.profiles.get(identifier, configuration.base))
            self.prepare(identifier, matches[0], settings, devices, bindings)
