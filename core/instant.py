"""The one UTC wire text of an instant in whole seconds, ``YYYY-MM-DDTHH:MM:SSZ``, that Team writes everywhere."""

import datetime


def text(epoch: int) -> str:
    """An epoch second as its UTC wire text."""
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
