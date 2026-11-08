"""In-memory fake of the QUADERNO Web API for offline tests.

Subclasses quaderno.Device and replaces only `_request`, so every CLI command runs
its real code paths (path resolution, pagination, chunked upload, downloads)."""
import re
import time
import uuid
from collections.abc import Collection
from typing import Any, BinaryIO, Protocol, TypeVar, overload
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from quaderno.client import Body
from quaderno.device import Device
from quaderno.errors import NotFoundError, QuadernoError
from quaderno.util import JsonObject, norm

FakeEntry = dict[str, Any]  # an entry as the device's API describes it
E = TypeVar("E", bound=BaseException)


@overload
def expect(fragment: str) -> pytest.RaisesExc[QuadernoError]: ...
@overload
def expect(fragment: str, exc: type[E]) -> pytest.RaisesExc[E]: ...


def expect(fragment: str, exc: type[BaseException] = QuadernoError) -> pytest.RaisesExc[Any]:
    """pytest.raises for `exc` (default QuadernoError) whose message contains
    `fragment` literally."""
    return pytest.raises(exc, match=re.escape(fragment))


class MakePdf(Protocol):
    """The mkpdf fixture: writes a small PDF under tmp_path, returns its path."""
    def __call__(self, rel: str, text: str = "x") -> str: ...


def _obj(body: Body) -> JsonObject:
    assert isinstance(body, dict), body
    return body


