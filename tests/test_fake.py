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


def _search_ns(keyword: str, **kw: Any) -> Q.Args:
    kw = {"target": "meta", "notes": False, "docs": False, "sort": None, "desc": False,
          "mark": "all", "long": False, **kw}
    return ns(keyword=keyword, **kw)


def test_search_cli(fake: FakeDevice, capsys: pytest.CaptureFixture[str]) -> None:
    fake.put_doc("Books/Rust.pdf", b"R")
    fake.put_doc("Note/misc.pdf", b"M")
    Q.cmd_search(fake, _search_ns("rust"))
    assert capsys.readouterr().out == "Document/Books/Rust.pdf\n"
    Q.cmd_search(fake, _search_ns("zzz"))
    assert "no matches for 'zzz'" in capsys.readouterr().err


def test_cp_cli(loaded: FakeDevice) -> None:
    Q.cmd_cp(loaded, ns(src="T/src/a.pdf", dst="T/x"))
    assert "Document/T/x/a.pdf" in loaded.paths()


def test_viewing_cli(fake: FakeDevice, capsys: pytest.CaptureFixture[str]) -> None:
    fake.put_doc("Books/a.pdf", b"A")
    Q.cmd_viewing(fake, ns())
    assert "nothing is open" in capsys.readouterr().out
    Q.cmd_open(fake, ns(target="Books/a.pdf", page=2))
    capsys.readouterr()
    Q.cmd_viewing(fake, ns())
    out = capsys.readouterr().out
    assert "Document/Books/a.pdf" in out and "page 2 of 10" in out


def test_config_cli(fake: FakeDevice, capsys: pytest.CaptureFixture[str]) -> None:
    Q.cmd_config(fake, ns(key=None, value=None))
    assert "timezone" in capsys.readouterr().out
    Q.cmd_config(fake, ns(key="timeout_to_standby", value="120"))
    assert fake.get_config("timeout_to_standby") == "120"
    assert capsys.readouterr().out == "timeout_to_standby = 120\n"


def test_new_cli_commands(fake: FakeDevice, mkpdf: MakePdf, tmp_path: Path,
                          capsys: pytest.CaptureFixture[str]) -> None:
    fake.put_doc("Books/a.pdf", b"A")
    fake.annotations[(fake.resolve("Books/a.pdf").id, 1)] = b"\xaa\xbb"
    Q.cmd_ping(fake, ns())
    assert capsys.readouterr().out.strip() == "ok"
    Q.cmd_marks(fake, ns())
    assert capsys.readouterr().out.splitlines() == ["star", "asterisk"]
    Q.cmd_replace(fake, ns(file=mkpdf("new.pdf", "NEW"), remote_doc="Books/a.pdf", revision=None))
    assert "revision:" in capsys.readouterr().out
    ann = tmp_path / "ann.bin"
    Q.cmd_annotations(fake, ns(remote_doc="Books/a.pdf", page=1, output=str(ann)))
    assert ann.read_bytes() == b"\xaa\xbb"
    capsys.readouterr()
    Q.cmd_templates(fake, ns(action="add", file=mkpdf("tpl.pdf", "T"), name="Lined"))
    tid = capsys.readouterr().out.splitlines()[-1].strip()
    Q.cmd_templates(fake, ns(action="ls"))
    assert "Lined" in capsys.readouterr().out
    Q.cmd_templates(fake, ns(action="rename", template_id=tid, name="Ruled"))
    Q.cmd_templates(fake, ns(action="rm", template_id=tid))
    Q.cmd_wifi(fake, ns(action="status"))
    assert "HomeWiFi" in capsys.readouterr().out
    Q.cmd_wifi(fake, ns(action="add", ssid="Lab", security="psk", password="pw"))
    Q.cmd_wifi(fake, ns(action="ls"))
    assert "Lab" in capsys.readouterr().out
    Q.cmd_wifi(fake, ns(action="rm", ssid="Lab", security="psk"))
    Q.cmd_power(fake, ns(action="sleep"))
    Q.cmd_pen_buttons(fake, ns(action="ls"))
    assert "eraser" in capsys.readouterr().out
    Q.cmd_pen_buttons(fake, ns(action="set", button="side", function="highlight"))
    cert = tmp_path / "ca.pem"
    cert.write_bytes(b"CA")
    Q.cmd_certs(fake, ns(action="add-ca", name="TestCA", file=str(cert)))
    Q.cmd_certs(fake, ns(action="ls"))
    assert "TestCA" in capsys.readouterr().out
    Q.cmd_certs(fake, ns(action="clear", yes=True))
    pkg = tmp_path / "fw.pkg"
    pkg.write_bytes(b"DPUP" + b"\x00" * 16)
    Q.cmd_firmware(fake, ns(action="upload", file=str(pkg)))
    Q.cmd_firmware(fake, ns(action="precheck"))
    assert '"image_file": "ok"' in capsys.readouterr().out
    with expect("--yes"):
        Q.cmd_firmware(fake, ns(action="update", yes=False))
    Q.cmd_firmware(fake, ns(action="update", yes=True))
    plog = tmp_path / "past.bin"
    Q.cmd_pastlog(fake, ns(output=str(plog)))
    assert plog.read_bytes() == b"ENCRYPTED-PASTLOG"
    shot = tmp_path / "screen.jpg"
    Q.cmd_screenshot(fake, ns(output=str(shot), format="jpeg"))
    assert shot.read_bytes().startswith(b"\xff\xd8\xff")
    # Ensure argparse accepts every new subcommand
    parser = Q.build_parser()
    for argv in [
        ["ping"], ["marks"], ["replace", "a.pdf", "Books/a.pdf"],
        ["annotations", "Books/a.pdf", "1"], ["templates", "ls"],
        ["wifi", "scan"], ["power", "sleep"], ["firmware", "precheck"],
        ["pen-buttons", "ls"], ["certs", "ls"], ["pastlog"],
        ["screenshot", "--format", "jpeg"], ["search", "x", "-f", "Books"],
        ["config", "--all"],
    ]:
        assert parser.parse_args(argv, namespace=Q.Args()).cmd == argv[0]
