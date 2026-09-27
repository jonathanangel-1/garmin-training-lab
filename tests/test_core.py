"""Offline persistence invariants, failure recovery, and API-token isolation."""

import json
import stat
from pathlib import Path

import pytest

from garmin_training import core


def test_atomic_replacement_failure_preserves_complete_old_state_and_cleans_temp(
    tmp_path, monkeypatch
):
    destination = tmp_path / "private" / "state.json"
    original = {"athlete": "synthetic", "version": 1, "text": "naïve"}
    core.write_json(destination, original)
    old_bytes = destination.read_bytes()

    def failed_replace(source, target):
        assert destination.read_bytes() == old_bytes
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        raise OSError("Synthetic rename failure")

    monkeypatch.setattr(core.os, "replace", failed_replace)
    with pytest.raises(OSError, match="rename failure"):
        core.write_json(destination, {"version": 2, "private": "replacement"})

    assert core.read_json(destination) == original
    assert destination.read_bytes() == old_bytes
    assert list(destination.parent.glob(".write-*")) == []


def test_invalid_json_numbers_cannot_corrupt_existing_state(tmp_path):
    path = tmp_path / "private" / "state.json"
    core.write_json(path, {"version": 1})
    with pytest.raises(ValueError):
        core.write_json(path, {"measurement": float("nan")})
    assert core.read_json(path) == {"version": 1}
    assert list(path.parent.glob(".write-*")) == []


def test_private_writes_tighten_existing_permissions_and_roundtrip_unicode(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o755)
    path = parent / "state.json"
    path.write_text("{}")
    path.chmod(0o644)
    value = {"note": "Distance — 5 km", "observed": None, "steps": 0}

    core.write_json(path, value)

    assert core.read_json(path) == value
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == value


def test_final_component_symlinks_cannot_read_or_overwrite_an_unrelated_file(tmp_path):
    outside = tmp_path / "unrelated.json"
    outside.write_text('{"private":"unchanged"}')
    target = tmp_path / "state" / "record.json"
    target.parent.mkdir()
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic"):
        core.read_json(target)
    with pytest.raises(ValueError, match="symbolic"):
        core.write_json(target, {"private": "replacement"})

    assert outside.read_text() == '{"private":"unchanged"}'
    assert target.is_symlink()


def test_final_directory_symlink_is_rejected_without_changing_target_mode(tmp_path):
    outside = tmp_path / "unrelated"
    outside.mkdir(mode=0o755)
    before = stat.S_IMODE(outside.stat().st_mode)
    link = tmp_path / "state"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic"):
        core.ensure_private_dir(link)

    assert stat.S_IMODE(outside.stat().st_mode) == before


def test_api_token_survives_restarts_and_is_independent_across_state_directories(tmp_path):
    first_state, other_state = tmp_path / "first", tmp_path / "other"
    first = core.api_token(first_state)
    assert len(first) >= 32
    assert core.api_token(first_state) == first
    assert core.api_token(other_state) != first
    token_path = first_state / "api-token"
    token_path.chmod(0o644)
    assert core.api_token(first_state) == first
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_corrupt_existing_api_token_is_rejected_without_silent_rotation(tmp_path):
    state = core.ensure_private_dir(tmp_path / "state")
    path = state / "api-token"
    path.write_text("invalid-short-token\n")

    with pytest.raises(ValueError, match="Invalid local API token"):
        core.api_token(state)

    assert path.read_text() == "invalid-short-token\n"


def test_api_token_symlink_is_rejected_without_consuming_or_modifying_target(tmp_path):
    state = core.ensure_private_dir(tmp_path / "state")
    outside = tmp_path / "unrelated-token"
    outside.write_text("SYNTHETIC_UNRELATED_TOKEN_WITH_LONG_VALUE\n")
    path = state / "api-token"
    path.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic"):
        core.api_token(state)

    assert outside.read_text() == "SYNTHETIC_UNRELATED_TOKEN_WITH_LONG_VALUE\n"
    assert path.is_symlink()
