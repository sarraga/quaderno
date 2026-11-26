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
optional `on_event` callback that receives an Event for each step.

Errors from the device are QuadernoError subclasses. Local file problems (a
directory that doesn't exist, no permission, a full disk) are OSError."""

import base64
import datetime
import errno
import logging
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import BinaryIO, cast
from urllib.parse import quote, urlencode

from . import credentials, discovery, pairing, usb
from .client import Client
from .credentials import Credentials
from .entry import Entry
from .errors import (CertificateError, ExistsError, NotEmptyError, NotFoundError, ProtocolError,
                     QuadernoError)
from .events import EventHandler, emit
from .models import (CertificateInfo, CommonValue, ConfigsV2, ConfigsV3, CurrentViewing,
                     DeviceInformation, DialogParameters, DocumentAttribute, DocumentProcessStatus,
                     DocumentsViewInfo, FileUploadResult, FolderViewInfo, IndicationInfo,
                     NoteTemplate, PenButtonAssignment, PenButtonsInfo, TransferWizardInfo,
                     TransferWizardNextScreen, UpdateFirmwarePrecheck, ViewerOpen2Request,
                     ViewerOpenConfirmRequest, WifiApConfig, WifiApInfo)
from .util import (ROOT, JsonObject, device_time, human, is_pattern, json_int, json_str, matcher,
                   nfc, norm, safe_local_name)

log = logging.getLogger("quaderno")

PathOrEntry = str | Entry
Patterns = str | Sequence[str]  # one path/pattern, or several

# search(): friendly names for the device's query values
SEARCH_TARGETS = {"meta": "document_meta", "body": "document_body", "mark": "annotation_pattern"}
SEARCH_ENTRY_TYPES = {"all": "all", "docs": "documents", "notes": "notes"}
ANNOTATION_TYPES = {"all": "all", "hand": "handwritings", "highlight": "highlights"}
# Scalar settings under /system/configs/<key> and the JSON key each one uses on the wire
_CONFIG_WIRE_KEY: dict[str, str] = {
    "use_mode": "use_mode",
    "annotations_permission": "deletable",
}
SETTABLE_CONFIGS = (
    "owner", "timezone", "date_format", "time_format", "timeout_to_standby",
    "mtp", "regulation_voltage", "use_mode", "annotations_permission",
    "additional_client_registration", "control_servers_editable", "initialized_flag",
    "wifi", "wifi_apmode", "wifi_sleep_policy",
)


@dataclass
class PutResult:
    uploaded: int = 0
    skipped: int = 0     # a document with that name was already there
    rejected: int = 0    # named paths that can't be uploaded (not a PDF, folder without recursive)
    missing: int = 0     # named local files that don't exist
    ignored: int = 0     # non-PDF files inside uploaded folders


def connect(addr: str | None = None, config_dir: str | None = None, timeout: float = 60,
            on_switch: usb.SwitchHandler | None = None,
            transport: discovery.Transport | None = None) -> "Device":
    """Find the device (or use `addr`), log in, and return a Device. Credentials
    come from `config_dir`, else $QUADERNO_CONFIG, else ~/.config/quaderno.
    `transport` selects `'usb'` (default), `'wifi'` (opt-in Wi-Fi mDNS), or
    `'auto'` (USB first, then Wi-Fi; also configurable via $QUADERNO_TRANSPORT).
    If pairing didn't store the device's certificate (it sent one that couldn't
    be read), TLS is not verified, and every call warns about it."""
    creds = credentials.load_credentials(credentials.find_credentials_dir(config_dir))
    if creds.pinned_cert is None:
        log.warning("warning: the device's TLS certificate is not checked: the credentials in "
                    "%s include none; run `quaderno pair --force` to pair again and save it",
                    creds.source)
    explicit_addr = addr or os.environ.get("QUADERNO_ADDR")
    resolved_transport = discovery.parse_transport(transport or os.environ.get("QUADERNO_TRANSPORT"))
    cached_addr = credentials.load_wifi_addr(creds.source)
    if explicit_addr:
        addr = explicit_addr
    else:
        addr = discovery.discover(
            creds.serial,
            transport=resolved_transport,
            on_switch=on_switch,
            cached_addr=cached_addr,
            pinned_cert=creds.pinned_cert,
        )
    dev = Device(addr, creds.client_id, creds.key, timeout=timeout, pinned_cert=creds.pinned_cert)
    dev.credentials = creds
    try:
        dev.authenticate()
    except CertificateError:
        if not explicit_addr and cached_addr and addr == cached_addr:
            credentials.clear_wifi_addr(creds.source)
        raise
    if not explicit_addr and resolved_transport != "usb" and credentials.is_wifi_addr(addr):
        credentials.save_wifi_addr(creds.source, addr)
    return dev


def _need_dir(path: str) -> None:
    """Raise FileNotFoundError naming the directory if `path` can't be created
    because its directory doesn't exist (checked before talking to the device)."""
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise FileNotFoundError(errno.ENOENT, "No such directory", parent)


def _atomic_write(target: str, write_to: Callable[[BinaryIO], int]) -> int:
    """Write via a private temporary file in target's directory and rename atomically."""
    _need_dir(target)
    parent = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(prefix=".quaderno-", suffix=".part", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            n = write_to(fh)
        os.replace(tmp, target)
        return n
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class Device(Client):
    """A logged-in QUADERNO. Use connect() to get one."""

    credentials: Credentials | None = None

    # --- lookup ----------------------------------------------------------------
    def resolve(self, path: str, *, v2: bool = False) -> Entry:
        """The Entry at `path` (exact, no patterns). Raises NotFoundError.
        With v2=True, uses GET /resolve/entry/path/{path}."""
        prefix = "/resolve/entry/path/" if v2 else "/resolve/entry/"
        return Entry.from_api(self.request_json("GET", prefix + quote(norm(path), safe="")))

    def resolve_ext_id(self, ext_id: str) -> Entry:
        """Look up an entry by external ID (GET /resolve/entry/ext_id/{ext_id})."""
        return Entry.from_api(self.request_json(
            "GET", "/resolve/entry/ext_id/" + quote(ext_id, safe="")))

    def get_document(self, doc_id: str, *, v2: bool = False) -> Entry:
        """Metadata for a document by ID (GET /documents/{id} or /documents2/{id})."""
        base = "/documents2/" if v2 else "/documents/"
        return Entry.from_api(self.request_json("GET", base + quote(doc_id, safe="")))

    def get_folder(self, folder_id: str, *, v2: bool = False) -> Entry:
        """Metadata for a folder by ID (GET /folders/{id} or /folders2/{id})."""
        base = "/folders2/" if v2 else "/folders/"
        return Entry.from_api(self.request_json("GET", base + quote(folder_id, safe="")))

    def try_resolve(self, path: str) -> Entry | None:
        """Resolve ``path`` to an ``Entry``, returning ``None`` on HTTP 404."""
        try:
            return self.resolve(path)
        except QuadernoError as err:
            if err.status == 404:
                return None
            raise

    def _folder(self, folder: PathOrEntry) -> Entry:
        """Resolve ``folder`` and verify it is a folder, raising ``QuadernoError`` otherwise."""
        f = folder if isinstance(folder, Entry) else self.resolve(folder)
        if not f.is_folder:
            raise QuadernoError(f"{f.path} is not a folder")
        return f

    def _paged(self, base: str, limit: int | None = None, page_size: int = 500) -> list[Entry]:
        """Collect entries from a {count, entry_list} endpoint, following offset
        pagination. `base` carries its own query string, if any; `limit` caps how
        many are fetched in all. The device's own default page size is 1300."""
        out: list[Entry] = []
        seen_ids: set[str] = set()
        while True:
            sep = "&" if "?" in base else "?"
            page = f"{base}{sep}offset={len(out)}&limit={page_size}"
            r = self.request_json("GET", page)
            batch = r.get("entry_list", [])
            if not isinstance(batch, list) or not all(isinstance(d, dict) for d in batch):
                raise ProtocolError(f"GET {page}: entry_list isn't a list of entries")
            added = 0
            for d in batch:
                entry = Entry.from_api(d)
                if entry.id not in seen_ids:
                    seen_ids.add(entry.id)
                    out.append(entry)
                    added += 1
            count = json_int(r, "count") if "count" in r else len(out)
            if not batch or added == 0 or len(out) >= count or (limit is not None and len(out) >= limit):
                return out[:limit] if limit is not None else out

    def _children(self, folder_id: str, *, v2: bool = False, order: str | None = None,
                  fields: Sequence[str] | None = None, limit: int | None = None) -> list[Entry]:
        """List direct child entries of ``folder_id`` via paginated ``entries``/``entries2``."""
        suffix = "entries2" if v2 else "entries"
        q: dict[str, str] = {}
        if order:
            q["order_type"] = order
        if fields:
            q["fields"] = ",".join(fields)
        qs = ("?" + urlencode(q)) if q else ""
        return self._paged(f"/folders/{quote(folder_id, safe='')}/{suffix}{qs}", limit=limit)

    def search(self, keyword: str, target: str = "meta", entry_type: str = "all",
               mark: str = "all", order: str | None = None,
               limit: int | None = None, *, origin_folder: PathOrEntry | None = None,
               fields: Sequence[str] | None = None, v2: bool = False) -> list[Entry]:
        """Search documents on the device. `target`: 'meta' (names, titles and
        authors), 'body' (text inside documents) or 'mark' (by annotation-mark
        name, e.g. 'star'). `entry_type` may narrow to 'docs' or 'notes'; `mark`
        ('all'/'hand'/'highlight') applies to a mark search; `order` is a device
        order_type such as 'reading_date_desc'; `origin_folder` restricts to a
        subtree. Returns the matching Entries."""
        q = {"entry_type": SEARCH_ENTRY_TYPES[entry_type],
             "search_target": SEARCH_TARGETS[target], "search_keyword": keyword}
        if target == "mark":
            q["annotation_type"] = ANNOTATION_TYPES[mark]
        if origin_folder is not None:
            q["origin_folder_id"] = self._folder(origin_folder).id
        if order:
            q["order_type"] = order
        if fields:
            q["fields"] = ",".join(fields)
        endpoint = "/documents2?" if v2 else "/documents?"
        return self._paged(endpoint + urlencode(q), limit=limit)

    def entries(self, folder: PathOrEntry = ROOT, *, v2: bool = False, order: str | None = None,
                fields: Sequence[str] | None = None, limit: int | None = None) -> list[Entry]:
        """Direct children of a folder (path or Entry)."""
        return self._children(self._folder(folder).id, v2=v2, order=order, fields=fields,
                              limit=limit)

    def walk(self, folder: PathOrEntry = ROOT, *, _seen: set[str] | None = None) -> Iterator[Entry]:
        """Everything below a folder, depth-first; parents come before children."""
        seen = set() if _seen is None else _seen
        root_entry = self._folder(folder)
        if root_entry.id in seen:
            return
        seen.add(root_entry.id)
        for e in self._children(root_entry.id):
            yield e
            if e.is_folder:
                yield from self.walk(e, _seen=seen)

    def find(self, pattern: str, regex: bool = False) -> tuple[list[Entry], bool]:
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
            found: list[Entry] = []
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

    def find_all(self, patterns: Patterns, regex: bool = False) -> tuple[list[Entry], bool]:
        """find() for several patterns, de-duplicated. Returns (entries, any_pattern)."""
        seen: set[str] = set()
        out: list[Entry] = []
        any_pattern = False
        for p in [patterns] if isinstance(patterns, str) else patterns:
            entries, by_pattern = self.find(p, regex)
            any_pattern |= by_pattern
            for e in entries:
                if e.id not in seen:
                    seen.add(e.id)
                    out.append(e)
        return out, any_pattern

    def ls(self, path: str = ROOT, recursive: bool = False,
           regex: bool = False) -> tuple[list[Entry], bool]:
        """List a folder (or pattern matches; a matched folder is listed itself,
        like `ls -d`, unless recursive). Folders first. Returns (entries, matched_by_pattern)."""
        matches, by_pattern = self.find(path, regex)
        rows: dict[str, Entry] = {}
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
    def download(self, doc: PathOrEntry, target: str, on_event: EventHandler | None = None) -> int:
        """Save a document (path or Entry) to the local file `target`, atomically,
        with the device's modification time. Returns the size in bytes. If that
        time is unreadable, the file is kept with a warning, without it."""
        _need_dir(target)
        doc = doc if isinstance(doc, Entry) else self.resolve(doc)
        n = _atomic_write(target, lambda fh: self.download_to(doc.id, fh))
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

    def get_tree(self, folder: PathOrEntry, dest: str, on_event: EventHandler | None = None) -> int:
        """Download a folder as dest/<folder name>/..., keeping its layout.
        Returns the number of documents."""
        top = self._folder(folder)
        base = os.path.join(dest, safe_local_name(top.name))
        prefix = top.path + "/"
        os.makedirs(base, exist_ok=True)
        count = 0
        for e in self.walk(top):
            rel = e.path[len(prefix):] if e.path.startswith(prefix) else e.name
            target = os.path.join(base, *[safe_local_name(p) for p in rel.split("/")])
            if e.is_folder:
                os.makedirs(target, exist_ok=True)
            else:
                self.download(e, target, on_event)
                count += 1
        emit(on_event, "summary", f"{count} documents from {top.path} -> {base}", top.path)
        return count

    def get(self, patterns: Patterns, dest: str = ".", recursive: bool = False, regex: bool = False,
            on_event: EventHandler | None = None) -> int:
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
    def upload(self, local: str, folder: PathOrEntry = ROOT, name: str | None = None,
               on_event: EventHandler | None = None) -> Entry:
        """Upload one PDF into an existing folder (under `name`, default its file
        name). Raises if a document with that name is already there."""
        f = self._folder(folder)
        name = nfc(name or os.path.basename(local))
        if any(nfc(e.name) == name for e in self._children(f.id)):
            raise ExistsError(f"{f.path}/{name} already exists")
        self.upload_file(local, f.id, name)
        emit(on_event, "uploaded", f"{local} -> {f.path}/{name}", f"{f.path}/{name}")
        return self.resolve(f"{f.path}/{name}")

    @staticmethod
    def _classify_put_inputs(named: Sequence[str], recursive: bool, res: PutResult,
                             on_event: EventHandler | None) -> list[str]:
        """Filter local ``put`` arguments, recording missing/rejected items in ``res``."""
        items: list[str] = []
        for local in named:
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
        return items

    def put(self, paths: Patterns, dest: str | None = None, recursive: bool = False,
            on_event: EventHandler | None = None) -> PutResult:
        """Upload PDFs and (with recursive=True) local folders, CLI-style:
        - dest an existing folder: everything goes into it;
        - dest missing and ending in .pdf: the single file is uploaded under that name;
        - dest missing, single folder with recursive: the folder is created as dest.
        Never overwrites (same-named documents are skipped). Paths that can't be
        uploaded are skipped with a warning event instead of aborting."""
        named = [paths] if isinstance(paths, str) else list(paths)
        target = norm(dest) if dest else ROOT
        d = self.try_resolve(target)
        if d is not None and not d.is_folder:
            raise ExistsError(f"{target} already exists")
        if d is None and len(named) > 1:
            raise self._no_such_folder(target)
        res = PutResult()
        items = self._classify_put_inputs(named, recursive, res, on_event)
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

    def _put_file(self, local: str, folder_id: str, folder_path: str, name: str,
                  existing: set[str], res: PutResult, on_event: EventHandler | None) -> bool:
        """Upload one local PDF into ``folder_id`` unless ``name`` already exists there."""
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

    def _put_tree(self, local_dir: str, remote_path: str, res: PutResult,
                  on_event: EventHandler | None) -> None:
        """Mirror local_dir into remote_path. Only PDFs are sent; folders are created
        only where there is a PDF at or below them."""
        plan: list[tuple[str, str, list[str]]] = []
        ignored = 0
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

    def _no_such_folder(self, target: str) -> NotFoundError:
        """Build a helpful ``NotFoundError`` suggesting similarly named device folders."""
        name = nfc(target.rsplit("/", 1)[-1]).casefold()
        hits = [e.path for e in self.walk(ROOT) if e.is_folder and nfc(e.name).casefold() == name]
        msg = (f"there is no folder {target} on the device; create it with `quaderno mkdir`, "
               "or give a new file name ending in .pdf")
        if hits:
            msg += "\ndid you mean: " + ", ".join(repr(h[len(ROOT) + 1:]) for h in hits[:5])
        return NotFoundError(msg)

    # --- organise ---------------------------------------------------------------
    def ensure_folder(self, path: str, on_event: EventHandler | None = None) -> str:
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

    def mkdir(self, path: str, parents: bool = False, on_event: EventHandler | None = None) -> str:
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

    def _plan_transfer(self, sources: list[Entry], dst: str, *, verb: str,
                       allow_same: bool = False) -> list[tuple[Entry, str, str, str]]:
        """Validate destination and compute ``(entry, parent_id, parent_path, name)`` plan.

        Checks all name collisions before any mutation occurs.

        Raises:
            ExistsError: If any destination name is taken or two sources collide.
            QuadernoError: If multiple sources target a non-existent folder or a
                folder would be moved into itself.
        """
        d = self.try_resolve(dst)
        if d is not None and not d.is_folder:
            raise ExistsError(f"{dst} already exists")
        if len(sources) > 1 and d is None:
            raise QuadernoError(f"{len(sources)} items to {verb}: {dst} must be an existing folder")
        taken = {nfc(c.name) for c in self._children(d.id)} if d is not None else set()
        plan: list[tuple[Entry, str, str, str]] = []
        claimed: dict[str, str] = {}
        for e in sources:
            if d is not None:
                parent_id, parent_path, name = d.id, d.path, e.name
            else:
                parent_path, name = dst.rsplit("/", 1)
                parent_id = self.resolve(parent_path).id
            target = f"{parent_path}/{name}"
            if not (allow_same and nfc(e.path) == nfc(target)):
                if e.is_folder and (parent_path == e.path or parent_path.startswith(e.path + "/")):
                    raise QuadernoError(f"cannot {verb} {e.path} into itself")
                if nfc(name) in taken:
                    raise ExistsError(f"cannot {verb} {e.path}: {target} already exists")
                if nfc(target) in claimed:
                    raise ExistsError(f"cannot {verb} both {claimed[nfc(target)]} and {e.path} "
                                      f"to {target}")
                claimed[nfc(target)] = e.path
            plan.append((e, parent_id, parent_path, name))
        return plan

    def move(self, patterns: Patterns, dst: str, regex: bool = False,
             on_event: EventHandler | None = None) -> list[tuple[str, str]]:
        """Move or rename documents/folders (patterns allowed). Several sources need
        an existing folder as `dst`. Everything is checked before anything moves:
        a name already taken at the destination, or two sources that would land on
        the same name, raise ExistsError. Returns [(old_path, new_path), ...]."""
        sources, _ = self.find_all(patterns, regex)
        if any(e.path == ROOT for e in sources):
            raise QuadernoError("refusing to move the root folder")
        plan = self._plan_transfer(sources, norm(dst), verb="move", allow_same=True)
        moved: list[tuple[str, str]] = []
        for e, parent_id, parent_path, name in plan:
            if nfc(e.path) == nfc(f"{parent_path}/{name}"):
                emit(on_event, "unchanged", f"{e.path}: already there", e.path)
                continue
            kind, field = ("folders", "folder_name") if e.is_folder else ("documents", "file_name")
            self.request("PUT", f"/{kind}/{e.id}", {"parent_folder_id": parent_id, field: name})
            new_path = (self.request("GET", f"/{kind}/{e.id}") or {}).get("entry_path")
            if not isinstance(new_path, str):
                new_path = f"{parent_path}/{name}"
            new_path = new_path.rstrip("/")
            emit(on_event, "moved", f"{e.path} -> {new_path}", new_path)
            moved.append((e.path, new_path))
        return moved

    def copy(self, patterns: Patterns, dst: str, regex: bool = False,
             on_event: EventHandler | None = None) -> list[tuple[str, str]]:
        """Copy documents on the device itself (no download/upload). Folders can't
        be copied. Several sources need an existing folder as `dst`; a single source
        may instead name a new file. Name clashes are checked before anything is
        copied. Returns [(source_path, new_path), ...]."""
        sources, _ = self.find_all(patterns, regex)
        folders = [e.path for e in sources if e.is_folder]
        if folders:
            raise QuadernoError(f"cannot copy the folder {folders[0]} (only documents can be copied)")
        plan = self._plan_transfer(sources, norm(dst), verb="copy")
        copied: list[tuple[str, str]] = []
        for e, parent_id, parent_path, name in plan:
            r = self.request("POST", f"/documents/{e.id}/copy",
                             {"parent_folder_id": parent_id, "file_name": name}, ok=(200, 201))
            new_id = (r or {}).get("document_id")
            new_path = f"{parent_path}/{name}"
            if isinstance(new_id, str):  # confirm the real path (the device may adjust the name)
                got = (self.request("GET", f"/documents/{new_id}") or {}).get("entry_path")
                if isinstance(got, str):
                    new_path = got.rstrip("/")
            emit(on_event, "copied", f"{e.path} -> {new_path}", new_path)
            copied.append((e.path, new_path))
        return copied

    def rm(self, patterns: Patterns, recursive: bool = False, regex: bool = False,
           confirm: Callable[[list[Entry]], bool] | None = None,
           on_event: EventHandler | None = None) -> list[Entry]:
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

    def _delete_empty_folder(self, e: Entry, on_event: EventHandler | None) -> None:
        """Verify folder ``e`` has no children and delete it on the device."""
        # Firmware refuses non-empty folder deletion with 400/40018 unless
        # force_delete_flag="true" is passed; we still verify emptiness up front
        # so rm -r deletes items individually with progress events.
        children = self._children(e.id)
        if children:
            raise NotEmptyError(f"{e.path} is not empty ({len(children)} entries); "
                                "use -r to delete its contents first")
        self.delete_folder(e.id)
        emit(on_event, "deleted", f"deleted {e.path}/", e.path)

    # --- low-level document/folder operations (v1/v2, attributes, annotations) ---
    def replace_document(self, doc: PathOrEntry, local: str, *,
                         target_revision: str | None = None, file_hash: str | None = None,
                         on_event: EventHandler | None = None) -> FileUploadResult:
        """Replace an existing document's PDF content in place (PUT /documents/{id}/file).

        Returns a ``FileUploadResult`` (``completed``, ``current_bytes``, ``received_bytes``,
        ``file_revision``). Passing ``target_revision`` guards against concurrent edits
        on the device (HTTP 400 / error_code 40017)."""
        entry = doc if isinstance(doc, Entry) else self.resolve(doc)
        if not entry.is_document:
            raise QuadernoError(f"{entry.path} is not a document")
        res = self.replace_file(entry.id, local, target_revision=target_revision,
                                file_hash=file_hash)
        emit(on_event, "uploaded", f"{local} -> {entry.path}", entry.path)
        return res

    def update_document(self, doc_id: str, *, file_name: str | None = None,
                        parent_folder_id: str | None = None,
                        ext_id: str | None = None) -> None:
        """Update document metadata (PUT /documents/{id}, or /documents2/{id} with ext_id)."""
        body: JsonObject = {}
        if file_name is not None:
            body["file_name"] = file_name
        if parent_folder_id is not None:
            body["parent_folder_id"] = parent_folder_id
        if ext_id is not None:
            body["ext_id"] = ext_id
        base = "/documents2/" if ext_id is not None else "/documents/"
        self.request("PUT", base + quote(doc_id, safe=""), body)

    def delete_document(self, doc_id: str, *, target_revision: str | None = None) -> None:
        """Delete a document by ID, optionally guarded by `target_revision`."""
        body: JsonObject | None = ({"target_revision": target_revision}
                                   if target_revision is not None else None)
        self.request("DELETE", f"/documents/{quote(doc_id, safe='')}", body)

    def update_folder(self, folder_id: str, *, folder_name: str | None = None,
                      parent_folder_id: str | None = None,
                      ext_id: str | None = None) -> None:
        """Update folder metadata (PUT /folders/{id}, or /folders2/{id} with ext_id)."""
        body: JsonObject = {}
        if folder_name is not None:
            body["folder_name"] = folder_name
        if parent_folder_id is not None:
            body["parent_folder_id"] = parent_folder_id
        if ext_id is not None:
            body["ext_id"] = ext_id
        base = "/folders2/" if ext_id is not None else "/folders/"
        self.request("PUT", base + quote(folder_id, safe=""), body)

    def delete_folder(self, folder_id: str, *, force: bool = False) -> None:
        """Delete a folder by ID. Without force=True, the device rejects non-empty
        folders with HTTP 400 (error_code 40018)."""
        body: JsonObject | None = {"force_delete_flag": "true"} if force else None
        self.request("DELETE", f"/folders/{quote(folder_id, safe='')}", body)

    def download_annotation(self, doc: PathOrEntry, page: int, target: str) -> int:
        """Save raw binary annotation data for `page` of `doc` to `target`."""
        _need_dir(target)
        entry = doc if isinstance(doc, Entry) else self.resolve(doc)
        if not entry.is_document:
            raise QuadernoError(f"{entry.path} is not a document")
        return _atomic_write(target, lambda fh: self.download_annotation_to(entry.id, page, fh))

    def get_document_attribute(self, doc: PathOrEntry) -> DocumentAttribute:
        """Get custom action attributes for a document (GET /documents/{id}/attribute)."""
        entry = doc if isinstance(doc, Entry) else self.resolve(doc)
        return cast(DocumentAttribute,
                    self.request_json("GET", f"/documents/{quote(entry.id, safe='')}/attribute"))

    def set_document_attribute(self, doc: PathOrEntry, attribute: DocumentAttribute) -> None:
        """Set custom action attributes on a document (PUT /documents/{id}/attribute)."""
        entry = doc if isinstance(doc, Entry) else self.resolve(doc)
        self.request("PUT", f"/documents/{quote(entry.id, safe='')}/attribute", attribute)

    def delete_document_attribute(self, doc: PathOrEntry) -> None:
        """Delete custom action attributes from a document (DELETE /documents/{id}/attribute)."""
        entry = doc if isinstance(doc, Entry) else self.resolve(doc)
        self.request("DELETE", f"/documents/{quote(entry.id, safe='')}/attribute")

    # --- viewer & note templates -----------------------------------------------
    def _single_document(self, path: PathOrEntry, regex: bool = False) -> Entry:
        """Resolve ``path``/pattern to exactly one document ``Entry`` or raise ``QuadernoError``."""
        matches = [path] if isinstance(path, Entry) else self.find(path, regex)[0]
        docs = [e for e in matches if e.is_document]
        if len(docs) != 1:
            if not docs:
                raise QuadernoError(f"{matches[0].path} is a folder")
            listing = "\n  ".join(e.path for e in docs[:10])
            raise QuadernoError(f"{path!r} matches {len(docs)} documents; be more "
                                f"specific:\n  {listing}")
        return docs[0]

    def open(self, path: PathOrEntry, page: int | None = None, regex: bool = False) -> Entry:
        """Show a document on the device's screen (at `page`, or where it was left).
        A pattern must match exactly one document. Returns its Entry."""
        e = self._single_document(path, regex)
        if page is None:
            self.request("PUT", "/viewer/controls/open", {"document_id": e.id})
        else:
            req: ViewerOpen2Request = {"document_id": e.id, "page": str(int(page))}
            try:
                self.request("PUT", "/viewer/controls/open2", req)
            except QuadernoError as err:
                if err.status in (404, 405):
                    raise QuadernoError("this firmware does not support opening at a page") from err
                raise
        return e

    def open_confirm(self, path: PathOrEntry, title: str, message: str,
                     positive_button: str, negative_button: str, *,
                     page: int | None = None, regex: bool = False) -> Entry:
        """Prompt the user with a two-button dialog before opening a document
        (PUT /viewer/controls/open2_confirm)."""
        e = self._single_document(path, regex)
        body: ViewerOpenConfirmRequest = {
            "document_id": e.id,
            "confirm_dialog_params": {
                "title": title, "message": message,
                "positive_button_caption": positive_button,
                "negative_button_caption": negative_button,
            },
        }
        if page is not None:
            body["page"] = str(int(page))
        self.request("PUT", "/viewer/controls/open2_confirm", body)
        return e

    def show_documents_view(self, view_type: str = "list",
                            order_type: str = "reading_date_desc") -> None:
        """Switch the device UI to the documents list view."""
        body: DocumentsViewInfo = {"view_type": view_type, "order_type": order_type}
        self.request("PUT", "/viewer/controls/transfer/documents_view", body)

    def show_folder_view(self, folder: PathOrEntry = ROOT, view_type: str = "list",
                         order_type: str = "entry_name_asc") -> None:
        """Switch the device UI to a specific folder view."""
        f = self._folder(folder)
        body: FolderViewInfo = {"folder_id": f.id, "view_type": view_type,
                                "order_type": order_type}
        self.request("PUT", "/viewer/controls/transfer/folder_view", body)

    def current_viewing(self) -> CurrentViewing:
        """What the device is showing now (GET /viewer/status/current_viewing):
        ``orientation`` (``'portlait'``/``'landscape'``), ``view_mode``, and
        ``views`` (each with ``entry_path``, ``current_page``, ``total_page``, ``title``)."""
        return cast(CurrentViewing, self.request_json("GET", "/viewer/status/current_viewing"))

    def preset_marks(self) -> list[str]:
        """List preset annotation mark names (GET /viewer/status/preset_marks)."""
        r = self.request_json("GET", "/viewer/status/preset_marks")
        items = r.get("pattern_list", [])
        if not isinstance(items, list):
            raise ProtocolError(f"GET /viewer/status/preset_marks: invalid pattern_list: {r!r}")
        out: list[str] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("pattern"), str):
                out.append(item["pattern"])
            elif isinstance(item, str):
                out.append(item)
        return out

    def note_templates(self) -> list[NoteTemplate]:
        """List all note templates (GET /viewer/configs/note_templates)."""
        r = self.request_json("GET", "/viewer/configs/note_templates")
        items = r.get("template_list", [])
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise ProtocolError(f"GET /viewer/configs/note_templates: invalid template_list: {r!r}")
        return cast(list[NoteTemplate], list(items))

    def get_note_template(self, template_id: str) -> NoteTemplate:
        """Get metadata for a single note template."""
        return cast(NoteTemplate, self.request_json(
            "GET", f"/viewer/configs/note_templates/{quote(template_id, safe='')}"))

    def upload_note_template(self, local: str, name: str | None = None,
                             on_event: EventHandler | None = None) -> str:
        """Create and upload a PDF note template; returns its note_template_id.
        Deletes the half-created template entry if the upload fails."""
        tname = nfc(name or os.path.splitext(os.path.basename(local))[0])
        tid = json_str(self.request_json("POST", "/viewer/configs/note_templates",
                                         {"template_name": tname}), "note_template_id")
        try:
            self.upload_note_template_file(tid, local)
        except BaseException:
            try:
                self.delete_note_template(tid)
            except (QuadernoError, OSError):
                log.warning("warning: could not remove incomplete note template %s", tname)
            raise
        emit(on_event, "uploaded", f"{local} -> template {tname} ({tid})", tid)
        return tid

    def rename_note_template(self, template_id: str, name: str) -> None:
        """Rename a note template (PUT /viewer/configs/note_templates/{id})."""
        self.request("PUT", f"/viewer/configs/note_templates/{quote(template_id, safe='')}",
                     {"template_name": nfc(name)})

    def delete_note_template(self, template_id: str) -> None:
        """Delete a note template (DELETE /viewer/configs/note_templates/{id})."""
        self.request("DELETE", f"/viewer/configs/note_templates/{quote(template_id, safe='')}")

    # --- settings & status -----------------------------------------------------
    def ping(self) -> None:
        """Authenticated keep-alive / connectivity check (GET /ping)."""
        self.request("GET", "/ping")

    def device_information(self) -> DeviceInformation:
        """Pre-auth device info from port 8080 (GET /register/information):
        model_name, serial_number, sku_code, device_color."""
        status, res = pairing.reg_request(self.addr, "GET", "/register/information")
        if status != 200 or not isinstance(res, dict):
            raise ProtocolError(f"GET /register/information -> HTTP {status}: {res!r}")
        return cast(DeviceInformation, res)

    def serial_number(self) -> str:
        """Device serial number from port 8080 (GET /register/serial_number)."""
        status, res = pairing.reg_request(self.addr, "GET", "/register/serial_number")
        if status != 200 or not isinstance(res, dict):
            raise ProtocolError(f"GET /register/serial_number -> HTTP {status}: {res!r}")
        return json_str(res, "value")

    def set_time(self, when: datetime.datetime | None = None) -> str:
        """Set the device clock (default: now). The clock can't be read back.
        Returns the value sent, 'YYYY-MM-DDTHH:MM:SSZ'."""
        value = device_time(when)
        self.request("PUT", "/system/configs/datetime", {"value": value})
        return value

    def timezone(self) -> str | None:
        """Return the device's configured IANA timezone string, or ``None`` if absent."""
        value = (self.request("GET", "/system/configs/timezone") or {}).get("value")
        return value if isinstance(value, str) else None

    def configs_v2(self) -> ConfigsV2:
        """Raw device settings from GET /system/configs2."""
        return cast(ConfigsV2, self.request_json("GET", "/system/configs2"))

    def configs_v3(self) -> ConfigsV3:
        """Raw device settings from GET /system/configs3 (most complete)."""
        return cast(ConfigsV3, self.request_json("GET", "/system/configs3"))

    def configs(self) -> dict[str, str]:
        """All scalar device settings as a flat {name: value} map. Uses
        GET /system/configs3 (falling back to /system/configs on older firmware)
        and normalizes 'timeout_to_sleep' to 'timeout_to_standby'."""
        try:
            r = self.request_json("GET", "/system/configs3")
        except QuadernoError as err:
            if err.status in (404, 405):
                r = self.request_json("GET", "/system/configs")
            else:
                raise
        out: dict[str, str] = {}
        for k, v in r.items():
            if not isinstance(v, dict):
                continue
            name = "timeout_to_standby" if k == "timeout_to_sleep" else k
            wire_key = _CONFIG_WIRE_KEY.get(name, "value")
            val = v.get(wire_key)
            if isinstance(val, str):
                out[name] = val
        return out

    def get_config_raw(self, key: str) -> JsonObject:
        """Raw JSON object returned by GET /system/configs/<key>."""
        return self.request_json("GET", "/system/configs/" + quote(key, safe=""))

    def get_config(self, key: str) -> str:
        """One scalar setting's value (GET /system/configs/<key>). Handles keys
        that use 'use_mode' or 'deletable' in addition to standard 'value'."""
        r = self.get_config_raw(key)
        wire_key = _CONFIG_WIRE_KEY.get(key, "value")
        return json_str(r, wire_key)

    def set_config(self, key: str, value: str) -> None:
        """Change a device setting (PUT /system/configs/<key>). Only known
        scalar settings may be changed; see SETTABLE_CONFIGS."""
        if key not in SETTABLE_CONFIGS:
            raise QuadernoError(f"{key!r} can't be set; settable: {', '.join(SETTABLE_CONFIGS)}")
        wire_key = _CONFIG_WIRE_KEY.get(key, "value")
        self.request("PUT", "/system/configs/" + quote(key, safe=""), {wire_key: value})

    def pen_buttons(self, *, assigning_only: bool = False,
                    choices_only: bool = False) -> PenButtonsInfo:
        """Get pen button assignments and/or available choices
        (GET /system/configs/pen_buttons_function)."""
        q: dict[str, str] = {}
        if assigning_only:
            q["assigning_only"] = "true"
        if choices_only:
            q["choices_only"] = "true"
        qs = ("?" + urlencode(q)) if q else ""
        return cast(PenButtonsInfo, self.request_json("GET", f"/system/configs/pen_buttons_function{qs}"))

    def set_pen_buttons(self, assigning: Sequence[PenButtonAssignment]) -> None:
        """Set pen button assignments (PUT /system/configs/pen_buttons_function)."""
        self.request("PUT", "/system/configs/pen_buttons_function",
                     {"assigning": list(assigning)})

    def info(self) -> dict[str, JsonObject]:
        """Firmware, storage, battery, owner, MAC, Wi-Fi state: the device's raw
        answers (a part that failed holds {'error': ...})."""
        def get(path: str) -> JsonObject:
            """Fetch one status endpoint, returning ``{'error': ...}`` on failure."""
            try:
                return self.request("GET", path) or {}
            except QuadernoError as err:
                return {"error": str(err)}
        return {"api_version": get("/api_version"),
                "firmware_version": get("/system/status/firmware_version"),
                "storage": get("/system/status/storage"),
                "battery": get("/system/status/battery"),
                "owner": get("/system/configs/owner"),
                "mac_address": get("/system/status/mac_address"),
                "wifi_state": get("/system/status/wifi_state")}

    def wifi_state(self) -> WifiApInfo:
        """Current Wi-Fi connection status (GET /system/status/wifi_state)."""
        return cast(WifiApInfo, self.request_json("GET", "/system/status/wifi_state"))

    def wifi_scan(self) -> list[WifiApInfo]:
        """Scan for nearby Wi-Fi access points (POST /system/controls/wifi_accesspoints/scan)."""
        r = self.request_json("POST", "/system/controls/wifi_accesspoints/scan")
        aps = r.get("aplist", [])
        if not isinstance(aps, list) or not all(isinstance(x, dict) for x in aps):
            raise ProtocolError(f"POST /system/controls/wifi_accesspoints/scan: bad aplist: {r!r}")
        return cast(list[WifiApInfo], list(aps))

    def wifi_accesspoints(self) -> list[WifiApConfig]:
        """List saved Wi-Fi access points (GET /system/configs/wifi_accesspoints)."""
        r = self.request_json("GET", "/system/configs/wifi_accesspoints")
        aps = r.get("aplist", [])
        if not isinstance(aps, list) or not all(isinstance(x, dict) for x in aps):
            raise ProtocolError(f"GET /system/configs/wifi_accesspoints: bad aplist: {r!r}")
        return cast(list[WifiApConfig], list(aps))

    def get_wifi_accesspoint(self, ssid: str, security: str) -> WifiApConfig:
        """Get config for one saved Wi-Fi access point (`security`: nonsec|psk|eap)."""
        return cast(WifiApConfig, self.request_json(
            "GET", f"/system/configs/wifi_accesspoints/{quote(ssid, safe='')}/{quote(security, safe='')}"))

    def add_wifi_accesspoint(self, ssid: str = "", security: str = "psk", passwd: str = "",
                             *, config: WifiApConfig | None = None, **extra: str) -> None:
        """Register and connect to a Wi-Fi access point
        (PUT /system/controls/wifi_accesspoints/register).

        Either pass ``ssid``/``security``/``passwd`` (plus optional ``ApConfigureObject``
        fields via ``**extra``) or pass a full ``WifiApConfig`` struct via ``config=``."""
        if config is not None:
            body: JsonObject = {"dhcp": "true", "proxy": "false", **config, **extra}
        else:
            body = {"ssid": ssid, "security": security, "dhcp": "true",
                    "proxy": "false", **extra}
            if passwd:
                body["passwd"] = passwd
        self.request("PUT", "/system/controls/wifi_accesspoints/register", body)

    def delete_wifi_accesspoint(self, ssid: str, security: str) -> None:
        """Delete a saved Wi-Fi access point."""
        self.request(
            "DELETE",
            f"/system/configs/wifi_accesspoints/{quote(ssid, safe='')}/{quote(security, safe='')}")

    def ca_certificates(self) -> list[CertificateInfo]:
        """List installed CA certificates (GET /system/configs/certificates/ca)."""
        r = self.request_json("GET", "/system/configs/certificates/ca")
        certs = r.get("cert", [])
        if not isinstance(certs, list) or not all(isinstance(x, dict) for x in certs):
            raise ProtocolError(f"GET /system/configs/certificates/ca: bad cert list: {r!r}")
        return cast(list[CertificateInfo], list(certs))

    def install_ca_certificate(self, name: str, filename: str, data: bytes) -> None:
        """Install a CA certificate (POST /system/configs/certificates/ca)."""
        self.request("POST", "/system/configs/certificates/ca", {
            "name": name, "filename": filename, "file": base64.b64encode(data).decode()})

    def client_certificates(self) -> list[CertificateInfo]:
        """List installed client certificates (GET /system/configs/certificates/client)."""
        r = self.request_json("GET", "/system/configs/certificates/client")
        certs = r.get("cert", [])
        if not isinstance(certs, list) or not all(isinstance(x, dict) for x in certs):
            raise ProtocolError(f"GET /system/configs/certificates/client: bad cert list: {r!r}")
        return cast(list[CertificateInfo], list(certs))

    def install_client_certificate(self, name: str, filename: str, data: bytes,
                                   password: str = "") -> None:
        """Install a client PKCS#12 certificate (POST /system/configs/certificates/client)."""
        self.request("POST", "/system/configs/certificates/client", {
            "name": name, "filename": filename,
            "file": base64.b64encode(data).decode(), "password": password})

    def clear_certificates(self) -> None:
        """Remove all installed user certificates (DELETE /system/configs/certificates)."""
        self.request("DELETE", "/system/configs/certificates")

    def document_process(self) -> DocumentProcessStatus:
        """Status of background document processing (GET /system/status/document_process)."""
        return cast(DocumentProcessStatus,
                    self.request_json("GET", "/system/status/document_process"))

    def abort_document_process(self, content_id: str | None = None) -> None:
        """Abort an active background document process."""
        qs = ("?" + urlencode({"content_id": content_id})) if content_id is not None else ""
        self.request("PUT", f"/system/controls/abort_document_process{qs}")

    # --- device controls, diagnostics & firmware update ------------------------
    def screenshot(self, path: str, *, fmt: str | None = None) -> int:
        """Save the current screen as PNG or JPEG. Returns its size in bytes.
        When `fmt` is omitted, '.jpg'/'.jpeg' selects JPEG via screen_shot2;
        otherwise PNG via screen_shot."""
        _need_dir(path)
        chosen = fmt.lower() if fmt is not None else (
            "jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "png")
        if chosen == "jpg":
            chosen = "jpeg"
        if chosen not in ("png", "jpeg"):
            raise QuadernoError(f"unsupported screenshot format {chosen!r} (use 'png' or 'jpeg')")
        endpoint = ("/system/controls/screen_shot2?format=jpeg"
                    if chosen == "jpeg" else "/system/controls/screen_shot")
        return _atomic_write(
            path, lambda fh: self.request_stream("GET", endpoint, fh, headers={"Accept": "*/*"}))

    def sleep(self) -> None:
        """Put the device into sleep/standby (PUT /system/controls/sleep)."""
        self.request("PUT", "/system/controls/sleep")

    def wake(self) -> None:
        """Wake/resume the device (PUT /system/controls/run)."""
        self.request("PUT", "/system/controls/run")

    def reboot(self) -> None:
        """Reboot the device (PUT /system/controls/reboot)."""
        self.request("PUT", "/system/controls/reboot")

    def shutdown(self) -> None:
        """Power off the device (PUT /system/controls/shutdown)."""
        self.request("PUT", "/system/controls/shutdown")

    def reset_configs(self) -> None:
        """Reset all system settings to defaults (PUT /system/controls/reset_configs)."""
        self.request("PUT", "/system/controls/reset_configs")

    def clear_documents(self) -> None:
        """Delete all documents on the device (PUT /system/controls/clear_documents)."""
        self.request("PUT", "/system/controls/clear_documents")

    def download_pastlog(self, target: str) -> int:
        """Download the encrypted diagnostic pastlog archive (GET /system/controls/pastlog)."""
        return _atomic_write(target, self.download_pastlog_to)

    def indicate(self, dialog_params: DialogParameters, *,
                 show_animation: bool = False) -> CommonValue | None:
        """Display an on-screen indication/dialog (POST /system/controls/indicate)."""
        body: IndicationInfo = {
            "dialog_params": dialog_params,
            "show_animation": "true" if show_animation else "false",
        }
        return cast(CommonValue | None, self.request("POST", "/system/controls/indicate", body))

    def update_indication(self, indication_id: str, dialog_params: DialogParameters, *,
                          show_animation: bool = False) -> None:
        """Update an active on-screen indication (PUT /system/controls/indicate/{id})."""
        body: IndicationInfo = {
            "dialog_params": dialog_params,
            "show_animation": "true" if show_animation else "false",
        }
        self.request("PUT", f"/system/controls/indicate/{quote(indication_id, safe='')}", body)

    def dismiss_indication(self, indication_id: str) -> None:
        """Dismiss an active on-screen indication (DELETE /system/controls/indicate/{id})."""
        self.request("DELETE", f"/system/controls/indicate/{quote(indication_id, safe='')}")

    def transfer_wizard(self, wizard_id_list: Sequence[str],
                        next_screen: TransferWizardNextScreen | None = None) -> None:
        """Trigger the on-device transfer wizard (PUT /system/controls/transfer/wizard)."""
        body: TransferWizardInfo = {"wizard_id_list": list(wizard_id_list)}
        if next_screen is not None:
            body["next_screen"] = next_screen
        self.request("PUT", "/system/controls/transfer/wizard", body)

    def customize_ux(self, payload: JsonObject) -> None:
        """Apply UX customization JSON (PUT /system/controls/customize_ux)."""
        self.request("PUT", "/system/controls/customize_ux", payload)

    def customize_setting_ux(self, payload: JsonObject) -> None:
        """Apply Settings UX customization JSON (PUT /system/controls/customize_setting_ux)."""
        self.request("PUT", "/system/controls/customize_setting_ux", payload)

    def firmware_precheck(self) -> UpdateFirmwarePrecheck:
        """Check battery & firmware image readiness (GET /system/controls/update_firmware/precheck)."""
        return cast(UpdateFirmwarePrecheck,
                    self.request_json("GET", "/system/controls/update_firmware/precheck"))

    def upload_firmware(self, local: str, on_event: EventHandler | None = None) -> FileUploadResult:
        """Upload a FwUpdater.pkg image to the device (PUT /system/controls/update_firmware/file)."""
        res = self.upload_firmware_file(local)
        emit(on_event, "uploaded", f"uploaded firmware {local}", local)
        return res

    def start_firmware_update(self) -> None:
        """Trigger reboot into recovery to flash the uploaded firmware
        (PUT /system/controls/update_firmware)."""
        self.request("PUT", "/system/controls/update_firmware")

    def notify_login_result(self, success: bool) -> None:
        """Notify device of external login result (PUT /notify/login_result)."""
        self.request("PUT", "/notify/login_result", {"value": "success" if success else "failure"})

    def notify_logout_result(self, success: bool) -> None:
        """Notify device of external logout result (PUT /notify/logout_result)."""
        self.request("PUT", "/notify/logout_result", {"value": "success" if success else "failure"})

    def notify_force_assigned(self, value: str) -> None:
        """Notify device of forced assignment (PUT /notify/force_assigned)."""
        self.request("PUT", "/notify/force_assigned", {"value": value})

    def install_custom_app(self, local: str, application_id: str, signature: str,
                           encrypted_key_crypto: str, iv: str) -> None:
        """Install a signed & encrypted custom app package (POST /custom_apps/install)."""
        self.upload_custom_app(local, application_id, signature, encrypted_key_crypto, iv)

    def sync(self, local: str, remote: str = ROOT, delete: bool = False, dry_run: bool = False,
             force: bool = False, on_event: EventHandler | None = None) -> dict[str, int]:
        """One-way incremental mirror of a device folder into `local`; see
        quaderno.sync.sync_folder. Returns counts per action."""
        from .sync import sync_folder
        return sync_folder(self, local, remote, delete=delete, dry_run=dry_run, force=force,
                           on_event=on_event)
