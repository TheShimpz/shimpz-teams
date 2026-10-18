#!/usr/local/bin/python3
"""Prove that the Browser CONNECT boundary is accepting local connections, and name the process that accepts them.

The proxy publishes its random generation before it listens, so a probe that connected prints the generation of the
process that answered; Team reads it from the probe's recorded output (ADR-0117 §4.3).
"""

import re
import socket
from pathlib import Path

# Beside the audit it names, in the directory only this process's user owns.
GENERATION_FILE = Path("/var/log/browser-egress/generation")
GENERATION_RE = re.compile(r"[0-9a-f]{32}")


def main() -> int:
    try:
        with socket.create_connection(("127.0.0.1", 3128), timeout=2):
            pass
        generation = GENERATION_FILE.read_text(encoding="ascii")
    except OSError, UnicodeDecodeError:
        return 1
    if not GENERATION_RE.fullmatch(generation):
        return 1
    print(generation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
