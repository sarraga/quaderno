"""The `quaderno` command: argument parsing and printing around the Device API."""

import argparse
import datetime
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import credentials, pairing, usb
from .device import SETTABLE_CONFIGS, Device, connect
from .entry import Entry
from .errors import AuthError, ConnectError, PairingError, QuadernoError
from .events import Event
from .util import ROOT, JsonObject, human


class Args(argparse.Namespace):
    """The parsed command line. Which attributes are set depends on the command."""
    cmd: str
    fn: "Command | None"
    addr: str | None
    config_dir: str | None
    verbose: bool
    regex: bool           # ls, get, mv, cp, rm, open
    recursive: bool       # ls, get, put, rm
    path: str             # ls
    long: bool            # ls, search
    remote: list[str]     # get, rm, mkdir: device paths
    output: str | None    # get -o; screenshot
    local: list[str]      # put: local files and directories
    dest: str | None      # put -d
    yes: bool             # rm
    parents: bool         # mkdir
    src: list[str]        # mv, cp
    dst: str              # mv, cp
    json: bool            # info
    directory: str        # sync: local directory
    folder: str           # sync: device folder
    delete: bool          # sync
    dry_run: bool         # sync
    force: bool           # sync, pair
    target: str           # open: document; search: -t (meta/body/mark)
    page: int | None      # open
    pin_file: str | None  # pair
    keyword: str          # search
    notes: bool           # search
    docs: bool            # search
    sort: str | None      # search
    desc: bool            # search
    mark: str             # search
    key: str | None       # config
    value: str | None     # config
    origin_folder: str | None  # search -f
    format: str | None    # screenshot --format
    action: str           # templates / wifi / power / firmware / pen-buttons / certs
    name: str | None      # templates add/rename; certs
    template_id: str      # templates rename/rm
    ssid: str             # wifi add/rm
    security: str         # wifi add/rm
    password: str | None  # wifi add; certs add-client
    file: str             # replace; templates add; firmware upload; certs
    remote_doc: str       # replace; annotations
    revision: str | None  # replace
    button: str           # pen-buttons set
    function: str         # pen-buttons set
    all_configs: bool     # config --all


Command = Callable[[Device, Args], "int | None"]


def show(event: Event) -> None:
    """Print an API event: warnings to stderr, the rest to stdout."""
    print(event.message, file=sys.stderr if event.warning else sys.stdout)


def open_device(args: Args) -> Device:
    def on_switch(tty: str, serial: str | None) -> None:
        if args.verbose:
            print(f"switching {tty} ({serial}) to USB Ethernet", file=sys.stderr)
    dev = connect(addr=args.addr, config_dir=args.config_dir, on_switch=on_switch)
    if args.verbose:
        tls = "pinned to devicecert.bin" if dev.pinned_cert else "unverified (no pinned cert)"
        source = dev.credentials.source if dev.credentials else "?"
        print(f"credentials: {source}\ndevice: {dev.addr}\ntls: {tls}", file=sys.stderr)
    return dev


# --- commands (each takes a Device and the parsed args) ---------------------------

def _row(e: Entry, long: bool, full_path: bool) -> str:
    """One listing line: the name or full path, and with -l size, date and pages."""
    name = (e.path if full_path else e.name) + ("/" if e.is_folder else "")
    if not long:
        return name
    size = human(e.size) if e.size is not None else "-"
    date = (e.modified or e.created or "")[:16].replace("T", " ")
    pages = e.pages if e.pages is not None else ""
    return f"{size:>8}  {date:16}  {str(pages):>4}  {name}"


def cmd_ls(dev: Device, args: Args) -> None:
    rows, by_pattern = dev.ls(args.path, recursive=args.recursive, regex=args.regex)
    full_path = args.recursive or by_pattern
    for e in rows:
        print(_row(e, args.long, full_path))


_SORT_FIELDS = {"name": "entry_name", "title": "title", "author": "author",
                "date": "modified_date", "read": "reading_date", "size": "file_size",
                "created": "created_date"}


