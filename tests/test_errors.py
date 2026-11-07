#!/usr/bin/env python3
"""Error detection and the exception hierarchy, offline: USB discovery states (fake
USB bus and clock), HTTP errors, dropped connections, login rejection, malformed
answers (not JSON: error; unreadable date or size: warning), CLI exit codes."""
import contextlib
import http.client
import io
import json
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quaderno  # noqa: E402
from quaderno import cli, client, credentials, usb  # noqa: E402
from quaderno.entry import Entry  # noqa: E402
from quaderno.usb import PID_ETHERNET, PID_SERIAL, UsbDevice  # noqa: E402
from quaderno.util import parse_device_time  # noqa: E402
from fakedevice import FakeDevice  # noqa: E402


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


# --- USB discovery ---------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class FakeBus:
    """A device that leaves serial mode after `switch_after` switch commands
    (None: never), then answers on the network unless `answers` is False."""

    def __init__(self, mode, switch_after=None, answers=True, unplug_after=None):
        self.mode, self.switch_after, self.answers = mode, switch_after, answers
        self.unplug_after, self.polls, self.sent = unplug_after, 0, []

    def usb_devices(self):
        self.polls += 1
        if self.mode is None or (self.unplug_after is not None and self.polls > self.unplug_after):
            return []
        if self.mode == "serial":
            return [UsbDevice(PID_SERIAL, "850000312345678", "/dev/cu.usbmodemFAKE")]
        return [UsbDevice(PID_ETHERNET, "850000312345678", None)]

    def switch_to_ethernet(self, tty):
        self.sent.append(tty)
        if self.switch_after is not None and len(self.sent) >= self.switch_after:
            self.mode = "ethernet"

    def first_reachable(self, cands, timeout=1.5):
        return cands[0] if self.mode == "ethernet" and self.answers and self.usb_devices() else None


def with_bus(bus, fn):
    clock = FakeClock()
    saved = (usb.usb_devices, usb.switch_to_ethernet, usb.first_reachable, usb.time)
    usb.usb_devices, usb.switch_to_ethernet, usb.first_reachable = \
        bus.usb_devices, bus.switch_to_ethernet, bus.first_reachable
    usb.time = clock
    try:
        return fn(), clock
    finally:
        usb.usb_devices, usb.switch_to_ethernet, usb.first_reachable, usb.time = saved


print("discover: not plugged in")
e = raises(quaderno.DeviceNotFoundError, lambda: with_bus(FakeBus(None), usb.discover))
assert isinstance(e, quaderno.ConnectError) and "plug it in" in str(e)

print("discover: already in network mode")
(addr, clock), = [with_bus(FakeBus("ethernet"), usb.discover)]
assert addr.startswith("fe80::c8d3:84ff:fec2:323b%")

print("discover: serial mode, ignores the first command, switches on the resend")
bus = FakeBus("serial", switch_after=2)
switches = []
(addr, clock), = [with_bus(bus, lambda: usb.discover(on_switch=lambda tty, s: switches.append(tty)))]
assert addr and len(bus.sent) == 2 and len(switches) == 2

print("discover: stuck in serial mode -> SerialModeError, after resending")
bus = FakeBus("serial", switch_after=None)
e = raises(quaderno.SerialModeError, lambda: with_bus(bus, lambda: usb.discover(switch_timeout=15)))
assert len(bus.sent) == 5, bus.sent            # t = 0, 3, 6, 9, 12 s
assert "stayed in USB serial mode" in str(e) and "re-plug" in str(e)

print("discover: network mode but not answering -> DeviceUnreachableError")
e = raises(quaderno.DeviceUnreachableError, lambda: with_bus(FakeBus("ethernet", answers=False), usb.discover))
assert "doesn't answer" in str(e)

print("discover: unplugged while waiting -> DeviceNotFoundError")
raises(quaderno.DeviceNotFoundError,
       lambda: with_bus(FakeBus("ethernet", answers=False, unplug_after=1), usb.discover))


