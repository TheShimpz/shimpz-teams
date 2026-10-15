import unittest

from core import text


class BoundedTextTests(unittest.TestCase):
    def test_a_character_bound_and_a_byte_bound_measure_differently(self) -> None:
        wave = "\U0001f44b"
        self.assertEqual(text.require(wave, ValueError, "invalid", max_chars=1), wave)
        with self.assertRaisesRegex(ValueError, "invalid"):
            text.require(wave, ValueError, "invalid", max_bytes=3)
        self.assertEqual(text.require(wave, ValueError, "invalid", max_bytes=4), wave)
        with self.assertRaisesRegex(ValueError, "too long"):
            text.require("ab", ValueError, "too long", max_chars=1)

    def test_only_one_trimmed_printable_line_is_admitted(self) -> None:
        for value in (None, 7, "", " padded", "padded ", "two\nlines", "tab\there"):
            with self.subTest(value=value), self.assertRaises(LookupError):
                text.require(value, LookupError, "refused", max_bytes=64)


if __name__ == "__main__":
    unittest.main()
