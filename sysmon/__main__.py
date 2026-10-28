"""Entry point for ``python -m sysmon``."""

import sys

from sysmon.cli import main

if __name__ == "__main__":
    sys.exit(main())
