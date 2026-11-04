#!/usr/bin/env python3
"""Folder commands (mkdir -p, put -r, get -r, mv, rm -r) against the in-memory fake device."""
import io, json, os, re, sys, tempfile, types, uuid
from urllib.parse import unquote, urlsplit, parse_qs

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quaderno as Q


from fakedevice import FakeDevice  # noqa: E402


def ns(**kw):
    kw.setdefault("regex", False)
    kw.setdefault("yes", False)
    if isinstance(kw.get("src"), str):
        kw["src"] = [kw["src"]]
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
print("put with bad arguments mixed in: skip each with a stderr line, upload the rest")
import contextlib  # noqa: E402
epub = os.path.join(tmp, "book.epub")
open(epub, "w").write("not a pdf")
good = os.path.join(tmp, "good.pdf")
mkpdf(good, "G")
err, out = io.StringIO(), io.StringIO()
with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
    rc = Q.cmd_put(d, ns(local=[epub, good, src, os.path.join(tmp, "missing.pdf")],
                         remote="T", recursive=False))
print("  ", err.getvalue().strip().replace("\n", "\n   "))
assert "Document/T/good.pdf" in d.paths()
for frag in ("book.epub: not a PDF", "src: is a directory (use -r)", "missing.pdf: no such file"):
    assert frag in err.getvalue(), frag
assert rc == 1  # a named file was missing
with contextlib.redirect_stderr(io.StringIO()):
    assert Q.cmd_put(d, ns(local=[epub], remote="T", recursive=False)) is None  # only a skip: exit 0
print("put -r reports non-PDFs it ignored inside folders")
with contextlib.redirect_stdout(out):
    Q.cmd_put(d, ns(local=[src], remote="T/copy3", recursive=True))
assert "2 non-PDF ignored" in out.getvalue(), out.getvalue()

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
print("put -d to a folder that doesn't exist: clear error with suggestions, nothing created")
d2 = FakeDevice()
d2.mkdirs("Books/Military History")
book = os.path.join(tmp, "Submarine Torpedo Tactics - Edward Monroe Jones & Shawn S. Roderick.pdf")
mkpdf(book, "sub")
before = d2.paths()
with contextlib.redirect_stdout(io.StringIO()):
    expect_error(lambda: Q.cmd_put(d2, ns(local=[book], remote="Military History", recursive=False)),
                 "did you mean: 'Books/Military History'")
    expect_error(lambda: Q.cmd_put(d2, ns(local=[book, good], remote="Military History",
                                          recursive=False)), "no folder Document/Military History")
assert d2.paths() == before
print("...the full folder path works, & and . in the name are fine")
with contextlib.redirect_stdout(io.StringIO()):
    Q.cmd_put(d2, ns(local=[book], remote="Books/Military History", recursive=False))
assert f"Document/Books/Military History/{os.path.basename(book)}" in d2.paths()
print("...a new name ending in .pdf still renames on upload")
with contextlib.redirect_stdout(io.StringIO()):
    Q.cmd_put(d2, ns(local=[good], remote="Books/renamed.pdf", recursive=False))
assert "Document/Books/renamed.pdf" in d2.paths()

print("failed upload removes the half-created document")
d2.fail_upload = True
before = d2.paths()
expect_error(lambda: Q.cmd_put(d2, ns(local=[good], remote="Books", recursive=False)), "simulated failure")
assert d2.paths() == before, set(d2.paths()) ^ set(before)
print("ALL FAKE-DEVICE TESTS PASSED")