def cmd_search(dev: Device, args: Args) -> None:
    order = f"{_SORT_FIELDS[args.sort]}_{'desc' if args.desc else 'asc'}" if args.sort else None
    entry_type = "notes" if args.notes else "docs" if args.docs else "all"
    origin = getattr(args, "origin_folder", None)
    rows = dev.search(args.keyword, target=args.target, entry_type=entry_type,
                      mark=args.mark, order=order, origin_folder=origin)
    for e in rows:
        print(_row(e, args.long, full_path=True))
    if not rows:
        print(f"no matches for {args.keyword!r}", file=sys.stderr)


def cmd_cp(dev: Device, args: Args) -> None:
    dev.copy(args.src, args.dst, regex=args.regex, on_event=show)


def cmd_viewing(dev: Device, args: Args) -> None:
    views = dev.current_viewing().get("views")
    if not isinstance(views, list) or not views:
        print("nothing is open on the device")
        return
    for v in views:
        path = str(v.get("entry_path", "?")).rstrip("/")
        page = ""
        if v.get("current_page"):
            page = f"  page {v['current_page']}"
            if v.get("total_page"):
                page += f" of {v['total_page']}"
        title = v.get("title")
        print(path + page + (f"  ({title})" if title else ""))


def cmd_config(dev: Device, args: Args) -> None:
    if getattr(args, "all_configs", False):
        print(json.dumps(dev.configs_v3(), indent=2))
        return
    if args.key is None:
        for k, val in sorted(dev.configs().items()):
            print(f"{k:32} {val}")
    elif args.value is None:
        if args.key in ("pen_buttons_function", "wifi_accesspoints"):
            print(json.dumps(dev.get_config_raw(args.key), indent=2))
        else:
            print(dev.get_config(args.key))
    else:
        dev.set_config(args.key, args.value)
        print(f"{args.key} = {dev.get_config(args.key)}")


def cmd_get(dev: Device, args: Args) -> None:
    dev.get(args.remote, args.output or ".", recursive=args.recursive, regex=args.regex,
            on_event=show)


def cmd_put(dev: Device, args: Args) -> int | None:
    """Uploads what it can, like cp. Exit status 1 if anything named couldn't be
    uploaded (missing, not a PDF, a folder without -r). Files already on the
    device and non-PDFs inside folders uploaded with -r don't count."""
    res = dev.put(args.local, args.dest, recursive=args.recursive, on_event=show)
    return 1 if res.missing or res.rejected else None


def cmd_rm(dev: Device, args: Args) -> None:
    def confirm(plan: list[Entry]) -> bool:
        print(f"pattern matches {len(plan)}:")
        for e in plan:
            print("  " + e.path + ("/" if e.is_folder else ""))
        if not sys.stdin.isatty():
            raise QuadernoError("not deleting pattern matches without confirmation; add --yes")
        extra = " and everything inside" if args.recursive and any(e.is_folder for e in plan) else ""
        if input(f"delete these {len(plan)}{extra}? [y/N] ").strip().lower() in ("y", "yes"):
            return True
        print("nothing deleted")
        return False
    dev.rm(args.remote, recursive=args.recursive, regex=args.regex,
           confirm=None if args.yes else confirm, on_event=show)


def cmd_mkdir(dev: Device, args: Args) -> None:
    for path in args.remote:
        dev.mkdir(path, parents=args.parents, on_event=show)


def cmd_mv(dev: Device, args: Args) -> None:
    dev.move(args.src, args.dst, regex=args.regex, on_event=show)


def cmd_info(dev: Device, args: Args) -> None:
    info = dev.info()
    if args.json:
        print(json.dumps(info, indent=2))
        return

    def val(d: JsonObject, k: str = "value") -> object:
        return d.get(k, d.get("error", "?"))
    st, b = info["storage"], info["battery"]
    print(f"address        {dev.addr}")
    print(f"api version    {val(info['api_version'])}")
    print(f"firmware       {val(info['firmware_version'])}")
    print(f"owner          {val(info['owner'])}")
    print(f"mac address    {val(info['mac_address'])}")
    try:
        cap, free = int(st["capacity"]), int(st["available"])
        print(f"storage        {human(cap - free)} used / {human(cap)} ({human(free)} free)")
    except (KeyError, TypeError, ValueError):
        print(f"storage        {val(st)}")
    if "level" in b:
        print(f"battery        {b['level']}% ({b.get('status', '?')}, {b.get('plugged', '?')})")
    else:
        print(f"battery        {val(b)}")
    w = info.get("wifi_state")
    if isinstance(w, dict) and "state" in w:
        ssid = w.get("ssid")
        print(f"wifi           {w['state']}" + (f" ({ssid})" if ssid else ""))


