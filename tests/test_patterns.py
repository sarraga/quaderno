"""Glob / regex remote paths in ls, get, mv, rm, open against the in-memory fake device."""
import builtins
import contextlib
import io
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fakedevice import FakeDevice, expect

from quaderno import cli as Q

LONG = ("Fighter Combat - Tactics and Maneuvering -- Robert L_ Shaw -- 1988 -- Naval Institute "
        "Press -- 9780870210594 -- cdf446d4aa8b1e83b4cd4dc824de74f8 -- Anna’s Archive.pdf")


def ns(**kw: Any) -> Q.Args:
    kw.setdefault("regex", False)
    kw.setdefault("yes", False)
    kw.setdefault("recursive", False)
    return Q.Args(**kw)


def run(fn: Callable[[FakeDevice, Q.Args], object], d: FakeDevice, **kw: Any) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(d, ns(**kw))
    return out.getvalue()


class Stdin:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


@pytest.fixture
def d() -> FakeDevice:
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


def test_move_long_name_with_a_short_glob(d: FakeDevice) -> None:
    out = run(Q.cmd_mv, d, src=["Fighter*"], dst="Books/")
    assert f"Document/Books/{LONG}" in d.paths(), out


def test_ls_patterns(d: FakeDevice) -> None:
    """Case-insensitive; folders listed like ls -d."""
    assert run(Q.cmd_ls, d, path="b*", long=False).split() == ["Document/Bonus/", "Document/Books/"]
    out = run(Q.cmd_ls, d, path="Note/2024-0?*", long=False)
    assert out.strip().splitlines() == ["Document/Note/2024-05 meeting.pdf",
                                        "Document/Note/2024-06 meeting.pdf"], out
    out = run(Q.cmd_ls, d, path="*/2024-05*", long=False)       # pattern in a later component
    assert out.strip() == "Document/Note/2024-05 meeting.pdf", out
    out = run(Q.cmd_ls, d, path="*/*/*deep*", long=False)       # patterns in several components
    assert out.strip() == "Document/Note/sub/2024-05 deep.pdf", out


def test_exact_names_win(d: FakeDevice) -> None:
    """Brackets and parentheses are taken literally when the file exists."""
    assert run(Q.cmd_ls, d, path="weird [draft].pdf", long=False).strip() == "weird [draft].pdf"
    assert run(Q.cmd_ls, d, path="DHS660-UG (1).pdf", long=False).strip() == "DHS660-UG (1).pdf"


def test_regex(d: FakeDevice) -> None:
    """-E: searched anywhere in the name, case-insensitive."""
    d.dev_move(LONG, f"Books/{LONG}")
    out = run(Q.cmd_ls, d, path=r"Note/^2024-0[56] meet", long=False, regex=True)
    assert len(out.strip().splitlines()) == 2, out
    out = run(Q.cmd_ls, d, path=r"Books/naval institute", long=False, regex=True)
    assert out.strip() == f"Document/Books/{LONG}", out
    with expect("bad regular expression"):
        run(Q.cmd_ls, d, path="Note/(", long=False, regex=True)


def test_get_downloads_every_match(d: FakeDevice, tmp_path: Path) -> None:
    run(Q.cmd_get, d, remote=["Note/*meeting*"], output=str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == ["2024-05 meeting.pdf", "2024-06 meeting.pdf"]
    with expect("is a folder"):
        run(Q.cmd_get, d, remote=["B*"], output=str(tmp_path))


def test_mv_several_matches_into_a_folder(d: FakeDevice) -> None:
    with expect("must be an existing folder"):
        run(Q.cmd_mv, d, src=["Note/*meeting*"], dst="Archive")
    run(Q.cmd_mv, d, src=["Note/*meeting*"], dst="Bonus")
    assert {"Document/Bonus/2024-05 meeting.pdf", "Document/Bonus/2024-06 meeting.pdf"} <= set(d.paths())
    assert "already there" in run(Q.cmd_mv, d, src=["Bonus/2024-05*"], dst="Bonus")


def test_rm_patterns(d: FakeDevice, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lists matches; without a TTY it refuses unless --yes."""
    monkeypatch.setattr(sys, "stdin", Stdin(tty=False))
    d.dev_move("Note/2024-05 meeting.pdf", "Bonus/2024-05 meeting.pdf")
    d.dev_move("Note/2024-06 meeting.pdf", "Bonus/2024-06 meeting.pdf")
    with expect("add --yes"):
        run(Q.cmd_rm, d, remote=["Bonus/*"])
    assert "Document/Bonus/2024-05 meeting.pdf" in d.paths()  # nothing deleted
    run(Q.cmd_rm, d, remote=["Bonus/*"], yes=True)
    assert not any(p.startswith("Document/Bonus/") for p in d.paths())
    # a literal path needs no confirmation
    run(Q.cmd_rm, d, remote=["weird [draft].pdf"])
    assert "Document/weird [draft].pdf" not in d.paths()
    # non-empty folder matched without -r: refused before anything is deleted
    before = d.paths()
    with expect("not empty"):
        run(Q.cmd_rm, d, remote=["Note", "DHS660*"], yes=True)
    assert d.paths() == before
    # -r with overlapping matches deletes each thing once
    run(Q.cmd_rm, d, remote=["Note", "Note/sub/*"], recursive=True, yes=True)
    assert not any(p.startswith("Document/Note") for p in d.paths())
    # rm -r '*' without --yes and without a TTY deletes nothing
    before = d.paths()
    with expect("add --yes"):
        run(Q.cmd_rm, d, remote=["*"], recursive=True)
    assert d.paths() == before


def test_rm_asks_at_a_terminal(d: FakeDevice, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", Stdin(tty=True))
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
    out = run(Q.cmd_rm, d, remote=["DHS*"])
    assert "nothing deleted" in out and "Document/DHS660-UG (1).pdf" in d.paths(), out
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    run(Q.cmd_rm, d, remote=["DHS*"])
    assert "Document/DHS660-UG (1).pdf" not in d.paths()


def test_open_needs_exactly_one_document(d: FakeDevice) -> None:
    run(Q.cmd_open, d, target="fighter*", page=None)
    assert d.opened == [(d._at(LONG)["entry_id"], None)]
    with expect("matches 2 documents"):
        run(Q.cmd_open, d, target="Note/*meeting*", page=None)
    with expect("is a folder"):
        run(Q.cmd_open, d, target="B*", page=None)


def test_leading_and_trailing_spaces_are_kept(d: FakeDevice) -> None:
    """The device had a folder named 'sailing ' (trailing space)."""
    d.put_doc("Books/sailing /Ocean Passages.pdf", b"op")
    d.put_doc("Books/ leading.pdf", b"lead")
    assert run(Q.cmd_ls, d, path="Books/sailing /", long=False).strip() == "Ocean Passages.pdf"
    out = run(Q.cmd_ls, d, path="Books/ leading.pdf", long=False)
    assert out.strip() == "leading.pdf" or out.rstrip("\n") == " leading.pdf", repr(out)
    with expect("404"):                                         # no space: a different name
        run(Q.cmd_ls, d, path="Books/sailing", long=False)
    assert run(Q.cmd_ls, d, path="Books/sail*", long=False).strip() == "Document/Books/sailing /"
    d.ensure_folder("Books/sailing /Charts")
    assert "Document/Books/sailing /Charts" in d.paths()


def test_no_match_and_not_found(d: FakeDevice) -> None:
    with expect("no match"):
        run(Q.cmd_ls, d, path="zzz*", long=False)
    with expect("404"):
        run(Q.cmd_ls, d, path="missing.pdf", long=False)
