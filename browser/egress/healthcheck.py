#!/usr/local/bin/python3
"""Prove that the Browser CONNECT boundary is accepting local connections."""

import socket


def main() -> int:
    try:
        with socket.create_connection(("127.0.0.1", 3128), timeout=2):
            pass
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