class FakeDevice(Device):
    def __init__(self) -> None:  # no network: Client.__init__ is not needed
        self.addr = "fake"
        self._conn = None
        self.pinned_cert = None
        self.e: dict[str, FakeEntry] = {
            "root": {"entry_id": "root", "entry_type": "folder", "entry_name": "Document",
                     "entry_path": "Document", "parent_folder_id": None}}
        self.files: dict[str, bytearray] = {}
        self.deletes: list[str] = []
        self.downloads = 0
        self.opened: list[tuple[str, int | None]] = []
        self.fail_upload = False  # make PUT /documents/{id}/file fail, to test cleanup
        self.clock = 1_700_000_000
        self.clock_set: str | None = None

    # --- helpers for tests ---------------------------------------------------
    def _stamp(self, x: FakeEntry) -> None:
        self.clock += 60
        x["modified_date"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock))
        n = int(x.get("file_revision", "r.0.0").rsplit(".", 1)[1]) + 1
        x["file_revision"] = f"{x['entry_id'][:12]}.0.{n}"

    def _by_path(self, path: str) -> FakeEntry | None:
        path = norm(path)
        for x in self.e.values():
            if x["entry_path"] == path:
                return x
        return None

    def _at(self, path: str) -> FakeEntry:
        x = self._by_path(path)
        assert x is not None, path
        return x

    def mkdirs(self, path: str) -> str:
        path = norm(path)
        x = self._by_path(path)
        if x:
            return str(x["entry_id"])
        parent, name = path.rsplit("/", 1)
        return self._new("folder", self.mkdirs(parent), name)

    def put_doc(self, path: str, data: bytes) -> str:
        path = norm(path)
        parent, name = path.rsplit("/", 1)
        i = self._new("document", self.mkdirs(parent), name)
        self.edit_doc(path, data)
        return i

    def edit_doc(self, path: str, data: bytes) -> None:
        x = self._at(path)
        self.files[x["entry_id"]] = bytearray(data)
        x["file_size"] = str(len(data))
        self._stamp(x)

    def dev_move(self, path: str, new_path: str) -> None:
        x = self._at(path)
        parent, name = norm(new_path).rsplit("/", 1)
        x["parent_folder_id"] = self.mkdirs(parent)
        x["entry_name"] = name
        self._repath(x["entry_id"])

    def dev_delete(self, path: str) -> None:
        del self.e[self._at(path)["entry_id"]]

    def paths(self) -> list[str]:
        return sorted(x["entry_path"] for x in self.e.values())

    # --- fake API -------------------------------------------------------------
    def _new(self, kind: str, parent: str, name: str) -> str:
        p = self.e[parent]
        path = p["entry_path"] + "/" + name
        if any(x["entry_path"] == path for x in self.e.values()):
            raise QuadernoError("exists", 409)
        i = uuid.uuid4().hex
        self.e[i] = {"entry_id": i, "entry_type": kind, "entry_name": name, "entry_path": path,
                     "parent_folder_id": parent}
        if kind == "document":
            self.e[i]["file_size"] = "0"
            self._stamp(self.e[i])
        return i

    def _kids(self, fid: str) -> list[FakeEntry]:
        return [x for x in self.e.values() if x["parent_folder_id"] == fid]

    def _repath(self, i: str) -> None:
        x = self.e[i]
        x["entry_path"] = self.e[x["parent_folder_id"]]["entry_path"] + "/" + x["entry_name"]
        for c in self._kids(i):
            self._repath(c["entry_id"])

    def _request(self, method: str, path: str, body: Body = None,
                 headers: dict[str, str] | None = None, ok: Collection[int] = (200, 204),
                 stream_to: BinaryIO | None = None) -> JsonObject | int | None:
        u = urlsplit(path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if method == "GET" and p.startswith("/resolve/entry/"):
            want = unquote(p[len("/resolve/entry/"):])
            for x in self.e.values():
                if x["entry_path"] == want:
                    return dict(x)
            raise NotFoundError("404")
        m = re.fullmatch(r"/folders/([^/]+)/entries", p)
        if method == "GET" and m:
            kids = sorted(self._kids(m.group(1)), key=lambda x: str(x["entry_name"]))
            off, lim = int(qs.get("offset", 0)), int(qs.get("limit", 100))
            lim = min(lim, 2)  # force pagination
            return {"count": len(kids), "entry_list": [dict(x) for x in kids[off:off + lim]]}
        if method == "PUT" and p in ("/viewer/controls/open", "/viewer/controls/open2"):
            b = _obj(body)
            self.opened.append((b["document_id"], b.get("page")))
            return None
        if method == "PUT" and p == "/system/configs/datetime":
            self.clock_set = _obj(body)["value"]
            return None
        if method == "GET" and p == "/system/configs/timezone":
            return {"value": "UTC"}
        status = {"/api_version": {"value": "2.1"},
                  "/system/status/firmware_version": {"value": "fake"},
                  "/system/status/storage": {"capacity": "1000", "available": "400"},
                  "/system/configs/owner": {"value": "tester"},
                  "/system/status/mac_address": {"value": "00:00:00:00:00:00"}}
        if method == "GET" and p in status:
            return status[p]
        if method == "GET" and p == "/system/status/battery":
            raise QuadernoError("GET /system/status/battery -> HTTP 404: no battery", 404)
        if method == "POST" and p == "/folders":
            b = _obj(body)
            return {"folder_id": self._new("folder", b["parent_folder_id"], b["folder_name"])}
        if method == "POST" and p == "/documents":
            b = _obj(body)
            if not b["file_name"].lower().endswith(".pdf"):  # what the real device answers
                raise QuadernoError("POST /documents -> HTTP 400: 40006 The value of a parameter "
                                    "is invalid format. Bad parameter : fileName(unsupported)", 400)
            return {"document_id": self._new("document", b["parent_folder_id"], b["file_name"])}
        m = re.fullmatch(r"/documents/([^/]+)/file", p)
        if m and method == "PUT":
            if self.fail_upload:
                raise QuadernoError("PUT -> HTTP 500: simulated failure", 500)
            assert isinstance(body, bytes)
            data = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            off, total = int(qs["offset_bytes"]), int(qs["total_bytes"])
            buf = self.files.setdefault(m.group(1), bytearray())
            assert len(buf) == off
            buf += data
            x = self.e[m.group(1)]
            x["file_size"] = str(len(buf))
            done = len(buf) >= total
            if done:
                self._stamp(x)
            return {"current_bytes": len(buf), "received_bytes": len(data),
                    "completed": "yes" if done else "no"}
        if m and method == "GET":
            assert stream_to is not None
            self.downloads += 1
            stream_to.write(bytes(self.files[m.group(1)]))
            return len(self.files[m.group(1)])
        m = re.fullmatch(r"/(documents|folders)/([^/]+)", p)
        if m and method == "DELETE":
            i = m.group(2)
            if m.group(1) == "folders" and self._kids(i):
                raise AssertionError("DELETE sent for NON-EMPTY folder " + self.e[i]["entry_path"])
            self.deletes.append(self.e[i]["entry_path"])
            del self.e[i]
            return None
        if m and method == "PUT":
            b = _obj(body)
            x = self.e[m.group(2)]
            x["parent_folder_id"] = b["parent_folder_id"]
            x["entry_name"] = b.get("file_name") or b.get("folder_name")
            self._repath(x["entry_id"])
            return None
        if m and method == "GET":
            return dict(self.e[m.group(2)])
        raise AssertionError(f"unhandled {method} {path}")
