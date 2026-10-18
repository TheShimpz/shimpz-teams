"""A Local Team whose image names no usable Browser: no turn is offered it, and teardown and reset have none."""

from types import SimpleNamespace


def no_browser() -> SimpleNamespace:
    return SimpleNamespace(
        available=False,
        remove_team=lambda _team_id: None,
        clean=lambda: None,
        startup=lambda: True,
        interrupt=lambda _team_id, _token: None,
    )
