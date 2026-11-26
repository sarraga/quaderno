"""macOS Bonjour (mDNSResponder) DNS-SD lookup via ``libSystem.B.dylib``.

This is a leaf stdlib-only module (``ctypes`` + ``select`` + ``socket``) used by
``quaderno.mdns`` on macOS to query the OS daemon's live Bonjour cache before
falling back to raw UDP multicast. Because ``mDNSResponder`` listens continuously
and caches unsolicited announcements (4500 s PTR TTL), an idle tablet in Wi-Fi
power-save resolves in ~15 ms even when raw multicast probes are dropped.
"""

import ctypes
import select
import socket
import time
from collections.abc import Callable, Sequence
from typing import Any

_FLAGS_MORE_COMING = 0x1
_FLAGS_ADD = 0x2
_PROTO_IPV4_V6 = 0x3  # kDNSServiceProtocol_IPv4 | kDNSServiceProtocol_IPv6

_BROWSE_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
)
_RESOLVE_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint16,
    ctypes.c_uint16, ctypes.c_void_p, ctypes.c_void_p,
)
_ADDR_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
    ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
)


def split_service_domain(service: str) -> tuple[str, str]:
    """Split a full DNS-SD service name into ``(regtype, domain)``.

    Args:
        service: Full service name such as ``'_dp_fujitsu._tcp.local'``.

    Returns:
        A ``(regtype, domain)`` pair with trailing dots, e.g.
        ``('_dp_fujitsu._tcp.', 'local.')``.
    """
    s = service.rstrip(".")
    for proto in ("._tcp.", "._udp."):
        if proto in s:
            head, _, domain = s.partition(proto)
            return head + proto, domain + "."
    return s + ".", "local."


def sockaddr_to_ip(raw: bytes) -> str | None:
    """Decode a macOS/BSD ``struct sockaddr`` into a text IPv4 or IPv6 address.

    Args:
        raw: Raw bytes beginning with ``(uint8 sa_len, uint8 sa_family, ...)``.

    Returns:
        An IP string (without zone scope), or ``None`` for unsupported/truncated input.
    """
    if len(raw) < 2:
        return None
    sa_len, family = raw[0], raw[1]
    if family == socket.AF_INET and sa_len >= 8 and len(raw) >= 8:
        return socket.inet_ntop(socket.AF_INET, raw[4:8])
    if family == socket.AF_INET6 and sa_len >= 24 and len(raw) >= 24:
        return socket.inet_ntop(socket.AF_INET6, raw[8:24])
    return None


def _load_libsystem() -> Any | None:
    """Load ``libSystem.B.dylib`` and configure DNS-SD C function prototypes.

    Returns:
        Configured ``ctypes.CDLL`` handle, or ``None`` if unavailable.
    """
    try:
        lib: Any = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.DNSServiceBrowse.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_char_p, _BROWSE_CB, ctypes.c_void_p,
        ]
        lib.DNSServiceBrowse.restype = ctypes.c_int32
        lib.DNSServiceResolve.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, _RESOLVE_CB, ctypes.c_void_p,
        ]
        lib.DNSServiceResolve.restype = ctypes.c_int32
        lib.DNSServiceGetAddrInfo.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_char_p, _ADDR_CB, ctypes.c_void_p,
        ]
        lib.DNSServiceGetAddrInfo.restype = ctypes.c_int32
        lib.DNSServiceRefSockFD.argtypes = [ctypes.c_void_p]
        lib.DNSServiceRefSockFD.restype = ctypes.c_int
        lib.DNSServiceProcessResult.argtypes = [ctypes.c_void_p]
        lib.DNSServiceProcessResult.restype = ctypes.c_int32
        lib.DNSServiceRefDeallocate.argtypes = [ctypes.c_void_p]
        lib.DNSServiceRefDeallocate.restype = None
        return lib
    except (OSError, AttributeError):
        return None


