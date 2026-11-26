"""Finding the device on Wi-Fi via mDNS (DNS-SD, RFC 6762 / 6763), stdlib only.

The QUADERNO advertises the DNS-SD service `_dp_fujitsu._tcp.local.` on port
8080 while Wi-Fi is awake. This module browses that service over UDP multicast
(224.0.0.251:5353), resolves PTR -> SRV -> A/AAAA records, and checks each
candidate's `/register/information` endpoint to read its serial number so the
paired device can be identified unambiguously."""

import http.client
import ipaddress
import json
import re
import select
import socket
import ssl
import struct
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from . import bonjour
from .errors import DeviceNotFoundError, DeviceUnreachableError
from .usb import probe
from .util import HTTPS_PORT, REG_PORT

SERVICE = "_dp_fujitsu._tcp.local"
MDNS_ADDR_V4 = "224.0.0.251"
MDNS_PORT = 5353
RESEND_INTERVAL = 0.5
PLATFORM = sys.platform

# Defensive limits against hostile or flooded LAN mDNS traffic
MAX_DNS_NAME_LEN = 255
MAX_RECORDS_PER_PACKET = 128
MAX_INFO_BYTES = 65536
MAX_PROBES = 16
MAX_PACKETS_PER_TICK = 64

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
    """Normalize a DNS name for case-insensitive lookup without trailing dot."""
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


def read_name(packet: bytes, offset: int, _depth: int = 0,
              _visited: set[int] | None = None) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name at `offset`.
    Returns `(dotted_name, offset_after_name_in_stream)`."""
    if _depth > 16:
        raise ValueError("DNS compression pointer loop")
    visited = set() if _visited is None else _visited
    labels: list[str] = []
    total_len = 0
    pos = offset
    jumped = False
    end_pos = offset
    size = len(packet)
    while True:
        if pos >= size:
            raise ValueError("truncated DNS name")
        if pos in visited:
            raise ValueError("DNS compression pointer loop")
        visited.add(pos)
        length = packet[pos]
        if length == 0:
            if not jumped:
                end_pos = pos + 1
            break
        if (length & 0xC0) == 0xC0:
            if pos + 1 >= size:
                raise ValueError("truncated DNS pointer")
            ptr = ((length & 0x3F) << 8) | packet[pos + 1]
            if ptr >= size or ptr in visited:
                raise ValueError("DNS compression pointer loop" if ptr in visited
                                 else "out-of-range DNS pointer")
            if not jumped:
                end_pos = pos + 2
                jumped = True
            rest, _ = read_name(packet, ptr, _depth + 1, visited)
            if rest:
                total_len += len(rest) + (1 if labels else 0)
                if total_len > MAX_DNS_NAME_LEN:
                    raise ValueError("DNS name too long")
                labels.append(rest)
            break
        if length & 0xC0:
            raise ValueError(f"unsupported DNS label flags: {length:#x}")
        pos += 1
        if pos + length > size:
            raise ValueError("truncated DNS label")
        total_len += length + (1 if labels else 0)
        if total_len > MAX_DNS_NAME_LEN:
            raise ValueError("DNS name too long")
        labels.append(packet[pos:pos + length].decode("utf-8", errors="replace"))
        pos += length
    return ".".join(labels), end_pos


def parse_records(packet: bytes) -> list[DnsRecord]:
    """Parse Answer, Authority, and Additional records from an mDNS response.
    Returns an empty list for queries or malformed packets."""
    if len(packet) < 12:
        return []
    _tid, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", packet[:12])
    if not (flags & FLAG_RESPONSE):
        return []
    offset = 12
    try:
        for _ in range(min(qdcount, MAX_RECORDS_PER_PACKET)):
            _, offset = read_name(packet, offset)
            offset += 4
            if offset > len(packet):
                return []
        if qdcount > MAX_RECORDS_PER_PACKET:
            return []
        records: list[DnsRecord] = []
        total_rrs = min(ancount + nscount + arcount, MAX_RECORDS_PER_PACKET)
        for _ in range(total_rrs):
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
    """True for a routable/LAN IPv4 or non-link-local IPv6 address.
    Rejects loopback, multicast, unspecified, reserved, and link-local ranges so a
    rogue LAN mDNS responder cannot steer pre-auth HTTP probes to localhost/metadata."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_link_local:
        return False
    return True


