"""Immutable container reads are cached without one cold read holding back any other container."""

import dataclasses
import threading
import unittest
from unittest import mock

from test_assistant_genesis import Container, archive, manifest

from assistant import cache as assistant_cache
from assistant import genesis as assistant_genesis
from assistant import manifest as assistant_manifest

WAIT = 10


class BlockedContainer(Container):
    """A container whose Docker archive read blocks until the test opens its gate."""

    def __init__(self, container_id: str, content: bytes, *, fail: bool = False) -> None:
        super().__init__(container_id, content)
        self.entered = threading.Semaphore(0)
        self.gate = threading.Event()
        self.fail = fail
        self._count = threading.Lock()

    def get_archive(self, path: str):
        with self._count:
            self.reads += 1
        self.entered.release()
        if not self.gate.wait(WAIT):
            raise AssertionError("the test never opened the read")
        if self.fail:
            raise OSError("Docker archive read failed")
        if path != assistant_genesis.assistant_manifest.MANIFEST_PATH:
            raise AssertionError(path)
        return iter((archive(self.content),)), {"name": "shimpz.toml", "size": len(self.content), "mode": 0o444}


class ArrivalEvent(threading.Event):
    """Signals each waiter's arrival, so a test knows a concurrent miss joined the read in flight."""

    def __init__(self, arrived: threading.Semaphore) -> None:
        super().__init__()
        self._arrived = arrived

    def wait(self, timeout: float | None = None) -> bool:
        self._arrived.release()
        return super().wait(timeout)


class Calls:
    """Run each call in its own thread and keep what it returned or raised."""

    def __init__(self) -> None:
        self.results: dict[str, object] = {}
        self.threads: list[threading.Thread] = []

    def start(self, name: str, call) -> threading.Thread:
        def run() -> None:
            try:
                self.results[name] = call()
            except (assistant_genesis.GenesisError, assistant_manifest.ManifestError) as exc:
                self.results[name] = exc

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def join(self) -> None:
        for thread in self.threads:
            thread.join(WAIT)
            if thread.is_alive():
                raise AssertionError("a cache call never returned")


def caches():
    """Each container cache with a call that reads through it and the value a successful read returns."""
    content = manifest("Cached guidance.")
    contract = assistant_manifest.parse_manifest_contract(content)
    genesis = assistant_genesis.GenesisCache()
    reviewed = assistant_manifest.ManifestContractCache()
    return content, (
        ("genesis", lambda container: genesis.get(container), "Cached guidance."),
        ("manifest", lambda container: reviewed.get(container, contract), contract),
    )


