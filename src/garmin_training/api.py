"""Authenticated, loopback-only API for local Garmin collection and analysis.

Run a single server worker bound to 127.0.0.1. Credentials are supplied only by
the separate local login command, never by this API.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .auth import load_client
from .collector import collect_snapshot
from .core import api_token, ensure_private_dir, read_json, validate_id, write_json
from .goals import Goal


def build_evidence(snapshot_dir: Path, output_dir: Path) -> dict:
    from .evidence import build_evidence as build

    return build(snapshot_dir, output_dir)


def run_analysis(**kwargs: Any) -> dict:
    from .roundtable import run_analysis as analyze

    return analyze(**kwargs)


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    detail_start: date | None = None
    max_details: int = Field(default=60, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_dates(self) -> "SyncRequest":
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        if self.detail_start is not None and not self.start <= self.detail_start <= self.end:
            raise ValueError("detail_start must be inside the collection date range")
        return self


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    goal: Goal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_local_origin(request: Request, origin: str) -> bool:
    """Check the complete origin, including scheme and effective port."""
    try:
        source = urlsplit(origin)
        target = urlsplit(str(request.url))
        if source.hostname not in {"localhost", "127.0.0.1"}:
            return False
        if source.username is not None or source.password is not None:
            return False
        if source.path not in {"", "/"} or source.query or source.fragment:
            return False
        if source.scheme not in {"http", "https"}:
            return False
        default_ports = {"http": 80, "https": 443}
        return (
            source.scheme == target.scheme
            and source.hostname == target.hostname
            and (source.port if source.port is not None else default_ports[source.scheme])
            == (target.port if target.port is not None else default_ports.get(target.scheme))
        )
    except (ValueError, KeyError):
        return False


def _safe_child(parent: Path, identifier: str) -> Path:
    try:
        safe_id = validate_id(identifier)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid identifier.") from None
    candidate = parent / safe_id
    if not candidate.resolve().is_relative_to(parent.resolve()):
        raise HTTPException(status_code=400, detail="Invalid identifier.")
    return candidate


def create_app(state_dir: Path) -> FastAPI:
    state_dir = ensure_private_dir(Path(state_dir))
    snapshots_dir = ensure_private_dir(state_dir / "snapshots")
    runs_dir = ensure_private_dir(state_dir / "runs")
    jobs_dir = ensure_private_dir(state_dir / "jobs")
    token = api_token(state_dir)
    job_lock = Lock()
    active_job: dict[str, str | None] = {"id": None}

    # A stopped process cannot resume an in-flight network/model operation safely.
    for path in jobs_dir.glob("*.json"):
        if path.is_symlink():
            continue
        try:
            job = read_json(path)
            if isinstance(job, dict) and job.get("status") in {"queued", "running"}:
                job.update(status="interrupted", finished_at=_now(), error="The local service restarted before this job finished.")
                write_json(path, job)
        except (OSError, ValueError, TypeError):
            continue

    app = FastAPI(title="Garmin Training Lab", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_access(request: Request, call_next: Callable[..., Any]):
        origins = request.headers.getlist("origin")
        if origins and (len(origins) != 1 or not _same_local_origin(request, origins[0])):
            return JSONResponse(status_code=403, content={"detail": "Origin is not allowed."})
        if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
            authorizations = request.headers.getlist("authorization")
            parts = authorizations[0].split() if len(authorizations) == 1 else []
            supplied = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
            if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                return JSONResponse(status_code=401, content={"detail": "Bearer authentication required."}, headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    # Outer middleware rejects DNS-rebinding Host values before route handling.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        # Pydantic's default response echoes invalid input; never reflect it here.
        return JSONResponse(status_code=422, content={"detail": "Invalid request. Check the fields, values, and date range."})

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=500, content={"detail": "The local service could not complete this request."})

    def job_path(identifier: str) -> Path:
        return _safe_child(jobs_dir, identifier).with_suffix(".json")

    def read_job(identifier: str) -> dict[str, Any]:
        path = job_path(identifier)
        if not path.is_file() or path.is_symlink():
            raise HTTPException(status_code=404, detail="Job not found.")
        result = read_json(path)
        if not isinstance(result, dict):
            raise HTTPException(status_code=404, detail="Job not found.")
        return result

    def snapshot_summaries() -> list[dict[str, Any]]:
        summaries = []
        for directory in snapshots_dir.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            path = directory / "manifest.json"
            if not path.is_file() or path.is_symlink():
                continue
            try:
                record = read_json(path)
                if not isinstance(record, dict):
                    continue
                summary = {key: record[key] for key in ("status", "boundaries", "counts", "created_at", "finished_at") if key in record}
                summaries.append({"id": directory.name, "summary": summary})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(summaries, key=lambda item: item["summary"].get("created_at", ""), reverse=True)

    def require_finished_snapshot(snapshot: Path) -> None:
        manifest_path = snapshot / "manifest.json"
        if (not snapshot.is_dir() or snapshot.is_symlink()
                or not manifest_path.is_file() or manifest_path.is_symlink()):
            raise HTTPException(status_code=404, detail="Snapshot not found.")
        try:
            manifest = read_json(manifest_path)
        except (OSError, ValueError, TypeError):
            raise HTTPException(status_code=409, detail="Snapshot manifest could not be read.") from None
        if not isinstance(manifest, dict) or manifest.get("status") not in ("complete", "partial"):
            raise HTTPException(status_code=409, detail="Snapshot collection must finish before analysis.")

    def enqueue(kind: str, tasks: BackgroundTasks, work: Callable[[str], None]) -> dict[str, Any]:
        if not job_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another local job is already running.")
        identifier = uuid4().hex
        active_job["id"] = identifier
        job = {"id": identifier, "kind": kind, "status": "queued", "created_at": _now()}
        job["snapshot_id" if kind == "sync" else "analysis_id"] = identifier
        try:
            write_json(job_path(identifier), job)
        except Exception:
            active_job["id"] = None
            job_lock.release()
            raise

        def execute() -> None:
            try:
                job.update(status="running", started_at=_now())
                write_json(job_path(identifier), job)
                work(identifier)
                job.update(status="completed", finished_at=_now())
            except Exception:
                job.update(status="failed", finished_at=_now(), error="The job failed. Check the local setup and retry.")
            finally:
                try:
                    write_json(job_path(identifier), job)
                finally:
                    active_job["id"] = None
                    job_lock.release()

        tasks.add_task(execute)
        return dict(job)

    @app.get("/health")
    def health():
        return {"app": "garmin-training-lab", "version": __version__}

    @app.get("/v1/status")
    def status():
        token_dir = state_dir / "garmin-tokens"
        token_file = token_dir / "garmin_tokens.json"
        return {
            "version": __version__,
            "busy": job_lock.locked(),
            "active_job_id": active_job["id"],
            "garmin_tokens_present": token_file.is_file() and not token_file.is_symlink() and not token_dir.is_symlink(),
            "snapshots_count": len(snapshot_summaries()),
            "analyses_count": sum(1 for directory in runs_dir.iterdir() if directory.is_dir() and not directory.is_symlink() and (directory / "final.json").is_file()),
        }

    @app.get("/v1/snapshots")
    def snapshots():
        return {"snapshots": snapshot_summaries()}

    @app.post("/v1/sync", status_code=202)
    def sync(body: SyncRequest, tasks: BackgroundTasks):
        def work(identifier: str) -> None:
            destination = ensure_private_dir(_safe_child(snapshots_dir, identifier))
            client = load_client(state_dir)
            manifest = collect_snapshot(client, destination, start=body.start, end=body.end, detail_start=body.detail_start, max_details=body.max_details)
            if not isinstance(manifest, dict) or manifest.get("status") not in {"complete", "partial"}:
                raise RuntimeError("Collection did not complete")

        return enqueue("sync", tasks, work)

    @app.post("/v1/analyses", status_code=202)
    def analyze(body: AnalysisRequest, tasks: BackgroundTasks):
        snapshot = _safe_child(snapshots_dir, body.snapshot_id)
        require_finished_snapshot(snapshot)

        def work(identifier: str) -> None:
            # A separate CLI process can change a snapshot after enqueueing.
            # Recheck before preparing evidence or starting any model calls.
            require_finished_snapshot(snapshot)
            destination = ensure_private_dir(_safe_child(runs_dir, identifier))
            build_evidence(snapshot, destination)
            evidence_path = destination / "evidence.json"
            if not evidence_path.is_file():
                raise RuntimeError("Evidence was not created")
            run_analysis(evidence_path=evidence_path, goal=body.goal.model_dump(mode="json"), run_dir=destination)
            if not (destination / "final.json").is_file():
                raise RuntimeError("Analysis was not created")

        return enqueue("analysis", tasks, work)

    @app.get("/v1/jobs/{identifier}")
    def job_status(identifier: str):
        return read_job(identifier)

    @app.get("/v1/analyses/{identifier}")
    def analysis_result(identifier: str):
        destination = _safe_child(runs_dir, identifier)
        final_path = destination / "final.json"
        if not final_path.is_file() or final_path.is_symlink():
            raise HTTPException(status_code=404, detail="Completed analysis not found.")
        # API-created runs must be committed as completed before they are served.
        if job_path(identifier).is_file() and read_job(identifier).get("status") != "completed":
            raise HTTPException(status_code=409, detail="Analysis has not completed.")
        return read_json(final_path)

    return app
