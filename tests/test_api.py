"""Local API boundaries and job lifecycle, with no Garmin or model traffic."""

import stat
from datetime import date
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from garmin_training import api
from garmin_training.core import api_token, ensure_private_dir, read_json, write_json


@pytest.fixture
def service(tmp_path, monkeypatch):
    state = tmp_path / "state"
    calls = []

    def fake_load_client(root):
        assert root == state
        calls.append("login")
        return object()

    def fake_collect(client, destination, *, start, end, detail_start, max_details):
        calls.append(("sync", start, end, detail_start, max_details))
        assert isinstance(start, date)
        manifest = {
            "status": "complete",
            "created_at": "2026-09-27T00:00:00+00:00",
            "boundaries": {"start": start.isoformat(), "end": end.isoformat()},
            "counts": {"activities": 2},
            "calls": [{"raw": "PRIVATE_RAW_PAYLOAD"}],
            "errors": ["PRIVATE_INTERNAL_ERROR"],
        }
        write_json(destination / "manifest.json", manifest)
        return manifest

    def fake_evidence(snapshot, destination):
        calls.append(("evidence", snapshot.name))
        write_json(destination / "evidence.json", {"synthetic": True})
        return {"synthetic": True}

    def fake_analysis(*, evidence_path, goal, run_dir, **kwargs):
        calls.append(("analysis", goal))
        assert read_json(evidence_path) == {"synthetic": True}
        final = {"synthetic": True, "summary": "Local mocked analysis", "goal": goal}
        write_json(run_dir / "final.json", final)
        return final

    monkeypatch.setattr(api, "load_client", fake_load_client)
    monkeypatch.setattr(api, "collect_snapshot", fake_collect)
    monkeypatch.setattr(api, "build_evidence", fake_evidence)
    monkeypatch.setattr(api, "run_analysis", fake_analysis)
    app = api.create_app(state)
    headers = {"Authorization": f"Bearer {api_token(state)}"}
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        yield state, client, headers, calls


def sync_body():
    return {"start": "2026-09-01", "end": "2026-09-27", "detail_start": "2026-09-10", "max_details": 3}