def _wait_sdref(lib: Any, ref: ctypes.c_void_p, deadline: float,
                is_done: Callable[[], bool]) -> None:
    """Process callbacks on a single ``DNSServiceRef`` until ``is_done()`` or ``deadline``.

    Always deallocates ``ref`` before returning.

    Args:
        lib: Loaded ``libSystem`` handle.
        ref: Valid ``DNSServiceRef`` pointer.
        deadline: Epoch timestamp after which waiting stops.
        is_done: Predicate returning ``True`` when enough data has arrived.
    """
    try:
        fd = int(lib.DNSServiceRefSockFD(ref))
        while fd >= 0 and time.time() < deadline and not is_done():
            wait = min(0.2, max(0.0, deadline - time.time()))
            ready, _, _ = select.select([fd], [], [], wait)
            if ready and lib.DNSServiceProcessResult(ref) != 0:
                break
    except (OSError, ValueError):
        pass
    finally:
        lib.DNSServiceRefDeallocate(ref)


def _browse_instances(lib: Any, services: Sequence[str], deadline: float,
                      max_items: int) -> list[tuple[int, str, str, str]]:
    """Browse ``services`` via ``DNSServiceBrowse`` until cached results drain or ``deadline``.

    Args:
        lib: Loaded ``libSystem`` handle.
        services: Service names (e.g. ``('_dp_fujitsu._tcp.local',)``).
        deadline: Epoch timestamp budget for the browse step.
        max_items: Maximum number of distinct service instances to collect.

    Returns:
        List of ``(ifidx, instance_name, regtype, domain)`` tuples.
    """
    instances: list[tuple[int, str, str, str]] = []
    seen: set[tuple[int, str, str, str]] = set()
    more_by_ref: dict[int, bool] = {}

    def on_browse(sd: int | None, flags: int, ifidx: int, err: int,
                  name: bytes | None, regtype: bytes | None, domain: bytes | None,
                  _ctx: object) -> None:
        """Record each added Bonjour instance and track ``MoreComing`` per handle."""
        if err == 0 and (flags & _FLAGS_ADD) and name and regtype and domain:
            item = (ifidx, name.decode("utf-8", errors="replace"),
                    regtype.decode("utf-8", errors="replace"),
                    domain.decode("utf-8", errors="replace"))
            if item not in seen and len(instances) < max_items:
                seen.add(item)
                instances.append(item)
        if sd is not None:
            more_by_ref[sd] = bool(flags & _FLAGS_MORE_COMING)

    cb = _BROWSE_CB(on_browse)
    refs: list[ctypes.c_void_p] = []
    try:
        for svc in services:
            regtype, domain = split_service_domain(svc)
            ref = ctypes.c_void_p()
            err = lib.DNSServiceBrowse(
                ctypes.byref(ref), 0, 0, regtype.encode("utf-8"),
                domain.encode("utf-8"), cb, None)
            if err == 0 and ref.value:
                refs.append(ref)
                more_by_ref[ref.value] = True
        fd_map = {int(lib.DNSServiceRefSockFD(r)): r for r in refs}
        fd_map = {fd: r for fd, r in fd_map.items() if fd >= 0}
        while fd_map and time.time() < deadline:
            wait = min(0.2, max(0.0, deadline - time.time()))
            try:
                ready, _, _ = select.select(list(fd_map), [], [], wait)
            except (OSError, ValueError):
                break
            for fd in ready:
                if lib.DNSServiceProcessResult(fd_map[fd]) != 0:
                    dead = fd_map.pop(fd)
                    if dead.value:
                        more_by_ref.pop(dead.value, None)
            if instances and not any(more_by_ref.values()):
                break
    finally:
        for ref in refs:
            lib.DNSServiceRefDeallocate(ref)
    return instances


