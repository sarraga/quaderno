"""Adversarial & security hardening regression tests."""
import base64
import io
import json
import os
import select
import socket
import struct
from pathlib import Path
from typing import Any

import pytest

import quaderno
from quaderno import (cli, client, credentials, crypto, device, discovery, entry, mdns, pairing,
                      sync, util)
from quaderno.errors import CertificateError, PairingError, ProtocolError, QuadernoError, RequestError
from quaderno.mdns import CLASS_IN, FLAG_RESPONSE, SERVICE, TYPE_A, TYPE_PTR, TYPE_SRV, WifiDevice

KEY = crypto.load_private_key(crypto.generate_keypair()[0])


# --- synthetic mDNS helpers ---------------------------------------------------------

def _rr(name: bytes, rtype: int, rdata: bytes, ttl: int = 120) -> bytes:
    return name + struct.pack("!HHIH", rtype, CLASS_IN, ttl, len(rdata)) + rdata


def _resp(answers: list[bytes], additional: list[bytes] = []) -> bytes:
    return struct.pack("!HHHHHH", 0, FLAG_RESPONSE, 0, len(answers), 0, len(additional)) + \
        b"".join(answers) + b"".join(additional)


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t


class _FakeSock:
    def __init__(self, packets: list[bytes], *, infinite: bool = False) -> None:
        self.packets = list(packets)
        self.infinite = infinite
        self.recv_count = 0

    def sendto(self, data: bytes, _dest: tuple[str, int]) -> int:
        return len(data)

    def recvfrom(self, _n: int) -> tuple[bytes, tuple[str, int]]:
        self.recv_count += 1
        if self.infinite:
            return b"\x00" * 12, ("192.168.1.99", 5353)
        if not self.packets:
            raise BlockingIOError
        return self.packets.pop(0), ("192.168.1.99", 5353)

    def close(self) -> None:
        pass


# --- 1. mDNS SSRF, DoS, DNS bomb & TLS pin defense ----------------------------------

@pytest.mark.parametrize("bad_ip", [
    "127.0.0.1", "127.1.2.3", "::1", "0.0.0.0", "::",
    "224.0.0.251", "ff02::fb", "169.254.169.254", "fe80::1",
])
def test_unusable_wifi_addrs_rejected(bad_ip: str) -> None:
    assert not mdns._usable_wifi_addr(bad_ip)
    assert not credentials.is_wifi_addr(bad_ip)


@pytest.mark.parametrize("good_ip", ["192.168.0.99", "10.0.0.5", "fd00::1", "2a02:8084::1"])
def test_usable_wifi_addrs_accepted(good_ip: str) -> None:
    assert mdns._usable_wifi_addr(good_ip)
    assert credentials.is_wifi_addr(good_ip)


def test_read_name_rejects_amplification_and_indirect_loops() -> None:
    # Indirect loop: 12 -> 14 -> 12
    loop = b"\x00" * 12 + b"\xc0\x0e\xc0\x0c"
    with pytest.raises(ValueError, match="loop"):
        mdns.read_name(loop, 12)

    # Amplification > 255 bytes via chained 60-byte labels
    label60 = bytes([60]) + b"a" * 60
    p = bytearray(b"\x00" * 12)
    off = 12
    for i in range(5):
        p.extend(label60)
        if i == 0:
            p.append(0)
        else:
            p.extend(struct.pack("!H", 0xC000 | off))
        off = len(p) - len(label60) - (1 if i == 0 else 2)
    with pytest.raises(ValueError, match="too long"):
        mdns.read_name(bytes(p), off)