def _rank_addrs(addrs: Sequence[str]) -> list[str]:
    """Prefer IPv4 over IPv6 on Wi-Fi and drop unusable/link-local addresses."""
    usable = [a for a in addrs if _usable_wifi_addr(a)]
    return sorted(dict.fromkeys(usable), key=lambda a: (1 if ":" in a else 0))


def probe_tls_pin(addr: str, pinned_cert: bytes, timeout: float = 2.0) -> bool:
    """True if `addr`:8443 completes TLS and presents exactly `pinned_cert`."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    try:
        with socket.create_connection((addr.strip("[]"), HTTPS_PORT), timeout=timeout) as raw_sock:
            with ctx.wrap_socket(raw_sock) as tls_sock:
                return tls_sock.getpeercert(binary_form=True) == pinned_cert
    except OSError:
        return False


def fetch_serial(addr: str, port: int = REG_PORT, timeout: float = 1.5) -> tuple[bool, str | None]:
    """Ask `GET http://<addr>:<port>/register/information` for the device's serial.
    Returns `(is_quaderno, serial_number)`."""
    conn = http.client.HTTPConnection(addr.strip("[]"), port, timeout=timeout)
    try:
        conn.request("GET", "/register/information",
                     headers={"Host": f"digitalpaper.local:{port}", "Accept": "application/json"})
        resp = conn.getresponse()
        raw = resp.read(MAX_INFO_BYTES + 1)
    except OSError:
        return False, None
    finally:
        conn.close()
    if resp.status != 200 or not raw or len(raw) > MAX_INFO_BYTES:
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

def _parse_ifconfig_ipv4(text: str) -> list[str]:
    """Extract IPv4 addresses of UP, RUNNING, MULTICAST, non-loopback,
    non-point-to-point interfaces from `ifconfig -u` output (macOS/BSD)."""
    addrs: list[str] = []
    usable = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            m = re.search(r"flags=[0-9a-fA-F]+<([^>]*)>", line)
            flags = set(m.group(1).split(",")) if m else set()
            usable = ({"UP", "RUNNING", "MULTICAST"} <= flags
                      and not ({"LOOPBACK", "POINTOPOINT"} & flags))
        elif usable:
            m = re.match(r"\s+inet\s+(\d+\.\d+\.\d+\.\d+)\b", line)
            if m and not m.group(1).startswith("127."):
                addrs.append(m.group(1))
    return addrs


def _parse_ip_addr_ipv4(text: str) -> list[str]:
    """Extract IPv4 addresses of UP, MULTICAST, non-loopback, non-point-to-point
    interfaces from `ip -4 addr show up` output (Linux)."""
    addrs: list[str] = []
    usable = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            m = re.search(r"<([^>]*)>", line)
            flags = set(m.group(1).split(",")) if m else set()
            usable = ({"UP", "MULTICAST"} <= flags
                      and not ({"LOOPBACK", "POINTOPOINT"} & flags))
        elif usable:
            m = re.match(r"\s+inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
            if m and not m.group(1).startswith("127."):
                addrs.append(m.group(1))
    return addrs


def _multicast_ipv4_addrs() -> list[str]:
    """Active LAN IPv4 interface addresses for mDNS multicast.

    Pinning IP_MULTICAST_IF / IP_ADD_MEMBERSHIP to non-point-to-point LAN
    interfaces keeps VPN tunnels (e.g. macOS `utun*`) or secondary adapters from
    capturing 224.0.0.251 traffic. Falls back to `['0.0.0.0']` if none can be
    enumerated."""
    addrs: list[str] = []
    try:
        if PLATFORM == "darwin":
            out = subprocess.run(["ifconfig", "-u"], capture_output=True, text=True,
                                 timeout=5, check=True).stdout
            addrs = _parse_ifconfig_ipv4(out)
        elif PLATFORM.startswith("linux"):
            out = subprocess.run(["ip", "-4", "addr", "show", "up"], capture_output=True,
                                 text=True, timeout=5, check=True).stdout
            addrs = _parse_ip_addr_ipv4(out)
    except (OSError, subprocess.SubprocessError):
        pass
    deduped = list(dict.fromkeys(addrs))
    if not deduped:
        return ["0.0.0.0"]
    # Prefer routable LAN addresses ahead of 169.254/16 link-local addresses
    return sorted(deduped, key=lambda a: (1 if a.startswith("169.254.") else 0))


def _open_sockets() -> list[tuple[socket.socket, bool]]:
    """Open UDP sockets for mDNS across active LAN IPv4 interfaces: multicast
    listener(s) on :5353 (QM) and ephemeral socket(s) (QU) so we receive both
    multicast and unicast replies even alongside VPNs or mDNSResponder."""
    socks: list[tuple[socket.socket, bool]] = []
    if_addrs = _multicast_ipv4_addrs()
    first_mcast: socket.socket | None = None

    for if_ip in if_addrs:
        mreq = struct.pack("4s4s", socket.inet_aton(MDNS_ADDR_V4), socket.inet_aton(if_ip))
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
            m.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            m.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            if if_ip != "0.0.0.0":
                m.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(if_ip))
            m.setblocking(False)
            socks.append((m, False))
            if first_mcast is None:
                first_mcast = m
        except OSError:
            if first_mcast is not None:
                try:
                    first_mcast.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError:
                    pass
        # Ephemeral socket (works even when :5353 cannot be bound; requests unicast reply)
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            if if_ip != "0.0.0.0":
                u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(if_ip))
            u.bind(("", 0))
            u.setblocking(False)
            socks.append((u, True))
        except OSError:
            pass
    return socks


# --- macOS Bonjour fast-path & UDP mDNS browse --------------------------------------

# Re-exported for unit tests and internal callers
_split_service_domain = bonjour.split_service_domain
_sockaddr_to_ip = bonjour.sockaddr_to_ip


@dataclass
class _BrowseState:
    """Mutable state collected during one mDNS/Bonjour browse session."""
    instances: dict[str, str] = field(default_factory=dict)          # norm(inst) -> original
    srvs: dict[str, tuple[str, int]] = field(default_factory=dict)   # norm(inst) -> (host, port)
    addrs: dict[str, list[str]] = field(default_factory=dict)        # norm(host) -> [ip, ...]
    queried_srv: set[str] = field(default_factory=set)
    queried_host: set[str] = field(default_factory=set)
    checked: set[tuple[str, int]] = field(default_factory=set)
    found: list[WifiDevice] = field(default_factory=list)
    found_by_key: dict[str, int] = field(default_factory=dict)       # serial:/inst: -> index


def _record_device(found: list[WifiDevice], found_by_key: dict[str, int],
                   inst_key: str, dev: WifiDevice) -> None:
    """Insert or IPv6->IPv4 upgrade ``dev`` in ``found``, deduplicating by serial or instance.

    Args:
        found: Accumulated list of verified ``WifiDevice`` instances (mutated in place).
        found_by_key: Lookup index mapping ``serial:<sn>`` and ``inst:<key>`` to indices.
        inst_key: Normalized DNS-SD instance name.
        dev: Newly verified ``WifiDevice``.
    """
    dedup_key = f"serial:{dev.serial}" if dev.serial is not None else f"inst:{inst_key}"
    idx = found_by_key.get(dedup_key)
    if idx is None:
        idx = len(found)
        found.append(dev)
        found_by_key[dedup_key] = idx
    elif ":" in found[idx].addr and ":" not in dev.addr:
        found[idx] = dev
    found_by_key[f"inst:{inst_key}"] = idx


def _verify_candidates(inst_name: str, host: str, port: int, raw_ips: Sequence[str],
                       state: _BrowseState, *, deadline: float,
                       want_serial: str | None, pinned_cert: bytes | None) -> bool:
    """Probe ranked IPs for one service instance and record the first verified device.

    Args:
        inst_name: Full DNS-SD instance name.
        host: Resolved SRV target hostname.
        port: Resolved SRV port (only ``REG_PORT`` is probed).
        raw_ips: Candidate IPv4/IPv6 strings from mDNS/Bonjour.
        state: Shared browse state (updated in place).
        deadline: Epoch timestamp budget.
        want_serial: Target serial number for early exit, or ``None``.
        pinned_cert: Optional DER certificate to verify on port 8443.

    Returns:
        ``True`` if ``want_serial`` matched and browsing can stop immediately.
    """
    if port != REG_PORT:
        return False
    inst_key = _norm_dns(inst_name)
    inst_idx = state.found_by_key.get(f"inst:{inst_key}")
    if inst_idx is not None and ":" not in state.found[inst_idx].addr:
        return False
    for ip in _rank_addrs(raw_ips):
        if time.time() >= deadline or len(state.checked) >= MAX_PROBES:
            break
        ep = (ip, port)
        if ep in state.checked:
            continue
        state.checked.add(ep)
        ok, serial = fetch_serial(ip, port)
        if not ok:
            continue
        if pinned_cert is not None and not probe_tls_pin(ip, pinned_cert, timeout=1.5):
            continue
        _record_device(state.found, state.found_by_key, inst_key,
                       WifiDevice(ip, port, serial, inst_name, host))
        if want_serial is not None and serial == want_serial:
            return True
        break
    return False


def _browse_bonjour(services: Sequence[str], *, timeout: float,
                    want_serial: str | None,
                    pinned_cert: bytes | None = None) -> list[WifiDevice]:
    """Discover and verify QUADERNO devices via macOS ``mDNSResponder`` (``bonjour.py``).

    Args:
        services: Service names to browse.
        timeout: Time budget in seconds.
        want_serial: Optional paired serial for early exit.
        pinned_cert: Optional DER certificate to verify over TLS 1.2.

    Returns:
        Verified ``WifiDevice`` list (empty on non-macOS platforms or cache miss).
    """
    if PLATFORM != "darwin" or timeout <= 0:
        return []
    deadline = time.time() + timeout
    state = _BrowseState()
    for inst_name, host, port, raw_ips in bonjour.resolve_services(
            services, timeout=timeout, max_items=MAX_PROBES):
        if time.time() >= deadline or len(state.checked) >= MAX_PROBES:
            break
        if _verify_candidates(inst_name, host, port, raw_ips, state,
                              deadline=deadline, want_serial=want_serial,
                              pinned_cert=pinned_cert):
            return state.found
    return state.found


def _send_query(socks: Sequence[tuple[socket.socket, bool]], name: str, qtype: int) -> None:
    """Transmit one mDNS query for ``(name, qtype)`` on every open browse socket."""
    for sock, unicast in socks:
        try:
            sock.sendto(build_query(name, qtype, unicast=unicast), (MDNS_ADDR_V4, MDNS_PORT))
        except OSError:
            pass


def _send_pending(socks: Sequence[tuple[socket.socket, bool]], services: Sequence[str],
                  state: _BrowseState) -> None:
    """Re-send PTR queries and any unresolved SRV/A/AAAA follow-up queries."""
    for svc in services:
        _send_query(socks, svc, TYPE_PTR)
    for inst_key, inst_name in list(state.instances.items()):
        if inst_key not in state.srvs:
            _send_query(socks, inst_name, TYPE_SRV)
            continue
        host, _port = state.srvs[inst_key]
        if not _rank_addrs(state.addrs.get(_norm_dns(host), ())):
            _send_query(socks, host, TYPE_A)
            _send_query(socks, host, TYPE_AAAA)


def _ingest_packet(packet: bytes, wanted: set[str], state: _BrowseState) -> None:
    """Parse one incoming mDNS response and merge PTR/SRV/A/AAAA records into ``state``."""
    for rec in parse_records(packet):
        if rec.ttl == 0 or rec.value is None:
            continue
        rname = _norm_dns(rec.name)
        if rec.rtype == TYPE_PTR and isinstance(rec.value, str) and rname in wanted:
            if len(state.instances) < MAX_PROBES:
                state.instances.setdefault(_norm_dns(rec.value), rec.value)
        elif rec.rtype == TYPE_SRV and isinstance(rec.value, tuple):
            port, target = rec.value
            if len(state.srvs) < MAX_PROBES * 2 or rname in state.srvs:
                state.srvs[rname] = (target, port)
        elif rec.rtype in (TYPE_A, TYPE_AAAA) and isinstance(rec.value, str):
            if len(state.addrs) < MAX_PROBES * 2 or rname in state.addrs:
                bucket = state.addrs.setdefault(rname, [])
                if rec.value not in bucket and len(bucket) < MAX_PROBES:
                    bucket.append(rec.value)


def _drain_ready(ready_socks: Sequence[socket.socket], wanted: set[str],
                 state: _BrowseState, deadline: float) -> None:
    """Read up to ``MAX_PACKETS_PER_TICK`` queued datagrams from each readable socket."""
    for sock in ready_socks:
        for _ in range(MAX_PACKETS_PER_TICK):
            if time.time() >= deadline:
                break
            try:
                packet, _src = sock.recvfrom(9000)
            except (BlockingIOError, OSError):
                break
            _ingest_packet(packet, wanted, state)


def _resolve_ready(socks: Sequence[tuple[socket.socket, bool]], state: _BrowseState, *,
                   deadline: float, want_serial: str | None,
                   pinned_cert: bytes | None) -> bool:
    """Send missing follow-up queries and verify ready endpoints in ``state``.

    Returns:
        ``True`` if ``want_serial`` matched and browsing can stop immediately.
    """
    for inst_key, inst_name in list(state.instances.items()):
        if time.time() >= deadline or len(state.checked) >= MAX_PROBES:
            break
        if inst_key not in state.srvs:
            if inst_key not in state.queried_srv:
                state.queried_srv.add(inst_key)
                _send_query(socks, inst_name, TYPE_SRV)
            continue
        host, port = state.srvs[inst_key]
        if port != REG_PORT:
            continue
        host_key = _norm_dns(host)
        raw_ips = state.addrs.get(host_key, ())
        if not _rank_addrs(raw_ips):
            if host_key not in state.queried_host:
                state.queried_host.add(host_key)
                _send_query(socks, host, TYPE_A)
                _send_query(socks, host, TYPE_AAAA)
            continue
        if _verify_candidates(inst_name, host, port, raw_ips, state,
                              deadline=deadline, want_serial=want_serial,
                              pinned_cert=pinned_cert):
            return True
    return False


# --- Linux Avahi cache fast-path ----------------------------------------------------

def _unescape_avahi(text: str) -> str:
    """Unescape Avahi's \\DDD decimal byte escapes (e.g. '\\032' -> ' ')."""
    raw = bytearray()
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 3 < n and text[i + 1:i + 4].isdigit():
            val = int(text[i + 1:i + 4])
            if 0 <= val <= 255:
                raw.append(val)
                i += 4
                continue
        raw.extend(text[i].encode("utf-8"))
        i += 1
    return raw.decode("utf-8", errors="replace")


