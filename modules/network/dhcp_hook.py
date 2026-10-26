#!/usr/bin/env python3
"""Pass DHCP events to the supervisor; the DHCP subprocess never edits routes or DNS."""
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["QMI_SUPERVISOR_LEASE"])
if sys.argv[1] in {"bound", "renew"}:
    values = {"address": os.environ["ip"], "netmask": os.environ.get("subnet"),
              "gateway": next(iter(os.environ.get("router", "").split()), None),
              "dns": os.environ.get("dns", "").split(), "mtu": os.environ.get("mtu")}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values))
    temporary.replace(path)
elif sys.argv[1] in {"deconfig", "nak", "leasefail"}:
    path.unlink(missing_ok=True)
