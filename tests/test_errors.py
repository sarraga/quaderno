"""Error detection and the exception hierarchy, offline: USB discovery states (fake
USB bus and clock), HTTP errors, dropped connections, login rejection, malformed
answers (not JSON: error; unreadable date or size: warning), CLI exit codes."""
import http.client
import json
import logging
import plistlib
import subprocess
import time
import types

import pytest
from fakedevice import FakeDevice

import quaderno
from quaderno import cli, client, credentials, usb
from quaderno.entry import Entry
from quaderno.usb import PID_ETHERNET, PID_SERIAL, UsbDevice
from quaderno.util import parse_device_time


def warnings_from(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "quaderno"]


# --- USB discovery ---------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class FakeBus:
    """A device on a fake USB bus. It leaves serial mode after `switch_after`
    switch commands (None: never). In network mode it has interface `iface` with
    link `link`, and answers there if `answers`."""

    def __init__(self, mode, switch_after=None, answers=True, unplug_after=None, iface="en8",
                 link=True, serial="850000312345678"):
        self.mode, self.switch_after, self.answers = mode, switch_after, answers
        self.unplug_after, self.iface, self.link, self.serial = unplug_after, iface, link, serial
        self.polls, self.sent, self.probed = 0, [], []

    def usb_devices(self):
        self.polls += 1
        if self.mode is None or (self.unplug_after is not None and self.polls > self.unplug_after):
            return []
        if self.mode == "serial":
            return [UsbDevice(PID_SERIAL, self.serial, "/dev/cu.usbmodemFAKE")]
        return [UsbDevice(PID_ETHERNET, self.serial, None, self.iface, self.link)]

    def switch_to_ethernet(self, tty):
        self.sent.append(tty)
        if self.switch_after is not None and len(self.sent) >= self.switch_after:
            self.mode = "ethernet"

    def probe(self, addr, timeout=1.5):
        self.probed.append(addr)
        return self.mode == "ethernet" and self.answers and addr.endswith(f"%{self.iface}")


@pytest.fixture
def on_bus(monkeypatch):
    """on_bus(bus, **kw) runs usb.discover against a FakeBus with a fake clock."""
    def run(bus, **kw):
        monkeypatch.setattr(usb, "usb_devices", bus.usb_devices)
        monkeypatch.setattr(usb, "switch_to_ethernet", bus.switch_to_ethernet)
        monkeypatch.setattr(usb, "probe", bus.probe)
        monkeypatch.setattr(usb, "time", FakeClock())
        return usb.discover(**kw)
    return run


def test_discover_not_plugged_in(on_bus):
    with pytest.raises(quaderno.DeviceNotFoundError, match="plug it in") as e:
        on_bus(FakeBus(None))
    assert isinstance(e.value, quaderno.ConnectError)


def test_discover_already_in_network_mode(on_bus):
    bus = FakeBus("ethernet")
    assert on_bus(bus) == "fe80::c8d3:84ff:fec2:323b%en8"
    assert bus.probed == ["fe80::c8d3:84ff:fec2:323b%en8"]   # only its own interface


def test_discover_switches_on_the_resend(on_bus):
    bus, switches = FakeBus("serial", switch_after=2), []
    assert on_bus(bus, on_switch=lambda tty, s: switches.append(tty))
    assert len(bus.sent) == 2 and len(switches) == 2


def test_discover_stuck_in_serial_mode(on_bus):
    bus = FakeBus("serial", switch_after=None)
    with pytest.raises(quaderno.SerialModeError) as e:
        on_bus(bus, switch_timeout=15)
    assert len(bus.sent) == 5, bus.sent            # t = 0, 3, 6, 9, 12 s
    assert "stayed in USB serial mode" in str(e.value) and "re-plug" in str(e.value)


@pytest.mark.parametrize("bus,message", [
    (FakeBus("ethernet", iface=None), "lists no network interface"),
    (FakeBus("ethernet", link=False, answers=False), "link (en8) is down"),
    (FakeBus("ethernet", answers=False), "link is up, but it doesn't answer at fe80::c8d3"),
])
def test_discover_says_why_the_device_does_not_answer(on_bus, bus, message):
    with pytest.raises(quaderno.DeviceUnreachableError) as e:
        on_bus(bus)
    assert message in str(e.value), str(e.value)


