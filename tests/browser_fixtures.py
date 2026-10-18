"""A Local Team whose image names no usable Browser: no turn is offered it, and teardown and reset have none."""

from types import SimpleNamespace


def no_browser() -> SimpleNamespace:
    return SimpleNamespace(available=False, remove_team=lambda _team_id: None, clean=lambda: None, startup=lambda: True)


def no_web_files() -> SimpleNamespace:
    """A file store that marks no file as from the web."""
    return SimpleNamespace(is_web=lambda _team_id, _digest: False)


def no_services() -> tuple[SimpleNamespace, SimpleNamespace]:
    return no_browser(), no_web_files()
