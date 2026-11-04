#!/usr/bin/env python3
"""`sync` against the in-memory fake device: incremental download, device-side
edits/moves/deletes, and never clobbering local changes."""
import contextlib
import io
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakedevice import FakeDevice, Q  # noqa: E402


def sync(d, local, remote="Document", **kw):
    args = types.SimpleNamespace(local=local, remote=remote, delete=kw.get("delete", False),
                                 dry_run=kw.get("dry_run", False), force=kw.get("force", False))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        Q.cmd_sync(d, args)
    text = out.getvalue()
    print("   " + text.strip().replace("\n", "\n   "))
    return text


def read(path):
    with open(path, "rb") as f:
        return f.read()


def files(root):
    out = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            if f != Q.SYNC_STATE:
                out.append(os.path.relpath(os.path.join(dp, f), root))
    return sorted(out)


def expect_error(fn, frag):
    try:
        fn()
    except Q.QuadernoError as e:
        assert frag in str(e), (frag, str(e))
        print("   ok error:", e)
        return
    raise AssertionError("expected error containing " + frag)


tmp = tempfile.mkdtemp()
L = os.path.join(tmp, "mirror")
d = FakeDevice()
d.put_doc("a.pdf", b"A1")
d.put_doc("Note/n1.pdf", b"N1")
d.put_doc("Note/deep/n2.pdf", b"N2")
d.put_doc("Books/b.pdf", b"B1")
d.mkdirs("Empty")

print("1. first sync downloads everything, sets mtimes, writes state")
sync(d, L)
assert files(L) == ["Books/b.pdf", "Note/deep/n2.pdf", "Note/n1.pdf", "a.pdf"], files(L)
assert os.path.isdir(os.path.join(L, "Empty"))
e = d._by_path("a.pdf")
assert int(os.stat(os.path.join(L, "a.pdf")).st_mtime) == Q.parse_device_time(e["modified_date"])
assert d.downloads == 4

print("2. second sync: nothing downloaded")
out = sync(d, L)
assert d.downloads == 4 and "4 unchanged" in out

print("3. device edit -> update; device move -> local move without download")
d.edit_doc("Note/n1.pdf", b"N1 edited on device")
d.move("Books/b.pdf", "Archive/b-renamed.pdf")
out = sync(d, L)
assert read(os.path.join(L, "Note/n1.pdf")) == b"N1 edited on device"
assert read(os.path.join(L, "Archive/b-renamed.pdf")) == b"B1"
assert not os.path.exists(os.path.join(L, "Books/b.pdf"))
assert d.downloads == 5, d.downloads  # only n1 re-downloaded
assert "1 updated" in out and "1 moved" in out

print("4. device delete: kept locally by default, removed with --delete (empty dirs pruned)")
d.delete("Note/deep/n2.pdf")
out = sync(d, L)
assert os.path.exists(os.path.join(L, "Note/deep/n2.pdf")) and "1 gone" in out
out = sync(d, L, delete=True)
assert not os.path.exists(os.path.join(L, "Note/deep")) and "1 deleted" in out

print("5. local edit + device edit -> local copy kept, never overwritten")
with open(os.path.join(L, "a.pdf"), "wb") as f:
    f.write(b"my local notes")
d.edit_doc("a.pdf", b"A2")
out = sync(d, L)
assert read(os.path.join(L, "a.pdf")) == b"my local notes" and "KEEP" in out
print("   ...and a locally edited file deleted on device is kept even with --delete")
d.delete("a.pdf")
out = sync(d, L, delete=True)
assert read(os.path.join(L, "a.pdf")) == b"my local notes"

print("6. file deleted locally is restored")
os.remove(os.path.join(L, "Archive/b-renamed.pdf"))
sync(d, L)
assert read(os.path.join(L, "Archive/b-renamed.pdf")) == b"B1"

print("7. moved AND changed on device: new copy downloaded, stale copy removed")
d.move("Archive/b-renamed.pdf", "Books/b2.pdf")
d.edit_doc("Books/b2.pdf", b"B2")
sync(d, L)
assert read(os.path.join(L, "Books/b2.pdf")) == b"B2"
assert not os.path.exists(os.path.join(L, "Archive/b-renamed.pdf"))

print("8. dry run changes nothing")
d.put_doc("new.pdf", b"NEW")
before = (files(L), read(os.path.join(L, Q.SYNC_STATE)), d.downloads)
out = sync(d, L, dry_run=True)
assert "(dry run) get" in out and (files(L), read(os.path.join(L, Q.SYNC_STATE)), d.downloads) == before

print("9. names that collide on a case-insensitive Mac volume: second one skipped")
d.put_doc("Case.pdf", b"upper")
d.put_doc("case.pdf", b"lower")
out = sync(d, L)
assert "CLASH" in out and len([f for f in files(L) if f.lower() == "case.pdf"]) == 1

print("10. guards")
expect_error(lambda: sync(d, L, remote="Note"), "mirrors Document")
other = os.path.join(tmp, "other")
os.makedirs(other)
with open(os.path.join(other, "n1.pdf"), "wb") as f:
    f.write(b"unrelated")
expect_error(lambda: sync(d, other, remote="Note"), "not empty")
out = sync(d, other, remote="Note", force=True)
assert read(os.path.join(other, "n1.pdf")) == b"unrelated" and "exists locally" in out

print("11. state survives as JSON keyed by entry id")
st = json.load(open(os.path.join(L, Q.SYNC_STATE)))
assert st["remote"] == "Document" and all({"path", "rev", "size", "mtime"} <= set(v) for v in st["entries"].values())
print("ALL SYNC TESTS PASSED")
