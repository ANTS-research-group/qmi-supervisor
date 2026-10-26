"""Checkpoint only resources owned by this process, for recovery after a crash."""
import json
from pathlib import Path


class RuntimeState:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError(f"Invalid runtime checkpoint: {self.path}")
        return data

    def save(self, data):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, **data}))
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)
