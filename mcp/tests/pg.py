"""A throwaway PostgreSQL for tests and evaluations: initdb in a temp dir, Unix socket only."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg


def _bindir() -> Path | None:
    """PostgreSQL's binaries: on PATH, or where Ubuntu and Homebrew keep them."""
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    for pattern in (
        "/usr/lib/postgresql/*/bin",
        "/opt/homebrew/opt/postgresql@*/bin",
        "/usr/local/opt/postgresql@*/bin",
    ):
        if hits := sorted(glob.glob(pattern)):
            return Path(hits[-1])
    return None


@contextmanager
def scratch_db(load: Callable[[psycopg.Connection], None], role: str) -> Iterator[str | None]:
    """A throwaway PostgreSQL, `load`ed as superuser; yields `role`'s DSN (None: no binaries)."""
    bindir = _bindir()
    if bindir is None:
        yield None
        return
    root = Path(tempfile.mkdtemp(prefix="mcp-pg-"))
    env = {**os.environ, "LC_ALL": "C"}  # macOS: postmaster refuses to start without a valid locale
    try:
        subprocess.run(
            [
                bindir / "initdb",
                "-D",
                root / "data",
                "-U",
                "postgres",
                "-A",
                "trust",
                "--no-sync",
                "-E",
                "UTF8",
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        (root / "sock").mkdir()
        subprocess.run(
            [
                bindir / "pg_ctl",
                "-D",
                root / "data",
                "-l",
                root / "log",
                "-w",
                "start",
                "-o",
                f"-k {root / 'sock'} -c listen_addresses='' -c fsync=off",
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        with psycopg.connect(
            f"host={root / 'sock'} user=postgres dbname=postgres", autocommit=True
        ) as conn:
            load(conn)
        yield f"host={root / 'sock'} user={role} dbname=postgres"
    finally:
        subprocess.run(
            [bindir / "pg_ctl", "-D", root / "data", "-m", "immediate", "stop"], capture_output=True
        )
        shutil.rmtree(root, ignore_errors=True)