def _parse_avahi_browse(text: str) -> list[tuple[str, str, str, int]]:
    """Parse resolved (`=`) rows from `avahi-browse -c -r -t -p`.
    Returns `(instance_fqdn, hostname, address, port)` tuples."""
    entries: list[tuple[str, str, str, int]] = []
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 9 or parts[0] != "=":
            continue
        name = _unescape_avahi(parts[3])
        regtype = _unescape_avahi(parts[4]).rstrip(".")
        domain = _unescape_avahi(parts[5]).rstrip(".")
        host = _unescape_avahi(parts[6]).rstrip(".")
        addr = parts[7].strip()
        try:
            port = int(parts[8])
        except ValueError:
            continue
        if not name or not host or not addr:
            continue
        inst = f"{name}.{regtype}.{domain}" if regtype and domain else name
        entries.append((inst, host, addr, port))
    return entries


def _browse_avahi(services: Sequence[str], *, timeout: float,
                  want_serial: str | None,
                  pinned_cert: bytes | None = None) -> list[WifiDevice]:
    """Query Linux's Avahi daemon cache via `avahi-browse -c -r -t -p` if available.

    When `avahi-daemon` is running, it caches the QUADERNO's unsolicited mDNS
    announcements even while Wi-Fi power-save drops raw multicast probes.
    Returns `[]` silently when `avahi-browse` isn't installed or the daemon isn't
    running, letting `browse()` fall back to raw UDP multicast."""
    if not PLATFORM.startswith("linux") or timeout <= 0:
        return []
    deadline = time.time() + timeout
    by_inst: dict[str, tuple[str, str, int, list[str]]] = {}
    for svc in services:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        regtype, domain = _split_service_domain(svc)
        cmd = ["avahi-browse", "-c", "-r", "-t", "-p",
               "-d", domain.rstrip("."), regtype.rstrip(".")]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=min(remaining, 2.0), check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        if res.returncode != 0:
            continue
        for inst, host, addr, port in _parse_avahi_browse(res.stdout):
            if port != REG_PORT:
                continue
            inst_key = _norm_dns(inst)
            if inst_key not in by_inst:
                if len(by_inst) >= MAX_PROBES:
                    continue
                by_inst[inst_key] = (inst, host, port, [])
            ips = by_inst[inst_key][3]
            if addr not in ips and len(ips) < MAX_PROBES:
                ips.append(addr)

    state = _BrowseState()
    for inst_name, host, port, raw_ips in by_inst.values():
        if time.time() >= deadline or len(state.checked) >= MAX_PROBES:
            break
        if _verify_candidates(inst_name, host, port, raw_ips, state,
                              deadline=deadline, want_serial=want_serial,
                              pinned_cert=pinned_cert):
            return state.found
    return state.found


