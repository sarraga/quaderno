"""Folder commands (mkdir -p, put -r, get -r, mv, rm -r) through the CLI command
functions, against the in-memory fake device."""
import os
from pathlib import Path
from typing import Any

import pytest
from fakedevice import FakeDevice, MakePdf, expect

from quaderno import cli as Q


def ns(**kw: Any) -> Q.Args:
    kw.setdefault("regex", False)
    kw.setdefault("yes", False)
    if isinstance(kw.get("src"), str):
        kw["src"] = [kw["src"]]
    return Q.Args(**kw)


@pytest.fixture
def src(mkpdf: MakePdf, tmp_path: Path) -> str:
    mkpdf("src/a.pdf", "A")
    mkpdf("src/sub/b.pdf", "B")
    mkpdf("src/sub/deep/c.pdf", "C")
    mkpdf("src/Café.pdf", "NFD name")                 # NFD on disk, as macOS may store it
    (tmp_path / "src" / "nopdfs").mkdir()
    (tmp_path / "src" / "nopdfs" / "x.txt").write_text("x")
    (tmp_path / "src" / "notes.txt").write_text("ignored")
    return str(tmp_path / "src")


@pytest.fixture
def loaded(src: str) -> FakeDevice:
    """A device with T/x/y and `src` uploaded as T/src."""
    d = FakeDevice()
    Q.cmd_mkdir(d, ns(remote=["T/x/y"], parents=True))
    Q.cmd_put(d, ns(local=[src], dest="T", recursive=True))
    return d


def test_mkdir_p(fake: FakeDevice) -> None:
    Q.cmd_mkdir(fake, ns(remote=["T/x/y"], parents=True))
    Q.cmd_mkdir(fake, ns(remote=["T/x/y"], parents=True))  # idempotent
    assert "Document/T/x/y" in fake.paths()
    with expect("404"):
        Q.cmd_mkdir(fake, ns(remote=["T/missing/z"], parents=False))


def test_put_r(loaded: FakeDevice, src: str, capsys: pytest.CaptureFixture[str]) -> None:
    d = loaded
    assert "Document/T/src/sub/deep/c.pdf" in d.paths()
    assert "Document/T/src/Café.pdf" in d.paths()      # stored NFC
    assert not any("nopdfs" in p for p in d.paths()), d.paths()
    before = d.paths()
    Q.cmd_put(d, ns(local=[src], dest="T", recursive=True))         # again: all skipped
    assert d.paths() == before
    Q.cmd_put(d, ns(local=[src], dest="T/copy2", recursive=True))   # new path: renames the dir
    assert "Document/T/copy2/sub/b.pdf" in d.paths()
    capsys.readouterr()
    Q.cmd_put(d, ns(local=[src], dest="T/copy3", recursive=True))
    assert "2 non-PDF ignored" in capsys.readouterr().out


