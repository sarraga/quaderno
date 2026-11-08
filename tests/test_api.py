"""The Python API used directly (no CLI), against the in-memory fake device."""
import datetime

import pytest
from fakedevice import FakeDevice

import quaderno
from quaderno import (Entry, ExistsError, NotEmptyError, NotFoundError, PutResult,
                      QuadernoError)


@pytest.fixture
def d():
    d = FakeDevice()
    d.put_doc("Books/a.pdf", b"A")
    d.put_doc("Books/Old/b.pdf", b"B")
    d.put_doc("Note/n.pdf", b"N")
    return d


def test_public_surface():
    assert callable(quaderno.connect) and callable(quaderno.pair) and quaderno.__version__
    assert issubclass(NotFoundError, QuadernoError)


def test_lookup(d):
    a = d.resolve("Books/a.pdf")
    assert isinstance(a, Entry) and a.is_document and a.relpath == "Books/a.pdf" and a.size == 1
    assert a.revision and a.mtime and a.parent_id == d.resolve("Books").id
    assert d.try_resolve("nope.pdf") is None
    with pytest.raises(NotFoundError):
        d.resolve("nope.pdf")
    assert [e.name for e in d.entries("Books")] == ["Old", "a.pdf"]
    assert [e.relpath for e in d.walk("Books")] == ["Books/Old", "Books/Old/b.pdf", "Books/a.pdf"]
    found, by_pattern = d.find("*/*.pdf")
    assert by_pattern and [e.relpath for e in found] == ["Books/a.pdf", "Note/n.pdf"]
    rows, by_pattern = d.ls("Books")
    assert not by_pattern and [e.name for e in rows] == ["Old", "a.pdf"]  # folders first
    with pytest.raises(NotFoundError):
        d.find("zz*")


def test_upload_and_put(d, mkpdf, tmp_path):
    events = []
    e = d.upload(mkpdf("c.pdf"), "Books", on_event=events.append)
    assert e.path == "Document/Books/c.pdf" and events[-1].kind == "uploaded"
    with pytest.raises(ExistsError):                                     # never overwrites
        d.upload(mkpdf("c.pdf"), "Books")
    res = d.put([mkpdf("c.pdf"), mkpdf("d.pdf"), str(tmp_path / "missing.pdf")], "Books",
                on_event=events.append)
    assert res == PutResult(uploaded=1, skipped=1, missing=1), res
    assert sorted(ev.kind for ev in events[-3:]) == ["skipped", "skipped", "uploaded"]
    assert any(ev.warning and "missing.pdf: no such file" in ev.message for ev in events)
    assert all(ev.message for ev in events)
    mkpdf("tree/x.pdf")
    mkpdf("tree/sub/y.pdf")
    (tmp_path / "tree" / "notes.txt").write_text("t")
    res = d.put(str(tmp_path / "tree"), "Books", recursive=True)
    assert res.uploaded == 2 and res.ignored == 1 and "Document/Books/tree/sub/y.pdf" in d.paths()


def test_get(d, tmp_path):
    copy = tmp_path / "a-copy.pdf"
    assert d.get("Books/a.pdf", str(copy)) == 1
    assert copy.read_bytes() == b"A"
    assert int(copy.stat().st_mtime) == d.resolve("Books/a.pdf").mtime
    out = tmp_path / "out"
    assert d.get("Books/Old", str(out), recursive=True) == 1 and (out / "Old" / "b.pdf").exists()
    with pytest.raises(QuadernoError, match="is a folder"):              # folder without recursive
        d.get("Books/Old", str(out))


def test_mkdir_and_move(d):
    events = []
    d.put_doc("Books/c.pdf", b"C")
    d.put_doc("Books/d.pdf", b"D")
    d.mkdir("Archive/2026", parents=True, on_event=events.append)
    assert d.resolve("Archive/2026").is_folder
    moved = d.move("Books/*.pdf", "Archive/2026", on_event=events.append)
    assert sorted(old for old, new in moved) == [
        "Document/Books/a.pdf", "Document/Books/c.pdf", "Document/Books/d.pdf"]
    assert all(new.startswith("Document/Archive/2026/") for old, new in moved)
    assert d.move("Archive/2026/a.pdf", "Archive/2026/renamed.pdf") == [
        ("Document/Archive/2026/a.pdf", "Document/Archive/2026/renamed.pdf")]
    assert {"created", "moved"} <= {ev.kind for ev in events}


def test_rm(d):
    for n in "xyz":
        d.put_doc(f"Archive/2026/{n}.pdf", n.encode())
    with pytest.raises(NotEmptyError):                                   # not recursive
        d.rm("Books/Old")
    before = d.paths()
    assert d.rm("Archive/2026/*", confirm=lambda plan: False) == [] and d.paths() == before
    seen, events = [], []
    deleted = d.rm("Archive/2026/*", confirm=lambda plan: seen.extend(plan) or True,
                   on_event=events.append)
    assert len(deleted) == 3 and len(seen) == 3 and not d.entries("Archive/2026")
    assert "deleted" in {ev.kind for ev in events}
    assert d.rm("Books/Old", recursive=True)[0].relpath == "Books/Old"   # literal: no confirm needed
    with pytest.raises(QuadernoError, match="root"):
        d.rm("/", recursive=True)


def test_device_controls(d):
    opened = d.open("Note/n*", page=3)
    assert opened.relpath == "Note/n.pdf" and d.opened[-1] == (opened.id, 3)
    when = datetime.datetime(2026, 11, 7, 12, 30, 5, tzinfo=datetime.timezone.utc)
    assert d.set_time(when) == "2026-11-07T12:30:05Z" == d.clock_set
    assert d.timezone() == "UTC"
    info = d.info()
    assert info["firmware_version"] == {"value": "fake"} and "error" in info["battery"]
