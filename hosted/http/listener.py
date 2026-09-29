"""The Hosted Team HTTP listener: bounded thread-per-request admission with slow-client expiry."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from hosted import state as runtime_state


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Thread-per-request server with hard admission and slow-client expiry."""

    daemon_threads = True

    def __init__(self, *args, max_concurrency: int | None = None, **kwargs) -> None:
        concurrency = runtime_state.MAX_HTTP_CONCURRENCY if max_concurrency is None else max_concurrency
        self._request_slots = threading.BoundedSemaphore(concurrency)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(runtime_state.HTTP_CONNECTION_TIMEOUT_SECONDS)
        return request, client_address

    def process_request(self, request, client_address) -> None:
        # Backpressure happens before a thread exists. At the ceiling, at most the kernel's bounded
        # listen backlog plus this accepted socket waits; Python thread count cannot grow unbounded.
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()
