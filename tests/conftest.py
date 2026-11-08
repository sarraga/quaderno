"""Shared fixtures. Everything under tests/test_*.py runs offline against the
in-memory fake device (tests/fakedevice.py); tests/hw_* need the real device and
are run by hand."""
import pytest
from fakedevice import FakeDevice


@pytest.fixture
def fake():
    return FakeDevice()


@pytest.fixture
def mkpdf(tmp_path):
    """mkpdf("dir/x.pdf", text) writes a small PDF under tmp_path; returns its path."""
    def make(rel, text="x"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n" + text.encode() + b"\n%%EOF\n")
        return str(path)
    return make
