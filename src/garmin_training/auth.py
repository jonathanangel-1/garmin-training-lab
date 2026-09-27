"""Terminal-only Garmin authentication; HTTP endpoints never accept passwords."""

import getpass
import hashlib
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from garminconnect import Garmin

from .core import ensure_private_dir, read_json, write_json


def _bind_account(client: Garmin, state_dir: Path) -> None:
    identity = getattr(client, "profile_id", None)
    if type(identity) is not int or identity <= 0:
        raise RuntimeError("Garmin account identity could not be verified")
    fingerprint = hashlib.sha256(str(identity).encode()).hexdigest()
    path = Path(state_dir) / "account.json"
    if path.exists():
        if read_json(path).get("fingerprint") != fingerprint:
            raise RuntimeError("This state directory belongs to another athlete; use a new --state-dir")
        return
    write_json(path, {"identity_type": "profile_id", "fingerprint": fingerprint})


def load_client(state_dir: Path) -> Garmin:
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    token_dir = ensure_private_dir(Path(state_dir) / "garmin-tokens")
    if not (token_dir / "garmin_tokens.json").is_file():
        raise RuntimeError("Connect Garmin first with: gtl login")
    client = Garmin()
    client.login(str(token_dir))
    _bind_account(client, state_dir)
    return client


def login(state_dir: Path) -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("Garmin login needs an interactive terminal")
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    token_dir = ensure_private_dir(Path(state_dir) / "garmin-tokens")
    if (token_dir / "garmin_tokens.json").exists():
        load_client(state_dir)
        print("Existing Garmin connection verified. Use another --state-dir for another athlete.")
        return
    print("Enter Garmin credentials here. Password and verification code are hidden.")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    if not email or not password:
        raise ValueError("Email and password are required")
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: getpass.getpass("Verification code: ").strip(),
    )
    password = None
    temporary = Path(tempfile.mkdtemp(prefix=".garmin-login-", dir=token_dir.parent))
    try:
        client.login(str(temporary))
        token_file = temporary / "garmin_tokens.json"
        if not token_file.is_file():
            raise RuntimeError("Garmin accepted login but session tokens were not saved")
        verified = Garmin()
        verified.login(str(temporary))
        account_path = Path(state_dir) / "account.json"
        existing_binding = account_path.exists()
        _bind_account(verified, state_dir)
        try:
            token_file.chmod(0o600)
            os.replace(token_file, token_dir / "garmin_tokens.json")
        except BaseException:
            if not existing_binding:
                account_path.unlink(missing_ok=True)
            raise
    finally:
        shutil.rmtree(temporary)
    print("Garmin connected. Only local session tokens were saved.")
