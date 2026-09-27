"""Offline account-isolation and verified-session publication tests.

Every Garmin client and credential prompt is replaced; no user tokens or
network-capable methods are used by these tests.
"""

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from garmin_training import auth
from garmin_training.core import ensure_private_dir

SESSION = {"synthetic_session": "TEST_SESSION_ONLY"}


@pytest.fixture(autouse=True)
def no_real_login(monkeypatch):
    monkeypatch.setattr(auth, "Garmin", Mock(side_effect=AssertionError("Unexpected client")))
    monkeypatch.setattr(auth.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", Mock(return_value="athlete@example.invalid"))
    monkeypatch.setattr(auth.getpass, "getpass", Mock(return_value="SYNTHETIC_PASSWORD"))


def client(profile_id=101, display_name="sample-athlete", login_effect=None):
    return SimpleNamespace(
        profile_id=profile_id,
        display_name=display_name,
        login=Mock(side_effect=login_effect),
    )


def stage_tokens(folder):
    (Path(folder) / "garmin_tokens.json").write_text(json.dumps(SESSION))


def verify_staged_tokens(folder):
    assert json.loads((Path(folder) / "garmin_tokens.json").read_text()) == SESSION


def cached_state(state, profile_id=101, display_name="sample-athlete"):
    token_dir = ensure_private_dir(state / "garmin-tokens")
    stage_tokens(token_dir)
    auth._bind_account(client(profile_id, display_name), state)
    return token_dir


def test_successful_login_requires_fresh_client_to_verify_saved_session(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "state"
    credential_client = client(login_effect=stage_tokens)
    verified_client = client(login_effect=verify_staged_tokens)
    factory = Mock(side_effect=[credential_client, verified_client])
    monkeypatch.setattr(auth, "Garmin", factory)

    auth.login(state)

    assert factory.call_count == 2
    assert factory.call_args_list[1].args == ()
    assert factory.call_args_list[1].kwargs == {}
    staging = Path(credential_client.login.call_args.args[0])
    assert staging == Path(verified_client.login.call_args.args[0])
    assert not staging.exists()
    token_file = state / "garmin-tokens" / "garmin_tokens.json"
    assert json.loads(token_file.read_text()) == SESSION
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "account.json").stat().st_mode) == 0o600
    saved_text = "\n".join(path.read_text() for path in state.rglob("*") if path.is_file())
    output = capsys.readouterr()
    for secret in ("SYNTHETIC_PASSWORD", "athlete@example.invalid", "sample-athlete"):
        assert secret not in saved_text
        assert secret not in output.out + output.err


def test_login_rejects_missing_saved_tokens_without_creating_binding(tmp_path, monkeypatch):
    state = tmp_path / "state"
    first = client()
    factory = Mock(return_value=first)
    monkeypatch.setattr(auth, "Garmin", factory)

    with pytest.raises(RuntimeError, match="tokens were not saved"):
        auth.login(state)

    assert factory.call_count == 1
    assert not (state / "account.json").exists()
    assert not (state / "garmin-tokens" / "garmin_tokens.json").exists()
    assert list(state.glob(".garmin-login-*")) == []


def test_saved_but_rejected_session_is_never_published(tmp_path, monkeypatch):
    state = tmp_path / "state"
    first = client(login_effect=stage_tokens)
    rejected = client(login_effect=RuntimeError("Synthetic verification failure"))
    monkeypatch.setattr(auth, "Garmin", Mock(side_effect=[first, rejected]))

    with pytest.raises(RuntimeError, match="verification failure"):
        auth.login(state)

    assert not (state / "account.json").exists()
    assert not (state / "garmin-tokens" / "garmin_tokens.json").exists()
    assert list(state.glob(".garmin-login-*")) == []


def test_existing_connection_is_verified_without_collecting_credentials(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    token_dir = cached_state(state)
    loaded = client(login_effect=verify_staged_tokens)
    factory = Mock(return_value=loaded)
    monkeypatch.setattr(auth, "Garmin", factory)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("Credential prompt")))
    monkeypatch.setattr(
        auth.getpass, "getpass", Mock(side_effect=AssertionError("Password prompt"))
    )

    auth.login(state)

    factory.assert_called_once_with()
    loaded.login.assert_called_once_with(str(token_dir))