def test_put_skips_bad_arguments_and_uploads_the_rest(fake: FakeDevice, src: str, mkpdf: MakePdf, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake.mkdirs("T")
    epub = tmp_path / "book.epub"
    epub.write_text("not a pdf")
    good = mkpdf("good.pdf", "G")
    rc = Q.cmd_put(fake, ns(local=[str(epub), good, src, str(tmp_path / "missing.pdf")],
                            dest="T", recursive=False))
    err = capsys.readouterr().err
    assert "Document/T/good.pdf" in fake.paths()
    for frag in ("book.epub: not a PDF", "src: is a directory (use -r)", "missing.pdf: no such file"):
        assert frag in err, frag
    assert rc == 1


@pytest.mark.parametrize("named,want", [
    (["book.epub"], 1),                 # not a PDF: nothing uploaded
    (["book.epub", "good.pdf"], 1),     # one rejected, one uploaded: still 1, as with cp
    (["src"], 1),                       # a folder without -r
    (["good.pdf"], None),               # all uploaded
])
def test_put_exit_status(named: list[str], want: int | None, fake: FakeDevice, src: str, mkpdf: MakePdf, tmp_path: Path) -> None:
    fake.mkdirs("T")
    (tmp_path / "book.epub").write_text("not a pdf")
    mkpdf("good.pdf", "G")
    assert Q.cmd_put(fake, ns(local=[str(tmp_path / n) for n in named], dest="T",
                              recursive=False)) == want


def test_put_exit_status_ignores_skips_and_non_pdfs_inside_folders(fake: FakeDevice, src: str, mkpdf: MakePdf) -> None:
    fake.mkdirs("T")
    good = mkpdf("good.pdf", "G")
    assert Q.cmd_put(fake, ns(local=[good], dest="T", recursive=False)) is None
    assert Q.cmd_put(fake, ns(local=[good], dest="T", recursive=False)) is None  # already there
    assert Q.cmd_put(fake, ns(local=[src], dest="T", recursive=True)) is None    # 2 non-PDF inside


def test_get_r(loaded: FakeDevice, src: str, tmp_path: Path) -> None:
    out = tmp_path / "out"
    Q.cmd_get(loaded, ns(remote=["T/src"], output=str(out), recursive=True))
    for rel in ["a.pdf", "sub/b.pdf", "sub/deep/c.pdf", "Café.pdf"]:
        assert (out / "src" / rel).read_bytes().startswith(b"%PDF"), rel
    assert (out / "src/sub/deep/c.pdf").read_bytes() == (Path(src) / "sub/deep/c.pdf").read_bytes()
    with expect("use -r"):
        Q.cmd_get(loaded, ns(remote=["T/src"], output=str(out), recursive=False))


def test_mv_folder(loaded: FakeDevice) -> None:
    d = loaded
    Q.cmd_mv(d, ns(src="T/src/sub", dst="T/x"))            # into existing folder
    assert "Document/T/x/sub/deep/c.pdf" in d.paths()
    Q.cmd_mv(d, ns(src="T/x/sub", dst="T/renamed"))        # rename + move
    assert "Document/T/renamed/deep/c.pdf" in d.paths()
    with expect("into itself"):
        Q.cmd_mv(d, ns(src="T", dst="T/x/y"))
    with expect("root"):
        Q.cmd_mv(d, ns(src="/", dst="T"))


def test_rm_r(loaded: FakeDevice) -> None:
    d = loaded
    with expect("not empty"):
        Q.cmd_rm(d, ns(remote=["T"], recursive=False))
    with expect("root"):
        Q.cmd_rm(d, ns(remote=["/"], recursive=True))
    Q.cmd_rm(d, ns(remote=["T"], recursive=True))
    assert d.paths() == ["Document"], d.paths()


def test_put_to_a_folder_that_does_not_exist(mkpdf: MakePdf) -> None:
    d = FakeDevice()
    d.mkdirs("Books/Military History")
    book = mkpdf("Submarine Torpedo Tactics - Edward Monroe Jones & Shawn S. Roderick.pdf", "sub")
    good = mkpdf("good.pdf", "G")
    before = d.paths()
    with expect("did you mean: 'Books/Military History'"):
        Q.cmd_put(d, ns(local=[book], dest="Military History", recursive=False))
    with expect("no folder Document/Military History"):
        Q.cmd_put(d, ns(local=[book, good], dest="Military History", recursive=False))
    assert d.paths() == before                             # nothing created
    # the full folder path works; & and . in the name are fine
    Q.cmd_put(d, ns(local=[book], dest="Books/Military History", recursive=False))
    assert f"Document/Books/Military History/{os.path.basename(book)}" in d.paths()
    # a new name ending in .pdf still renames on upload
    Q.cmd_put(d, ns(local=[good], dest="Books/renamed.pdf", recursive=False))
    assert "Document/Books/renamed.pdf" in d.paths()


def test_failed_upload_removes_the_half_created_document(mkpdf: MakePdf) -> None:
    d = FakeDevice()
    d.mkdirs("Books")
    good = mkpdf("good.pdf", "G")
    d.fail_upload = True
    before = d.paths()
    with expect("simulated failure"):
        Q.cmd_put(d, ns(local=[good], dest="Books", recursive=False))
    assert d.paths() == before, set(d.paths()) ^ set(before)
