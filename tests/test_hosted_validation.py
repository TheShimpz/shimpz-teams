from __future__ import annotations

import unittest

from hosted import validation


class HostedValidationTests(unittest.TestCase):
    def test_team_id_is_sanitized_and_bounded(self) -> None:
        self.assertEqual(validation.validate_team_id(" Team-One "), "team_one")
        for value in (None, "", "---", "a" * 41):
            with self.subTest(value=value), self.assertRaises(validation.ValidationError):
                validation.validate_team_id(value)

    def test_chat_message_requires_bounded_nonempty_text(self) -> None:
        self.assertEqual(validation.validate_chat_message(" hello "), "hello")
        for value in (None, "  ", "x" * (validation.MAX_CHAT_MESSAGE + 1)):
            with self.subTest(value=value), self.assertRaises(validation.ValidationError):
                validation.validate_chat_message(value)


if __name__ == "__main__":
    unittest.main()
