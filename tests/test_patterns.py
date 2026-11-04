#!/usr/bin/env python3
"""Glob / regex remote paths in ls, get, mv, rm, open against the in-memory fake device."""
import contextlib
import io
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakedevice import FakeDevice, Q  # noqa: E402

LONG = ("Fighter Combat - Tactics and Maneuvering -- Robert L_ Shaw -- 1988 -- Naval Institute "
        "Press -- 9780870210594 -- cdf446d4aa8b1e83b4cd4dc824de74f8 -- Anna’s Archive.pdf")


def ns(**kw):
    kw.setdefault("regex", False)
    kw.setdefault("yes", False)
    kw.setdefault("recursive", False)
    return types.SimpleNamespace(**kw)


def run(fn, d, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(d, ns(**kw))
    return out.getvalue()


def expect_error(fn, frag):
    try:
        fn()
    except Q.QuadernoError as e:
        assert frag in str(e), (frag, str(e))
        print("   ok error:", str(e).splitlines()[0])
        return
    raise AssertionError("expected error containing " + frag)


def fresh():
    d = FakeDevice()
    d.put_doc(LONG, b"book")
    d.put_doc("DHS660-UG (1).pdf", b"manual")
    d.put_doc("weird [draft].pdf", b"literal brackets")
    d.put_doc("Books/Old.pdf", b"old")
    d.put_doc("Note/2024-05 meeting.pdf", b"m1")
    d.put_doc("Note/2024-06 meeting.pdf", b"m2")
    d.put_doc("Note/sub/2024-05 deep.pdf", b"deep")
    d.mkdirs("Bonus")
    return d


d = fresh()
print("1. user's case: move the long-named book into Books with a short glob")
out = run(Q.cmd_mv, d, src=["Fighter*"], dst="Books/")
assert f"Document/Books/{LONG}" in d.paths(), out
print("  ", out.strip())

print("2. ls with patterns: case-insensitive, folders listed like ls -d, -R descends")
out = run(Q.cmd_ls, d, path="b*", long=False)
assert out.split() == ["Document/Bonus/", "Document/Books/"], out
out = run(Q.cmd_ls, d, path="Note/2024-0?*", long=False)
assert out.strip().splitlines() == ["Document/Note/2024-05 meeting.pdf", "Document/Note/2024-06 meeting.pdf"], out
out = run(Q.cmd_ls, d, path="*/2024-05*", long=False)       # pattern in a later component
assert out.strip() == "Document/Note/2024-05 meeting.pdf", out
out = run(Q.cmd_ls, d, path="*/*/*deep*", long=False)       # patterns in several components
assert out.strip() == "Document/Note/sub/2024-05 deep.pdf", out

print("3. exact names win: brackets and parentheses are taken literally when the file exists")
out = run(Q.cmd_ls, d, path="weird [draft].pdf", long=False)
assert out.strip() == "weird [draft].pdf", out
out = run(Q.cmd_ls, d, path="DHS660-UG (1).pdf", long=False)
assert out.strip() == "DHS660-UG (1).pdf", out

print("4. regex (-E): searched anywhere in the name, case-insensitive")
out = run(Q.cmd_ls, d, path=r"Note/^2024-0[56] meet", long=False, regex=True)
assert len(out.strip().splitlines()) == 2, out
out = run(Q.cmd_ls, d, path=r"Books/naval institute", long=False, regex=True)
assert out.strip() == f"Document/Books/{LONG}", out
expect_error(lambda: run(Q.cmd_ls, d, path="Note/(", long=False, regex=True), "bad regular expression")

print("5. get with a pattern downloads every match")
tmp = tempfile.mkdtemp()
run(Q.cmd_get, d, remote=["Note/*meeting*"], output=tmp)
assert sorted(os.listdir(tmp)) == ["2024-05 meeting.pdf", "2024-06 meeting.pdf"]
expect_error(lambda: run(Q.cmd_get, d, remote=["B*"], output=tmp), "is a folder")

print("6. mv several matches into a folder; destination must exist")
expect_error(lambda: run(Q.cmd_mv, d, src=["Note/*meeting*"], dst="Archive"), "must be an existing folder")
run(Q.cmd_mv, d, src=["Note/*meeting*"], dst="Bonus")
assert "Document/Bonus/2024-05 meeting.pdf" in d.paths() and "Document/Bonus/2024-06 meeting.pdf" in d.paths()
out = run(Q.cmd_mv, d, src=["Bonus/2024-05*"], dst="Bonus")
assert "already there" in out

print("7. rm with a pattern: lists matches, refuses without a TTY unless --yes")
expect_error(lambda: run(Q.cmd_rm, d, remote=["Bonus/*"]), "add --yes")
assert "Document/Bonus/2024-05 meeting.pdf" in d.paths()  # nothing deleted
run(Q.cmd_rm, d, remote=["Bonus/*"], yes=True)
assert not any(p.startswith("Document/Bonus/") for p in d.paths())
print("   ...a literal path needs no confirmation")
run(Q.cmd_rm, d, remote=["weird [draft].pdf"])
assert "Document/weird [draft].pdf" not in d.paths()
print("   ...non-empty folder matched without -r: refused before anything is deleted")
before = d.paths()
expect_error(lambda: run(Q.cmd_rm, d, remote=["Note", "DHS660*"], yes=True), "not empty")
assert d.paths() == before
print("   ...-r with overlapping matches deletes each thing once")
run(Q.cmd_rm, d, remote=["Note", "Note/sub/*"], recursive=True, yes=True)
assert not any(p.startswith("Document/Note") for p in d.paths())
print("   ...rm -r '*' without --yes and without a TTY deletes nothing")
before = d.paths()
expect_error(lambda: run(Q.cmd_rm, d, remote=["*"], recursive=True), "add --yes")
assert d.paths() == before

print("   ...at a terminal it asks; 'n' keeps everything, 'y' deletes")
import builtins  # noqa: E402


class _TTY:
    def isatty(self):
        return True


real_stdin, real_input = Q.sys.stdin, builtins.input
try:
    Q.sys.stdin = _TTY()
    builtins.input = lambda prompt="": "n"
    out = run(Q.cmd_rm, d, remote=["DHS*"])
    assert "nothing deleted" in out and "Document/DHS660-UG (1).pdf" in d.paths(), out
    builtins.input = lambda prompt="": "y"
    run(Q.cmd_rm, d, remote=["DHS*"])
    assert "Document/DHS660-UG (1).pdf" not in d.paths()
finally:
    Q.sys.stdin, builtins.input = real_stdin, real_input

print("8. open: needs exactly one document")
d = fresh()
run(Q.cmd_open, d, remote="fighter*", page=None)
assert d.opened == [(d._by_path(LONG)["entry_id"], None)]
expect_error(lambda: run(Q.cmd_open, d, remote="Note/*meeting*", page=None), "matches 2 documents")
expect_error(lambda: run(Q.cmd_open, d, remote="B*", page=None), "is a folder")

print("9. no match / not found")
expect_error(lambda: run(Q.cmd_ls, d, path="zzz*", long=False), "no match")
expect_error(lambda: run(Q.cmd_ls, d, path="missing.pdf", long=False), "404")
print("ALL PATTERN TESTS PASSED")