def cmd_screenshot(dev: Device, args: Args) -> None:
    fmt = getattr(args, "format", None)
    default_out = "quaderno-screen.jpg" if fmt == "jpeg" else "quaderno-screen.png"
    output = args.output or default_out
    dev.screenshot(output, fmt=fmt)
    print(f"saved {output}")


def cmd_ping(dev: Device, args: Args) -> None:
    dev.ping()
    print("ok")


def cmd_marks(dev: Device, args: Args) -> None:
    for m in dev.preset_marks():
        print(m)


def cmd_replace(dev: Device, args: Args) -> None:
    res = dev.replace_document(args.remote_doc, args.file, target_revision=args.revision,
                               on_event=show)
    rev = res.get("file_revision")
    if rev:
        print(f"revision: {rev}")


def cmd_annotations(dev: Device, args: Args) -> None:
    output = args.output or f"annotations-p{args.page}.bin"
    assert args.page is not None
    n = dev.download_annotation(args.remote_doc, args.page, output)
    print(f"saved {output} ({human(n)})")


def cmd_pastlog(dev: Device, args: Args) -> None:
    output = args.output or "quaderno-pastlog.bin"
    n = dev.download_pastlog(output)
    print(f"saved {output} ({human(n)})")


def cmd_templates(dev: Device, args: Args) -> None:
    if args.action == "ls":
        for t in dev.note_templates():
            tid = t.get("note_template_id", "?")
            name = t.get("template_name", "?")
            mod = t.get("is_modifiable", "")
            print(f"{tid}  {name}" + (f"  (modifiable={mod})" if mod else ""))
    elif args.action == "add":
        tid = dev.upload_note_template(args.file, name=args.name, on_event=show)
        print(tid)
    elif args.action == "rename":
        assert args.name is not None
        dev.rename_note_template(args.template_id, args.name)
        print(f"renamed {args.template_id} -> {args.name}")
    elif args.action == "rm":
        dev.delete_note_template(args.template_id)
        print(f"deleted {args.template_id}")


def cmd_wifi(dev: Device, args: Args) -> None:
    if args.action == "status":
        print(json.dumps(dev.wifi_state(), indent=2))
    elif args.action in ("on", "off"):
        dev.set_config("wifi", args.action)
        print(f"wifi = {args.action}")
    elif args.action == "scan":
        for ap in dev.wifi_scan():
            print(f"{ap.get('ssid', '')}\t{ap.get('security', '')}\t"
                  f"{ap.get('frequency_band', '')}\t{ap.get('rssi_level', '')}\t"
                  f"{ap.get('state', '')}")
    elif args.action == "ls":
        for cfg in dev.wifi_accesspoints():
            print(f"{cfg.get('ssid', '')}\t{cfg.get('security', '')}")
    elif args.action == "add":
        dev.add_wifi_accesspoint(args.ssid, security=args.security, passwd=args.password or "")
        print(f"added {args.ssid} ({args.security})")
    elif args.action == "rm":
        dev.delete_wifi_accesspoint(args.ssid, args.security)
        print(f"deleted {args.ssid} ({args.security})")


def cmd_power(dev: Device, args: Args) -> None:
    actions: dict[str, Callable[[], None]] = {
        "sleep": dev.sleep, "wake": dev.wake, "reboot": dev.reboot, "shutdown": dev.shutdown,
    }
    actions[args.action]()
    print(f"sent {args.action}")


def cmd_firmware(dev: Device, args: Args) -> None:
    if args.action == "precheck":
        print(json.dumps(dev.firmware_precheck(), indent=2))
    elif args.action == "upload":
        res = dev.upload_firmware(args.file, on_event=show)
        print(json.dumps(res, indent=2))
    elif args.action == "update":
        if not args.yes:
            raise QuadernoError("starting a firmware update reboots the device; pass --yes to proceed")
        dev.start_firmware_update()
        print("firmware update started")


