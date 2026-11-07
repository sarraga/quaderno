"""The `quaderno` command: argument parsing and printing around the Device API."""

import argparse
import datetime
import json
import logging
import os
import sys
import time

from . import credentials, pairing, usb
from .device import Device, connect
from .errors import AuthError, ConnectError, PairingError, QuadernoError
from .util import ROOT, human


def show(event):
    """Print an API event: warnings to stderr, the rest to stdout."""
    print(event.message, file=sys.stderr if event.warning else sys.stdout)


def open_device(args):
    def on_switch(tty, serial):
        if args.verbose:
            print(f"switching {tty} ({serial}) to USB Ethernet", file=sys.stderr)
    dev = connect(addr=args.addr, workspace=args.workspace, on_switch=on_switch)
    if args.verbose:
        tls = "pinned to devicecert.bin" if dev.pinned_cert else "unverified (no pinned cert)"
        print(f"credentials: {dev.credentials.source}\ndevice: {dev.addr}\ntls: {tls}",
              file=sys.stderr)
    return dev


# --- commands (each takes a Device and the parsed args) ---------------------------

def cmd_ls(dev, args):
    rows, by_pattern = dev.ls(args.path, recursive=args.recursive, regex=args.regex)
    full_path = args.recursive or by_pattern
    for e in rows:
        name = (e.path if full_path else e.name) + ("/" if e.is_folder else "")
        if args.long:
            size = human(e.size) if e.size is not None else "-"
            date = (e.modified or e.created or "")[:16].replace("T", " ")
            pages = e.pages if e.pages is not None else ""
            print(f"{size:>8}  {date:16}  {str(pages):>4}  {name}")
        else:
            print(name)


def cmd_get(dev, args):
    dev.get(args.remote, args.output or ".", recursive=args.recursive, regex=args.regex,
            on_event=show)


def cmd_put(dev, args):
    """Exit status 1 only if a named local file does not exist."""
    res = dev.put(args.local, args.remote, recursive=args.recursive, on_event=show)
    return 1 if res.missing else None


def cmd_rm(dev, args):
    def confirm(plan):
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


def cmd_mkdir(dev, args):
    for path in args.remote:
        dev.mkdir(path, parents=args.parents, on_event=show)


def cmd_mv(dev, args):
    dev.move(args.src, args.dst, regex=args.regex, on_event=show)


def cmd_info(dev, args):
    info = dev.info()
    if args.json:
        print(json.dumps(info, indent=2))
        return

    def val(d, k="value"):
        return d.get(k, d.get("error", "?")) if isinstance(d, dict) else d
    st, b = info["storage"], info["battery"]
    print(f"address        {dev.addr}")
    print(f"api version    {val(info['api_version'])}")
    print(f"firmware       {val(info['firmware_version'])}")
    print(f"owner          {val(info['owner'])}")
    print(f"mac address    {val(info['mac_address'])}")
    if "capacity" in st:
        cap, free = int(st["capacity"]), int(st["available"])
        print(f"storage        {human(cap - free)} used / {human(cap)} ({human(free)} free)")
    else:
        print(f"storage        {val(st)}")
    if "level" in b:
        print(f"battery        {b['level']}% ({b.get('status', '?')}, {b.get('plugged', '?')})")
    else:
        print(f"battery        {val(b)}")


def cmd_screenshot(dev, args):
    dev.screenshot(args.output)
    print(f"saved {args.output}")


def cmd_open(dev, args):
    e = dev.open(args.remote, page=args.page, regex=args.regex)
    where = f" at page {args.page}" if args.page is not None else ""
    print(f"opened {e.path}{where} on the device")


def mac_timezone_name():
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return time.strftime("%Z")


def cmd_time(dev, args):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    print(f"device clock set to {dev.set_time(now)}")
    tz = dev.timezone()
    print(f"device timezone {tz}, Mac timezone {mac_timezone_name()}")
    try:
        from zoneinfo import ZoneInfo
        dev_off = now.astimezone(ZoneInfo(tz)).utcoffset()
    except Exception:
        return
    mac_off = now.astimezone().utcoffset()
    if dev_off != mac_off:
        hours = (mac_off - dev_off).total_seconds() / 3600
        print(f"note: the device shows {abs(hours):g} h {'behind' if hours > 0 else 'ahead of'} "
              f"the Mac's local time because its timezone differs; change it in the "
              f"QUADERNO's own settings")


def cmd_sync(dev, args):
    dev.sync(args.local, args.remote, delete=args.delete, dry_run=args.dry_run,
             force=args.force, on_event=show)