def browse(services: Sequence[str] = (SERVICE,), *, timeout: float = 5.0,
           want_serial: str | None = None,
           pinned_cert: bytes | None = None) -> list[WifiDevice]:
    """Browse mDNS (plus macOS Bonjour / Linux Avahi cache fast-paths) for QUADERNOs.

    Args:
        services: DNS-SD service names to query (default ``('_dp_fujitsu._tcp.local',)``).
        timeout: Maximum total discovery duration in seconds.
        want_serial: Optional paired serial number; exits immediately when matched.
        pinned_cert: Optional DER TLS certificate; rejects candidates failing TLS pin.

    Returns:
        Deduplicated list of verified ``WifiDevice`` entries (preferring IPv4).
    """
    start = time.time()
    deadline = start + timeout
    if PLATFORM == "darwin":
        bonjour_devs = (_browse_bonjour(services, timeout=min(timeout, 2.0),
                                        want_serial=want_serial, pinned_cert=pinned_cert)
                        if pinned_cert is not None else
                        _browse_bonjour(services, timeout=min(timeout, 2.0),
                                        want_serial=want_serial))
        if bonjour_devs and (want_serial is None
                             or any(d.serial == want_serial for d in bonjour_devs)):
            return bonjour_devs
        if time.time() >= deadline:
            return bonjour_devs
    elif PLATFORM.startswith("linux"):
        if pinned_cert is not None:
            avahi_devs = _browse_avahi(services, timeout=min(timeout, 2.0),
                                       want_serial=want_serial, pinned_cert=pinned_cert)
        else:
            avahi_devs = _browse_avahi(services, timeout=min(timeout, 2.0),
                                       want_serial=want_serial)
        if avahi_devs and (want_serial is None
                           or any(d.serial == want_serial for d in avahi_devs)):
            return avahi_devs
        if time.time() >= deadline:
            return avahi_devs

    socks = _open_sockets()
    if not socks:
        return []
    wanted = {_norm_dns(s) for s in services}
    state = _BrowseState()
    raw_socks = [s for s, _ in socks]
    try:
        for svc in services:
            _send_query(socks, svc, TYPE_PTR)
        next_resend = time.time() + RESEND_INTERVAL
        first_found_at: float | None = None
        while True:
            now = time.time()
            if now >= deadline or (first_found_at is not None and now - first_found_at >= 0.5):
                break
            if not state.found and now >= next_resend:
                _send_pending(socks, services, state)
                next_resend = now + RESEND_INTERVAL
            try:
                ready, _, _ = select.select(raw_socks, [], [], min(0.2, deadline - now))
            except (OSError, ValueError):
                break
            _drain_ready(ready, wanted, state, deadline)
            if _resolve_ready(socks, state, deadline=deadline,
                              want_serial=want_serial, pinned_cert=pinned_cert):
                break
            if state.found and first_found_at is None:
                first_found_at = time.time()
    finally:
        for sock, _ in socks:
            sock.close()
    return state.found


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


