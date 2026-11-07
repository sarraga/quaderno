"""Documents and folders on the device."""

from dataclasses import dataclass, field

from .util import ROOT, parse_device_time


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
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
        """Modification time as epoch seconds, or None."""
        return parse_device_time(self.modified)

    @classmethod
    def from_api(cls, d):
        return cls(id=d["entry_id"], name=d.get("entry_name", ""),
                   path=d.get("entry_path", "").rstrip("/"), type=d.get("entry_type", ""),
                   parent_id=d.get("parent_folder_id"), size=_int(d.get("file_size")),
                   pages=_int(d.get("total_page")), revision=d.get("file_revision"),
                   modified=d.get("modified_date"), created=d.get("created_date"), raw=dict(d))
