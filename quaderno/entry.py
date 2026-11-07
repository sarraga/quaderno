"""Documents and folders on the device."""

from dataclasses import dataclass, field

from .errors import ProtocolError
from .util import ROOT, parse_device_time


def _int(v):
    """None (field absent) gives None; anything int() can't read raises ValueError."""
    if v is None:
        return None
    try:
        return int(v)
    except TypeError:
        raise ValueError(f"not an integer: {v!r}") from None


def _field(d, key, parse):
    """Parse d[key]: absent -> None, malformed -> ProtocolError naming the entry."""
    try:
        return parse(d.get(key))
    except ValueError as err:
        where = d.get("entry_path") or d.get("entry_id")
        raise ProtocolError(f"the device sent an unreadable {key} for {where}: "
                            f"{d.get(key)!r} ({err})") from err


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
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_folder(self):
        return self.type == "folder"

    @property
    def is_document(self):
        return self.type == "document"

    @property
    def relpath(self):
        return self.path[len(ROOT) + 1:] if self.path.startswith(ROOT + "/") else ""

    @property
    def mtime(self):
        """Modification time as epoch seconds, or None if the device gave none."""
        return parse_device_time(self.modified)

    @classmethod
    def from_api(cls, d):
        """Build from a Web API entry. Absent fields become None; a field that is
        present but malformed raises ProtocolError (checked here, so a listed
        Entry's mtime can't fail later)."""
        for key in ("modified_date", "created_date"):
            _field(d, key, parse_device_time)
        return cls(id=d["entry_id"], name=d.get("entry_name", ""),
                   path=d.get("entry_path", "").rstrip("/"), type=d.get("entry_type", ""),
                   parent_id=d.get("parent_folder_id"), size=_field(d, "file_size", _int),
                   pages=_field(d, "total_page", _int), revision=d.get("file_revision"),
                   modified=d.get("modified_date"), created=d.get("created_date"), raw=dict(d))
