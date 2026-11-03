"""Exclusive local file creation and restrictive POSIX/Windows permissions."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess


def no_symlinks(path: Path) -> Path:
    path = path.absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError("symlink paths are not allowed for security material")
    return path


def protect(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, check=True)
        match = re.search(r"S-1-\d+(?:-\d+)+", identity.stdout)
        if not match:
            raise RuntimeError("cannot resolve current Windows SID")
        rights = "(OI)(CI)F" if directory else "F"
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"*{match.group()}:{rights}"], capture_output=True, check=True)
    else:
        path.chmod(0o700 if directory else 0o600)


def new_private_directory(path: Path) -> Path:
    path = no_symlinks(path)
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    protect(path, directory=True)
    return path


def write_new_private(path: Path, data: bytes) -> None:
    path = no_symlinks(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        protect(path)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def read_private(path: Path, *, max_bytes: int = 16384) -> bytes:
    path = no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("security material must be a regular unlinked file")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise PermissionError("security material must be owned by this user with mode 0600")
    if info.st_size > max_bytes:
        raise ValueError("security file exceeds size limit")
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError("security file exceeds size limit")
    return data
