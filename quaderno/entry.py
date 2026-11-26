"""Documents and folders on the device."""

import logging
from dataclasses import dataclass, field
from typing import Any

from .errors import ProtocolError
from .util import ROOT, JsonObject, json_str, parse_device_time

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
    ext_id: str | None = None
    title: str | None = None
    author: str | None = None
    document_type: str | None = None  # "normal" or "note"
    current_page: int | None = None
    file_hash: str | None = None
    reading_date: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_folder(self) -> bool:
        """``True`` when this entry represents a device folder."""
        return self.type == "folder"

    @property
    def is_document(self) -> bool:
        """``True`` when this entry represents a PDF document or notebook."""
        return self.type == "document"

    @property
    def relpath(self) -> str:
        """Device path relative to ``Document/``, or ``''`` for the root folder."""
        return self.path[len(ROOT) + 1:] if self.path.startswith(ROOT + "/") else ""

    @property
    def mtime(self) -> int | None:
        """Modification time as epoch seconds, or ``None`` if absent.

        Raises:
            ProtocolError: If ``modified`` is present but not a valid ISO-8601
                timestamp with a UTC offset.
        """
        try:
            return parse_device_time(self.modified)
        except ValueError as err:
            raise ProtocolError(f"the device sent an unreadable modified_date for "
                                f"{self.path}: {self.modified!r} ({err})") from err

    @classmethod
    def from_api(cls, d: JsonObject) -> "Entry":
        """Build an ``Entry`` from a raw Web API JSON object.

        Args:
            d: Decoded JSON object containing at least ``entry_id``.

        Returns:
            Populated immutable ``Entry``.

        Raises:
            ProtocolError: If ``entry_id`` is missing or any string metadata
                field has a non-string JSON type.
        """
        def opt_str(key: str) -> str | None:
            """Return optional string field ``d[key]``, rejecting non-string values."""
            v = d.get(key)
            if v is None:
                return None
            if not isinstance(v, str):
                raise ProtocolError(f"the device's answer has non-text {key!r}: {v!r}")
            return v

        eid = json_str(d, "entry_id")
        name = opt_str("entry_name") or ""
        path = (opt_str("entry_path") or "").rstrip("/")
        etype = opt_str("entry_type") or ""
        return cls(id=eid, name=name, path=path, type=etype,
                   parent_id=opt_str("parent_folder_id"), size=_int_field(d, "file_size"),
                   pages=_int_field(d, "total_page"), revision=opt_str("file_revision"),
                   modified=opt_str("modified_date"), created=opt_str("created_date"),
                   ext_id=opt_str("ext_id"), title=opt_str("title"), author=opt_str("author"),
                   document_type=opt_str("document_type"),
                   current_page=_int_field(d, "current_page"), file_hash=opt_str("file_hash"),
                   reading_date=opt_str("reading_date"), raw=dict(d))
