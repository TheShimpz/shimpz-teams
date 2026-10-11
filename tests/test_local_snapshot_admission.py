"""One admission verdict per staged Local snapshot: full install admission, including every preview check."""

import unittest

from docker.errors import DockerException, ImageNotFound

from local.install import snapshots
from tests import catalog_fixtures
from tests.local_snapshot_fixtures import IMAGE_ID
from tests.local_snapshot_fixtures import client as _client
from tests.test_assistant_manifest import manifest

# A Stored Input the current contract refuses: it names its host and header but declares no reviewed routes.
UNROUTED_STORED_INPUT = manifest(
    integrations=(
        "[stored_inputs.api-token]\n"
        'kind = "password"\n'
        'label = "API token"\n'
        f'description = "{catalog_fixtures.STORED_INPUT_HELP}"\n'
        f'help_url = "{catalog_fixtures.HELP_URL}"\n'
        'host = "api.example.com"\n'
        'header = "X-Api-Key"\n'
    )
)

ADMISSIBLE = snapshots.Admission(snapshots.ADMISSIBLE)
INADMISSIBLE = snapshots.Admission(snapshots.INADMISSIBLE, snapshots.DECLARATION_INVALID)
UNAVAILABLE = snapshots.Admission(snapshots.UNAVAILABLE)


class SnapshotVerdictTests(unittest.TestCase):
    def test_a_snapshot_the_full_install_admission_accepts_is_admissible(self) -> None:
        client, _image, _container = _client()

        self.assertEqual(snapshots.admission(client, IMAGE_ID), ADMISSIBLE)

    def test_a_declaration_the_current_contract_refuses_is_inadmissible(self) -> None:
        client, _image, container = _client(declaration=UNROUTED_STORED_INPUT)

        self.assertEqual(snapshots.admission(client, IMAGE_ID), INADMISSIBLE)
        container.remove.assert_called_once_with(force=True, v=False)

    def test_a_label_only_a_preview_compares_makes_the_snapshot_inadmissible(self) -> None:
        # Install admission once compared only the identity, Creators, and capabilities with the labels; the preview
        # also compares the name and summary, so a snapshot it refuses must never be offered or installed.
        for label in (snapshots.NAME_LABEL, snapshots.SUMMARY_LABEL):
            with self.subTest(label=label):
                client, image, _container = _client()
                image.attrs["Config"]["Labels"][label] = "Another label"

                self.assertEqual(
                    snapshots.admission(client, IMAGE_ID),
                    snapshots.Admission(snapshots.INADMISSIBLE, snapshots.IMAGE_INCONSISTENT),
                )
                with self.assertRaisesRegex(snapshots.InconsistentSnapshotError, "does not match its image labels"):
                    snapshots.admit(client, IMAGE_ID)
                with self.assertRaisesRegex(snapshots.InconsistentSnapshotError, "does not match its image labels"):
                    snapshots.preview(client, IMAGE_ID)

    def test_an_image_without_a_stage_file_is_inconsistent(self) -> None:
        client, _image, _container = _client(pack=None)

        self.assertEqual(
            snapshots.admission(client, IMAGE_ID),
            snapshots.Admission(snapshots.INADMISSIBLE, snapshots.IMAGE_INCONSISTENT),
        )

    def test_docker_failing_to_answer_is_unavailable_never_inadmissible(self) -> None:
        for failure in (ImageNotFound("removed"), DockerException("offline")):
            with self.subTest(failure=type(failure).__name__):
                client, _image, _container = _client()
                client.images.get.side_effect = failure

                self.assertEqual(snapshots.admission(client, IMAGE_ID), UNAVAILABLE)
        client, _image, _container = _client()
        client.containers.create.side_effect = DockerException("offline")

        self.assertEqual(snapshots.admission(client, IMAGE_ID), UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