def test_browse_rejects_non_reg_port_and_caps_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = mdns.encode_name(SERVICE)
    answers: list[bytes] = []
    additional: list[bytes] = []
    # Rogue instance advertising port 22 (SSH) on localhost + 30 fake port-8080 IPs
    bad_inst = mdns.encode_name("Rogue._dp_fujitsu._tcp.local")
    bad_host = mdns.encode_name("rogue.local")
    answers.append(_rr(svc, TYPE_PTR, bad_inst))
    additional.append(_rr(bad_inst, TYPE_SRV, struct.pack("!HHH", 0, 0, 22) + bad_host))
    additional.append(_rr(bad_host, TYPE_A, socket.inet_pton(socket.AF_INET, "192.168.0.5")))

    for i in range(1, 30):
        inst = mdns.encode_name(f"Fake{i}._dp_fujitsu._tcp.local")
        host = mdns.encode_name(f"fake{i}.local")
        answers.append(_rr(svc, TYPE_PTR, inst))
        additional.append(_rr(inst, TYPE_SRV, struct.pack("!HHH", 0, 0, 8080) + host))
        additional.append(_rr(host, TYPE_A, socket.inet_pton(socket.AF_INET, f"192.168.1.{i}")))

    sock = _FakeSock([_resp(answers, additional)])
    clock = _FakeClock()
    probed: list[tuple[str, int]] = []

    def fake_select(rlist: list[_FakeSock], _w: object, _x: object,
                    wait: float) -> tuple[list[_FakeSock], list[object], list[object]]:
        if sock.packets:
            return [sock], [], []
        clock.t += wait
        return [], [], []

    def fake_fetch(ip: str, port: int = 8080) -> tuple[bool, str | None]:
        probed.append((ip, port))
        return False, None

    monkeypatch.setattr(mdns, "_browse_bonjour", lambda *a, **kw: [])
    monkeypatch.setattr(mdns, "_open_sockets", lambda: [(sock, True)])
    monkeypatch.setattr(select, "select", fake_select)
    monkeypatch.setattr(mdns, "time", clock)
    monkeypatch.setattr(mdns, "fetch_serial", fake_fetch)

    assert mdns.browse(timeout=2.0) == []
    assert all(port == 8080 for _, port in probed)
    assert ("192.168.0.5", 22) not in probed
    assert len(probed) <= mdns.MAX_PROBES


def test_browse_survives_udp_flood_without_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSock([], infinite=True)
    clock = _FakeClock()

    def fake_select(rlist: list[_FakeSock], _w: object, _x: object,
                    wait: float) -> tuple[list[_FakeSock], list[object], list[object]]:
        clock.t += 0.5
        return [sock], [], []

    monkeypatch.setattr(mdns, "_browse_bonjour", lambda *a, **kw: [])
    monkeypatch.setattr(mdns, "_open_sockets", lambda: [(sock, True)])
    monkeypatch.setattr(select, "select", fake_select)
    monkeypatch.setattr(mdns, "time", clock)

    assert mdns.browse(timeout=1.0) == []
    assert sock.recv_count <= mdns.MAX_PACKETS_PER_TICK * 3


def test_browse_skips_rogue_responder_failing_tls_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = mdns.encode_name(SERVICE)
    rogue_inst = mdns.encode_name("Rogue._dp_fujitsu._tcp.local")
    rogue_host = mdns.encode_name("rogue.local")
    real_inst = mdns.encode_name("Real._dp_fujitsu._tcp.local")
    real_host = mdns.encode_name("real.local")
    pkt = _resp(
        [_rr(svc, TYPE_PTR, rogue_inst), _rr(svc, TYPE_PTR, real_inst)],
        [
            _rr(rogue_inst, TYPE_SRV, struct.pack("!HHH", 0, 0, 8080) + rogue_host),
            _rr(rogue_host, TYPE_A, socket.inet_pton(socket.AF_INET, "192.168.0.66")),
            _rr(real_inst, TYPE_SRV, struct.pack("!HHH", 0, 0, 8080) + real_host),
            _rr(real_host, TYPE_A, socket.inet_pton(socket.AF_INET, "192.168.0.99")),
        ],
    )
    sock = _FakeSock([pkt])
    clock = _FakeClock()

    def fake_select(rlist: list[_FakeSock], _w: object, _x: object,
                    wait: float) -> tuple[list[_FakeSock], list[object], list[object]]:
        if sock.packets:
            return [sock], [], []
        clock.t += wait
        return [], [], []

    monkeypatch.setattr(mdns, "_browse_bonjour", lambda *a, **kw: [])
    monkeypatch.setattr(mdns, "_open_sockets", lambda: [(sock, True)])
    monkeypatch.setattr(select, "select", fake_select)
    monkeypatch.setattr(mdns, "time", clock)
    # Both claim the victim's serial, but only .99 passes TLS pinning
    monkeypatch.setattr(mdns, "fetch_serial", lambda ip, port=8080: (True, "850000302014492"))
    monkeypatch.setattr(mdns, "probe_tls_pin",
                        lambda ip, pin, timeout=1.5: ip == "192.168.0.99" and pin == b"REALCERT")

    devs = mdns.browse(timeout=5.0, want_serial="850000302014492", pinned_cert=b"REALCERT")
    assert len(devs) == 1 and devs[0].addr == "192.168.0.99"


