"""The one shape every request-scoped provider or decision API key must have before Team uses it."""

MIN_API_KEY_BYTES = 16
MAX_API_KEY_BYTES = 8 * 1024


def admissible_api_key(value: object) -> bool:
    """A bounded key of printable ASCII with no space or control character, so it never needs trimming or escaping."""
    return (
        isinstance(value, str)
        and MIN_API_KEY_BYTES <= len(value) <= MAX_API_KEY_BYTES
        and all(33 <= ord(character) <= 126 for character in value)
    )