def discover(serial_hint: str | None = None, *, timeout: float = 5.0,
             services: Sequence[str] = (SERVICE,),
             pinned_cert: bytes | None = None) -> str:
    """Find a QUADERNO on Wi-Fi via mDNS and return its IP address.

    Raises DeviceNotFoundError if none (or not the paired one) answers mDNS, or
    DeviceUnreachableError if the device answered mDNS but HTTPS port 8443 refuses."""
    if pinned_cert is not None:
        devs = browse(services=services, timeout=timeout, want_serial=serial_hint,
                      pinned_cert=pinned_cert)
    else:
        devs = browse(services=services, timeout=timeout, want_serial=serial_hint)
    if not devs:
        raise DeviceNotFoundError(
            "no QUADERNO found on Wi-Fi via mDNS: make sure the tablet is awake, on the "
            "same Wi-Fi network, and advertising mDNS (or pass --addr)")
    dev = _choose(devs, serial_hint)
    reachable = (probe_tls_pin(dev.addr, pinned_cert, timeout=2.0)
                 if pinned_cert is not None else probe(dev.addr, timeout=2.0))
    if not reachable:
        raise DeviceUnreachableError(
            f"found QUADERNO ({dev.serial or dev.host}) on Wi-Fi at {dev.addr}, but it "
            f"doesn't answer on port {HTTPS_PORT}: wake or unlock it")
    return dev.addr