def cmd_pen_buttons(dev: Device, args: Args) -> None:
    if args.action == "ls":
        print(json.dumps(dev.pen_buttons(), indent=2))
    elif args.action == "set":
        dev.set_pen_buttons([{"button_id": args.button, "function": args.function}])
        print(f"{args.button} = {args.function}")


def cmd_certs(dev: Device, args: Args) -> None:
    if args.action == "ls":
        print(json.dumps({"ca": dev.ca_certificates(), "client": dev.client_certificates()}, indent=2))
    elif args.action == "add-ca":
        assert args.name is not None
        with open(args.file, "rb") as fh:
            dev.install_ca_certificate(args.name, os.path.basename(args.file), fh.read())
        print(f"installed CA cert {args.name}")
    elif args.action == "add-client":
        assert args.name is not None
        with open(args.file, "rb") as fh:
            dev.install_client_certificate(args.name, os.path.basename(args.file), fh.read(),
                                           password=args.password or "")
        print(f"installed client cert {args.name}")
    elif args.action == "clear":
        if not args.yes:
            raise QuadernoError("clearing all installed certificates requires --yes")
        dev.clear_certificates()
        print("cleared certificates")


def cmd_open(dev: Device, args: Args) -> None:
    e = dev.open(args.target, page=args.page, regex=args.regex)
    where = f" at page {args.page}" if args.page is not None else ""
    print(f"opened {e.path}{where} on the device")


def mac_timezone_name() -> str:
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return time.strftime("%Z")


