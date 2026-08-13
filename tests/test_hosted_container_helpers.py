from __future__ import annotations

import unittest

from hosted import container


class HostedContainerHelperTests(unittest.TestCase):
    def test_names_labels_projects_and_dependencies_delegate_exactly(self) -> None:
        self.assertEqual(container.team_db_project("team_1"), "team_team_1")
        self.assertEqual(container.core_deps(), [(container.POSTGRES_CONTAINER, ["postgres"])])


if __name__ == "__main__":
    unittest.main()
