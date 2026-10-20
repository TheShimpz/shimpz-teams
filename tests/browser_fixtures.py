"""A Local Team whose image names no usable Browser: no turn is offered it, and teardown and reset have none."""

from types import SimpleNamespace


def attach(controller) -> None:
    """A controller with no usable Browser, no sign-in session, and no Sign-in to end or delete."""
    controller.browser_service = no_browser()
    controller.sign_in_sessions = SimpleNamespace(end_team=lambda _team_id: None, end_all=lambda: None)
    controller.sign_in_store = SimpleNamespace(delete_team=lambda _team_id: True, delete_all=lambda: None)


def no_browser() -> SimpleNamespace:
    return SimpleNamespace(
        available=False,
        remove_team=lambda _team_id: None,
        clean=lambda: None,
        startup=lambda: True,
        interrupt=lambda _team_id, _token: None,
    )
