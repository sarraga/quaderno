"""One-way sync, device -> this computer."""

import json
import os
import stat
import tempfile
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict, TypeGuard

from .entry import Entry
from .errors import QuadernoError
from .events import EventHandler, emit
from .util import ROOT, nfc, safe_local_name

if TYPE_CHECKING:
    from .device import Device

SYNC_STATE = ".quaderno-sync.json"

_ActFn = Callable[[str, str, str, str | None], None]


class SyncRecord(TypedDict):
    path: str          # local path relative to the sync root, '/'-separated
    rev: str | None    # the device's file_revision when downloaded
    size: int          # local size and mtime right after download: if either
    mtime: int         # changed, the local copy was edited and is left alone


class SyncState(TypedDict):
    remote: str                       # the device folder mirrored, e.g. 'Document'
    entries: dict[str, SyncRecord]    # by device entry id


def _safe_relpath(rel: str) -> str:
    """Validate a ``'/'``-separated relative path from sync state or the device.

    Args:
        rel: Relative path string.

    Returns:
        Validated relative path string.

    Raises:
        QuadernoError: If any component is unsafe or collides with ``SYNC_STATE``.
    """
    parts = rel.split("/")
    if not parts or any(p == "" for p in parts):
        raise QuadernoError(f"refusing unsafe sync path: {rel!r}")
    clean = "/".join(safe_local_name(p) for p in parts)
    if clean == SYNC_STATE:
        raise QuadernoError(f"refusing sync path colliding with {SYNC_STATE}")
    return clean


def _is_record(r: Any) -> TypeGuard[SyncRecord]:
    """Return ``True`` if ``r`` is a well-formed ``SyncRecord`` with a safe path."""
    if not (isinstance(r, dict) and isinstance(r.get("path"), str)
            and (r.get("rev") is None or isinstance(r.get("rev"), str))
            and not isinstance(r.get("size"), bool) and isinstance(r.get("size"), int)
            and not isinstance(r.get("mtime"), bool) and isinstance(r.get("mtime"), int)):
        return False
    try:
        _safe_relpath(r["path"])
    except QuadernoError:
        return False
    return True


def _load_state(path: str) -> SyncState:
    """Load and validate a ``.quaderno-sync.json`` state file from ``path``.

    Args:
        path: Absolute path to ``.quaderno-sync.json``.

    Returns:
        Validated ``SyncState`` mapping.

    Raises:
        QuadernoError: If the file is malformed JSON or contains unsafe/unknown records.
    """
    with open(path) as f:
        try:
            state = json.load(f)
        except ValueError as err:
            raise QuadernoError(f"{path} is damaged ({err}); move it away to sync afresh") from err
    entries = state.get("entries") if isinstance(state, dict) else None
    if (not isinstance(state, dict) or not isinstance(state.get("remote"), str)
            or not isinstance(entries, dict) or not all(_is_record(r) for r in entries.values())):
        raise QuadernoError(f"{path} is not a sync state file this version understands; "
                            "move it away to sync afresh")
    return {"remote": state["remote"], "entries": {str(k): v for k, v in entries.items()}}


