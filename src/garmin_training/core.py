"""Local state with private permissions and no implicit publication."""

import json
import os
import re
import secrets
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Private directories cannot be symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_text(path: Path, value: str) -> None:
    path = Path(path)
    parent = ensure_private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("Private files cannot be symbolic links")
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(value)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> object:
    if Path(path).is_symlink():
        raise ValueError("Private files cannot be symbolic links")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Invalid identifier")
    return value


def api_token(state_dir: Path) -> str:
    root = ensure_private_dir(state_dir)
    path = root / "api-token"
    if path.is_symlink():
        raise ValueError("API token cannot be a symbolic link")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text().strip()
        if len(token) < 32:
            raise ValueError("Invalid local API token")
        path.chmod(0o600)
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token