def test_discover_does_not_trust_the_registry_link_status(on_bus):
    """Seen on the device: IOLinkStatus read 0 right after a plug-in while it answered."""
    assert on_bus(FakeBus("ethernet", link=False)) == "fe80::c8d3:84ff:fec2:323b%en8"


def test_discover_unplugged_while_waiting(on_bus):
    with pytest.raises(quaderno.DeviceNotFoundError, match="disconnected"):
        on_bus(FakeBus("ethernet", answers=False, unplug_after=1))


@pytest.mark.parametrize("serial,hint", [("85000A", None), (None, "not-a-serial")])
def test_discover_rejects_a_serial_that_is_not_numeric(on_bus, serial, hint):
    with pytest.raises(quaderno.DeviceNotFoundError, match="isn't numeric"):
        on_bus(FakeBus("ethernet", serial=serial), serial_hint=hint)


def test_discover_uses_the_paired_serial_for_a_device_reporting_none(on_bus):
    assert on_bus(FakeBus("ethernet", serial=None), serial_hint="850000312345678") == \
        "fe80::c8d3:84ff:fec2:323b%en8"


A, B = UsbDevice(PID_ETHERNET, "111", None, "en8", True), UsbDevice(PID_SERIAL, "222", "/dev/cu.x")


def test_choose_picks_the_paired_device_among_several():
    assert usb._choose([A, B], "222") == (B, "222")


@pytest.mark.parametrize("devs,hint,message", [
    ([A], "222", "isn't the one these credentials were paired with (222)"),
    ([A, B], None, "2 QUADERNOs on USB (serials 111, 222)"),
    ([A._replace(serial=None)], None, "reports no serial number"),
])
def test_choose_refuses_to_guess(devs, hint, message):
    with pytest.raises(quaderno.DeviceNotFoundError) as e:
        usb._choose(devs, hint)
    assert message in str(e.value), str(e.value)


# --- reading the I/O Registry ---------------------------------------------------------

def ioreg_device(pid, serial, **children):
    """An `ioreg -a -l` node for a QUADERNO, with the subtree layout seen on macOS 27."""
    if pid == PID_ETHERNET:
        kids = [{"IOObjectClass": "IOUserNetworkEthernet", "IORegistryEntryChildren": [
            {"IOLinkStatus": children["link"], "IORegistryEntryChildren": [
                {"BSD Name": children["iface"]}]}]}]
    else:
        kids = [{"IORegistryEntryChildren": [{"IOCalloutDevice": children["tty"]}]}]
    return {"idVendor": 0x04C5, "idProduct": pid, "USB Serial Number": serial,
            "IORegistryEntryChildren": kids}


@pytest.fixture
def ioreg(monkeypatch):
    """ioreg(nodes) makes usb_devices() read `nodes` as ioreg's output."""
    def use(nodes):
        out = types.SimpleNamespace(stdout=plistlib.dumps(nodes), returncode=0)
        monkeypatch.setattr(usb.subprocess, "run", lambda *a, **kw: out)
    return use


@pytest.mark.parametrize("status,link", [(3, True), (1, False), (0, None)])
def test_usb_devices_reads_interface_and_link(ioreg, status, link):
    ioreg([{"idVendor": 0x05AC, "idProduct": 1},             # someone else's device
           ioreg_device(PID_ETHERNET, "111", iface="en8", link=status),
           ioreg_device(PID_SERIAL, "222", tty="/dev/cu.usbmodem101")])
    assert usb.usb_devices() == [UsbDevice(PID_ETHERNET, "111", None, "en8", link),
                                 UsbDevice(PID_SERIAL, "222", "/dev/cu.usbmodem101")]


def test_usb_devices_with_nothing_attached(ioreg):
    ioreg([])
    assert usb.usb_devices() == []


