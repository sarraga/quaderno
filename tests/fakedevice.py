"""In-memory fake of the QUADERNO Web API for offline tests.

Subclasses quaderno.Device and replaces only `request`, so every CLI command runs its
real code paths (path resolution, pagination, chunked upload, downloads)."""
import os
import re
import sys
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quaderno import cli as Q  # noqa: E402  (CLI commands, for the CLI-level tests)
from quaderno.device import Device  # noqa: E402
from quaderno.errors import NotFoundError, QuadernoError  # noqa: E402
from quaderno.util import norm  # noqa: E402


class FakeDevice(Device):
    def __init__(self):
        self.addr = "fake"
        self._conn = None
        self.pinned_cert = None
        self.e = {"root": {"entry_id": "root", "entry_type": "folder", "entry_name": "Document",
                           "entry_path": "Document", "parent_folder_id": None}}
        self.files = {}
        self.deletes = []
        self.downloads = 0
        self.opened = []
        self.fail_upload = False  # make PUT /documents/{id}/file fail, to test cleanup
        self.clock = 1_700_000_000

    # --- helpers for tests ---------------------------------------------------
    def _stamp(self, x):
        self.clock += 60
        x["modified_date"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock))
        n = int(x.get("file_revision", "r.0.0").rsplit(".", 1)[1]) + 1
        x["file_revision"] = f"{x['entry_id'][:12]}.0.{n}"

    def _by_path(self, path):
        path = norm(path)
        for x in self.e.values():
            if x["entry_path"] == path:
                return x
        return None

    def mkdirs(self, path):
        path = norm(path)
        x = self._by_path(path)
        if x:
            return x["entry_id"]
        parent, name = path.rsplit("/", 1)
        return self._new("folder", self.mkdirs(parent), name)

    def put_doc(self, path, data):
        path = norm(path)
        parent, name = path.rsplit("/", 1)
        i = self._new("document", self.mkdirs(parent), name)
        self.edit_doc(path, data)
        return i

    def edit_doc(self, path, data):
        x = self._by_path(path)
        self.files[x["entry_id"]] = bytearray(data)
        x["file_size"] = str(len(data))
        self._stamp(x)

    def dev_move(self, path, new_path):
        x = self._by_path(path)
        parent, name = norm(new_path).rsplit("/", 1)
        x["parent_folder_id"] = self.mkdirs(parent)
        x["entry_name"] = name
        self._repath(x["entry_id"])

    def dev_delete(self, path):
        x = self._by_path(path)
        del self.e[x["entry_id"]]

    def paths(self):
        return sorted(x["entry_path"] for x in self.e.values())

    # --- fake API -------------------------------------------------------------
    def _new(self, kind, parent, name):
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

    def _kids(self, fid):
        return [x for x in self.e.values() if x["parent_folder_id"] == fid]

    def _repath(self, i):
        x = self.e[i]
        x["entry_path"] = self.e[x["parent_folder_id"]]["entry_path"] + "/" + x["entry_name"]
        for c in self._kids(i):
            self._repath(c["entry_id"])

    def request(self, method, path, body=None, headers=None, ok=(200, 204), raw=False, stream_to=None):
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
            kids = sorted(self._kids(m.group(1)), key=lambda x: x["entry_name"])
            off, lim = int(qs.get("offset", 0)), int(qs.get("limit", 100))
            lim = min(lim, 2)  # force pagination
            return {"count": len(kids), "entry_list": [dict(x) for x in kids[off:off + lim]]}
        if method == "PUT" and p in ("/viewer/controls/open", "/viewer/controls/open2"):
            self.opened.append((body["document_id"], body.get("page")))
            return None
        if method == "PUT" and p == "/system/configs/datetime":
            self.clock_set = body["value"]
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
            return {"folder_id": self._new("folder", body["parent_folder_id"], body["folder_name"])}
        if method == "POST" and p == "/documents":
            if not body["file_name"].lower().endswith(".pdf"):  # what the real device answers
                raise QuadernoError("POST /documents -> HTTP 400: 40006 The value of a parameter "
                                      "is invalid format. Bad parameter : fileName(unsupported)", 400)
            return {"document_id": self._new("document", body["parent_folder_id"], body["file_name"])}
        m = re.fullmatch(r"/documents/([^/]+)/file", p)
        if m and method == "PUT":
            if self.fail_upload:
                raise QuadernoError("PUT -> HTTP 500: simulated failure", 500)
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
            x = self.e[m.group(2)]
            x["parent_folder_id"] = body["parent_folder_id"]
            x["entry_name"] = body.get("file_name") or body.get("folder_name")
            self._repath(x["entry_id"])
            return None
        if m and method == "GET":
            return dict(self.e[m.group(2)])
        raise AssertionError(f"unhandled {method} {path}")