def test_health_is_the_only_public_api(service):
    state, client, headers, calls = service
    assert set(client.get("/health").json()) == {"app", "version"}
    for path in ("/v1/status", "/v1/snapshots", "/v1/jobs/missing", "/v1/unknown"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v1/sync", json=sync_body()).status_code == 401
    assert client.get("/v1/status", headers=headers).status_code == 200
    assert calls == []
    assert api_token(state) not in client.get("/v1/status", headers=headers).text
    assert client.get("/openapi.json").status_code == 404


def test_status_requires_actual_non_symlinked_garmin_token_file(service, tmp_path):
    state, client, headers, calls = service
    assert client.get("/v1/status", headers=headers).json()["garmin_tokens_present"] is False
    token_dir = ensure_private_dir(state / "garmin-tokens")
    assert client.get("/v1/status", headers=headers).json()["garmin_tokens_present"] is False
    token_file = token_dir / "garmin_tokens.json"
    write_json(token_file, {"synthetic": True})
    assert client.get("/v1/status", headers=headers).json()["garmin_tokens_present"] is True
    token_file.unlink()
    target = tmp_path / "synthetic-token.json"
    write_json(target, {"synthetic": True})
    token_file.symlink_to(target)
    assert client.get("/v1/status", headers=headers).json()["garmin_tokens_present"] is False
    assert calls == []


@pytest.mark.parametrize("origin", [
    "https://evil.example", "http://127.0.0.1:8766", "http://localhost:8765",
    "https://127.0.0.1:8765", "null", "http://127.0.0.1.evil.example:8765",
    "http://user@127.0.0.1:8765", "http://127.0.0.1:8765/path", "http://127.0.0.1:0",
])
def test_cross_origin_is_rejected_even_with_token(service, origin):
    _, client, headers, calls = service
    response = client.post("/v1/sync", headers={**headers, "Origin": origin}, json=sync_body())
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert calls == []


def test_same_origin_is_allowed_and_wrong_host_is_rejected(service):
    _, client, headers, _ = service
    assert client.get("/v1/status", headers={**headers, "Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/health", headers={"Origin": "https://evil.example"}).status_code == 403


def test_successful_jobs_and_private_persistence(service):
    state, client, headers, calls = service
    response = client.post("/v1/sync", headers=headers, json=sync_body())
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    completed = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
    assert completed["status"] == "completed"
    listing = client.get("/v1/snapshots", headers=headers)
    assert listing.json()["snapshots"][0]["id"] == job["snapshot_id"]
    assert listing.json()["snapshots"][0]["summary"]["counts"] == {"activities": 2}
    assert "PRIVATE_" not in listing.text

    goal = {"description": "Assess marathon readiness", "race_date": "2026-11-01", "distance_km": 42.195}
    response = client.post("/v1/analyses", headers=headers, json={"snapshot_id": job["snapshot_id"], "goal": goal})
    assert response.status_code == 202
    analysis = response.json()
    assert client.get(f"/v1/jobs/{analysis['id']}", headers=headers).json()["status"] == "completed"
    result = client.get(f"/v1/analyses/{analysis['analysis_id']}", headers=headers)
    assert result.status_code == 200
    assert result.json()["synthetic"] is True
    assert result.json()["goal"]["race_date"] == "2026-11-01"
    assert calls[1][1:3] == (date(2026, 9, 1), date(2026, 9, 27))
    assert client.get("/v1/status", headers=headers).json()["busy"] is False
    for path in [state / "api-token", state / "jobs" / f"{job['id']}.json", state / "jobs" / f"{analysis['id']}.json"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "jobs").stat().st_mode) == 0o700


@pytest.mark.parametrize("identifier", ["..", "../outside", "safe/../../outside", "bad$id", ""])
def test_analysis_rejects_path_traversal(service, identifier):
    _, client, headers, calls = service
    response = client.post("/v1/analyses", headers=headers, json={"snapshot_id": identifier, "goal": {"description": "Check training"}})
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("manifest", [
    {"status": "collecting"}, {"status": "interrupted"}, {"status": "failed"},
    {"status": "queued"}, {"status": "unknown"}, {"status": []}, {"status": {}}, {}, [], None,
])
def test_analysis_rejects_unfinished_or_invalid_snapshot_before_queueing(service, manifest):
    state, client, headers, calls = service
    snapshot = ensure_private_dir(state / "snapshots" / "synthetic")
    write_json(snapshot / "manifest.json", manifest)
    response = client.post("/v1/analyses", headers=headers, json={
        "snapshot_id": "synthetic", "goal": {"description": "Check training"},
    })
    assert response.status_code == 409
    assert calls == []
    assert list((state / "jobs").iterdir()) == []
    assert list((state / "runs").iterdir()) == []


def test_analysis_rejects_unreadable_or_symlinked_manifest(service, tmp_path):
    state, client, headers, calls = service
    snapshot = ensure_private_dir(state / "snapshots" / "synthetic")
    manifest = snapshot / "manifest.json"
    manifest.write_text("PRIVATE_INVALID_JSON")
    body = {"snapshot_id": "synthetic", "goal": {"description": "Check training"}}
    response = client.post("/v1/analyses", headers=headers, json=body)
    assert response.status_code == 409
    assert "PRIVATE_INVALID_JSON" not in response.text
    manifest.unlink()
    target = tmp_path / "synthetic-manifest.json"
    write_json(target, {"status": "complete"})
    manifest.symlink_to(target)
    assert client.post("/v1/analyses", headers=headers, json=body).status_code == 404
    assert calls == []
    assert list((state / "jobs").iterdir()) == []


@pytest.mark.parametrize("status", ["complete", "partial"])
def test_analysis_accepts_only_finished_snapshot_states(service, status):
    state, client, headers, calls = service
    snapshot = ensure_private_dir(state / "snapshots" / "synthetic")
    write_json(snapshot / "manifest.json", {"status": status})
    response = client.post("/v1/analyses", headers=headers, json={
        "snapshot_id": "synthetic", "goal": {"description": "Check training"},
    })
    assert response.status_code == 202
    assert calls[0] == ("evidence", "synthetic")
    assert calls[1][0] == "analysis"
    assert client.get(f"/v1/jobs/{response.json()['id']}", headers=headers).json()["status"] == "completed"


def test_snapshot_status_is_rechecked_before_background_analysis(service, monkeypatch):
    state, client, headers, calls = service
    snapshot = ensure_private_dir(state / "snapshots" / "synthetic")
    manifest = snapshot / "manifest.json"
    write_json(manifest, {"status": "complete"})
    original = api.read_json
    reads = 0

    def changed_after_enqueue(path):
        nonlocal reads
        if path == manifest:
            reads += 1
            return {"status": "complete" if reads == 1 else "collecting"}
        return original(path)

    monkeypatch.setattr(api, "read_json", changed_after_enqueue)
    response = client.post("/v1/analyses", headers=headers, json={
        "snapshot_id": "synthetic", "goal": {"description": "Check training"},
    })
    assert response.status_code == 202
    assert reads == 2
    assert calls == []
    assert client.get(f"/v1/jobs/{response.json()['id']}", headers=headers).json()["status"] == "failed"
    assert list((state / "runs").iterdir()) == []


def test_id_lookup_and_symlinks_cannot_escape_state(service, tmp_path):
    state, client, headers, calls = service
    outside = ensure_private_dir(tmp_path / "outside")
    write_json(outside / "manifest.json", {"private": "do not expose"})
    (state / "snapshots" / "escaped").symlink_to(outside, target_is_directory=True)
    response = client.post("/v1/analyses", headers=headers, json={"snapshot_id": "escaped", "goal": {"description": "Check training"}})
    assert response.status_code == 400
    assert client.get("/v1/jobs/bad%24id", headers=headers).status_code == 400
    assert client.get("/v1/jobs/unknown", headers=headers).status_code == 404
    assert client.get("/v1/analyses/unknown", headers=headers).status_code == 404
    assert calls == []


@pytest.mark.parametrize("patch", [
    {"max_details": 1001}, {"max_details": -1}, {"end": "2026-08-01"},
    {"detail_start": "2026-08-01"}, {"password": "SECRET_PASSWORD"},
    {"start": "SECRET_PASSWORD"},
])
def test_validation_does_not_echo_input(service, patch):
    _, client, headers, calls = service
    response = client.post("/v1/sync", headers=headers, json={**sync_body(), **patch})
    assert response.status_code == 422
    assert "SECRET_PASSWORD" not in response.text
    assert calls == []


def test_job_errors_are_sanitized_and_lock_released(service, monkeypatch):
    state, client, headers, _ = service

    def fail(_state):
        raise RuntimeError("SECRET_GARMIN_PASSWORD oauth_token=SECRET_TOKEN")

    monkeypatch.setattr(api, "load_client", fail)
    response = client.post("/v1/sync", headers=headers, json=sync_body())
    assert response.status_code == 202
    job = client.get(f"/v1/jobs/{response.json()['id']}", headers=headers)
    assert job.json()["status"] == "failed"
    assert "SECRET" not in job.text
    assert "SECRET" not in (state / "jobs" / f"{response.json()['id']}.json").read_text()
    assert client.get("/v1/status", headers=headers).json()["busy"] is False
    assert client.post("/v1/sync", headers=headers, json=sync_body()).status_code == 202


def test_restart_marks_dangling_jobs_interrupted(tmp_path):
    state = ensure_private_dir(tmp_path / "state")
    jobs = ensure_private_dir(state / "jobs")
    write_json(jobs / "oldjob.json", {"id": "oldjob", "kind": "sync", "status": "running"})
    app = api.create_app(state)
    with TestClient(app) as client:
        response = client.get("/v1/jobs/oldjob", headers={"Authorization": f"Bearer {api_token(state)}"})
    assert response.status_code == 200
    assert response.json()["status"] == "interrupted"
    assert "finished_at" in response.json()


def test_jobs_are_serialized(service, monkeypatch):
    _, client, headers, _ = service
    entered, release = Event(), Event()
    original = api.collect_snapshot
    responses = []

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "collect_snapshot", blocking)
    thread = Thread(target=lambda: responses.append(client.post("/v1/sync", headers=headers, json=sync_body())))
    thread.start()
    try:
        assert entered.wait(5)
        assert client.get("/v1/status", headers=headers).json()["busy"] is True
        assert client.post("/v1/sync", headers=headers, json=sync_body()).status_code == 409
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert responses[0].status_code == 202


def test_internal_error_is_sanitized(service, monkeypatch):
    _, client, headers, _ = service

    def broken_read(_path):
        raise RuntimeError("PRIVATE_ERROR_SECRET")

    response = client.post("/v1/sync", headers=headers, json=sync_body())
    monkeypatch.setattr(api, "read_json", broken_read)
    response = client.get(f"/v1/jobs/{response.json()['id']}", headers=headers)
    assert response.status_code == 500
    assert "PRIVATE_ERROR_SECRET" not in response.text