def test_ioreg_failure_is_not_taken_for_no_device(monkeypatch):
    def fail(*a, **kw):
        raise subprocess.TimeoutExpired("ioreg", 15)
    monkeypatch.setattr(usb.subprocess, "run", fail)
    with pytest.raises(quaderno.ConnectError, match="couldn't read the list of USB devices"):
        usb.usb_devices()


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


@pytest.fixture
def c(monkeypatch):
    """A Client whose connections replay FakeConn.script."""
    monkeypatch.setattr(client, "PinnedHTTPSConnection", FakeConn)
    FakeConn.script, FakeConn.made = [], 0
    return client.Client("fe80::1%en9", "id", key=None)


def err_body(code, msg):
    return json.dumps({"error_code": code, "message": msg}).encode()


def test_http_404_and_400(c):
    FakeConn.script = [FakeResponse(404, err_body("40401", "not found"))]
    with pytest.raises(quaderno.NotFoundError) as e:
        c.request("GET", "/resolve/entry/x")
    assert e.value.status == 404 and e.value.code == "40401"
    FakeConn.script = [FakeResponse(400, err_body("40006", "fileName(unsupported)"))]
    with pytest.raises(quaderno.RequestError) as e:
        c.request("POST", "/documents", {})
    assert e.value.status == 400 and e.value.code == "40006" and "fileName" in str(e.value)


def test_refused_or_timed_out_is_unreachable(c):
    FakeConn.script = [ConnectionRefusedError(61, "Connection refused")]
    with pytest.raises(quaderno.DeviceUnreachableError, match="Connection refused") as e:
        c.request("GET", "/ping")
    assert isinstance(e.value, quaderno.ConnectError)
    FakeConn.script = [TimeoutError("timed out")]
    with pytest.raises(quaderno.DeviceUnreachableError):
        c.request("GET", "/ping")


def test_stale_kept_alive_connection_reconnects_once(c):
    FakeConn.script = [FakeResponse(200, b'{"value": 1}')]
    assert c.request("GET", "/a") == {"value": 1} and FakeConn.made == 1
    FakeConn.script = [http.client.RemoteDisconnected("closed"), FakeResponse(200, b'{"value": 2}')]
    assert c.request("GET", "/b") == {"value": 2} and FakeConn.made == 2


def test_fresh_connection_that_drops_is_not_retried(c):
    FakeConn.script = [http.client.RemoteDisconnected("closed"), FakeResponse(200, b"{}")]
    with pytest.raises(quaderno.DeviceUnreachableError):
        c.request("GET", "/c")
    assert FakeConn.made == 1


def test_login_rejected(c):
    FakeConn.script = [FakeResponse(404, err_body("40401", "client not found"))]
    with pytest.raises(quaderno.LoginError, match="pair --force") as e:
        c.authenticate()
    assert isinstance(e.value, quaderno.AuthError)


def test_answer_that_is_not_json(c):
    FakeConn.script = [FakeResponse(200, b"<html>oops</html>")]
    with pytest.raises(quaderno.ProtocolError, match="isn't JSON") as e:
        c.request("GET", "/folders/x/entries")
    assert "/folders/x/entries" in str(e.value)


# --- malformed fields: absent is None; unreadable warns, or errors where it matters --

DOC = {"entry_id": "d1", "entry_name": "x.pdf", "entry_path": "Document/x.pdf",
       "entry_type": "document", "file_size": "1234", "total_page": "7",
       "modified_date": "2024-04-08T11:31:43Z", "created_date": "2024-04-01T09:00:00Z"}


def test_parse_device_time():
    assert parse_device_time(None) is None
    assert parse_device_time("2024-04-08T11:31:43Z") == 1712575903
    assert parse_device_time("2024-04-08T13:31:43+02:00") == 1712575903


@pytest.mark.parametrize("bad", ["", "yesterday", "2024-04-08T11:31:43", 1712575903])
def test_parse_device_time_rejects(bad):
    with pytest.raises(ValueError):
        parse_device_time(bad)


def test_entry_fields_well_formed_and_absent():
    e = Entry.from_api(DOC)
    assert (e.size, e.pages, e.mtime) == (1234, 7, 1712575903)
    bare = Entry.from_api({"entry_id": "f1", "entry_path": "Document/F", "entry_type": "folder"})
    assert (bare.size, bare.pages, bare.mtime) == (None, None, None)


