"""Offline tests for bash Tab completion (`completions/quaderno.bash`)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH_SCRIPT = ROOT / "completions" / "quaderno.bash"


def _run_bash_complete(tmp_path: Path, words: list[str], cword: int,
                       device_lines: list[str] | None = None) -> tuple[list[str], str]:
    """Run `_quaderno` in a real bash subshell with a stub `quaderno` on PATH.
    Returns `(COMPREPLY lines, logged __complete argv)`."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not installed")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    log_file = tmp_path / "complete.log"
    if log_file.exists():
        log_file.unlink()
    lines_body = "\n".join(device_lines or [])
    stub = stub_dir / "quaderno"
    stub.write_text(
        f"#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(log_file)!r}\n"
        f"cat <<'__QUADERNO_EOF__'\n"
        f"{lines_body}\n"
        f"__QUADERNO_EOF__\n"
    )
    stub.chmod(0o755)

    arr_init = " ".join(repr(w) for w in words)
    driver = (
        f"source {str(BASH_SCRIPT)!r}\n"
        f"COMP_WORDS=({arr_init})\n"
        f"COMP_CWORD={cword}\n"
        f"_quaderno\n"
        f"printf '%s\\n' \"${{COMPREPLY[@]}}\"\n"
    )
    env = {**os.environ, "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}"}
    res = subprocess.run([bash, "-c", driver], env=env, capture_output=True, text=True, check=True)
    replies = [line for line in res.stdout.splitlines() if line]
    logged = log_file.read_text().strip() if log_file.exists() else ""
    return replies, logged


def test_bash_completion_subcommands_and_options(tmp_path: Path) -> None:
    replies, _ = _run_bash_complete(tmp_path, ["quaderno", "sc"], 1)
    assert replies == ["screenshot"]

    replies, _ = _run_bash_complete(tmp_path, ["quaderno", "--wi"], 1)
    assert replies == ["--wifi"]

    replies, _ = _run_bash_complete(tmp_path, ["quaderno", "power", "s"], 2)
    assert sorted(replies) == ["shutdown", "sleep"]

    replies, _ = _run_bash_complete(tmp_path, ["quaderno", "search", "-t", "m"], 3)
    assert sorted(replies) == ["mark", "meta"]


def test_bash_completion_remote_paths_case_escaping_and_global_flags(tmp_path: Path) -> None:
    # Case-insensitive folder matching + forwarding --wifi and --config
    replies, logged = _run_bash_complete(
        tmp_path,
        ["quaderno", "--wifi", "--config", "/tmp/cfg", "ls", "bo"],
        5,
        device_lines=["Books/", "Note/", "Samples/"],
    )
    assert replies == ["Books/"]
    assert logged == "__complete --config /tmp/cfg --wifi -- bo"

    # Spaces escaped cleanly when unquoted, preserved cleanly when single-quoted
    replies, _ = _run_bash_complete(
        tmp_path,
        ["quaderno", "get", "Books/comp"],
        2,
        device_lines=["Books/Computer engineering/", "Books/Rust.pdf"],
    )
    assert replies == [r"Books/Computer\ engineering/"]

    replies, _ = _run_bash_complete(
        tmp_path,
        ["quaderno", "ls", "'Books/comp"],
        2,
        device_lines=["Books/Computer engineering/"],
    )
    assert replies == ["'Books/Computer engineering/"]

    # put -d passes --dirs to __complete
    replies, logged = _run_bash_complete(
        tmp_path,
        ["quaderno", "put", "a.pdf", "-d", "No"],
        4,
        device_lines=["Note/", "Books/"],
    )
    assert replies == ["Note/"]
    assert "--dirs" in logged

    # Glob patterns are skipped without invoking __complete
    replies, logged = _run_bash_complete(
        tmp_path,
        ["quaderno", "ls", "Books/*rust*"],
        2,
        device_lines=["Books/Rust.pdf"],
    )
    assert replies == []
    assert logged == ""
