"""Offline tests for pure-stdlib Wi-Fi mDNS discovery and unified USB/Wi-Fi transport."""
import select
import socket
import struct
from collections.abc import Sequence
from pathlib import Path

import pytest

import quaderno
from quaderno import cli, credentials, crypto, discovery, mdns, usb
from quaderno.mdns import (CLASS_IN, FLAG_RESPONSE, SERVICE, TYPE_A, TYPE_AAAA, TYPE_PTR,
                           TYPE_SRV, WifiDevice)

KEY = crypto.load_private_key(crypto.generate_keypair()[0])


# --- synthetic mDNS packet builders -------------------------------------------------

def _rr(name: bytes, rtype: int, rdata: bytes, ttl: int = 120) -> bytes:
    return name + struct.pack("!HHIH", rtype, CLASS_IN, ttl, len(rdata)) + rdata


def _response_packet(answers: Sequence[bytes], additional: Sequence[bytes] = ()) -> bytes:
    header = struct.pack("!HHHHHH", 0, FLAG_RESPONSE, 0, len(answers), 0, len(additional))
    return header + b"".join(answers) + b"".join(additional)


def _service_response(instance: str, host: str, ip: str, port: int = 8080, *,
                      service: str = SERVICE, extra_ipv6: str | None = None) -> bytes:
    """An mDNS response with PTR in answers and SRV + A (+ optional AAAA) in additional."""
    svc_wire = mdns.encode_name(service)
    inst_wire = mdns.encode_name(instance)
    host_wire = mdns.encode_name(host)
    ptr_rr = _rr(svc_wire, TYPE_PTR, inst_wire)
    srv_rr = _rr(inst_wire, TYPE_SRV, struct.pack("!HHH", 0, 0, port) + host_wire)
    a_rr = _rr(host_wire, TYPE_A, socket.inet_pton(socket.AF_INET, ip))
    add = [srv_rr, a_rr]
    if extra_ipv6:
        add.append(_rr(host_wire, TYPE_AAAA, socket.inet_pton(socket.AF_INET6, extra_ipv6)))
    return _response_packet([ptr_rr], add)


# --- DNS wire codec -----------------------------------------------------------------

def test_encode_and_build_query() -> None:
    pkt = mdns.build_query("_dp_fujitsu._tcp.local", TYPE_PTR, unicast=True)
    tid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", pkt[:12])
    assert (tid, flags, qd, an, ns, ar) == (0, 0, 1, 0, 0, 0)
    name, end = mdns.read_name(pkt, 12)
    assert name == "_dp_fujitsu._tcp.local"
    qtype, qclass = struct.unpack("!HH", pkt[end:end + 4])
    assert (qtype, qclass) == (TYPE_PTR, 0x8001)
    assert mdns.parse_records(pkt) == []  # queries are ignored


def test_read_name_follows_compression_pointers_and_rejects_loops() -> None:
    # Build a packet with: offset 12 = "local\0", offset 19 = "Android" + ptr->12
    buf = b"\x00" * 12 + b"\x05local\x00" + b"\x07Android\xc0\x0c"
    name, end = mdns.read_name(buf, 19)
    assert name == "Android.local" and end == len(buf)
    # Self-pointing loop
    loop = b"\x00" * 12 + b"\xc0\x0c"
    with pytest.raises(ValueError, match="loop"):
        mdns.read_name(loop, 12)


def test_parse_records_reads_answers_and_additional_sections() -> None:
    pkt = _service_response("Digital Paper FMVDP41._dp_fujitsu._tcp.local",
                            "Android.local", "192.168.1.42", 8080,
                            extra_ipv6="fe80::1234")
    recs = mdns.parse_records(pkt)
    by_type = {r.rtype: r for r in recs}
    assert by_type[TYPE_PTR].value == "Digital Paper FMVDP41._dp_fujitsu._tcp.local"
    assert by_type[TYPE_SRV].value == (8080, "Android.local")
    assert by_type[TYPE_A].value == "192.168.1.42"
    assert by_type[TYPE_AAAA].value == "fe80::1234"


def test_parse_records_tolerates_truncated_garbage() -> None:
    assert mdns.parse_records(b"") == []
    assert mdns.parse_records(b"\x00\x00\x80\x00\x00\x00\x00\x01\x00\x00\x00\x00\x05abc") == []


def test_rank_addrs_prefers_ipv4_and_drops_unscoped_link_local() -> None:
    assert mdns._rank_addrs(["fe80::1", "fd00::5", "192.168.1.42", "192.168.1.42"]) == [
        "192.168.1.42", "fd00::5"]


# --- browse() over fake UDP sockets -------------------------------------------------

