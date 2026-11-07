#!/usr/bin/env python3
"""The Python API used directly (no CLI), against the in-memory fake device."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakedevice import FakeDevice  # noqa: E402

import quaderno  # noqa: E402
from quaderno import Entry, NotFoundError, PutResult, QuadernoError  # noqa: E402

tmp = tempfile.mkdtemp()


def local_pdf(name, text="x"):
    path = os.path.join(tmp, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n" + text.encode() + b"\n%%EOF\n")
    return path


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


d = FakeDevice()
d.put_doc("Books/a.pdf", b"A")
d.put_doc("Books/Old/b.pdf", b"B")
d.put_doc("Note/n.pdf", b"N")
events = []
on = events.append

print("public surface")
assert callable(quaderno.connect) and callable(quaderno.pair) and quaderno.__version__
assert issubclass(NotFoundError, QuadernoError)

print("lookup: resolve / try_resolve / entries / walk / find / ls")
a = d.resolve("Books/a.pdf")
assert isinstance(a, Entry) and a.is_document and a.relpath == "Books/a.pdf" and a.size == 1
assert a.revision and a.mtime and a.parent_id == d.resolve("Books").id
assert d.try_resolve("nope.pdf") is None
raises(NotFoundError, lambda: d.resolve("nope.pdf"))
assert [e.name for e in d.entries("Books")] == ["Old", "a.pdf"]
assert [e.relpath for e in d.walk("Books")] == ["Books/Old", "Books/Old/b.pdf", "Books/a.pdf"]
found, by_pattern = d.find("*/*.pdf")
assert by_pattern and [e.relpath for e in found] == ["Books/a.pdf", "Note/n.pdf"]
rows, by_pattern = d.ls("Books")
assert not by_pattern and [e.name for e in rows] == ["Old", "a.pdf"]  # folders first
raises(NotFoundError, lambda: d.find("zz*"))

print("upload / put: results and events, never overwrites")
e = d.upload(local_pdf("c.pdf"), "Books", on_event=on)
assert e.path == "Document/Books/c.pdf" and events[-1].kind == "uploaded"
raises(QuadernoError, lambda: d.upload(local_pdf("c.pdf"), "Books"))      # exists
res = d.put([local_pdf("c.pdf"), local_pdf("d.pdf"), os.path.join(tmp, "missing.pdf")], "Books", on_event=on)
assert res == PutResult(uploaded=1, skipped=1, missing=1), res
assert [ev.kind for ev in events[-3:]] == ["skipped", "skipped", "uploaded"] or \
       sorted(ev.kind for ev in events[-3:]) == ["skipped", "skipped", "uploaded"]
assert any(ev.warning and "missing.pdf: no such file" in ev.message for ev in events)
local_pdf("tree/x.pdf"); local_pdf("tree/sub/y.pdf")
open(os.path.join(tmp, "tree", "notes.txt"), "w").write("t")
res = d.put(os.path.join(tmp, "tree"), "Books", recursive=True, on_event=on)
assert res.uploaded == 2 and res.ignored == 1 and "Document/Books/tree/sub/y.pdf" in d.paths()

print("get: files and folders")
out = os.path.join(tmp, "out")
assert d.get("Books/a.pdf", os.path.join(tmp, "a-copy.pdf")) == 1
assert open(os.path.join(tmp, "a-copy.pdf"), "rb").read() == b"A"
assert d.get("Books/Old", out, recursive=True) == 1 and os.path.exists(os.path.join(out, "Old", "b.pdf"))
raises(QuadernoError, lambda: d.get("Books/Old", out))                     # folder without recursive
assert int(os.stat(os.path.join(tmp, "a-copy.pdf")).st_mtime) == a.mtime

print("mkdir / move")
d.mkdir("Archive/2026", parents=True, on_event=on)
assert d.resolve("Archive/2026").is_folder
moved = d.move("Books/*.pdf", "Archive/2026", on_event=on)
assert sorted(old for old, new in moved) == ["Document/Books/a.pdf", "Document/Books/c.pdf", "Document/Books/d.pdf"]
assert all(new.startswith("Document/Archive/2026/") for old, new in moved)
assert d.move("Archive/2026/a.pdf", "Archive/2026/renamed.pdf") == [
    ("Document/Archive/2026/a.pdf", "Document/Archive/2026/renamed.pdf")]

print("rm: checks first, confirm callback for patterns")
raises(QuadernoError, lambda: d.rm("Books/Old"))                          # not empty, not recursive
before = d.paths()
assert d.rm("Archive/2026/*", confirm=lambda plan: False) == [] and d.paths() == before
seen = []
deleted = d.rm("Archive/2026/*", confirm=lambda plan: seen.extend(plan) or True, on_event=on)
assert len(deleted) == 3 and len(seen) == 3 and not d.entries("Archive/2026")
assert d.rm("Books/Old", recursive=True)[0].relpath == "Books/Old"        # literal: no confirm needed
raises(QuadernoError, lambda: d.rm("/", recursive=True))

print("device: open / set_time / timezone / info")
opened = d.open("Note/n*", page=3)
assert opened.relpath == "Note/n.pdf" and d.opened[-1] == (opened.id, 3)
import datetime  # noqa: E402
when = datetime.datetime(2026, 11, 7, 12, 30, 5, tzinfo=datetime.timezone.utc)
assert d.set_time(when) == "2026-11-07T12:30:05Z" == d.clock_set
assert d.timezone() == "UTC"
info = d.info()
assert info["firmware_version"] == {"value": "fake"} and "error" in info["battery"]

print("events carry kind, message, path, warning")
assert {"uploaded", "skipped", "created", "moved", "deleted"} <= {ev.kind for ev in events}
assert all(ev.message for ev in events)
print("ALL API TESTS PASSED")