def test_malformed_date_does_not_stop_a_listing(caplog):
    e = Entry.from_api({**DOC, "modified_date": "08/04/2024", "created_date": ""})
    assert e.modified == "08/04/2024" and e.created == "" and not warnings_from(caplog)
    with pytest.raises(quaderno.ProtocolError) as err:
        e.mtime
    assert "modified_date" in str(err.value) and "Document/x.pdf" in str(err.value)


@pytest.mark.parametrize("key,val,attr", [("file_size", "12kB", "size"), ("total_page", [7], "pages")])
def test_malformed_size_or_pages_reads_as_missing_with_a_warning(key, val, attr, caplog):
    e = Entry.from_api({**DOC, key: val})
    assert getattr(e, attr) is None
    msgs = warnings_from(caplog)
    assert len(msgs) == 1 and key in msgs[0] and "Document/x.pdf" in msgs[0], msgs


@pytest.fixture
def bad_date_device():
    dev = FakeDevice()
    dev.e[dev.put_doc("Document/Books/x.pdf", b"%PDF-1.7 body")]["modified_date"] = "08/04/2024"
    return dev


def test_get_with_a_malformed_date_keeps_the_file(bad_date_device, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cli, "connect", lambda **kw: bad_date_device)
    assert cli.main(["get", "Books/x.pdf", "-o", str(tmp_path)]) == 0
    saved = tmp_path / "x.pdf"
    assert saved.read_bytes() == b"%PDF-1.7 body"
    assert abs(saved.stat().st_mtime - time.time()) < 60       # not a device time
    msgs = warnings_from(caplog)
    assert len(msgs) == 1 and "saved" in msgs[0] and "08/04/2024" in msgs[0], msgs


def test_sync_with_a_malformed_date_completes_and_warns(bad_date_device, tmp_path, caplog):
    bad_date_device.sync(str(tmp_path))                        # no event callback
    assert (tmp_path / "Books" / "x.pdf").exists()
    msgs = warnings_from(caplog)
    assert len(msgs) == 1 and "modified_date" in msgs[0], msgs


# --- credentials and CLI exit codes ------------------------------------------------

def test_no_credentials_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(quaderno.NotPairedError):
        credentials.find_credentials_dir()


@pytest.mark.parametrize("exc,want", [
    (quaderno.SerialModeError("stuck"), 3), (quaderno.DeviceNotFoundError("none"), 3),
    (quaderno.DeviceUnreachableError("silent"), 3), (quaderno.NotPairedError("np"), 4),
    (quaderno.CertificateError("cert"), 4), (quaderno.WrongPinError("pin"), 4),
    (quaderno.ExistsError("exists"), 1), (quaderno.NotFoundError("nf"), 1),
    (quaderno.ProtocolError("bad json"), 1)])
def test_cli_exit_codes(exc, want, monkeypatch, capsys):
    def fake_connect(**kw):
        raise exc
    monkeypatch.setattr(cli, "connect", fake_connect)
    assert cli.main(["ls"]) == want
    assert capsys.readouterr().err.startswith("quaderno: ")


# --- TLS without a pinned certificate ------------------------------------------------

@pytest.mark.parametrize("pinned", [None, b"0\x82der"])
def test_connect_warns_when_tls_is_not_checked(pinned, monkeypatch, caplog):
    creds = credentials.Credentials(client_id="id", key=None, serial=None, pinned_cert=pinned,
                                    source="/some/config")
    monkeypatch.setattr(credentials, "find_credentials_dir", lambda explicit=None: creds.source)
    monkeypatch.setattr(credentials, "load_credentials", lambda cred_dir=None: creds)
    monkeypatch.setattr(quaderno.Device, "authenticate", lambda self: None)
    dev = quaderno.connect(addr="fe80::1%en9")
    assert dev.pinned_cert == pinned
    msgs = warnings_from(caplog)
    if pinned is None:
        assert len(msgs) == 1 and "not checked" in msgs[0] and "/some/config" in msgs[0], msgs
    else:
        assert not msgs