class FakeUdpSocket:
    def __init__(self, inbox: list[bytes]) -> None:
        self.inbox = list(inbox)
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, dest: tuple[str, int]) -> int:
        self.sent.append((data, dest))
        return len(data)

    def recvfrom(self, _bufsize: int) -> tuple[bytes, tuple[str, int]]:
        if not self.inbox:
            raise BlockingIOError
        return self.inbox.pop(0), ("192.168.1.42", 5353)

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t


def test_browse_resolves_ptr_srv_a_and_stops_early_on_serial_match(
        monkeypatch: pytest.MonkeyPatch) -> None:
    inst = "Digital Paper FMVDP41._dp_fujitsu._tcp.local"
    # Simulate a responder that sends PTR first, then SRV+A after follow-up query
    ptr_only = _response_packet([_rr(mdns.encode_name(SERVICE), TYPE_PTR, mdns.encode_name(inst))])
    srv_and_a = _response_packet(
        [_rr(mdns.encode_name(inst), TYPE_SRV,
             struct.pack("!HHH", 0, 0, 8080) + mdns.encode_name("Android.local"))],
        [_rr(mdns.encode_name("Android.local"), TYPE_A, socket.inet_pton(socket.AF_INET, "192.168.1.50"))],
    )
    sock = FakeUdpSocket([ptr_only])
    clock = FakeClock()

    def fake_select(rlist: list[FakeUdpSocket], _w: object, _x: object,
                    wait: float) -> tuple[list[FakeUdpSocket], list[object], list[object]]:
        if sock.inbox:
            return [sock], [], []
        # Once browse() sends the follow-up SRV query, feed the SRV+A reply
        if len(sock.sent) >= 2 and not getattr(sock, "_fed_srv", False):
            setattr(sock, "_fed_srv", True)
            sock.inbox.append(srv_and_a)
            return [sock], [], []
        clock.t += wait
        return [], [], []

    monkeypatch.setattr(mdns, "_open_sockets", lambda: [(sock, True)])
    monkeypatch.setattr(select, "select", fake_select)
    monkeypatch.setattr(mdns, "time", clock)
    monkeypatch.setattr(mdns, "fetch_serial", lambda ip, port=8080: (True, "850000312345678"))

    devs = mdns.browse(timeout=5.0, want_serial="850000312345678")
    assert devs == [WifiDevice("192.168.1.50", 8080, "850000312345678", inst, "Android.local")]
    assert sock.closed
    assert clock.t == 1000.0  # exited immediately on serial match without waiting timeout


def test_mdns_discover_selection_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    d1 = WifiDevice("192.168.1.10", 8080, "111", "DP1._dp_fujitsu._tcp.local", "a.local")
    d2 = WifiDevice("192.168.1.20", 8080, "222", "DP2._dp_fujitsu._tcp.local", "b.local")
    monkeypatch.setattr(mdns, "probe", lambda addr, timeout=2.0: True)

    monkeypatch.setattr(mdns, "browse", lambda **kw: [d1, d2])
    assert mdns.discover("222") == "192.168.1.20"
    with pytest.raises(quaderno.DeviceNotFoundError, match="2 QUADERNOs on Wi-Fi"):
        mdns.discover(None)
    with pytest.raises(quaderno.DeviceNotFoundError, match="isn't the one these credentials"):
        mdns.discover("999")

    monkeypatch.setattr(mdns, "browse", lambda **kw: [])
    with pytest.raises(quaderno.DeviceNotFoundError, match="no QUADERNO found on Wi-Fi"):
        mdns.discover("111")

    monkeypatch.setattr(mdns, "browse", lambda **kw: [d1])
    monkeypatch.setattr(mdns, "probe", lambda addr, timeout=2.0: False)
    with pytest.raises(quaderno.DeviceUnreachableError, match="doesn't answer on port 8443"):
        mdns.discover("111")


# --- unified discovery (USB -> Wi-Fi fallback, cached IP, Windows) ------------------

def test_discover_defaults_to_usb_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usb, "is_supported", lambda: True)
    def usb_missing(*a: object, **kw: object) -> str:
        raise quaderno.DeviceNotFoundError("no QUADERNO on USB")
    monkeypatch.setattr(usb, "discover", usb_missing)
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: pytest.fail("Wi-Fi must be opt-in"))
    with pytest.raises(quaderno.DeviceNotFoundError, match="no QUADERNO on USB"):
        discovery.discover("111")


def test_discover_auto_prefers_usb_and_falls_back_to_wifi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usb, "is_supported", lambda: True)
    monkeypatch.setattr(usb, "discover", lambda *a, **kw: "fe80::1%en8")
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: pytest.fail("should not browse Wi-Fi"))
    assert discovery.discover("111", transport="auto") == "fe80::1%en8"

    # USB absent -> falls back to Wi-Fi mDNS and invokes cache callback
    saved: list[str] = []
    def usb_missing(*a: object, **kw: object) -> str:
        raise quaderno.DeviceNotFoundError("no QUADERNO on USB")
    monkeypatch.setattr(usb, "discover", usb_missing)
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: "192.168.1.42")
    assert discovery.discover("111", transport="auto", on_wifi_addr=saved.append) == "192.168.1.42"
    assert saved == ["192.168.1.42"]


