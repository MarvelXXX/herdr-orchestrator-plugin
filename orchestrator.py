#!/usr/bin/env python3
"""The plugin's portable entrypoint; no global CLI or pip installation required."""
from herdr_orchestrator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