class ContainerCacheTests(unittest.TestCase):
    def test_a_blocked_cold_read_never_delays_a_warm_hit_for_another_container(self) -> None:
        content, cases = caches()
        for name, get, expected in cases:
            with self.subTest(cache=name):
                warm = Container("warm", content)
                cold = BlockedContainer("cold", content)
                self.assertEqual(get(warm), expected)
                calls = Calls()
                calls.start("cold", lambda get=get, cold=cold: get(cold))
                self.assertTrue(cold.entered.acquire(timeout=WAIT))
                hit = calls.start("warm", lambda get=get, warm=warm: get(warm))
                hit.join(WAIT)
                # The warm hit returned while the cold read is still blocked inside Docker.
                self.assertFalse(hit.is_alive())
                self.assertFalse(cold.gate.is_set())
                self.assertEqual((calls.results["warm"], warm.reads), (expected, 1))
                cold.gate.set()
                calls.join()
                self.assertEqual(calls.results["cold"], expected)

    def test_concurrent_misses_for_one_container_read_once_and_each_compare_their_review(self) -> None:
        content = manifest("Cached guidance.")
        contract = assistant_manifest.parse_manifest_contract(content)
        drifted = dataclasses.replace(contract, allowed_hosts=("api.example.com",))
        cache = assistant_manifest.ManifestContractCache()
        container = BlockedContainer("shared", content)
        arrived = threading.Semaphore(0)

        class Observed(assistant_cache._Flight):
            __slots__ = ()

            def __init__(self) -> None:
                super().__init__()
                self.done = ArrivalEvent(arrived)

        calls = Calls()
        with mock.patch.object(assistant_cache, "_Flight", Observed):
            calls.start("leader", lambda: cache.get(container, contract))
            self.assertTrue(container.entered.acquire(timeout=WAIT))
            for index in range(3):
                calls.start(f"follower-{index}", lambda: cache.get(container, contract))
            calls.start("drifted", lambda: cache.get(container, drifted))
            for _ in range(4):
                self.assertTrue(arrived.acquire(timeout=WAIT))
            container.gate.set()
            calls.join()
        self.assertEqual(container.reads, 1)
        self.assertEqual(
            {calls.results[f"follower-{index}"] for index in range(3)} | {calls.results["leader"]}, {contract}
        )
        # The shared read never skips a caller's own review comparison.
        self.assertIsInstance(calls.results["drifted"], assistant_manifest.ManifestError)
        self.assertEqual(cache.get(container, contract), contract)
        self.assertEqual(container.reads, 1)

    def test_a_failed_read_fails_every_waiter_and_is_never_cached(self) -> None:
        content = manifest("Cached guidance.")
        cache = assistant_genesis.GenesisCache()
        container = BlockedContainer("failing", content, fail=True)
        arrived = threading.Semaphore(0)

        class Observed(assistant_cache._Flight):
            __slots__ = ()

            def __init__(self) -> None:
                super().__init__()
                self.done = ArrivalEvent(arrived)

        calls = Calls()
        with mock.patch.object(assistant_cache, "_Flight", Observed):
            calls.start("leader", lambda: cache.get(container))
            self.assertTrue(container.entered.acquire(timeout=WAIT))
            calls.start("follower", lambda: cache.get(container))
            self.assertTrue(arrived.acquire(timeout=WAIT))
            container.gate.set()
            calls.join()
        self.assertEqual(container.reads, 1)
        for name in ("leader", "follower"):
            self.assertIsInstance(calls.results[name], assistant_genesis.GenesisError)
        container.fail = False
        self.assertEqual(cache.get(container), "Cached guidance.")
        self.assertEqual(container.reads, 2)

    def test_a_container_discarded_while_its_read_runs_is_read_again(self) -> None:
        content = manifest("Cached guidance.")
        cache = assistant_genesis.GenesisCache()
        container = BlockedContainer("replaced", content)
        calls = Calls()
        calls.start("read", lambda: cache.get(container))
        self.assertTrue(container.entered.acquire(timeout=WAIT))
        cache.discard(container.id)
        container.gate.set()
        calls.join()
        self.assertEqual(calls.results["read"], "Cached guidance.")
        self.assertEqual(cache.get(container), "Cached guidance.")
        self.assertEqual(container.reads, 2)

    def test_distinct_cold_reads_run_within_a_fixed_bound(self) -> None:
        content = manifest("Cached guidance.")
        cache = assistant_genesis.GenesisCache()
        bound = assistant_cache.MAX_CONCURRENT_READS
        containers = [BlockedContainer(f"cold-{index}", content) for index in range(bound + 1)]
        calls = Calls()
        for container in containers:
            calls.start(container.id, lambda container=container: cache.get(container))
        for container in containers[:bound]:
            self.assertTrue(container.entered.acquire(timeout=WAIT))
        # The read past the bound waits for a slot and never reaches Docker meanwhile.
        self.assertFalse(containers[bound].entered.acquire(timeout=0.2))
        containers[0].gate.set()
        self.assertTrue(containers[bound].entered.acquire(timeout=WAIT))
        for container in containers:
            container.gate.set()
        calls.join()
        self.assertEqual({calls.results[container.id] for container in containers}, {"Cached guidance."})


if __name__ == "__main__":
    unittest.main()
