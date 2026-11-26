"""Finding the device on Wi-Fi via mDNS (DNS-SD, RFC 6762 / 6763), stdlib only.

The QUADERNO advertises the DNS-SD service `_dp_fujitsu._tcp.local.` on port
8080 while Wi-Fi is awake. This module browses that service over UDP multicast
(224.0.0.251:5353), resolves PTR -> SRV -> A/AAAA records, and checks each
candidate's `/register/information` endpoint to read its serial number so the
paired device can be identified unambiguously."""

import ctypes
import http.client
import ipaddress
import json
import re
import select
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any, NamedTuple

from .errors import DeviceNotFoundError, DeviceUnreachableError
from .usb import probe
from .util import HTTPS_PORT, REG_PORT

SERVICE = "_dp_fujitsu._tcp.local"
MDNS_ADDR_V4 = "224.0.0.251"
MDNS_PORT = 5353
RESEND_INTERVAL = 0.5
PLATFORM = sys.platform

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


# --- macOS Bonjour (mDNSResponder via libSystem) ------------------------------------

_DNSSD_FLAGS_MORE_COMING = 0x1
_DNSSD_FLAGS_ADD = 0x2
_DNSSD_PROTO_IPV4_V6 = 0x3  # kDNSServiceProtocol_IPv4 | kDNSServiceProtocol_IPv6


def _split_service_domain(service: str) -> tuple[str, str]:
    """Split '_dp_fujitsu._tcp.local' into ('_dp_fujitsu._tcp.', 'local.')."""
    s = service.rstrip(".")
    for proto in ("._tcp.", "._udp."):
        if proto in s:
            head, _, domain = s.partition(proto)
            return head + proto, domain + "."
    return s + ".", "local."


def _sockaddr_to_ip(raw: bytes) -> str | None:
    """Decode a macOS/BSD `struct sockaddr` (uint8 sa_len, uint8 sa_family, ...) to an IP."""
    if len(raw) < 2:
        return None
    sa_len, family = raw[0], raw[1]
    if family == socket.AF_INET and sa_len >= 8 and len(raw) >= 8:
        return socket.inet_ntop(socket.AF_INET, raw[4:8])
    if family == socket.AF_INET6 and sa_len >= 24 and len(raw) >= 24:
        return socket.inet_ntop(socket.AF_INET6, raw[8:24])
    return None


def _record_device(found: list[WifiDevice], found_by_key: dict[str, int],
                   inst_key: str, dev: WifiDevice) -> None:
    """Insert or IPv6->IPv4 upgrade `dev` in `found`, deduplicating by serial (or instance)."""
    dedup_key = f"serial:{dev.serial}" if dev.serial is not None else f"inst:{inst_key}"
    idx = found_by_key.get(dedup_key)
    if idx is None:
        idx = len(found)
        found.append(dev)
        found_by_key[dedup_key] = idx
    elif ":" in found[idx].addr and ":" not in dev.addr:
        found[idx] = dev
    found_by_key[f"inst:{inst_key}"] = idx


