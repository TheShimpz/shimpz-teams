from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from chat import contract as assistant_chat


class AssistantChatContractTests(unittest.TestCase):
    def test_prompt_contains_only_file_metadata_and_message(self) -> None:
        prompt = assistant_chat.build_prompt(
            "Say hello to Ada",
            [
                {
                    "id": "a" * 32,
                    "name": "brief.txt",
                    "media_type": "text/plain",
                    "size": 12,
                    "sha256": "must-not-enter-model-context",
                }
            ],
        )
        decoded = json.loads(prompt)
        self.assertEqual(set(decoded), {"files", "message"})
        self.assertEqual(set(decoded["files"][0]), {"id", "name", "media_type", "size"})
        self.assertNotIn("must-not-enter-model-context", prompt)


if __name__ == "__main__":
    unittest.main()
