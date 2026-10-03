"""Opt-in proof of the preparation helper in a real container of the built Team image (ADR-0093).

Run with SHIMPZ_RUN_DOCKER_TESTS=1 and SHIMPZ_TEST_TEAM_IMAGE naming a locally built Local or Hosted Team image.
"""

from __future__ import annotations

import os
import unittest
from contextlib import suppress

from prepare import helper as preparation_helper
from prepare import service
from tests import prepare_fixtures

ENABLED = os.environ.get("SHIMPZ_RUN_DOCKER_TESTS") == "1" and bool(os.environ.get("SHIMPZ_TEST_TEAM_IMAGE"))


@unittest.skipUnless(ENABLED, "real Docker test is opt-in")
class RealHelperTests(unittest.TestCase):
    def test_the_real_helper_prepares_files_without_network_privilege_or_mounts(self) -> None:
        import docker
        from docker.errors import DockerException, NotFound

        client = docker.from_env()
        image = client.images.get(os.environ["SHIMPZ_TEST_TEAM_IMAGE"]).id
        observed: list[dict[str, object]] = []
        removed: list[str] = []

        def started(container: object) -> None:
            container.reload()
            observed.append(container.attrs)

        def kwargs() -> dict[str, object]:
            return preparation_helper.base_kwargs(
                image, f"shimpz-prepare-proof-{os.getpid()}", {"com.shimpz.test": "prepare"}
            )

        originals = {
            "photo.jpg": prepare_fixtures.oriented_jpeg((2400, 1800), 6),
            "report.pdf": prepare_fixtures.text_pdf("Quarterly total 1000"),
            "secret.pdf": prepare_fixtures.encrypted_pdf(),
        }
        files = [
            service.StoredFile(f"{index:032x}", name, len(data), "0" * 64, lambda data=data: data)
            for index, (name, data) in enumerate(originals.items())
        ]
        try:
            prepared = service.prepare_attachments(
                files,
                lambda: preparation_helper.PreparationHelper(
                    client,
                    kwargs,
                    transport_errors=(DockerException,),
                    started=started,
                    stopped=lambda container: removed.append(container.id),
                ),
            )
        finally:
            for container in client.containers.list(all=True, filters={"label": "com.shimpz.test=prepare"}):
                with suppress(NotFound):
                    container.remove(force=True)
            client.close()

        photo, report, secret = (item.content for item in prepared)
        self.assertEqual((photo["type"], photo["width"], photo["height"]), ("image", 948, 1264))
        self.assertEqual(report, {"type": "text", "text": "Quarterly total 1000", "pdf": True})
        self.assertEqual(secret, {"type": "opaque", "reason": "encrypted"})
        self.assertEqual(len(observed), 1)
        host = observed[0]["HostConfig"]
        self.assertEqual(host["NetworkMode"], "none")
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertEqual(host["Memory"], host["MemorySwap"])
        self.assertEqual(host["PidsLimit"], 128)
        self.assertFalse(host.get("Binds"))
        self.assertEqual(observed[0]["Mounts"], [])
        self.assertEqual(observed[0]["Config"]["User"], "65534:65534")
        self.assertEqual(removed, [observed[0]["Id"]])


if __name__ == "__main__":
    unittest.main()
