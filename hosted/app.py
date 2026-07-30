"""Hosted Team entrypoint."""

from hosted.http.server import Handler, _BoundedThreadingHTTPServer, main

__all__ = ["Handler", "_BoundedThreadingHTTPServer", "main"]


if __name__ == "__main__":
    main()