def _save_state(root: str, state_path: str, state: SyncState) -> None:
    """Atomically write ``state`` to ``state_path`` via a temporary file in ``root``."""
    fd, tmp = tempfile.mkstemp(prefix=".quaderno-sync.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1, ensure_ascii=False)
        os.replace(tmp, state_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _local_path(root: str, rel: str) -> str:
    """Resolve relative sync path ``rel`` under ``root`` and verify root containment.

    Args:
        root: Absolute path to the local sync root directory.
        rel: ``'/'``-separated relative path.

    Returns:
        Absolute path inside ``root``.

    Raises:
        QuadernoError: If ``rel`` is unsafe or resolves outside ``root`` via symlinks.
    """
    clean = _safe_relpath(rel)
    candidate = os.path.join(root, *clean.split("/"))
    real_root = os.path.realpath(root)
    real_parent = os.path.realpath(os.path.dirname(candidate))
    if os.path.commonpath([real_root, real_parent]) != real_root:
        raise QuadernoError(f"refusing path escaping sync root: {rel!r}")
    return candidate


def _is_pristine(root: str, rec: SyncRecord) -> bool:
    """Return ``True`` if ``rec['path']`` is still an unmodified regular file matching ``rec``."""
    try:
        st = os.lstat(_local_path(root, rec["path"]))
    except (OSError, QuadernoError):
        return False
    return (stat.S_ISREG(st.st_mode)
            and st.st_size == rec["size"] and int(st.st_mtime) == rec["mtime"])


def _init_state(root: str, state_path: str, remote_path: str, *, force: bool) -> SyncState:
    """Load existing sync state or initialize a new one for ``root`` <-> ``remote_path``.

    Raises:
        QuadernoError: If ``root`` mirrors a different folder, or is non-empty without ``force``.
    """
    if os.path.exists(state_path):
        state = _load_state(state_path)
        if state["remote"] != remote_path:
            raise QuadernoError(f"{root} mirrors {state['remote']}, not {remote_path}")
        return state
    if os.path.isdir(root) and os.listdir(root) and not force:
        raise QuadernoError(f"{root} is not empty and has no sync state; use an empty "
                            "directory, or --force to sync into it without touching "
                            "existing files")
    return {"remote": remote_path, "entries": {}}


def _sync_document(dev: "Device", root: str, state_path: str, state: SyncState,
                   entry: Entry, rel: str, target: str, stats: dict[str, int], *,
                   dry_run: bool, act: _ActFn) -> None:
    """Apply one remote document's state (unchanged, moved, kept, new, or updated)."""
    entries = state["entries"]
    old = entries.get(entry.id)
    if old and old["rev"] == entry.revision and _is_pristine(root, old):
        if old["path"] == rel:
            stats["unchanged"] += 1
            return
        if not os.path.lexists(target):  # moved/renamed on the device
            act("move", "move", f"{old['path']} -> {rel}", entry.path)
            if not dry_run:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(_local_path(root, old["path"]), target)
                old["path"] = rel
                _save_state(root, state_path, state)
            stats["moved"] += 1
            return
    if os.path.lexists(target) and not (old and old["path"] == rel and _is_pristine(root, old)):
        stats["kept"] += 1
        why = "changed locally" if old and old["path"] == rel else "exists locally, not from sync"
        act("keep", "KEEP", f"{rel} ({why}; device version not downloaded)", entry.path)
        return
    stats["updated" if old else "new"] += 1
    act("update" if old else "get", "update" if old else "get", rel, entry.path)
    if dry_run:
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    size = dev.download(entry, target)
    if old and old["path"] != rel and _is_pristine(root, old):
        os.remove(_local_path(root, old["path"]))  # moved and changed: drop the stale copy
    entries[entry.id] = {"path": rel, "rev": entry.revision, "size": size,
                         "mtime": int(os.lstat(target).st_mtime)}
    _save_state(root, state_path, state)


def _prune_deleted(root: str, entries: dict[str, SyncRecord], seen: set[str],
                   stats: dict[str, int], *, delete: bool, dry_run: bool, act: _ActFn) -> None:
    """Report or delete local copies of documents removed from the device."""
    for eid in [k for k in entries if k not in seen]:
        rec = entries[eid]
        if not delete:
            stats["gone"] += 1
            act("gone", "gone", f"{rec['path']} (deleted on device; kept, use --delete)", None)
            continue
        if _is_pristine(root, rec):
            act("delete", "delete", rec["path"], None)
            if not dry_run:
                p = _local_path(root, rec["path"])
                os.remove(p)
                d = os.path.dirname(p)
                while (d != root and d.startswith(root + os.sep)
                       and os.path.isdir(d) and not os.path.islink(d) and not os.listdir(d)):
                    os.rmdir(d)
                    d = os.path.dirname(d)
            stats["deleted"] += 1
        else:
            stats["kept"] += 1
            act("keep", "KEEP", f"{rec['path']} (deleted on device but changed locally)", None)
        if not dry_run:
            del entries[eid]


def sync_folder(dev: "Device", local: str, remote: str = ROOT, delete: bool = False,
                dry_run: bool = False, force: bool = False,
                on_event: EventHandler | None = None) -> dict[str, int]:
    """Mirror a device folder into a local directory, downloading only what changed.

    Follows moves/renames on the device without re-downloading. A local file is
    replaced, moved, or deleted only when still identical (regular file, size,
    mtime) to what sync last wrote. State lives in ``LOCAL/.quaderno-sync.json``.

    Args:
        dev: Authenticated ``Device`` session.
        local: Local destination directory (created if missing).
        remote: Remote device folder path (default ``'Document'``).
        delete: When ``True``, remove unmodified local copies deleted on device.
        dry_run: When ``True``, report actions without modifying local disk.
        force: Allow initial sync into an existing non-empty directory.
        on_event: Optional progress/warning event callback.

    Returns:
        Dictionary of action counts (``new``, ``updated``, ``moved``,
        ``unchanged``, ``kept``, ``deleted``, ``gone``, ``conflict``).
    """
    root = os.path.abspath(os.path.expanduser(local))
    folder = dev.resolve(remote)
    if not folder.is_folder:
        raise QuadernoError(f"{folder.path} is not a folder")
    state_path = os.path.join(root, SYNC_STATE)
    state = _init_state(root, state_path, folder.path, force=force)
    stats = {"new": 0, "updated": 0, "moved": 0, "unchanged": 0, "kept": 0,
             "deleted": 0, "gone": 0, "conflict": 0}
    prefix_text = "(dry run) " if dry_run else ""

    def act(kind: str, tag: str, text: str, path: str | None) -> None:
        """Emit one formatted sync event."""
        emit(on_event, kind, f"{prefix_text}{tag:7} {text}", path)

    if not dry_run:
        os.makedirs(root, exist_ok=True)
    prefix = folder.path + "/"
    seen: set[str] = set()
    planned: dict[str, str] = {}
    for entry in dev.walk(folder):
        if not entry.path.startswith(prefix):
            continue
        rel = "/".join(safe_local_name(p) for p in entry.path[len(prefix):].split("/"))
        if rel == SYNC_STATE:
            stats["conflict"] += 1
            act("clash", "CLASH", f"{rel} (reserved for sync state; skipped)", entry.path)
            continue
        key = nfc(rel).casefold()
        if key in planned:
            stats["conflict"] += 1
            act("clash", "CLASH", f"{rel} (same local name as {planned[key]}; skipped)", entry.path)
            continue
        planned[key] = rel
        target = _local_path(root, rel)
        if entry.is_folder:
            if not dry_run:
                os.makedirs(target, exist_ok=True)
            continue
        seen.add(entry.id)
        _sync_document(dev, root, state_path, state, entry, rel, target, stats,
                       dry_run=dry_run, act=act)

    _prune_deleted(root, state["entries"], seen, stats, delete=delete, dry_run=dry_run, act=act)
    if not dry_run:
        _save_state(root, state_path, state)
    parts = [f"{v} {k}" for k, v in stats.items() if v]
    emit(on_event, "summary", f"{prefix_text}sync {folder.path} -> {root}: "
         + (", ".join(parts) or "nothing to do"), folder.path)
    return stats
