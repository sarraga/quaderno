#!/usr/bin/env python3
"""In-memory fake of the DPT Web API; runs the real Quaderno methods and CLI commands."""
import io, json, os, re, sys, tempfile, types, uuid
from urllib.parse import unquote, urlsplit, parse_qs

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quaderno as Q


class FakeDevice(Q.Quaderno):
    def __init__(self):
        self.addr = "fake"
        self.e = {"root": {"entry_id": "root", "entry_type": "folder", "entry_name": "Document",
                           "entry_path": "Document", "parent_folder_id": None}}
        self.files = {}
        self.deletes = []

    def _new(self, kind, parent, name):
        p = self.e[parent]
        path = p["entry_path"] + "/" + name
        if any(x["entry_path"] == path for x in self.e.values()):
            raise Q.QuadernoError("exists", 409)
        i = uuid.uuid4().hex
        self.e[i] = {"entry_id": i, "entry_type": kind, "entry_name": name, "entry_path": path,
                     "parent_folder_id": parent, "file_size": 0}
        return i

    def _children(self, fid):
        return [x for x in self.e.values() if x["parent_folder_id"] == fid]

    def _repath(self, i):
        x = self.e[i]
        x["entry_path"] = self.e[x["parent_folder_id"]]["entry_path"] + "/" + x["entry_name"]
        for c in self._children(i):
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
            raise Q.QuadernoError("404", 404)
        m = re.fullmatch(r"/folders/([^/]+)/entries", p)
        if method == "GET" and m:
            kids = sorted(self._children(m.group(1)), key=lambda x: x["entry_name"])
            off, lim = int(qs.get("offset", 0)), int(qs.get("limit", 100))
            lim = min(lim, 2)  # force pagination
            return {"count": len(kids), "entry_list": [dict(x) for x in kids[off:off + lim]]}
        if method == "POST" and p == "/folders":
            return {"folder_id": self._new("folder", body["parent_folder_id"], body["folder_name"])}
        if method == "POST" and p == "/documents":
            return {"document_id": self._new("document", body["parent_folder_id"], body["file_name"])}
        m = re.fullmatch(r"/documents/([^/]+)/file", p)
        if m and method == "PUT":
            data = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            off, total = int(qs["offset_bytes"]), int(qs["total_bytes"])
            buf = self.files.setdefault(m.group(1), bytearray())
            assert len(buf) == off
            buf += data
            self.e[m.group(1)]["file_size"] = len(buf)
            done = len(buf) >= total
            return {"current_bytes": len(buf), "received_bytes": len(data),
                    "completed": "yes" if done else "no"}
        if m and method == "GET":
            stream_to.write(bytes(self.files[m.group(1)]))
            return len(self.files[m.group(1)])
        m = re.fullmatch(r"/(documents|folders)/([^/]+)", p)
        if m and method == "DELETE":
            i = m.group(2)
            if m.group(1) == "folders" and self._children(i):
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

    def paths(self):
        return sorted(x["entry_path"] for x in self.e.values())


def ns(**kw):
    return types.SimpleNamespace(**kw)


def mkpdf(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n" + text.encode() + b"\n%%EOF\n")


def expect_error(fn, frag):
    try:
        fn()
    except Q.QuadernoError as e:
        assert frag in str(e), (frag, str(e))
        print("  ok error:", e)
        return
    raise AssertionError("expected error containing " + frag)


tmp = tempfile.mkdtemp()
src = os.path.join(tmp, "src")
mkpdf(f"{src}/a.pdf", "A")
mkpdf(f"{src}/sub/b.pdf", "B")
mkpdf(f"{src}/sub/deep/c.pdf", "C")
mkpdf(f"{src}/Café.pdf", "NFD name")       # NFD on disk
os.makedirs(f"{src}/nopdfs"); open(f"{src}/nopdfs/x.txt", "w").write("x")
open(f"{src}/notes.txt", "w").write("ignored")

d = FakeDevice()
print("mkdir -p")
Q.cmd_mkdir(d, ns(remote=["T/x/y"], parents=True))
Q.cmd_mkdir(d, ns(remote=["T/x/y"], parents=True))  # idempotent
expect_error(lambda: Q.cmd_mkdir(d, ns(remote=["T/missing/z"], parents=False)), "404")

print("put -r into existing folder")
Q.cmd_put(d, ns(local=[src], remote="T", recursive=True))
assert "Document/T/src/sub/deep/c.pdf" in d.paths()
assert "Document/T/src/Café.pdf" in d.paths()          # stored NFC
assert not any("nopdfs" in p for p in d.paths()), d.paths()
print("put -r again -> all skipped")
Q.cmd_put(d, ns(local=[src], remote="T", recursive=True))
print("put -r to new path (rename dir)")
Q.cmd_put(d, ns(local=[src], remote="T/copy2", recursive=True))
assert "Document/T/copy2/sub/b.pdf" in d.paths()
expect_error(lambda: Q.cmd_put(d, ns(local=[src], remote="T", recursive=False)), "use -r")

print("get -r")
out = os.path.join(tmp, "out")
Q.cmd_get(d, ns(remote=["T/src"], output=out, recursive=True))
for rel in ["a.pdf", "sub/b.pdf", "sub/deep/c.pdf", "Café.pdf"]:
    got = open(os.path.join(out, "src", rel), "rb").read()
    assert got.startswith(b"%PDF"), rel
assert open(f"{out}/src/sub/deep/c.pdf", "rb").read() == open(f"{src}/sub/deep/c.pdf", "rb").read()
expect_error(lambda: Q.cmd_get(d, ns(remote=["T/src"], output=out, recursive=False)), "use -r")

print("mv folder")
Q.cmd_mv(d, ns(src="T/src/sub", dst="T/x"))            # into existing folder
assert "Document/T/x/sub/deep/c.pdf" in d.paths()
Q.cmd_mv(d, ns(src="T/x/sub", dst="T/renamed"))        # rename + move
assert "Document/T/renamed/deep/c.pdf" in d.paths()
expect_error(lambda: Q.cmd_mv(d, ns(src="T", dst="T/x/y")), "into itself")
expect_error(lambda: Q.cmd_mv(d, ns(src="/", dst="T")), "root")

print("rm guards")
expect_error(lambda: Q.cmd_rm(d, ns(remote=["T"], recursive=False)), "not empty")
expect_error(lambda: Q.cmd_rm(d, ns(remote=["/"], recursive=True)), "root")
print("rm -r")
Q.cmd_rm(d, ns(remote=["T"], recursive=True))
assert d.paths() == ["Document"], d.paths()
print("ALL FAKE-DEVICE TESTS PASSED")
