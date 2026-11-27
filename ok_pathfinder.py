#!/usr/bin/env python3
"""
Entry point for ok-ru-friends-pathfinder.
Run without arguments for the interactive dashboard, or pass CLI subcommands/flags.
"""

import sys
from ok_traverser.cli import main

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if __name__ == "__main__":
    sys.exit(main())
