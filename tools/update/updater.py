#!/usr/bin/env python3
"""Entry point of qmi-supervisor-update (stdlib only, no GI); see modules/update/cli.py."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from modules.update.cli import main

if __name__ == '__main__':
    sys.exit(main())