def _browse_bonjour(services: Sequence[str], *, timeout: float,
                    want_serial: str | None) -> list[WifiDevice]:
    """Query macOS's native mDNSResponder daemon via libSystem's DNS-SD C API.

    On macOS, mDNSResponder listens continuously and caches the QUADERNO's
    unsolicited announcements (4500 s PTR TTL). Using its cache finds an idle
    tablet in ~15 ms even when Wi-Fi power-save drops raw multicast probes."""
    if PLATFORM != "darwin" or timeout <= 0:
        return []
    try:
        lib: Any = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    except OSError:
        return []

    browse_cb_t = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
    )
    resolve_cb_t = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint16,
        ctypes.c_uint16, ctypes.c_void_p, ctypes.c_void_p,
    )
    addr_cb_t = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
        ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
    )
    try:
        lib.DNSServiceBrowse.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_char_p, browse_cb_t, ctypes.c_void_p,
        ]
        lib.DNSServiceBrowse.restype = ctypes.c_int32
        lib.DNSServiceResolve.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, resolve_cb_t, ctypes.c_void_p,
        ]
        lib.DNSServiceResolve.restype = ctypes.c_int32
        lib.DNSServiceGetAddrInfo.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_char_p, addr_cb_t, ctypes.c_void_p,
        ]
        lib.DNSServiceGetAddrInfo.restype = ctypes.c_int32
        lib.DNSServiceRefSockFD.argtypes = [ctypes.c_void_p]
        lib.DNSServiceRefSockFD.restype = ctypes.c_int
        lib.DNSServiceProcessResult.argtypes = [ctypes.c_void_p]
        lib.DNSServiceProcessResult.restype = ctypes.c_int32
        lib.DNSServiceRefDeallocate.argtypes = [ctypes.c_void_p]
        lib.DNSServiceRefDeallocate.restype = None
    except AttributeError:
        return []

    deadline = time.time() + timeout
    instances: list[tuple[int, str, str, str]] = []
    seen_insts: set[tuple[int, str, str, str]] = set()
    more_coming = True

    def on_browse(_sd: object, flags: int, ifidx: int, err: int,
                  name: bytes | None, regtype: bytes | None, domain: bytes | None,
                  _ctx: object) -> None:
        nonlocal more_coming
        if err == 0 and (flags & _DNSSD_FLAGS_ADD) and name and regtype and domain:
            item = (ifidx, name.decode("utf-8", errors="replace"),
                    regtype.decode("utf-8", errors="replace"),
                    domain.decode("utf-8", errors="replace"))
            if item not in seen_insts:
                seen_insts.add(item)
                instances.append(item)
        more_coming = bool(flags & _DNSSD_FLAGS_MORE_COMING)

    cb_browse = browse_cb_t(on_browse)
    brefs: list[ctypes.c_void_p] = []
    try:
        for svc in services:
            regtype, domain = _split_service_domain(svc)
            ref = ctypes.c_void_p()
            err = lib.DNSServiceBrowse(
                ctypes.byref(ref), 0, 0, regtype.encode("utf-8"),
                domain.encode("utf-8"), cb_browse, None)
            if err == 0 and ref.value:
                brefs.append(ref)
        if not brefs:
            return []
        fd_map = {int(lib.DNSServiceRefSockFD(r)): r for r in brefs}
        browse_end = min(deadline, time.time() + min(timeout, 1.5))
        while time.time() < browse_end:
            wait = min(0.2, max(0.0, browse_end - time.time()))
            try:
                ready, _, _ = select.select(list(fd_map), [], [], wait)
            except (OSError, ValueError):
                break
            for fd in ready:
                if lib.DNSServiceProcessResult(fd_map[fd]) != 0:
                    fd_map.pop(fd, None)
            if instances and not more_coming:
                break
    finally:
        for ref in brefs:
            lib.DNSServiceRefDeallocate(ref)

    if not instances:
        return []

    found: list[WifiDevice] = []
    found_by_key: dict[str, int] = {}
    checked: set[tuple[str, int]] = set()

    for ifidx, name, regtype, domain in instances:
        if time.time() >= deadline:
            break
        resolved: list[tuple[str, int]] = []

        def on_resolve(_sd: object, _flags: int, _ifidx: int, err: int,
                       _fullname: bytes | None, hosttarget: bytes | None, port_be: int,
                       _txtlen: int, _txt: object, _ctx: object) -> None:
            if err == 0 and hosttarget:
                resolved.append((hosttarget.decode("utf-8", errors="replace").rstrip("."),
                                 socket.ntohs(port_be)))

        cb_resolve = resolve_cb_t(on_resolve)
        rref = ctypes.c_void_p()
        err = lib.DNSServiceResolve(
            ctypes.byref(rref), 0, ifidx, name.encode("utf-8"),
            regtype.encode("utf-8"), domain.encode("utf-8"), cb_resolve, None)
        if err != 0 or not rref.value:
            continue
        try:
            rfd = int(lib.DNSServiceRefSockFD(rref))
            res_end = min(deadline, time.time() + 1.0)
            while time.time() < res_end and not resolved:
                wait = min(0.2, max(0.0, res_end - time.time()))
                ready, _, _ = select.select([rfd], [], [], wait)
                if ready and lib.DNSServiceProcessResult(rref) != 0:
                    break
        except (OSError, ValueError):
            pass
        finally:
            lib.DNSServiceRefDeallocate(rref)

        if not resolved:
            continue
        host, port = resolved[0]
        raw_ips: list[str] = []
        addr_more = True

        def on_addr(_sd: object, flags: int, _ifidx: int, err: int,
                    _hostname: bytes | None, sa_ptr: int | None, _ttl: int,
                    _ctx: object) -> None:
            nonlocal addr_more
            if err == 0 and (flags & _DNSSD_FLAGS_ADD) and sa_ptr:
                hdr = ctypes.string_at(sa_ptr, 2)
                sa_len = hdr[0]
                if sa_len >= 8:
                    ip = _sockaddr_to_ip(ctypes.string_at(sa_ptr, sa_len))
                    if ip:
                        raw_ips.append(ip)
            addr_more = bool(flags & _DNSSD_FLAGS_MORE_COMING)

        cb_addr = addr_cb_t(on_addr)
        aref = ctypes.c_void_p()
        err = lib.DNSServiceGetAddrInfo(
            ctypes.byref(aref), 0, ifidx, _DNSSD_PROTO_IPV4_V6,
            (host + ".").encode("utf-8"), cb_addr, None)
        if err == 0 and aref.value:
            try:
                afd = int(lib.DNSServiceRefSockFD(aref))
                addr_end = min(deadline, time.time() + 1.0)
                while time.time() < addr_end:
                    wait = min(0.2, max(0.0, addr_end - time.time()))
                    ready, _, _ = select.select([afd], [], [], wait)
                    if ready and lib.DNSServiceProcessResult(aref) != 0:
                        break
                    if raw_ips and not addr_more:
                        break
            except (OSError, ValueError):
                pass
            finally:
                lib.DNSServiceRefDeallocate(aref)

        inst_name = f"{name}.{regtype}{domain}".rstrip(".")
        inst_key = _norm_dns(inst_name)
        for ip in _rank_addrs(raw_ips):
            ep = (ip, port)
            if ep in checked:
                continue
            checked.add(ep)
            ok, serial = fetch_serial(ip, port)
            if not ok:
                continue
            _record_device(found, found_by_key, inst_key,
                           WifiDevice(ip, port, serial, inst_name, host))
            if want_serial is not None and serial == want_serial:
                return found
            break
    return found