def test_connect_clears_poisoned_wifi_cache_on_cert_mismatch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials.save_wifi_addr(str(tmp_path), "192.168.0.66")
    creds = credentials.Credentials(client_id="id", key=KEY, serial="111",
                                    pinned_cert=b"REALCERT", source=str(tmp_path))
    monkeypatch.setattr(credentials, "find_credentials_dir", lambda explicit=None: str(tmp_path))
    monkeypatch.setattr(credentials, "load_credentials", lambda cred_dir=None: creds)
    monkeypatch.setattr(discovery, "discover", lambda *a, **kw: "192.168.0.66")

    def bad_cert(self: client.Client) -> None:
        raise CertificateError("mismatch")

    monkeypatch.setattr(device.Device, "authenticate", bad_cert)
    with pytest.raises(CertificateError):
        device.connect(transport="wifi")
    assert credentials.load_wifi_addr(str(tmp_path)) is None


def test_try_cached_wifi_uses_tls_pin_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mdns, "probe_tls_pin", lambda addr, pin, timeout=1.0: False)
    monkeypatch.setattr(mdns, "discover", lambda *a, **kw: "192.168.0.99")
    assert discovery.discover("111", transport="wifi", cached_addr="192.168.0.66",
                              pinned_cert=b"REALCERT") == "192.168.0.99"


# --- 2. Sync traversal, symlink & collision defense ---------------------------------

def test_sync_rejects_traversal_and_reserved_paths_in_state(tmp_path: Path) -> None:
    for bad in ("../outside.pdf", "/etc/passwd", "sub/../escape.pdf", "a\\b.pdf",
                sync.SYNC_STATE, ""):
        p = tmp_path / sync.SYNC_STATE
        p.write_text(json.dumps({
            "remote": "Document",
            "entries": {"1": {"path": bad, "rev": "r1", "size": 5, "mtime": 1}},
        }))
        with pytest.raises(QuadernoError, match="not a sync state file"):
            sync._load_state(str(p))


def test_safe_local_name_rejects_backslashes() -> None:
    with pytest.raises(QuadernoError, match="unsafe"):
        util.safe_local_name("..\\evil.pdf")


def test_sync_skips_sync_state_collision_and_ignores_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("do not delete")
    sync_dir = tmp_path / "mirror"
    sync_dir.mkdir()
    link = sync_dir / "note.pdf"
    link.symlink_to(outside)
    st = os.stat(outside)
    # Seed state pointing to symlink with matching size/mtime
    (sync_dir / sync.SYNC_STATE).write_text(json.dumps({
        "remote": "Document",
        "entries": {"gone-id": {"path": "note.pdf", "rev": "1",
                                "size": st.st_size, "mtime": int(st.st_mtime)}},
    }))

    class _FakeDev:
        def resolve(self, _p: str) -> entry.Entry:
            return entry.Entry(id="root", name="Document", path="Document", type="folder")

        def walk(self, _f: entry.Entry) -> list[entry.Entry]:
            return [entry.Entry(id="evil", name=sync.SYNC_STATE,
                                path=f"Document/{sync.SYNC_STATE}", type="document", revision="1")]

    stats = sync.sync_folder(_FakeDev(), str(sync_dir), delete=True)  # type: ignore[arg-type]
    assert stats["conflict"] == 1
    assert stats["deleted"] == 0 and stats["kept"] == 1
    assert outside.exists() and link.is_symlink()


