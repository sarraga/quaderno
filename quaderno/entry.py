"""Documents and folders on the device."""

import logging
from dataclasses import dataclass, field
from typing import Any

from .errors import ProtocolError
from .util import ROOT, JsonObject, parse_device_time

log = logging.getLogger("quaderno")


def _int_field(d: JsonObject, key: str) -> int | None:
    """d[key] as an int; None if absent. These fields (size, page count) are only
    displayed, so an unreadable one is logged and read as missing rather than
    failing the whole listing."""
    v = d.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        log.warning("warning: the device sent an unreadable %s for %s: %r", key,
                    d.get("entry_path") or d.get("entry_id"), v)
        return None


@dataclass(frozen=True)
class Entry:
    """A document or folder. `path` is the full device path without a trailing
    slash, e.g. 'Document/Books/x.pdf'; `relpath` drops the leading 'Document/'."""

    id: str
    name: str
    path: str
    type: str                      # "document" or "folder"
    parent_id: str | None = None
    size: int | None = None        # bytes (documents)
    pages: int | None = None       # documents
    revision: str | None = None    # changes whenever the document's file changes
    modified: str | None = None    # device time, 'YYYY-MM-DDTHH:MM:SSZ'
    created: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_folder(self) -> bool:
        return self.type == "folder"

    @property
    def is_document(self) -> bool:
        return self.type == "document"

    @property
    def relpath(self) -> str:
        return self.path[len(ROOT) + 1:] if self.path.startswith(ROOT + "/") else ""

    @property
    def mtime(self) -> int | None:
        """Modification time as epoch seconds; None if the device gave none.
        Raises ProtocolError if it gave one this client can't read."""
        try:
            return parse_device_time(self.modified)
        except ValueError as err:
            raise ProtocolError(f"the device sent an unreadable modified_date for "
                                f"{self.path}: {self.modified!r} ({err})") from err

    @classmethod
    def from_api(cls, d: JsonObject) -> "Entry":
        """Build from a Web API entry. Dates are kept as the device's text and
        only parsed when used (mtime); see _int_field for size and page count."""
        return cls(id=d["entry_id"], name=d.get("entry_name", ""),
                   path=d.get("entry_path", "").rstrip("/"), type=d.get("entry_type", ""),
                   parent_id=d.get("parent_folder_id"), size=_int_field(d, "file_size"),
                   pages=_int_field(d, "total_page"), revision=d.get("file_revision"),
                   modified=d.get("modified_date"), created=d.get("created_date"), raw=dict(d))