def cmd_pair(args):
    def read_pin():
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

    def on_event(ev):
        print(ev.message, file=sys.stderr if ev.warning else sys.stdout, flush=True)
    addr = args.addr or os.environ.get("QUADERNO_ADDR") or usb.discover(
        on_switch=lambda tty, s: args.verbose and print(f"switching {tty} ({s})", file=sys.stderr))
    pairing.pair(read_pin, addr=addr, force=args.force, on_event=on_event)


def complete(argv):
    """`quaderno __complete [--dirs] [--] PREFIX`: device paths for shell Tab
    completion, one per line, folders ending in '/'. Lists the folder PREFIX is in,
    keeping the folder part exactly as typed. Never prints errors; prints nothing
    (at once) when the device isn't connected."""
    dirs_only = "--dirs" in argv
    words = [a for a in argv if a not in ("--dirs", "--")]
    prefix = words[0] if words else ""
    try:
        addr = os.environ.get("QUADERNO_ADDR")
        if not addr and not usb.usb_devices():
            return 0
        creds = credentials.load_credentials()
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
    except (QuadernoError, OSError, ValueError, KeyError):
        pass
    return 0


# --- argument parsing -------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="quaderno",
                                 description="Fujitsu QUADERNO over USB: files, sync, pairing.")
    ap.add_argument("--addr", help="device address, e.g. 'fe80::...%%en8' (default: auto)")
    ap.add_argument("--workspace", help="credentials dir (default: ~/.config/quaderno, "
                                        "else the PC App's pairing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # Remote paths in ls/get/mv/rm/open accept glob patterns ('Fighter*'); -E makes
    # them regular expressions. Quote them so the shell leaves them alone.
    pat = argparse.ArgumentParser(add_help=False)
    pat.add_argument("-E", "--regex", action="store_true",
                     help="treat remote path components as regular expressions "
                          "(case-insensitive search) instead of glob patterns")

    def add(name, fn, help, patterns=False):
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
    p.add_argument("-d", "--dest", dest="remote", help="remote folder or new path (default: Document)")
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

    p = add("screenshot", cmd_screenshot, "save a PNG of the current screen")
    p.add_argument("output", nargs="?", default="quaderno-screen.png")

    p = add("sync", cmd_sync, "mirror a device folder into a local directory (incremental)")
    p.add_argument("local", help="local directory (created if missing)")
    p.add_argument("remote", nargs="?", default=ROOT, help="device folder (default: Document)")
    p.add_argument("--delete", action="store_true",
                   help="also delete local copies of documents deleted on the device")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--force", action="store_true",
                   help="allow a first sync into a non-empty directory (existing files are kept)")

    p = add("open", cmd_open, "open a document on the device's screen", patterns=True)
    p.add_argument("remote")
    p.add_argument("-p", "--page", type=int, help="page number")

    add("time", cmd_time, "set the device clock from the Mac")
    add("addr", None, "print the device address (switches USB mode if needed)")

    p = add("pair", None, "pair with the device using the PIN on its screen")
    p.add_argument("--force", action="store_true",
                   help="replace an existing pairing in ~/.config/quaderno")
    p.add_argument("--pin-file", help="read the PIN from this file once it appears (instead of a prompt)")
    return ap


EXIT_ERROR, EXIT_CONNECT, EXIT_AUTH = 1, 3, 4  # 2 is argparse's usage error


def exit_code(err):
    """3: the device can't be reached; 4: pairing/credentials/identity; 1: the rest."""
    if isinstance(err, ConnectError):
        return EXIT_CONNECT
    if isinstance(err, (AuthError, PairingError)):
        return EXIT_AUTH
    return EXIT_ERROR


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["__complete"]:
        return complete(argv[1:])
    args = build_parser().parse_args(argv)
    logging.basicConfig(format="%(message)s", level=logging.WARNING, stream=sys.stderr)
    try:
        if args.cmd == "pair":
            cmd_pair(args)
            return 0
        if args.cmd == "addr":
            try:
                serial = credentials.load_credentials(
                    credentials.find_credentials_dir(args.workspace)).serial
            except QuadernoError:
                serial = None
            print(args.addr or usb.discover(serial))
            return 0
        with open_device(args) as dev:
            return args.fn(dev, args) or 0
    except QuadernoError as err:
        print(f"quaderno: {err}", file=sys.stderr)
        return exit_code(err)
    except KeyboardInterrupt:
        return 130
