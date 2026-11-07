"""The device API: a logged-in QUADERNO and what you can do with it.

    import quaderno
    dev = quaderno.connect()                    # find it on USB, log in
    entries, _ = dev.ls("Books")
    dev.get("Books/x.pdf", "~/Downloads")
    dev.put(["a.pdf", "b.pdf"], "Books")
    dev.move("Fighter*", "Books")
    dev.rm("Books/Old", recursive=True)

Remote paths are relative to the top folder 'Document' ('Books/x.pdf',
'/Books/x.pdf' and 'Document/Books/x.pdf' are the same). Where noted, they may
be glob patterns ('Books/*.pdf', case-insensitive) or, with regex=True, regular
expressions; an exact name always wins. Operations that change things accept an
optional `on_event` callback that receives an Event for each step."""

import logging
import os
from dataclasses import dataclass
from urllib.parse import quote

from . import credentials, usb
from .client import Client
from .entry import Entry
from .errors import ExistsError, NotEmptyError, NotFoundError, ProtocolError, QuadernoError
from .events import emit
from .util import ROOT, device_time, human, is_pattern, matcher, nfc, norm, safe_local_name

log = logging.getLogger("quaderno")


@dataclass
class PutResult:
    uploaded: int = 0
    skipped: int = 0     # a document with that name was already there
    rejected: int = 0    # named paths that can't be uploaded (not a PDF, folder without recursive)
    missing: int = 0     # named local files that don't exist
    ignored: int = 0     # non-PDF files inside uploaded folders


def connect(addr=None, workspace=None, timeout=60, on_switch=None):
    """Find the device (or use `addr`), log in, and return a Device.
    Credentials: `workspace` dir, else ~/.config/quaderno, else the PC App's."""
    creds = credentials.load_credentials(credentials.find_credentials_dir(workspace))
    addr = addr or os.environ.get("QUADERNO_ADDR") or usb.discover(creds.serial, on_switch=on_switch)
    dev = Device(addr, creds.client_id, creds.key, timeout=timeout, pinned_cert=creds.pinned_cert)
    dev.credentials = creds
    dev.authenticate()
    return dev