def cmd_time(dev: Device, args: Args) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    print(f"device clock set to {dev.set_time(now)}")
    tz = dev.timezone()
    print(f"device timezone {tz}, Mac timezone {mac_timezone_name()}")
    if tz is None:
        return
    try:
        dev_off = now.astimezone(ZoneInfo(tz)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return
    mac_off = now.astimezone().utcoffset()
    if dev_off is not None and mac_off is not None and dev_off != mac_off:
        hours = (mac_off - dev_off).total_seconds() / 3600
        print(f"note: the device shows {abs(hours):g} h {'behind' if hours > 0 else 'ahead of'} "
              f"the Mac's local time because its timezone differs; change it with "
              f"`quaderno config timezone <zone>` or in the QUADERNO's settings")


def cmd_sync(dev: Device, args: Args) -> None:
    dev.sync(args.directory, args.folder, delete=args.delete, dry_run=args.dry_run,
             force=args.force, on_event=show)


def cmd_pair(args: Args) -> None:
    def read_pin() -> str:
        prompt = "Enter the PIN shown on the QUADERNO: "
        if args.pin_file:
            # Lets another process (or person) supply the PIN by writing this file.
            print(f"PIN is on the QUADERNO screen; waiting for it in {args.pin_file}", flush=True)
            deadline = time.time() + 150
            while time.time() < deadline:
                if os.path.exists(args.pin_file):
                    time.sleep(0.2)
                    with open(args.pin_file) as f:
                        pin = f.read()
                    os.remove(args.pin_file)
                    if pin.strip():
                        return pin
                time.sleep(0.3)
            raise PairingError("timed out waiting for the PIN")
        if sys.stdin.isatty():
            return input(prompt)
        print(prompt, end="", flush=True)
        return sys.stdin.readline()

    def on_event(ev: Event) -> None:
        print(ev.message, file=sys.stderr if ev.warning else sys.stdout, flush=True)

    def on_switch(tty: str, serial: str | None) -> None:
        if args.verbose:
            print(f"switching {tty} ({serial})", file=sys.stderr)
    addr = args.addr or os.environ.get("QUADERNO_ADDR") or usb.discover(on_switch=on_switch)
    pairing.pair(read_pin, addr=addr, force=args.force, cred_dir=args.config_dir,
                 on_event=on_event)


def complete(argv: list[str]) -> int:
    """`quaderno __complete [--dirs] [--config DIR] [--addr ADDR] [--] PREFIX`:
    device paths for shell Tab completion, one per line, folders ending in '/'.
    Lists the folder PREFIX is in, keeping the folder part exactly as typed. The
    shell passes on --config and --addr from the command line being completed.
    Never prints errors; prints nothing (at once) when the device isn't connected."""
    dirs_only, config_dir, addr, prefix = False, None, os.environ.get("QUADERNO_ADDR"), ""
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--dirs":
            dirs_only = True
        elif a in ("--config", "--addr") and rest:
            value = rest.pop(0)
            if a == "--config":
                config_dir = os.path.expanduser(value)  # as typed: the shell hasn't expanded ~
            else:
                addr = value
        elif a == "--":
            prefix = rest[0] if rest else ""
            break
        else:
            prefix = a
    try:
        if not addr and not usb.usb_devices():
            return 0
        creds = credentials.load_credentials(credentials.find_credentials_dir(config_dir))
        with Device(addr or usb.discover(creds.serial), creds.client_id, creds.key, timeout=5,
                    pinned_cert=creds.pinned_cert) as dev:
            dev.authenticate()
            typed_dir, sep, _ = prefix.rpartition("/")
            folder = dev.resolve(typed_dir)
            if not folder.is_folder:
                return 0
            for e in sorted(dev.entries(folder), key=lambda e: e.name.lower()):
                if dirs_only and not e.is_folder:
                    continue
                print(typed_dir + sep + e.name + ("/" if e.is_folder else ""))
    except (QuadernoError, OSError):
        pass
    return 0


# --- argument parsing -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="quaderno",
                                 description="Fujitsu QUADERNO over USB: files, sync, pairing.")
    ap.add_argument("--addr", help="device address, e.g. 'fe80::...%%en8' (default: auto)")
    ap.add_argument("--config", dest="config_dir", metavar="DIR",
                    help="credentials directory, for reading and for `pair` "
                         "(default: $QUADERNO_CONFIG or ~/.config/quaderno)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # Remote paths in ls/get/mv/rm/open accept glob patterns ('Fighter*'); -E makes
    # them regular expressions. Quote them so the shell leaves them alone.
    pat = argparse.ArgumentParser(add_help=False)
    pat.add_argument("-E", "--regex", action="store_true",
                     help="treat remote path components as regular expressions "
                          "(case-insensitive search) instead of glob patterns")

    def add(name: str, fn: Command | None, help: str,
            patterns: bool = False) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, parents=[pat] if patterns else [])
        p.set_defaults(fn=fn)
        return p

    p = add("ls", cmd_ls, "list a folder (default: Document)", patterns=True)
    p.add_argument("path", nargs="?", default=ROOT)
    p.add_argument("-l", "--long", action="store_true", help="size, date, pages")
    p.add_argument("-R", "--recursive", action="store_true")

    p = add("get", cmd_get, "download documents (or folders with -r)", patterns=True)
    p.add_argument("remote", nargs="+")
    p.add_argument("-o", "--output", help="file or directory (default: .)")
    p.add_argument("-r", "--recursive", action="store_true", help="download folders")

    p = add("put", cmd_put, "upload PDFs (or directories with -r)")
    p.add_argument("local", nargs="+")
    p.add_argument("-d", "--dest", help="remote folder or new path (default: Document)")
    p.add_argument("-r", "--recursive", action="store_true", help="upload directories")

    p = add("rm", cmd_rm, "delete documents or empty folders (-r: with contents)", patterns=True)
    p.add_argument("remote", nargs="+")
    p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before deleting pattern matches")

    p = add("mkdir", cmd_mkdir, "create folders")
    p.add_argument("remote", nargs="+")
    p.add_argument("-p", "--parents", action="store_true", help="create missing parents; ok if exists")

    p = add("mv", cmd_mv, "move or rename documents/folders (several -> into a folder)", patterns=True)
    p.add_argument("src", nargs="+")
    p.add_argument("dst")

    p = add("info", cmd_info, "firmware, storage, battery, owner")
    p.add_argument("--json", action="store_true")

    p = add("screenshot", cmd_screenshot, "save a PNG or JPEG of the current screen")
    p.add_argument("output", nargs="?", default=None)
    p.add_argument("--format", choices=["png", "jpeg"], default=None,
                   help="image format (default: inferred from filename, else png)")

    p = add("sync", cmd_sync, "mirror a device folder into a local directory (incremental)")
    p.add_argument("directory", metavar="local", help="local directory (created if missing)")
    p.add_argument("folder", metavar="remote", nargs="?", default=ROOT,
                   help="device folder (default: Document)")
    p.add_argument("--delete", action="store_true",
                   help="also delete local copies of documents deleted on the device")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--force", action="store_true",
                   help="allow a first sync into a non-empty directory (existing files are kept)")

    p = add("open", cmd_open, "open a document on the device's screen", patterns=True)
    p.add_argument("target", metavar="remote")
    p.add_argument("-p", "--page", type=int, help="page number")

    p = add("search", cmd_search, "find documents by name, text, or annotation mark")
    p.add_argument("keyword")
    p.add_argument("-t", "--target", choices=["meta", "body", "mark"], default="meta",
                   help="names/titles/authors (meta, default), document text (body), "
                        "or annotation-mark name (mark)")
    p.add_argument("-f", "--folder", dest="origin_folder", default=None,
                   help="limit search to a device folder")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--notes", action="store_true", help="only notebooks")
    g.add_argument("--docs", action="store_true", help="only documents (exclude notebooks)")
    p.add_argument("--sort", choices=list(_SORT_FIELDS),
                   help="order by name, title, author, date, read, size or created")
    p.add_argument("--desc", action="store_true", help="descending order (with --sort)")
    p.add_argument("--mark", choices=["all", "hand", "highlight"], default="all",
                   help="annotation kind for -t mark (default: all)")
    p.add_argument("-l", "--long", action="store_true", help="size, date, pages")

    p = add("cp", cmd_cp, "copy documents on the device (into a folder, or to a new name)",
            patterns=True)
    p.add_argument("src", nargs="+")
    p.add_argument("dst")

    p = add("replace", cmd_replace, "replace an existing document's PDF content in place")
    p.add_argument("file", metavar="local")
    p.add_argument("remote_doc", metavar="remote")
    p.add_argument("--revision", default=None, help="expected file_revision guard")

    p = add("annotations", cmd_annotations, "download raw annotation bytes for one page")
    p.add_argument("remote_doc", metavar="remote")
    p.add_argument("page", type=int)
    p.add_argument("-o", "--output", default=None)

    add("viewing", cmd_viewing, "show which document is open on the device")
    add("marks", cmd_marks, "list preset annotation marks")
    add("ping", cmd_ping, "verify authenticated connection to the device")

    p = add("pastlog", cmd_pastlog, "download encrypted diagnostic pastlog archive")
    p.add_argument("output", nargs="?", default=None)

    p = add("config", cmd_config, "show device settings, or change one")
    p.add_argument("--all", dest="all_configs", action="store_true",
                   help="print full /system/configs3 JSON")
    p.add_argument("key", nargs="?", help="setting name; omit to list all settings")
    p.add_argument("value", nargs="?",
                   help="new value (settable: " + ", ".join(SETTABLE_CONFIGS) + ")")

    p = add("templates", cmd_templates, "manage note templates")
    tsub = p.add_subparsers(dest="action", required=True)
    tsub.add_parser("ls", help="list note templates")
    tp = tsub.add_parser("add", help="upload a PDF note template")
    tp.add_argument("file", metavar="local")
    tp.add_argument("--name", default=None)
    tp = tsub.add_parser("rename", help="rename a note template")
    tp.add_argument("template_id")
    tp.add_argument("name")
    tp = tsub.add_parser("rm", help="delete a note template")
    tp.add_argument("template_id")

    p = add("wifi", cmd_wifi, "inspect or configure Wi-Fi")
    wsub = p.add_subparsers(dest="action", required=True)
    wsub.add_parser("status", help="current Wi-Fi connection")
    wsub.add_parser("on", help="enable Wi-Fi")
    wsub.add_parser("off", help="disable Wi-Fi")
    wsub.add_parser("scan", help="scan nearby access points")
    wsub.add_parser("ls", help="list saved access points")
    wp = wsub.add_parser("add", help="register a Wi-Fi access point")
    wp.add_argument("ssid")
    wp.add_argument("--security", choices=["nonsec", "psk", "eap"], default="psk")
    wp.add_argument("--password", default=None)
    wp = wsub.add_parser("rm", help="delete a saved Wi-Fi access point")
    wp.add_argument("ssid")
    wp.add_argument("security", choices=["nonsec", "psk", "eap"])

    p = add("power", cmd_power, "sleep, wake, reboot, or shut down the device")
    p.add_argument("action", choices=["sleep", "wake", "reboot", "shutdown"])

    p = add("firmware", cmd_firmware, "precheck, upload, or trigger a firmware update")
    fsub = p.add_subparsers(dest="action", required=True)
    fsub.add_parser("precheck", help="check battery and uploaded image readiness")
    fp = fsub.add_parser("upload", help="upload FwUpdater.pkg")
    fp.add_argument("file", metavar="pkg")
    fp = fsub.add_parser("update", help="reboot into recovery and apply uploaded update")
    fp.add_argument("-y", "--yes", action="store_true", help="confirm firmware update")

    p = add("pen-buttons", cmd_pen_buttons, "show or assign pen button functions")
    pbsub = p.add_subparsers(dest="action", required=True)
    pbsub.add_parser("ls", help="show current pen button assignments and choices")
    pbp = pbsub.add_parser("set", help="assign a pen button function")
    pbp.add_argument("button")
    pbp.add_argument("function")

    p = add("certs", cmd_certs, "manage CA and client certificates")
    csub = p.add_subparsers(dest="action", required=True)
    csub.add_parser("ls", help="list installed certificates")
    cp = csub.add_parser("add-ca", help="install a CA certificate")
    cp.add_argument("name")
    cp.add_argument("file")
    cp = csub.add_parser("add-client", help="install a client PKCS#12 certificate")
    cp.add_argument("name")
    cp.add_argument("file")
    cp.add_argument("--password", default=None)
    cp = csub.add_parser("clear", help="remove all installed certificates")
    cp.add_argument("-y", "--yes", action="store_true", help="confirm certificate wipe")

    add("time", cmd_time, "set the device clock from the Mac")
    add("addr", None, "print the device address (switches USB mode if needed)")

    p = add("pair", None, "pair with the device using the PIN on its screen")
    p.add_argument("--force", action="store_true",
                   help="replace an existing pairing")
    p.add_argument("--pin-file", help="read the PIN from this file once it appears (instead of a prompt)")
    return ap