def test_discover_does_not_mask_usb_serial_or_unreachable_errors(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usb, "is_supported", lambda: True)
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: pytest.fail("must not hide USB error"))

    for exc in (quaderno.SerialModeError("stuck"), quaderno.DeviceUnreachableError("link down")):
        def fail(*a: object, _e: Exception = exc, **kw: object) -> str:
            raise _e
        monkeypatch.setattr(usb, "discover", fail)
        with pytest.raises(type(exc)):
            discovery.discover("111", transport="auto")


def test_discover_uses_cached_wifi_ip_when_serial_matches_and_falls_back_when_stale(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usb, "probe", lambda addr, timeout=0.4: addr == "192.168.1.42")
    monkeypatch.setattr(mdns, "fetch_serial", lambda addr, timeout=1.0: (True, "111"))
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: pytest.fail("cache should have hit"))
    assert discovery.discover("111", transport="wifi", cached_addr="192.168.1.42") == "192.168.1.42"

    # Stale cache (another serial now has that IP) -> falls through to mDNS
    monkeypatch.setattr(mdns, "fetch_serial", lambda addr, timeout=1.0: (True, "222"))
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: "192.168.1.99")
    assert discovery.discover("111", transport="wifi", cached_addr="192.168.1.42") == "192.168.1.99"


def test_discover_skips_usb_on_unsupported_os_in_auto_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usb, "PLATFORM", "win32")
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: "192.168.1.77")
    assert discovery.discover("111", transport="auto") == "192.168.1.77"


def test_discover_combines_not_found_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usb, "is_supported", lambda: True)
    def no_usb(*a: object, **kw: object) -> str:
        raise quaderno.DeviceNotFoundError("no QUADERNO on USB")
    def no_wifi(*a: object, **kw: object) -> str:
        raise quaderno.DeviceNotFoundError("no QUADERNO found on Wi-Fi")
    monkeypatch.setattr(usb, "discover", no_usb)
    monkeypatch.setattr(mdns, "discover", no_wifi)
    with pytest.raises(quaderno.DeviceNotFoundError, match="USB or Wi-Fi"):
        discovery.discover("111", transport="auto")


def test_parse_transport_validation() -> None:
    assert discovery.parse_transport(None) == "usb"
    assert discovery.parse_transport("WIFI") == "wifi"
    assert discovery.parse_transport("usb") == "usb"
    assert discovery.parse_transport("auto") == "auto"
    with pytest.raises(quaderno.ConnectError, match="unknown transport"):
        discovery.parse_transport("bluetooth")


# --- credential cache, CLI --wifi/--usb, and completion fast-fail -------------------

def test_wifi_addr_cache_ignores_link_local(tmp_path: Path) -> None:
    credentials.save_wifi_addr(str(tmp_path), "fe80::1%en8")
    assert credentials.load_wifi_addr(str(tmp_path)) is None
    credentials.save_wifi_addr(str(tmp_path), "192.168.1.42")
    assert credentials.load_wifi_addr(str(tmp_path)) == "192.168.1.42"


def test_cli_addr_and_transport_flags(monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[object] = []
    def not_paired(explicit: str | None = None) -> str:
        raise quaderno.NotPairedError("np")
    monkeypatch.setattr(credentials, "find_credentials_dir", not_paired)
    def fake_discover(serial: str | None = None, **kw: object) -> str:
        seen.append(kw.get("transport"))
        return "192.168.1.55"
    monkeypatch.setattr(discovery, "discover", fake_discover)
    assert cli.main(["--wifi", "addr"]) == 0
    assert capsys.readouterr().out.strip() == "192.168.1.55"
    assert seen == ["wifi"]


def test_completion_never_runs_mdns_browse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    creds = credentials.Credentials(client_id="id", key=KEY, serial="111",
                                    pinned_cert=b"cert", source=str(tmp_path))
    monkeypatch.setattr(usb, "usb_devices", lambda: [])
    monkeypatch.setattr(credentials, "find_credentials_dir", lambda explicit=None: str(tmp_path))
    monkeypatch.setattr(credentials, "load_credentials", lambda cred_dir=None: creds)
    monkeypatch.setattr(mdns, "browse", lambda **kw: pytest.fail("Tab completion must not browse"))
    assert cli.complete(["--", "Bo"]) == 0
    assert cli.complete(["--wifi", "--", "Bo"]) == 0
    assert capsys.readouterr() == ("", "")
