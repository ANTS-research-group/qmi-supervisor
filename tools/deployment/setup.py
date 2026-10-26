#!/usr/bin/env python3
"""Source and installed entry point for provisioning (stdlib only)."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from modules.deployment.provisioning import main

if __name__ == '__main__':
    sys.exit(main())
