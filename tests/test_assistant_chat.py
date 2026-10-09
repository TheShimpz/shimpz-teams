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


class ConversationWindowTests(unittest.TestCase):
    def test_only_the_exact_bounded_wire_shape_is_admitted(self) -> None:
        window = assistant_chat.conversation_window(
            [
                {"role": "user", "text": "List my DNS zones", "truncated": False},
                {"role": "assistant", "text": "Install Cloudflare first.", "truncated": False},
            ]
        )
        self.assertEqual([entry.role for entry in window], ["user", "assistant"])
        self.assertEqual(assistant_chat.conversation_window([]), ())
        for value in (
            None,
            ({"role": "user", "text": "x", "truncated": False},),
            [{"role": "user", "text": "x"}],
            [{"role": "user", "text": "x", "truncated": False, "extra": 1}],
            [{"role": "system", "text": "x", "truncated": False}],
            [{"role": "user", "text": "", "truncated": False}],
            [{"role": "user", "text": "x", "truncated": "no"}],
            [{"role": "user", "text": "x" * 513, "truncated": False}],
            [{"role": "user", "text": "x" * 512, "truncated": False}] * 9,
        ):
            with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                assistant_chat.conversation_window(value)