# --- HTTP client ------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, body=b"", will_close=False):
        self.status, self._body, self.will_close = status, body, will_close
        self.headers = http.client.HTTPMessage()

    def read(self, n=-1):
        data, self._body = (self._body, b"") if n < 0 else (self._body[:n], self._body[n:])
        return data


class FakeConn:
    """Scripted connection: each entry in `script` is a FakeResponse or an exception,
    consumed one per request across all connections created."""
    script, made = [], 0

    def __init__(self, *a, **kw):
        FakeConn.made += 1

    def request(self, *a, **kw):
        nxt = FakeConn.script[0]
        if isinstance(nxt, BaseException):
            FakeConn.script.pop(0)
            raise nxt

    def getresponse(self):
        return FakeConn.script.pop(0)

    def close(self):
        pass


client.PinnedHTTPSConnection = FakeConn


def make():
    FakeConn.made = 0
    return client.Client("fe80::1%en9", "id", key=None)


def err_body(code, msg):
    return json.dumps({"error_code": code, "message": msg}).encode()


print("HTTP 404 -> NotFoundError with the device's code; 400 -> RequestError")
c = make()
FakeConn.script = [FakeResponse(404, err_body("40401", "not found"))]
e = raises(quaderno.NotFoundError, lambda: c.request("GET", "/resolve/entry/x"))
assert e.status == 404 and e.code == "40401"
FakeConn.script = [FakeResponse(400, err_body("40006", "fileName(unsupported)"))]
e = raises(quaderno.RequestError, lambda: c.request("POST", "/documents", {}))
assert e.status == 400 and e.code == "40006" and "fileName" in str(e)

print("connection refused / timeout -> DeviceUnreachableError (no raw OSError)")
c = make()
FakeConn.script = [ConnectionRefusedError(61, "Connection refused")]
e = raises(quaderno.DeviceUnreachableError, lambda: c.request("GET", "/ping"))
assert isinstance(e, quaderno.ConnectError) and "Connection refused" in str(e)
FakeConn.script = [TimeoutError("timed out")]
raises(quaderno.DeviceUnreachableError, lambda: c.request("GET", "/ping"))

print("stale kept-alive connection: reconnects once and succeeds")
c = make()
FakeConn.script = [FakeResponse(200, b'{"value": 1}')]
assert c.request("GET", "/a") == {"value": 1} and FakeConn.made == 1
FakeConn.script = [http.client.RemoteDisconnected("closed"), FakeResponse(200, b'{"value": 2}')]
assert c.request("GET", "/b") == {"value": 2} and FakeConn.made == 2
print("...but a fresh connection that drops is not retried")
c = make()
FakeConn.script = [http.client.RemoteDisconnected("closed"), FakeResponse(200, b"{}")]
raises(quaderno.DeviceUnreachableError, lambda: c.request("GET", "/c"))
assert FakeConn.made == 1

print("login rejected -> LoginError (an AuthError)")
c = make()
FakeConn.script = [FakeResponse(404, err_body("40401", "client not found"))]
e = raises(quaderno.LoginError, c.authenticate)
assert isinstance(e, quaderno.AuthError) and "pair --force" in str(e)

print("200 with a body that isn't JSON -> ProtocolError (no raw ValueError)")
c = make()
FakeConn.script = [FakeResponse(200, b"<html>oops</html>")]
e = raises(quaderno.ProtocolError, lambda: c.request("GET", "/folders/x/entries"))
assert "isn't JSON" in str(e) and "/folders/x/entries" in str(e)


# --- malformed fields: absent is None; unreadable warns, or errors where it matters --

class Captured(logging.Handler):
    """Collects the package's log warnings (and keeps them off the test output)."""

    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


cap = Captured()
logging.getLogger("quaderno").addHandler(cap)

print("parse_device_time: absent -> None; malformed or offset-less -> ValueError")
assert parse_device_time(None) is None
assert parse_device_time("2024-04-08T11:31:43Z") == 1712575903
assert parse_device_time("2024-04-08T13:31:43+02:00") == 1712575903
for bad in ("", "yesterday", "2024-04-08T11:31:43", 1712575903):
    raises(ValueError, lambda: parse_device_time(bad))

