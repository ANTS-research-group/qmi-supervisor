"""The modem access interface implemented by every driver.

Drivers are trusted code: they run inside the modem actor as root and own
bearers, host routes and the QMI/AT transport. They are never plugins. The
protocol is documentation and typing only; the actor calls these members.
"""
from typing import Protocol


class ModemDriver(Protocol):
    device: object
    generation: int
    ready: bool
    static: dict
    collection_report: dict
    observation_state: str | None
    sim_report: dict | None
    sim_recovery: dict
    registration: object
    registered_plmn: str | None

    def open(self, device): ...

    def prepare(self): ...

    def registered(self) -> bool: ...

    def registration_diagnosis(self): ...

    def connect(self): ...

    def configure_ip(self): ...

    def healthy(self) -> bool: ...

    def present(self) -> bool: ...

    def maintain_network(self): ...

    def metrics(self): ...

    def disconnect(self): ...

    def close(self): ...

    def pump(self, timeout): ...


class ModemCompanion(Protocol):
    """Read-only extra data attached to a driver, never a second owner."""
    name: str

    def applies_to(self, device) -> bool: ...

    def open(self, device): ...

    def observe(self, reason: str) -> dict: ...

    def close(self): ...
