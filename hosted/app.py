"""Hosted Team entrypoint."""

from hosted.http.listener import BoundedThreadingHTTPServer
from hosted.http.server import Handler, main

__all__ = ["BoundedThreadingHTTPServer", "Handler", "main"]


if __name__ == "__main__":
    main()