class Device(Client):
    """A logged-in QUADERNO. Use connect() to get one."""

    credentials = None

    # --- lookup ----------------------------------------------------------------
    def resolve(self, path):
        """The Entry at `path` (exact, no patterns). Raises NotFoundError."""
        return Entry.from_api(self.request("GET", "/resolve/entry/" + quote(norm(path), safe="")))

    def try_resolve(self, path):
        try:
            return self.resolve(path)
        except QuadernoError as err:
            if err.status == 404:
                return None
            raise

    def _folder(self, folder):
        f = folder if isinstance(folder, Entry) else self.resolve(folder)
        if not f.is_folder:
            raise QuadernoError(f"{f.path} is not a folder")
        return f

    def _children(self, folder_id):
        out, offset = [], 0
        while True:
            r = self.request("GET", f"/folders/{folder_id}/entries?offset={offset}&limit=100")
            batch = r.get("entry_list", [])
            out += [Entry.from_api(d) for d in batch]
            offset = len(out)
            if offset >= int(r.get("count", offset)) or not batch:
                return out

    def entries(self, folder=ROOT):
        """Direct children of a folder (path or Entry)."""
        return self._children(self._folder(folder).id)

    def walk(self, folder=ROOT):
        """Everything below a folder, depth-first; parents come before children."""
        for e in self.entries(folder):
            yield e
            if e.is_folder:
                yield from self.walk(e)

    def find(self, pattern, regex=False):
        """Entries matching a path whose components may be patterns.
        Returns (entries sorted by path, matched_by_pattern)."""
        path = norm(pattern)
        exact = self.try_resolve(path)
        if exact is not None:
            return [exact], False
        parts = path.split("/")[1:]
        if not any(is_pattern(p, regex) for p in parts):
            self.resolve(path)  # not found and not a pattern: raise the device's 404
        current = [self.resolve(ROOT)]
        for i, part in enumerate(parts):
            found = []
            for folder in current:
                literal = self.try_resolve(folder.path + "/" + part)
                if literal is not None:
                    found.append(literal)
                elif is_pattern(part, regex):
                    match = matcher(part, regex)
                    found += [c for c in self._children(folder.id) if match(c.name)]
            last = i == len(parts) - 1
            current = found if last else [x for x in found if x.is_folder]
            if not current:
                raise NotFoundError(f"no match for {pattern!r}")
        return sorted(current, key=lambda x: x.path.lower()), True

    def find_all(self, patterns, regex=False):
        """find() for several patterns, de-duplicated. Returns (entries, any_pattern)."""
        if isinstance(patterns, str):
            patterns = [patterns]
        seen, out, any_pattern = set(), [], False
        for p in patterns:
            entries, by_pattern = self.find(p, regex)
            any_pattern |= by_pattern
            for e in entries:
                if e.id not in seen:
                    seen.add(e.id)
                    out.append(e)
        return out, any_pattern

    def ls(self, path=ROOT, recursive=False, regex=False):
        """List a folder (or pattern matches; a matched folder is listed itself,
        like `ls -d`, unless recursive). Folders first. Returns (entries, matched_by_pattern)."""
        matches, by_pattern = self.find(path, regex)
        rows = {}
        for entry in matches:
            if not entry.is_folder:
                rows[entry.id] = entry
                continue
            if by_pattern:
                rows[entry.id] = entry
                if not recursive:
                    continue
            for e in (self.walk(entry) if recursive else self._children(entry.id)):
                rows[e.id] = e
        return sorted(rows.values(), key=lambda e: (not e.is_folder, e.path.lower())), by_pattern

    # --- download ---------------------------------------------------------------
    def download(self, doc, target, on_event=None):
        """Save a document (path or Entry) to the local file `target`, atomically,
        with the device's modification time. Returns the size in bytes. If that
        time is unreadable, the file is kept with a warning, without it."""
        doc = doc if isinstance(doc, Entry) else self.resolve(doc)
        tmp = target + ".part"
        try:
            with open(tmp, "wb") as fh:
                n = self.download_to(doc.id, fh)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        try:
            mtime = doc.mtime
        except ProtocolError as err:  # the contents are fine; only the date is lost
            log.warning("warning: %s; saved %s without the device's modification time",
                        err, target)
            mtime = None
        if mtime is not None:
            os.utime(target, (mtime, mtime))
        emit(on_event, "downloaded", f"{doc.path} -> {target} ({human(n)})", doc.path)
        return n

    def get_tree(self, folder, dest, on_event=None):
        """Download a folder as dest/<folder name>/..., keeping its layout.
        Returns the number of documents."""
        folder = self._folder(folder)
        base = os.path.join(dest, safe_local_name(folder.name))
        prefix = folder.path + "/"
        os.makedirs(base, exist_ok=True)
        count = 0
        for e in self.walk(folder):
            rel = e.path[len(prefix):] if e.path.startswith(prefix) else e.name
            target = os.path.join(base, *[safe_local_name(p) for p in rel.split("/")])
            if e.is_folder:
                os.makedirs(target, exist_ok=True)
            else:
                self.download(e, target, on_event)
                count += 1
        emit(on_event, "summary", f"{count} documents from {folder.path} -> {base}", folder.path)
        return count

    def get(self, patterns, dest=".", recursive=False, regex=False, on_event=None):
        """Download documents (patterns allowed); folders need recursive=True.
        With several items, `dest` must be a directory. Returns the document count."""
        targets, _ = self.find_all(patterns, regex)
        if not recursive:
            folders = [e.path for e in targets if e.is_folder]
            if folders:
                raise QuadernoError(f"{folders[0]} is a folder (use -r)")
        dest = os.path.expanduser(dest)
        if len(targets) > 1 or recursive:
            if os.path.exists(dest) and not os.path.isdir(dest):
                raise QuadernoError(f"{dest} is not a directory ({len(targets)} items to download)")
            os.makedirs(dest, exist_ok=True)
        count = 0
        for e in targets:
            if e.is_folder:
                count += self.get_tree(e, dest, on_event)
            else:
                target = os.path.join(dest, safe_local_name(e.name)) if os.path.isdir(dest) else dest
                self.download(e, target, on_event)
                count += 1
        return count

    # --- upload -----------------------------------------------------------------
    def upload(self, local, folder=ROOT, name=None, on_event=None):
        """Upload one PDF into an existing folder (under `name`, default its file
        name). Raises if a document with that name is already there."""
        f = self._folder(folder)
        name = nfc(name or os.path.basename(local))
        if any(nfc(e.name) == name for e in self._children(f.id)):
            raise ExistsError(f"{f.path}/{name} already exists")
        self.upload_file(local, f.id, name)
        emit(on_event, "uploaded", f"{local} -> {f.path}/{name}", f"{f.path}/{name}")
        return self.resolve(f"{f.path}/{name}")

    def put(self, paths, dest=None, recursive=False, on_event=None):
        """Upload PDFs and (with recursive=True) local folders, CLI-style:
        - dest an existing folder: everything goes into it;
        - dest missing and ending in .pdf: the single file is uploaded under that name;
        - dest missing, single folder with recursive: the folder is created as dest.
        Never overwrites (same-named documents are skipped). Paths that can't be
        uploaded are skipped with a warning event instead of aborting."""
        if isinstance(paths, str):
            paths = [paths]
        target = norm(dest) if dest else ROOT
        d = self.try_resolve(target)
        if d is not None and not d.is_folder:
            raise ExistsError(f"{target} already exists")
        if d is None and len(paths) > 1:
            raise self._no_such_folder(target)
        res, items = PutResult(), []
        for local in paths:
            if os.path.isdir(local):
                if recursive:
                    items.append(local)
                else:
                    res.rejected += 1
                    emit(on_event, "skipped", f"skip {local}: is a directory (use -r)", local, True)
            elif not os.path.exists(local):
                res.missing += 1
                emit(on_event, "skipped", f"skip {local}: no such file", local, True)
            elif not local.lower().endswith(".pdf"):
                res.rejected += 1
                emit(on_event, "skipped", f"skip {local}: not a PDF", local, True)
            else:
                items.append(local)
        if not items:
            emit(on_event, "nothing", "nothing to upload", None, True)
            return res
        if d is None:
            local = items[0]
            if os.path.isdir(local):
                self._put_tree(local, target, res, on_event)
            else:
                if not target.lower().endswith(".pdf"):
                    raise self._no_such_folder(target)  # almost always a mistyped/missing folder
                parent, name = target.rsplit("/", 1)
                self._put_file(local, self.resolve(parent).id, parent, name, set(), res, on_event)
            return res
        existing = {nfc(e.name) for e in self._children(d.id)}
        for local in items:
            if os.path.isdir(local):
                name = nfc(os.path.basename(os.path.normpath(local)))
                self._put_tree(local, f"{d.path}/{name}", res, on_event)
            else:
                self._put_file(local, d.id, d.path, os.path.basename(local), existing, res, on_event)
        return res

    def _put_file(self, local, folder_id, folder_path, name, existing, res, on_event):
        name = nfc(name)
        if name in existing:
            res.skipped += 1
            emit(on_event, "skipped", f"skip {folder_path}/{name}: already exists",
                 f"{folder_path}/{name}", True)
            return False
        self.upload_file(local, folder_id, name)
        existing.add(name)
        res.uploaded += 1
        emit(on_event, "uploaded", f"{local} -> {folder_path}/{name}", f"{folder_path}/{name}")
        return True

    def _put_tree(self, local_dir, remote_path, res, on_event):
        """Mirror local_dir into remote_path. Only PDFs are sent; folders are created
        only where there is a PDF at or below them."""
        plan, ignored = [], 0
        for root, dirs, files in os.walk(local_dir):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            visible = [f for f in files if not f.startswith(".")]
            pdfs = sorted(f for f in visible if f.lower().endswith(".pdf"))
            ignored += len(visible) - len(pdfs)
            if pdfs:
                rel = os.path.relpath(root, local_dir)
                sub = remote_path if rel == "." else \
                    remote_path + "/" + "/".join(nfc(p) for p in rel.split(os.sep))
                plan.append((root, sub, pdfs))
        folder_id = self.ensure_folder(remote_path, on_event)
        uploaded = skipped = 0
        for root, sub, pdfs in plan:
            fid = folder_id if sub == remote_path else self.ensure_folder(sub, on_event)
            existing = {nfc(e.name) for e in self._children(fid)}
            for f in pdfs:
                if self._put_file(os.path.join(root, f), fid, sub, f, existing, res, on_event):
                    uploaded += 1
                else:
                    skipped += 1
        res.ignored += ignored
        extra = f", {ignored} non-PDF ignored" if ignored else ""
        emit(on_event, "summary", f"{uploaded} uploaded, {skipped} skipped{extra} -> {remote_path}",
             remote_path)

    def _no_such_folder(self, target):
        name = nfc(target.rsplit("/", 1)[-1]).casefold()
        hits = [e.path for e in self.walk(ROOT) if e.is_folder and nfc(e.name).casefold() == name]
        msg = (f"there is no folder {target} on the device; create it with `quaderno mkdir`, "
               "or give a new file name ending in .pdf")
        if hits:
            msg += "\ndid you mean: " + ", ".join(repr(h[len(ROOT) + 1:]) for h in hits[:5])
        return NotFoundError(msg)

    # --- organise ---------------------------------------------------------------
    def ensure_folder(self, path, on_event=None):
        """mkdir -p: create missing folders along `path`; returns the folder id."""
        path = norm(path)
        e = self.try_resolve(path)
        if e is not None:
            if not e.is_folder:
                raise ExistsError(f"{path} exists and is not a folder")
            return e.id
        if path == ROOT:
            raise NotFoundError("root folder not found")
        parent, name = path.rsplit("/", 1)
        folder_id = self.create_folder(self.ensure_folder(parent, on_event), name)
        emit(on_event, "created", f"created {path}/", path)
        return folder_id

    def mkdir(self, path, parents=False, on_event=None):
        """Create a folder; with parents=True also missing parents (and no error if
        it exists). Returns the folder id."""
        path = norm(path)
        if parents:
            return self.ensure_folder(path, on_event)
        if path == ROOT:
            raise ExistsError(f"{ROOT} already exists")
        parent, name = path.rsplit("/", 1)
        folder_id = self.create_folder(self.resolve(parent).id, name)
        emit(on_event, "created", f"created {path}/", path)
        return folder_id

    def move(self, patterns, dst, regex=False, on_event=None):
        """Move or rename documents/folders (patterns allowed). Several sources need
        an existing folder as `dst`. Returns [(old_path, new_path), ...]."""
        sources, _ = self.find_all(patterns, regex)
        dst = norm(dst)
        d = self.try_resolve(dst)
        if d is not None and not d.is_folder:
            raise ExistsError(f"{dst} already exists")
        if len(sources) > 1 and d is None:
            raise QuadernoError(f"{len(sources)} items to move: {dst} must be an existing folder")
        if any(e.path == ROOT for e in sources):
            raise QuadernoError("refusing to move the root folder")
        moved = []
        for e in sources:
            if d is not None:
                parent_id, parent_path, name = d.id, d.path, e.name
            else:
                parent_path, name = dst.rsplit("/", 1)
                parent_id = self.resolve(parent_path).id
            if e.path == f"{parent_path}/{name}":
                emit(on_event, "unchanged", f"{e.path}: already there", e.path)
                continue
            if e.is_folder:
                if parent_path == e.path or parent_path.startswith(e.path + "/"):
                    raise QuadernoError(f"cannot move {e.path} into itself")
                kind, field = "folders", "folder_name"
            else:
                kind, field = "documents", "file_name"
            self.request("PUT", f"/{kind}/{e.id}", {"parent_folder_id": parent_id, field: name})
            new = self.request("GET", f"/{kind}/{e.id}") or {}
            new_path = new.get("entry_path", f"{parent_path}/{name}").rstrip("/")
            emit(on_event, "moved", f"{e.path} -> {new_path}", new_path)
            moved.append((e.path, new_path))
        return moved

    def rm(self, patterns, recursive=False, regex=False, confirm=None, on_event=None):
        """Delete documents, and folders (empty ones, or with contents if
        recursive). Everything is checked before anything is deleted. If a pattern
        matched and `confirm` is given, confirm(entries) must return True to go on.
        Returns the entries deleted (top level), or [] if not confirmed."""
        plan, by_pattern = self.find_all(patterns, regex)
        plan.sort(key=lambda e: e.path)
        for e in plan:
            if e.path == ROOT:
                raise QuadernoError("refusing to delete the root folder")
            if e.is_folder and not recursive and self._children(e.id):
                raise NotEmptyError(f"{e.path} is not empty; use -r to delete its contents too")
        folders = [e.path + "/" for e in plan if e.is_folder]
        plan = [e for e in plan if not any(e.path.startswith(f) for f in folders)]
        if by_pattern and confirm is not None and not confirm(plan):
            return []
        for e in plan:
            if e.is_document:
                self.request("DELETE", f"/documents/{e.id}")
                emit(on_event, "deleted", f"deleted {e.path}", e.path)
                continue
            if recursive:
                tree = list(self.walk(e))
                for x in tree:
                    if x.is_document:
                        self.request("DELETE", f"/documents/{x.id}")
                        emit(on_event, "deleted", f"deleted {x.path}", x.path)
                for x in sorted((x for x in tree if x.is_folder),
                                key=lambda x: x.path.count("/"), reverse=True):
                    self._delete_empty_folder(x, on_event)
            self._delete_empty_folder(e, on_event)
        return plan

    def _delete_empty_folder(self, e, on_event):
        # The device's behaviour on non-empty folders is undocumented (it may delete
        # recursively), so DELETE is only ever sent for a folder verified to be empty.
        children = self._children(e.id)
        if children:
            raise NotEmptyError(f"{e.path} is not empty ({len(children)} entries); "
                                "use -r to delete its contents first")
        self.request("DELETE", f"/folders/{e.id}")
        emit(on_event, "deleted", f"deleted {e.path}/", e.path)

    # --- the device itself -------------------------------------------------------
    def open(self, path, page=None, regex=False):
        """Show a document on the device's screen (at `page`, or where it was left).
        A pattern must match exactly one document. Returns its Entry."""
        matches = [path] if isinstance(path, Entry) else self.find(path, regex)[0]
        docs = [e for e in matches if e.is_document]
        if len(docs) != 1:
            if not docs:
                raise QuadernoError(f"{matches[0].path} is a folder")
            listing = "\n  ".join(e.path for e in docs[:10])
            raise QuadernoError(f"{path!r} matches {len(docs)} documents; be more "
                                f"specific:\n  {listing}")
        e = docs[0]
        if page is None:
            self.request("PUT", "/viewer/controls/open", {"document_id": e.id})
        else:
            try:
                self.request("PUT", "/viewer/controls/open2", {"document_id": e.id, "page": int(page)})
            except QuadernoError as err:
                if err.status in (404, 405):
                    raise QuadernoError("this firmware does not support opening at a page") from err
                raise
        return e

    def set_time(self, when=None):
        """Set the device clock (default: now). The clock can't be read back.
        Returns the value sent, 'YYYY-MM-DDTHH:MM:SSZ'."""
        value = device_time(when)
        self.request("PUT", "/system/configs/datetime", {"value": value})
        return value

    def timezone(self):
        return (self.request("GET", "/system/configs/timezone") or {}).get("value")

    def info(self):
        """Firmware, storage, battery, owner, MAC: the device's raw answers
        (a part that failed holds {'error': ...})."""
        def get(path):
            try:
                return self.request("GET", path)
            except QuadernoError as err:
                return {"error": str(err)}
        return {"api_version": get("/api_version"),
                "firmware_version": get("/system/status/firmware_version"),
                "storage": get("/system/status/storage"),
                "battery": get("/system/status/battery"),
                "owner": get("/system/configs/owner"),
                "mac_address": get("/system/status/mac_address")}

    def screenshot(self, path):
        """Save the current screen as a PNG file. Returns its size in bytes."""
        with open(path, "wb") as fh:
            return self.request("GET", "/system/controls/screen_shot", stream_to=fh,
                                headers={"Accept": "*/*"})

    def sync(self, local, remote=ROOT, delete=False, dry_run=False, force=False, on_event=None):
        """One-way incremental mirror of a device folder into `local`; see
        quaderno.sync.sync_folder. Returns counts per action."""
        from .sync import sync_folder
        return sync_folder(self, local, remote, delete=delete, dry_run=dry_run, force=force,
                           on_event=on_event)
