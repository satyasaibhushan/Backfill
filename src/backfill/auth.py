import os
import secrets
import stat
from pathlib import Path


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("data directory must be a real directory owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("data directory must have mode 0700")


def read_secret(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("credential must be a regular file owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("credential file must have mode 0600")
        value = stream.read().strip()
    if len(value) < 32:
        raise RuntimeError("invalid credential file")
    return value


def write_secret(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value + "\n")


def owner_token(root: Path) -> str:
    private_directory(root)
    path = root / "owner.token"
    try:
        write_secret(path, secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return read_secret(path)
