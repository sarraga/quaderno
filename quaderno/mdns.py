"""Finding the device on Wi-Fi via mDNS (DNS-SD, RFC 6762 / 6763), stdlib only.

The QUADERNO advertises the DNS-SD service `_dp_fujitsu._tcp.local.` on port
8080 while Wi-Fi is awake. This module browses that service over UDP multicast
(224.0.0.251:5353), resolves PTR -> SRV -> A/AAAA records, and checks each
candidate's `/register/information` endpoint to read its serial number so the
paired device can be identified unambiguously."""

import http.client
import ipaddress
import json
import select
import socket
import struct
import time
from collections.abc import Sequence
from typing import NamedTuple

from .errors import DeviceNotFoundError, DeviceUnreachableError
from .usb import probe
from .util import HTTPS_PORT, REG_PORT

SERVICE = "_dp_fujitsu._tcp.local"
MDNS_ADDR_V4 = "224.0.0.251"
MDNS_PORT = 5353

TYPE_A = 1
TYPE_PTR = 12
TYPE_AAAA = 28
TYPE_SRV = 33
CLASS_IN = 1
FLAG_RESPONSE = 0x8000
FLAG_QU = 0x8000  # RFC 6762 §5.4: request unicast response


class DnsRecord(NamedTuple):
    name: str
    rtype: int
    ttl: int
    # TYPE_PTR -> target name (str); TYPE_A/AAAA -> IP string (str);
    # TYPE_SRV -> (port, target) tuple; other -> None
    value: str | tuple[int, str] | None


class WifiDevice(NamedTuple):
    addr: str
    port: int
    serial: str | None
    instance: str
    host: str


# --- DNS wire encoding & decoding ---------------------------------------------------

def _norm_dns(name: str) -> str:
    return name.rstrip(".").casefold()


def encode_name(name: str) -> bytes:
    """Encode a dotted domain name into DNS wire-format labels."""
    parts = [p for p in name.rstrip(".").split(".") if p]
    out = bytearray()
    for part in parts:
        raw = part.encode("utf-8")
        if len(raw) > 63:
            raise ValueError(f"DNS label too long: {part!r}")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def build_query(name: str, qtype: int, *, unicast: bool = True) -> bytes:
    """Build a single-question mDNS query packet (ID 0 per RFC 6762)."""
    qclass = CLASS_IN | (FLAG_QU if unicast else 0)
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + encode_name(name) + struct.pack("!HH", qtype, qclass)


def read_name(packet: bytes, offset: int, _depth: int = 0) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name at `offset`.
    Returns `(dotted_name, offset_after_name_in_stream)`."""
    if _depth > 16:
        raise ValueError("DNS compression pointer loop")
    labels: list[str] = []
    pos = offset
    jumped = False
    end_pos = offset
    size = len(packet)
    while True:
        if pos >= size:
            raise ValueError("truncated DNS name")
        length = packet[pos]
        if length == 0:
            if not jumped:
                end_pos = pos + 1
            break
        if (length & 0xC0) == 0xC0:
            if pos + 1 >= size:
                raise ValueError("truncated DNS pointer")
            ptr = ((length & 0x3F) << 8) | packet[pos + 1]
            if ptr >= size:
                raise ValueError("out-of-range DNS pointer")
            if not jumped:
                end_pos = pos + 2
                jumped = True
            rest, _ = read_name(packet, ptr, _depth + 1)
            if rest:
                labels.append(rest)
            break
        if length & 0xC0:
            raise ValueError(f"unsupported DNS label flags: {length:#x}")
        pos += 1
        if pos + length > size:
            raise ValueError("truncated DNS label")
        labels.append(packet[pos:pos + length].decode("utf-8", errors="replace"))
        pos += length
    return ".".join(labels), end_pos


def parse_records(packet: bytes) -> list[DnsRecord]:
    """Parse all Answer, Authority, and Additional records from an mDNS response.
    Returns an empty list for queries or malformed packets."""
    if len(packet) < 12:
        return []
    _tid, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", packet[:12])
    if not (flags & FLAG_RESPONSE):
        return []
    offset = 12
    try:
        for _ in range(qdcount):
            _, offset = read_name(packet, offset)
            offset += 4
            if offset > len(packet):
                return []
        records: list[DnsRecord] = []
        for _ in range(ancount + nscount + arcount):
            name, offset = read_name(packet, offset)
            if offset + 10 > len(packet):
                break
            rtype, _rclass, ttl, rdlength = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            rdata_start = offset
            rdata_end = offset + rdlength
            if rdata_end > len(packet):
                break
            offset = rdata_end
            value: str | tuple[int, str] | None = None
            if rtype == TYPE_PTR:
                target, _ = read_name(packet, rdata_start)
                value = target
            elif rtype == TYPE_SRV and rdlength >= 6:
                _prio, _weight, port = struct.unpack("!HHH", packet[rdata_start:rdata_start + 6])
                target, _ = read_name(packet, rdata_start + 6)
                value = (port, target)
            elif rtype == TYPE_A and rdlength == 4:
                value = socket.inet_ntop(socket.AF_INET, packet[rdata_start:rdata_end])
            elif rtype == TYPE_AAAA and rdlength == 16:
                value = socket.inet_ntop(socket.AF_INET6, packet[rdata_start:rdata_end])
            records.append(DnsRecord(name, rtype, ttl, value))
        return records
    except (struct.error, ValueError, OSError):
        return []


# --- device info check --------------------------------------------------------------

def _usable_wifi_addr(addr: str) -> bool:
    """True for IPv4 or non-link-local IPv6 addresses reachable without a zone scope."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.version == 6 and ip.is_link_local)


