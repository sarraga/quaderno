"""Progress reporting: API operations call an optional `on_event(Event)` callback
instead of printing. `message` is ready to show; `warning` marks things a CLI
would print to stderr (skipped files, problems that did not stop the operation)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str          # e.g. "created", "uploaded", "downloaded", "deleted", "moved", "skipped"
    message: str
    path: str | None = None
    warning: bool = False


def emit(on_event, kind, message, path=None, warning=False):
    if on_event is not None:
        on_event(Event(kind, message, path, warning))