def test_cached_tokens_from_another_profile_cannot_rebind_existing_history(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    cached_state(state, profile_id=101, display_name="same-display-name")
    original_binding = (state / "account.json").read_bytes()
    wrong_athlete = client(profile_id=202, display_name="same-display-name")
    monkeypatch.setattr(auth, "Garmin", Mock(return_value=wrong_athlete))

    with pytest.raises(RuntimeError, match="another athlete"):
        auth.load_client(state)

    assert (state / "account.json").read_bytes() == original_binding


def test_same_profile_remains_bound_after_display_name_changes(tmp_path, monkeypatch):
    state = tmp_path / "state"
    cached_state(state, profile_id=101, display_name="old-display-name")
    renamed = client(profile_id=101, display_name="new-display-name")
    monkeypatch.setattr(auth, "Garmin", Mock(return_value=renamed))

    assert auth.load_client(state) is renamed


@pytest.mark.parametrize("profile_id", [None, 0, -1, True, "101", 1.5])
def test_cached_identity_requires_verified_positive_integer_profile_id(
    tmp_path, monkeypatch, profile_id
):
    state = tmp_path / "state"
    token_dir = ensure_private_dir(state / "garmin-tokens")
    stage_tokens(token_dir)
    unverified = client(profile_id=profile_id, display_name="plausible-display-name")
    monkeypatch.setattr(auth, "Garmin", Mock(return_value=unverified))

    with pytest.raises(RuntimeError, match="identity"):
        auth.load_client(state)

    assert not (state / "account.json").exists()


def test_new_credentials_cannot_replace_a_different_athletes_binding(tmp_path, monkeypatch):
    state = tmp_path / "state"
    auth._bind_account(client(profile_id=101), state)
    original_binding = (state / "account.json").read_bytes()
    first = client(profile_id=202, login_effect=stage_tokens)
    verified = client(profile_id=202, login_effect=verify_staged_tokens)
    monkeypatch.setattr(auth, "Garmin", Mock(side_effect=[first, verified]))

    with pytest.raises(RuntimeError, match="another athlete"):
        auth.login(state)

    assert (state / "account.json").read_bytes() == original_binding
    assert not (state / "garmin-tokens" / "garmin_tokens.json").exists()
    assert list(state.glob(".garmin-login-*")) == []


def test_noninteractive_login_stops_before_prompts_or_state_changes(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(auth.sys.stdin, "isatty", lambda: False)
    prompt = Mock(side_effect=AssertionError("Unexpected prompt"))
    monkeypatch.setattr("builtins.input", prompt)

    with pytest.raises(RuntimeError, match="interactive terminal"):
        auth.login(state)

    assert not state.exists()
    prompt.assert_not_called()


def test_missing_cached_connection_stops_before_client_construction(tmp_path, monkeypatch):
    factory = Mock(side_effect=AssertionError("No client should be constructed"))
    monkeypatch.setattr(auth, "Garmin", factory)
    with pytest.raises(RuntimeError, match="Connect Garmin first"):
        auth.load_client(tmp_path / "state")
    factory.assert_not_called()


def test_failed_token_publication_leaves_no_new_account_binding(tmp_path, monkeypatch):
    state = tmp_path / "state"
    first = client(login_effect=stage_tokens)
    verified = client(login_effect=verify_staged_tokens)
    monkeypatch.setattr(auth, "Garmin", Mock(side_effect=[first, verified]))
    real_replace = auth.os.replace

    def fail_token_publish(source, destination):
        if Path(destination).name == "garmin_tokens.json":
            raise OSError("Synthetic token publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(auth.os, "replace", fail_token_publish)
    with pytest.raises(OSError, match="publication failure"):
        auth.login(state)

    assert not (state / "garmin-tokens" / "garmin_tokens.json").exists()
    assert not (state / "account.json").exists()
    assert list(state.glob(".garmin-login-*")) == []


def test_failed_token_publication_preserves_preexisting_binding_byte_for_byte(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    auth._bind_account(client(profile_id=101), state)
    account_path = state / "account.json"
    original = json.loads(account_path.read_text())
    original["created_at"] = "2026-01-01T00:00:00Z"
    account_path.write_text(json.dumps(original))
    before = account_path.read_bytes()
    first = client(login_effect=stage_tokens)
    verified = client(login_effect=verify_staged_tokens)
    monkeypatch.setattr(auth, "Garmin", Mock(side_effect=[first, verified]))
    real_replace = auth.os.replace

    def fail_token_publish(source, destination):
        if Path(destination).name == "garmin_tokens.json":
            raise OSError("Synthetic token publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(auth.os, "replace", fail_token_publish)
    with pytest.raises(OSError, match="publication failure"):
        auth.login(state)

    assert account_path.read_bytes() == before
    assert not (state / "garmin-tokens" / "garmin_tokens.json").exists()
    assert list(state.glob(".garmin-login-*")) == []
