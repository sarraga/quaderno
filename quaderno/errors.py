"""Exceptions raised by the quaderno package."""


class QuadernoError(Exception):
    """Any failure talking to the device or carrying out an operation.

    `status` is the HTTP status the device answered with, when there was one."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class NotFoundError(QuadernoError):
    """A path or entry does not exist on the device (HTTP 404, or no pattern match)."""

    def __init__(self, msg, status=404):
        super().__init__(msg, status)