EXIT_ERROR, EXIT_CONNECT, EXIT_AUTH = 1, 3, 4  # 2 is argparse's usage error


def exit_code(err: QuadernoError) -> int:
    """3: the device can't be reached; 4: pairing/credentials/identity; 1: the rest."""
    if isinstance(err, ConnectError):
        return EXIT_CONNECT
    if isinstance(err, (AuthError, PairingError)):
        return EXIT_AUTH
    return EXIT_ERROR


def describe_os_error(err: OSError) -> str:
    """'PATH: reason' for a local file problem, as cp and friends print it."""
    if err.filename is not None and err.strerror:
        return f"{err.filename}: {err.strerror}"
    return str(err)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["__complete"]:
        return complete(argv[1:])
    args = build_parser().parse_args(argv, namespace=Args())
    logging.basicConfig(format="%(message)s", level=logging.WARNING, stream=sys.stderr)
    try:
        if args.cmd == "pair":
            cmd_pair(args)
            return 0
        if args.cmd == "addr":
            try:
                serial = credentials.load_credentials(
                    credentials.find_credentials_dir(args.config_dir)).serial
            except QuadernoError:
                serial = None
            print(args.addr or usb.discover(serial))
            return 0
        fn = args.fn
        if fn is None:  # every other command sets one
            raise QuadernoError(f"{args.cmd}: nothing to run")
        with open_device(args) as dev:
            return fn(dev, args) or 0
    except QuadernoError as err:
        print(f"quaderno: {err}", file=sys.stderr)
        return exit_code(err)
    except OSError as err:  # local files: a missing directory, no permission, disk full
        print(f"quaderno: {describe_os_error(err)}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