# --- 3. Protocol bounds & malformed JSON defense ------------------------------------

def test_entry_from_api_raises_protocol_error_on_malformed_fields() -> None:
    with pytest.raises(ProtocolError):
        entry.Entry.from_api({"entry_name": "no-id"})
    with pytest.raises(ProtocolError):
        entry.Entry.from_api({"entry_id": "1", "entry_path": 12345})


def test_paged_and_walk_stop_on_loops() -> None:
    class _LoopDev(device.Device):
        def __init__(self) -> None:
            super().__init__("127.0.0.1", "id", KEY)
            self.calls = 0

        def request_json(self, method: str, path: str, body: Any = None,
                         headers: Any = None, ok: Any = (200, 204)) -> dict[str, Any]:
            self.calls += 1
            if self.calls > 5:
                raise AssertionError("infinite loop not prevented")
            if path.startswith("/folders/root/entries"):
                return {"count": 999999, "entry_list": [
                    {"entry_id": "f1", "entry_name": "Loop", "entry_path": "Document/Loop",
                     "entry_type": "folder"},
                ]}
            if path.startswith("/folders/f1/entries"):
                return {"count": 1, "entry_list": [
                    {"entry_id": "root", "entry_name": "Document", "entry_path": "Document",
                     "entry_type": "folder"},
                ]}
            return {"entry_id": "root", "entry_name": "Document", "entry_path": "Document",
                    "entry_type": "folder"}

    dev = _LoopDev()
    walked = list(dev.walk("Document"))
    assert [e.id for e in walked] == ["f1", "root"]


def test_chunked_upload_rejects_stalled_or_negative_offset(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 100)

    class _BadUploadClient(client.Client):
        def __init__(self, returned_offset: int) -> None:
            super().__init__("127.0.0.1", "id", KEY)
            self.returned_offset = returned_offset

        def request_json(self, *a: Any, **kw: Any) -> dict[str, Any]:
            return {"current_bytes": self.returned_offset, "completed": "no"}

    with pytest.raises(RequestError, match="stalled"):
        _BadUploadClient(0)._send_chunked_multipart("/documents/1/file", str(pdf))
    with pytest.raises(ProtocolError, match="out-of-range"):
        _BadUploadClient(-5)._send_chunked_multipart("/documents/1/file", str(pdf))


# --- 4. Crypto, pairing & CLI hardening ---------------------------------------------

def test_cert_to_der_rejects_non_x509_starting_with_0x30() -> None:
    assert crypto.cert_to_der(b"\x30not-a-valid-certificate") is None
    assert crypto.cert_to_der(b"0123456789") is None


def test_unwrap_rejects_truncated_data() -> None:
    with pytest.raises(PairingError, match="too short"):
        crypto.unwrap(b"\x00" * 16, b"a" * 32, b"k" * 16)


def test_pairing_rejects_oversized_dh_and_warns_on_wifi(monkeypatch: pytest.MonkeyPatch) -> None:
    huge_yb = base64.b64encode(b"\x01" * 1000).decode()

    def fake_reg(_addr: str, _method: str, path: str, _body: Any = None) -> tuple[int, Any]:
        if path == "/register/pin":
            return 200, {"a": base64.b64encode(b"1" * 16).decode(),
                         "b": base64.b64encode(b"2" * 6).decode(),
                         "c": huge_yb}
        return 200, {}

    monkeypatch.setattr(pairing, "reg_request", fake_reg)
    with pytest.raises(PairingError, match="oversized DH"):
        pairing.register("192.168.0.99", "cid", "pub", lambda: "12345678")


def test_sanitize_terminal_escapes_ansi_controls() -> None:
    dirty = "Evil\x1b]52;c;YQ==\x07\r\x9b31mSSID\tOK\n"
    clean = util.sanitize_terminal(dirty)
    assert "\x1b" not in clean and "\x07" not in clean and "\r" not in clean and "\x9b" not in clean
    assert "\tOK\n" in clean


def test_cli_read_secret_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cr3t-pass\n"))
    args = cli.Args(password=None, password_stdin=True)
    assert cli._read_secret(args, "Password: ", required=True) == "s3cr3t-pass"