def _rank_addrs(addrs: Sequence[str]) -> list[str]:
    """Prefer IPv4 over IPv6 on Wi-Fi and drop un-scoped IPv6 link-local addresses."""
    usable = [a for a in addrs if _usable_wifi_addr(a)]
    return sorted(dict.fromkeys(usable), key=lambda a: (1 if ":" in a else 0))


def fetch_serial(addr: str, port: int = REG_PORT, timeout: float = 1.5) -> tuple[bool, str | None]:
    """Ask `GET http://<addr>:<port>/register/information` for the device's serial.
    Returns `(is_quaderno, serial_number)`."""
    conn = http.client.HTTPConnection(addr.strip("[]"), port, timeout=timeout)
    try:
        conn.request("GET", "/register/information",
                     headers={"Host": f"digitalpaper.local:{port}", "Accept": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
    except OSError:
        return False, None
    finally:
        conn.close()
    if resp.status != 200 or not raw:
        return False, None
    try:
        data = json.loads(raw)
    except ValueError:
        return False, None
    if not isinstance(data, dict):
        return False, None
    serial = data.get("serial_number")
    return True, serial if isinstance(serial, str) and serial else None


# --- multicast browse ---------------------------------------------------------------

def _open_sockets() -> list[tuple[socket.socket, bool]]:
    """Open UDP sockets for mDNS: one multicast listener on :5353 (QM) and one
    ephemeral socket (QU) so we receive both multicast and unicast replies."""
    socks: list[tuple[socket.socket, bool]] = []
    # Multicast socket on :5353 (receives multicast replies from devices ignoring QU)
    try:
        m = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        m.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                m.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        m.bind(("", MDNS_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(MDNS_ADDR_V4), socket.inet_aton("0.0.0.0"))
        m.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        m.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        m.setblocking(False)
        socks.append((m, False))
    except OSError:
        pass
    # Ephemeral socket (works even when :5353 cannot be bound; requests unicast reply)
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        u.bind(("", 0))
        u.setblocking(False)
        socks.append((u, True))
    except OSError:
        pass
    return socks


def browse(services: Sequence[str] = (SERVICE,), *, timeout: float = 3.0,
           want_serial: str | None = None) -> list[WifiDevice]:
    """Browse mDNS for QUADERNO services and return verified WifiDevices.
    Exits early as soon as `want_serial` is matched (or shortly after the first
    device answers when `want_serial` is None)."""
    socks = _open_sockets()
    if not socks:
        return []
    wanted = {_norm_dns(s) for s in services}
    instances: dict[str, str] = {}            # norm(instance) -> original instance
    srvs: dict[str, tuple[str, int]] = {}     # norm(instance) -> (host, port)
    addrs: dict[str, list[str]] = {}          # norm(host) -> [ip, ...]
    queried_srv: set[str] = set()
    queried_host: set[str] = set()
    checked: set[tuple[str, int]] = set()
    found: list[WifiDevice] = []
    seen_serials: set[tuple[str, str | None]] = set()

    def send_query(name: str, qtype: int) -> None:
        for sock, unicast in socks:
            try:
                sock.sendto(build_query(name, qtype, unicast=unicast), (MDNS_ADDR_V4, MDNS_PORT))
            except OSError:
                pass

    def send_initial() -> None:
        for svc in services:
            send_query(svc, TYPE_PTR)

    def ingest(packet: bytes) -> None:
        for rec in parse_records(packet):
            if rec.ttl == 0 or rec.value is None:
                continue
            rname = _norm_dns(rec.name)
            if rec.rtype == TYPE_PTR and isinstance(rec.value, str) and rname in wanted:
                instances.setdefault(_norm_dns(rec.value), rec.value)
            elif rec.rtype == TYPE_SRV and isinstance(rec.value, tuple):
                port, target = rec.value
                srvs[rname] = (target, port)
            elif rec.rtype in (TYPE_A, TYPE_AAAA) and isinstance(rec.value, str):
                bucket = addrs.setdefault(rname, [])
                if rec.value not in bucket:
                    bucket.append(rec.value)

    def resolve_ready() -> bool:
        """Issue follow-up queries and verify newly resolved endpoints.
        Returns True if `want_serial` was matched and we can stop immediately."""
        for inst_key, inst_name in list(instances.items()):
            if inst_key not in srvs:
                if inst_key not in queried_srv:
                    queried_srv.add(inst_key)
                    send_query(inst_name, TYPE_SRV)
                continue
            host, port = srvs[inst_key]
            host_key = _norm_dns(host)
            candidates = _rank_addrs(addrs.get(host_key, ()))
            if not candidates:
                if host_key not in queried_host:
                    queried_host.add(host_key)
                    send_query(host, TYPE_A)
                    send_query(host, TYPE_AAAA)
                continue
            for ip in candidates:
                ep = (ip, port)
                if ep in checked:
                    continue
                checked.add(ep)
                ok, serial = fetch_serial(ip, port)
                if not ok:
                    continue
                key = (ip, serial)
                if key not in seen_serials:
                    seen_serials.add(key)
                    found.append(WifiDevice(ip, port, serial, inst_name, host))
                if want_serial is not None and serial == want_serial:
                    return True
        return False

    try:
        send_initial()
        start = time.time()
        deadline = start + timeout
        first_found_at: float | None = None
        resent = False
        raw_socks = [s for s, _ in socks]
        while True:
            now = time.time()
            if now >= deadline:
                break
            if first_found_at is not None and now - first_found_at >= 0.5:
                break
            if not resent and not found and now - start >= timeout / 2:
                send_initial()
                resent = True
            wait = min(0.2, deadline - now)
            try:
                ready, _, _ = select.select(raw_socks, [], [], wait)
            except (OSError, ValueError):
                break
            for sock in ready:
                while True:
                    try:
                        packet, _src = sock.recvfrom(9000)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    ingest(packet)
            if resolve_ready():
                break
            if found and first_found_at is None:
                first_found_at = time.time()
    finally:
        for sock, _ in socks:
            sock.close()
    return found


def _choose(devs: Sequence[WifiDevice], serial_hint: str | None) -> WifiDevice:
    """Pick the paired device (if `serial_hint` is given) or the sole device."""
    listed = ", ".join(d.serial or d.addr for d in devs)
    if serial_hint:
        same = [d for d in devs if d.serial == serial_hint]
        if same:
            return same[0]
        raise DeviceNotFoundError(
            f"the QUADERNO on Wi-Fi (serial {listed}) isn't the one these credentials were "
            f"paired with ({serial_hint}): connect that one, or pair with this one using "
            "--config DIR")
    if len(devs) > 1:
        raise DeviceNotFoundError(
            f"{len(devs)} QUADERNOs on Wi-Fi (serials {listed}): pass --addr to pick one")
    return devs[0]


def discover(serial_hint: str | None = None, *, timeout: float = 3.0,
             services: Sequence[str] = (SERVICE,)) -> str:
    """Find a QUADERNO on Wi-Fi via mDNS and return its IP address.

    Raises DeviceNotFoundError if none (or not the paired one) answers mDNS, or
    DeviceUnreachableError if the device answered mDNS but HTTPS port 8443 refuses."""
    devs = browse(services=services, timeout=timeout, want_serial=serial_hint)
    if not devs:
        raise DeviceNotFoundError(
            "no QUADERNO found on Wi-Fi via mDNS: make sure the tablet is awake, on the "
            "same Wi-Fi network, and advertising mDNS (or pass --addr)")
    dev = _choose(devs, serial_hint)
    if not probe(dev.addr, timeout=2.0):
        raise DeviceUnreachableError(
            f"found QUADERNO ({dev.serial or dev.host}) on Wi-Fi at {dev.addr}, but it "
            f"doesn't answer on port {HTTPS_PORT}: wake or unlock it")
    return dev.addr
