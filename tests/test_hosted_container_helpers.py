from __future__ import annotations

import io
import tarfile
import unittest
from unittest import mock

from hosted import container


class HostedContainerHelperTests(unittest.TestCase):
    def test_inbox_tar_has_exact_single_file_ownership_and_contents(self) -> None:
        encoded = container.build_inbox_tar("brief.txt", b"hello")
        with tarfile.open(fileobj=io.BytesIO(encoded), mode="r") as archive:
            self.assertEqual(archive.getnames(), ["brief.txt"])
            member = archive.getmember("brief.txt")
            self.assertEqual((member.mode, member.uid, member.gid), (0o644, 1000, 1000))
            self.assertEqual(archive.extractfile(member).read(), b"hello")

    def test_names_labels_projects_and_dependencies_delegate_exactly(self) -> None:
        with (
            mock.patch.object(container.network_policy, "network_labels", return_value={"kind": "core"}) as labels,
            mock.patch.object(container.network_policy, "volume_name", side_effect=("config", "workspace")) as volume,
        ):
            self.assertEqual(container.team_network_labels("team_1", "core"), {"kind": "core"})
            self.assertEqual(container.team_config_volume("team_1"), "config")
            self.assertEqual(container.team_workspace_volume("team_1"), "workspace")
        labels.assert_called_once_with("team_1", "core")
        self.assertEqual(volume.call_count, 2)
        self.assertEqual(container.team_db_project("team_1"), "team_team_1")
        self.assertEqual(container.core_deps(), [(container.POSTGRES_CONTAINER, ["postgres"])])


if __name__ == "__main__":
    unittest.main()
