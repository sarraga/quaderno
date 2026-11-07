"""One-way sync, device -> Mac."""

import json
import os

from .errors import QuadernoError
from .events import emit
from .util import ROOT, nfc, safe_local_name

SYNC_STATE = ".quaderno-sync.json"


def sync_folder(dev, local, remote=ROOT, delete=False, dry_run=False, force=False, on_event=None):
    """Mirror a device folder into a local directory, downloading only what changed
    (by the device's file_revision). Follows moves/renames on the device without
    re-downloading. A local file is only replaced, moved or deleted if it is still
    exactly as sync left it (size and mtime); anything else is kept and reported.
    Documents deleted on the device are kept locally unless `delete`.

    State lives in LOCAL/.quaderno-sync.json. Returns counts per action."""
    root = os.path.abspath(os.path.expanduser(local))
    folder = dev.resolve(remote)
    if not folder.is_folder:
        raise QuadernoError(f"{folder.path} is not a folder")
    remote_path = folder.path
    state_path = os.path.join(root, SYNC_STATE)
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        if state.get("remote") != remote_path:
            raise QuadernoError(f"{root} mirrors {state.get('remote')}, not {remote_path}")
    else:
        if os.path.isdir(root) and os.listdir(root) and not force:
            raise QuadernoError(f"{root} is not empty and has no sync state; use an empty "
                                "directory, or --force to sync into it without touching "
                                "existing files")
        state = {"remote": remote_path, "entries": {}}
    entries = state["entries"]
    stats = {"new": 0, "updated": 0, "moved": 0, "unchanged": 0, "kept": 0,
             "deleted": 0, "gone": 0, "conflict": 0}
    prefix_text = "(dry run) " if dry_run else ""

    def save():
        if dry_run:
            return
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, ensure_ascii=False)
        os.replace(tmp, state_path)

    def local_of(rel):
        return os.path.join(root, *rel.split("/"))

    def pristine(rec):
        """True if the local copy is exactly what sync last wrote."""
        try:
            st = os.stat(local_of(rec["path"]))
        except OSError:
            return False
        return st.st_size == rec["size"] and int(st.st_mtime) == rec["mtime"]

    def act(kind, tag, text, path):
        emit(on_event, kind, f"{prefix_text}{tag:7} {text}", path)

    if not dry_run:
        os.makedirs(root, exist_ok=True)
    prefix = remote_path + "/"
    seen, planned = set(), {}
    for e in dev.walk(folder):
        if not e.path.startswith(prefix):
            continue
        rel = "/".join(safe_local_name(p) for p in e.path[len(prefix):].split("/"))
        key = nfc(rel).casefold()  # macOS volumes are usually case-insensitive
        if key in planned:
            stats["conflict"] += 1
            act("clash", "CLASH", f"{rel} (same local name as {planned[key]}; skipped)", e.path)
            continue
        planned[key] = rel
        target = local_of(rel)
        if e.is_folder:
            if not dry_run:
                os.makedirs(target, exist_ok=True)
            continue

        seen.add(e.id)
        old = entries.get(e.id)
        if old and old["rev"] == e.revision and pristine(old):
            if old["path"] == rel:
                stats["unchanged"] += 1
                continue
            if not os.path.exists(target):  # moved/renamed on the device
                act("move", "move", f"{old['path']} -> {rel}", e.path)
                if not dry_run:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.replace(local_of(old["path"]), target)
                    old["path"] = rel
                    save()
                stats["moved"] += 1
                continue
        if os.path.exists(target) and not (old and old["path"] == rel and pristine(old)):
            stats["kept"] += 1
            why = "changed locally" if old and old["path"] == rel else "exists locally, not from sync"
            act("keep", "KEEP", f"{rel} ({why}; device version not downloaded)", e.path)
            continue
        stats["updated" if old else "new"] += 1
        act("update" if old else "get", "update" if old else "get", rel, e.path)
        if dry_run:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        size = dev.download(e, target)
        if old and old["path"] != rel and pristine(old):
            os.remove(local_of(old["path"]))  # moved and changed: drop the stale copy
        entries[e.id] = {"path": rel, "rev": e.revision, "size": size,
                         "mtime": int(os.stat(target).st_mtime)}
        save()

    for eid in [k for k in entries if k not in seen]:
        rec = entries[eid]
        if not delete:
            stats["gone"] += 1
            act("gone", "gone", f"{rec['path']} (deleted on device; kept, use --delete)", None)
            continue
        if pristine(rec):
            act("delete", "delete", rec["path"], None)
            if not dry_run:
                os.remove(local_of(rec["path"]))
                d = os.path.dirname(local_of(rec["path"]))
                while d != root and os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
                    d = os.path.dirname(d)
            stats["deleted"] += 1
        else:
            stats["kept"] += 1
            act("keep", "KEEP", f"{rec['path']} (deleted on device but changed locally)", None)
        if not dry_run:
            del entries[eid]
    save()
    parts = [f"{v} {k}" for k, v in stats.items() if v]
    emit(on_event, "summary", f"{prefix_text}sync {remote_path} -> {root}: "
         + (", ".join(parts) or "nothing to do"), remote_path)
    return stats