def browse(services: Sequence[str] = (SERVICE,), *, timeout: float = 5.0,
           want_serial: str | None = None) -> list[WifiDevice]:
    """Browse mDNS for QUADERNO services and return verified WifiDevices.
    Exits early as soon as `want_serial` is matched (or shortly after the first
    device answers when `want_serial` is None)."""
    start = time.time()
    deadline = start + timeout
    if PLATFORM == "darwin":
        bonjour_devs = _browse_bonjour(services, timeout=min(timeout, 2.0),
                                       want_serial=want_serial)
        if bonjour_devs and (want_serial is None
                             or any(d.serial == want_serial for d in bonjour_devs)):
            return bonjour_devs
        if time.time() >= deadline:
            return bonjour_devs

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
    found_by_key: dict[str, int] = {}         # serial:<sn> or inst:<key> -> index in found

    def send_query(name: str, qtype: int) -> None:
        for sock, unicast in socks:
            try:
                sock.sendto(build_query(name, qtype, unicast=unicast), (MDNS_ADDR_V4, MDNS_PORT))
            except OSError:
                pass

    def send_initial() -> None:
        for svc in services:
            send_query(svc, TYPE_PTR)

    def send_pending() -> None:
        """Re-query services and any partially resolved SRV/A/AAAA records.
        Over Wi-Fi power-save, individual multicast frames are often dropped."""
        send_initial()
        for inst_key, inst_name in list(instances.items()):
            if inst_key not in srvs:
                send_query(inst_name, TYPE_SRV)
                continue
            host, _port = srvs[inst_key]
            if not _rank_addrs(addrs.get(_norm_dns(host), ())):
                send_query(host, TYPE_A)
                send_query(host, TYPE_AAAA)

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
            inst_idx = found_by_key.get(f"inst:{inst_key}")
            if inst_idx is not None and ":" not in found[inst_idx].addr:
                continue
            for ip in candidates:
                ep = (ip, port)
                if ep in checked:
                    continue
                checked.add(ep)
                ok, serial = fetch_serial(ip, port)
                if not ok:
                    continue
                _record_device(found, found_by_key, inst_key,
                               WifiDevice(ip, port, serial, inst_name, host))
                if want_serial is not None and serial == want_serial:
                    return True
                break
        return False

    try:
        send_initial()
        next_resend = time.time() + RESEND_INTERVAL
        first_found_at: float | None = None
        raw_socks = [s for s, _ in socks]
        while True:
            now = time.time()
            if now >= deadline:
                break
            if first_found_at is not None and now - first_found_at >= 0.5:
                break
            if not found and now >= next_resend:
                send_pending()
                next_resend = now + RESEND_INTERVAL
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


def discover(serial_hint: str | None = None, *, timeout: float = 5.0,
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