doc = {"entry_id": "d1", "entry_name": "x.pdf", "entry_path": "Document/x.pdf",
       "entry_type": "document", "file_size": "1234", "total_page": "7",
       "modified_date": "2024-04-08T11:31:43Z", "created_date": "2024-04-01T09:00:00Z"}
print("Entry.from_api: well-formed and absent fields")
e = Entry.from_api(doc)
assert (e.size, e.pages, e.mtime) == (1234, 7, 1712575903)
bare = Entry.from_api({"entry_id": "f1", "entry_path": "Document/F", "entry_type": "folder"})
assert (bare.size, bare.pages, bare.mtime) == (None, None, None)

print("Entry.from_api: a malformed date doesn't stop a listing; mtime raises ProtocolError")
e = Entry.from_api({**doc, "modified_date": "08/04/2024", "created_date": ""})
assert e.modified == "08/04/2024" and e.created == "" and not cap.msgs
err = raises(quaderno.ProtocolError, lambda: e.mtime)
assert "modified_date" in str(err) and "Document/x.pdf" in str(err), str(err)

print("Entry.from_api: malformed size or page count -> None, with a warning")
for key, val in [("file_size", "12kB"), ("total_page", [7])]:
    cap.msgs.clear()
    e = Entry.from_api({**doc, key: val})
    assert getattr(e, "size" if key == "file_size" else "pages") is None
    assert len(cap.msgs) == 1 and key in cap.msgs[0] and "Document/x.pdf" in cap.msgs[0], cap.msgs

print("get with a malformed date: file saved (exit 0), date not applied, warning")
dev = FakeDevice()
dev.e[dev.put_doc("Document/Books/x.pdf", b"%PDF-1.7 body")]["modified_date"] = "08/04/2024"
out = tempfile.mkdtemp()
cli.connect = lambda **kw: dev
cap.msgs.clear()
with contextlib.redirect_stdout(io.StringIO()):
    assert cli.main(["get", "Books/x.pdf", "-o", out]) == 0
saved = os.path.join(out, "x.pdf")
with open(saved, "rb") as fh:
    assert fh.read() == b"%PDF-1.7 body"
assert abs(os.stat(saved).st_mtime - time.time()) < 60       # not a device time
assert len(cap.msgs) == 1 and "saved" in cap.msgs[0] and "08/04/2024" in cap.msgs[0], cap.msgs

print("sync (no event callback) with a malformed date: completes, warning still shown")
cap.msgs.clear()
mirror = tempfile.mkdtemp()
dev.sync(mirror)
assert os.path.exists(os.path.join(mirror, "Books", "x.pdf"))
assert len(cap.msgs) == 1 and "modified_date" in cap.msgs[0], cap.msgs


# --- credentials and CLI exit codes ------------------------------------------------

print("no credentials anywhere -> NotPairedError")
empty = tempfile.mkdtemp()
credentials.CONFIG_DIR, credentials.APP_DIR = os.path.join(empty, "cfg"), os.path.join(empty, "app")
raises(quaderno.NotPairedError, credentials.find_credentials_dir)


def run_cli(exc, argv=("ls",)):
    def fake_connect(**kw):
        raise exc
    cli.connect = fake_connect
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, err.getvalue()


print("CLI exit codes: 3 connect, 4 auth/pairing, 1 other")
for exc, want in [(quaderno.SerialModeError("stuck"), 3), (quaderno.DeviceNotFoundError("none"), 3),
                  (quaderno.DeviceUnreachableError("silent"), 3), (quaderno.NotPairedError("np"), 4),
                  (quaderno.CertificateError("cert"), 4), (quaderno.WrongPinError("pin"), 4),
                  (quaderno.ExistsError("exists"), 1), (quaderno.NotFoundError("nf"), 1),
                  (quaderno.ProtocolError("bad json"), 1)]:
    code, err = run_cli(exc)
    assert code == want and err.startswith("quaderno: "), (exc, code, err)
print("ALL ERROR TESTS PASSED")