def _resolve_service(lib: Any, ifidx: int, name: str, regtype: str, domain: str,
                     deadline: float) -> tuple[str, int] | None:
    """Resolve one Bonjour service instance to ``(host, port)``.

    Args:
        lib: Loaded ``libSystem`` handle.
        ifidx: Interface index reported by ``DNSServiceBrowse``.
        name: Instance name.
        regtype: Service registration type (with trailing dot).
        domain: Service domain (with trailing dot).
        deadline: Epoch timestamp budget.

    Returns:
        ``(hostname_without_trailing_dot, port)`` or ``None`` on timeout/error.
    """
    resolved: list[tuple[str, int]] = []

    def on_resolve(_sd: object, _flags: int, _ifidx: int, err: int,
                   _fullname: bytes | None, hosttarget: bytes | None, port_be: int,
                   _txtlen: int, _txt: object, _ctx: object) -> None:
        """Record the resolved SRV target hostname and host-byte-order port."""
        if err == 0 and hosttarget:
            resolved.append((hosttarget.decode("utf-8", errors="replace").rstrip("."),
                             socket.ntohs(port_be)))

    cb = _RESOLVE_CB(on_resolve)
    ref = ctypes.c_void_p()
    err = lib.DNSServiceResolve(
        ctypes.byref(ref), 0, ifidx, name.encode("utf-8"),
        regtype.encode("utf-8"), domain.encode("utf-8"), cb, None)
    if err != 0 or not ref.value:
        return None
    _wait_sdref(lib, ref, deadline, lambda: bool(resolved))
    return resolved[0] if resolved else None


def _resolve_addresses(lib: Any, ifidx: int, host: str, deadline: float) -> list[str]:
    """Resolve IPv4/IPv6 addresses for ``host`` on ``ifidx`` via ``DNSServiceGetAddrInfo``.

    Args:
        lib: Loaded ``libSystem`` handle.
        ifidx: Interface index to resolve on.
        host: Target hostname (without trailing dot).
        deadline: Epoch timestamp budget.

    Returns:
        List of IP address strings returned by ``mDNSResponder``.
    """
    ips: list[str] = []
    more = True

    def on_addr(_sd: object, flags: int, _ifidx: int, err: int,
                _hostname: bytes | None, sa_ptr: int | None, _ttl: int,
                _ctx: object) -> None:
        """Decode each returned BSD ``sockaddr`` pointer into an IP string."""
        nonlocal more
        if err == 0 and (flags & _FLAGS_ADD) and sa_ptr:
            sa_len = ctypes.string_at(sa_ptr, 2)[0]
            if 8 <= sa_len <= 128:
                ip = sockaddr_to_ip(ctypes.string_at(sa_ptr, sa_len))
                if ip:
                    ips.append(ip)
        more = bool(flags & _FLAGS_MORE_COMING)

    cb = _ADDR_CB(on_addr)
    ref = ctypes.c_void_p()
    err = lib.DNSServiceGetAddrInfo(
        ctypes.byref(ref), 0, ifidx, _PROTO_IPV4_V6,
        (host + ".").encode("utf-8"), cb, None)
    if err != 0 or not ref.value:
        return []
    _wait_sdref(lib, ref, deadline, lambda: bool(ips) and not more)
    return ips


def resolve_services(services: Sequence[str], *, timeout: float,
                     max_items: int = 16) -> list[tuple[str, str, int, list[str]]]:
    """Browse and resolve Bonjour services via macOS ``mDNSResponder``.

    Args:
        services: Full DNS-SD service names to browse.
        timeout: Total time budget in seconds.
        max_items: Maximum number of instances to resolve.

    Returns:
        List of ``(instance_fullname, host, port, raw_ips)`` tuples. Returns an
        empty list if ``libSystem`` is unavailable or nothing resolves in time.
    """
    if timeout <= 0:
        return []
    lib = _load_libsystem()
    if lib is None:
        return []
    deadline = time.time() + timeout
    instances = _browse_instances(lib, services, min(deadline, time.time() + min(timeout, 1.5)),
                                  max_items)
    results: list[tuple[str, str, int, list[str]]] = []
    for ifidx, name, regtype, domain in instances:
        if time.time() >= deadline:
            break
        srv = _resolve_service(lib, ifidx, name, regtype, domain, min(deadline, time.time() + 1.0))
        if srv is None:
            continue
        host, port = srv
        ips = _resolve_addresses(lib, ifidx, host, min(deadline, time.time() + 1.0))
        inst_name = f"{name}.{regtype}{domain}".rstrip(".")
        results.append((inst_name, host, port, ips))
    return results
