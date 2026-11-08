"""`sync` against the in-memory fake device: incremental download, device-side
edits/moves/deletes, and never clobbering local changes."""
import contextlib
import io
import json
import os
import types

import pytest
from fakedevice import FakeDevice, Q, expect

from quaderno.sync import SYNC_STATE
from quaderno.util import parse_device_time


def sync(d, local, remote="Document", **kw):
    args = types.SimpleNamespace(local=local, remote=remote, delete=kw.get("delete", False),
                                 dry_run=kw.get("dry_run", False), force=kw.get("force", False))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        Q.cmd_sync(d, args)
    return out.getvalue()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def files(root):
    out = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            if f != SYNC_STATE:
                out.append(os.path.relpath(os.path.join(dp, f), root))
    return sorted(out)


@pytest.fixture
def d():
    d = FakeDevice()
    d.put_doc("a.pdf", b"A1")
    d.put_doc("Note/n1.pdf", b"N1")
    d.put_doc("Note/deep/n2.pdf", b"N2")
    d.put_doc("Books/b.pdf", b"B1")
    d.mkdirs("Empty")
    return d


def test_incremental_sync(d, tmp_path):
    """One mirror through a sequence of device-side and local changes."""
    L = str(tmp_path / "mirror")

    # first sync downloads everything, sets mtimes, writes state
    sync(d, L)
    assert files(L) == ["Books/b.pdf", "Note/deep/n2.pdf", "Note/n1.pdf", "a.pdf"], files(L)
    assert os.path.isdir(os.path.join(L, "Empty"))
    e = d._by_path("a.pdf")
    assert int(os.stat(os.path.join(L, "a.pdf")).st_mtime) == parse_device_time(e["modified_date"])
    assert d.downloads == 4

    # second sync: nothing downloaded
    out = sync(d, L)
    assert d.downloads == 4 and "4 unchanged" in out

    # device edit -> update; device move -> local move without download
    d.edit_doc("Note/n1.pdf", b"N1 edited on device")
    d.dev_move("Books/b.pdf", "Archive/b-renamed.pdf")
    out = sync(d, L)
    assert read(os.path.join(L, "Note/n1.pdf")) == b"N1 edited on device"
    assert read(os.path.join(L, "Archive/b-renamed.pdf")) == b"B1"
    assert not os.path.exists(os.path.join(L, "Books/b.pdf"))
    assert d.downloads == 5, d.downloads  # only n1 re-downloaded
    assert "1 updated" in out and "1 moved" in out

    # device delete: kept locally by default, removed with --delete (empty dirs pruned)
    d.dev_delete("Note/deep/n2.pdf")
    out = sync(d, L)
    assert os.path.exists(os.path.join(L, "Note/deep/n2.pdf")) and "1 gone" in out
    out = sync(d, L, delete=True)
    assert not os.path.exists(os.path.join(L, "Note/deep")) and "1 deleted" in out

    # local edit + device edit -> local copy kept, never overwritten
    with open(os.path.join(L, "a.pdf"), "wb") as f:
        f.write(b"my local notes")
    d.edit_doc("a.pdf", b"A2")
    out = sync(d, L)
    assert read(os.path.join(L, "a.pdf")) == b"my local notes" and "KEEP" in out
    # ...and a locally edited file deleted on device is kept even with --delete
    d.dev_delete("a.pdf")
    sync(d, L, delete=True)
    assert read(os.path.join(L, "a.pdf")) == b"my local notes"

    # file deleted locally is restored
    os.remove(os.path.join(L, "Archive/b-renamed.pdf"))
    sync(d, L)
    assert read(os.path.join(L, "Archive/b-renamed.pdf")) == b"B1"

    # moved AND changed on device: new copy downloaded, stale copy removed
    d.dev_move("Archive/b-renamed.pdf", "Books/b2.pdf")
    d.edit_doc("Books/b2.pdf", b"B2")
    sync(d, L)
    assert read(os.path.join(L, "Books/b2.pdf")) == b"B2"
    assert not os.path.exists(os.path.join(L, "Archive/b-renamed.pdf"))

    # dry run changes nothing
    d.put_doc("new.pdf", b"NEW")
    before = (files(L), read(os.path.join(L, SYNC_STATE)), d.downloads)
    out = sync(d, L, dry_run=True)
    assert "(dry run) get" in out
    assert (files(L), read(os.path.join(L, SYNC_STATE)), d.downloads) == before

    # state is JSON keyed by entry id
    st = json.load(open(os.path.join(L, SYNC_STATE)))
    assert st["remote"] == "Document"
    assert all({"path", "rev", "size", "mtime"} <= set(v) for v in st["entries"].values())


def test_names_that_clash_on_a_case_insensitive_volume(d, tmp_path):
    d.put_doc("Case.pdf", b"upper")
    d.put_doc("case.pdf", b"lower")
    L = str(tmp_path / "mirror")
    out = sync(d, L)
    assert "CLASH" in out and len([f for f in files(L) if f.lower() == "case.pdf"]) == 1


def test_guards(d, tmp_path):
    L = str(tmp_path / "mirror")
    sync(d, L)
    with expect("mirrors Document"):
        sync(d, L, remote="Note")
    other = tmp_path / "other"
    other.mkdir()
    (other / "n1.pdf").write_bytes(b"unrelated")
    with expect("not empty"):
        sync(d, str(other), remote="Note")
    out = sync(d, str(other), remote="Note", force=True)
    assert (other / "n1.pdf").read_bytes() == b"unrelated" and "exists locally" in out
