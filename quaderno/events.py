"""Progress reporting: API operations call an optional `on_event(Event)` callback
instead of printing. `message` is ready to show; `warning` marks things a CLI
would print to stderr (skipped files, problems that did not stop the operation)."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str          # e.g. "created", "uploaded", "downloaded", "deleted", "moved", "skipped"
    message: str
    path: str | None = None
    warning: bool = False


EventHandler = Callable[[Event], None]


def emit(on_event: EventHandler | None, kind: str, message: str, path: str | None = None,
         warning: bool = False) -> None:
    if on_event is not None:
        on_event(Event(kind, message, path, warning))
