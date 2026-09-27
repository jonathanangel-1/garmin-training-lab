"""Resumable, read-only Garmin collection into a private local snapshot.

The collector never authenticates or mutates Garmin. An authenticated client is
supplied by the caller. Use a separate snapshot directory for each athlete. Raw
responses remain private and unchanged; the manifest describes missing data and
failed calls rather than silently converting failures into empty datasets.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
UPSTREAM_COMMIT = "c3c1c0d66579696e3843cba20f985c66069140b9"
MAX_ACTIVITY_PAGES = 100
ACTIVITY_PAGE_SIZE = 100
DETAIL_CHART_LIMIT = 20_000
DETAIL_POLYLINE_LIMIT = 20_000

RANGE_METHODS = (
    "get_daily_steps",
    "get_sleep_daily",
    "get_rhr_daily",
    "get_hrv_data_range",
    "get_max_metrics_range",
    "get_body_battery",
)
DAILY_METHODS = (
    "get_user_summary",
    "get_training_readiness",
    "get_hydration_data",
    "get_nutrition_daily_food_log",
    "get_nutrition_daily_meals",
    "get_nutrition_daily_settings",
)
DETAIL_METHODS = (
    "get_activity",
    "get_activity_splits",
    "get_activity_details",
    "get_activity_weather",
    "get_activity_gear",
    "get_activity_hr_in_timezones",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Snapshot directories must not be symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _atomic_json(path: Path, payload: Any) -> str:
    """Replace a JSON file atomically; temporary and final files are owner-only."""
    data = (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".collect-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return _sha(data)


def _windows(start: date, end: date):
    current = start
    while current <= end:
        last = min(current + timedelta(days=27), end)
        yield current, last
        current = last + timedelta(days=1)


def _local_date(activity: dict[str, Any]) -> date | None:
    """Use the recorded local date, never a UTC fallback across travel days."""
    value = activity.get("startTimeLocal")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _is_running(activity: dict[str, Any]) -> bool:
    kind = activity.get("activityType")
    key = kind.get("typeKey") if isinstance(kind, dict) else None
    return isinstance(key, str) and (
        key == "running" or key.endswith("_running") or key in {"ultra_run", "obstacle_run"}
    )


def _error(exc: Exception) -> tuple[dict[str, Any], bool]:
    """Classify failures without persisting exception text or credentials."""
    name = type(exc).__name__
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    if not isinstance(code, int):
        match = re.search(r"(?:API Error|HTTP|Error|status(?: code)?)\s*[:=]?\s*(\d{3})\b", str(exc), re.I)
        code = int(match.group(1)) if match else None
    lower = name.lower()
    limited = code == 429 or "toomanyrequests" in lower or "ratelimit" in lower
    unauthorized = code == 401 or "authentication" in lower or "unauthorized" in lower
    reason = "rate_limited" if limited else "authentication_failed" if unauthorized else "request_failed"
    if not (limited or unauthorized) and (isinstance(exc, NotImplementedError) or code in {403, 404, 405, 410, 501}):
        reason = "endpoint_or_resource_unavailable"
    return {"type": name, "http_status": code, "reason": reason}, limited or unauthorized


class _Collection:
    def __init__(self, client: Any, folder: Path, manifest: dict[str, Any], pause: float):
        self.client = client
        self.folder = folder
        self.manifest = manifest
        self.pause = pause
        self.interrupted = False
        self.last_request_finished: float | None = None
        self.missing_methods: set[str] = set()
        self.cache: dict[str, dict[str, Any]] = {}
        previous = folder / "manifest.json"
        if previous.exists():
            if previous.is_symlink():
                raise ValueError("Snapshot manifest must not be a symbolic link")
            try:
                stored = json.loads(previous.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("Existing snapshot manifest is unreadable; choose a new snapshot directory") from exc
            if stored.get("schema_version") == SCHEMA_VERSION and stored.get("upstream_commit") == UPSTREAM_COMMIT:
                self.cache = {
                    row["key"]: row for row in stored.get("resume_cache", stored.get("calls", []))
                    if isinstance(row, dict) and row.get("key") and row.get("status") in {"ok", "empty"}
                }

    def checkpoint(self) -> None:
        calls = self.manifest["calls"]
        counts = self.manifest["counts"]
        counts.update({
            "calls": len(calls),
            "network_calls": sum(bool(row.get("attempted")) for row in calls),
            "cached_calls": sum(bool(row.get("cached")) for row in calls),
            "successful_calls": sum(row["status"] in {"ok", "empty"} for row in calls),
            "empty_calls": sum(row["status"] == "empty" for row in calls),
            "failed_calls": sum(row["status"] in {"error", "interrupted"} for row in calls),
            "unavailable_calls": sum(row["status"] == "unavailable" for row in calls),
        })
        # Retain successful calls not reached yet during this resume. An auth
        # failure while retrying an earlier gap must not erase later cache hits.
        self.manifest["resume_cache"] = list(self.cache.values())
        self.manifest["updated_at"] = _now()
        _atomic_json(self.folder / "manifest.json", self.manifest)

    def problem(self, reason: str, **details: Any) -> None:
        self.manifest["completeness"].append({"reason": reason, **details})

    def call(self, method: str, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
        if self.interrupted:
            return False, None
        request = {"method": method, "args": list(args), "kwargs": kwargs}
        key = _sha(json.dumps(request, sort_keys=True, separators=(",", ":")).encode())
        relative = f"raw/{method}-{key[:20]}.json"
        output = self.folder / relative
        row = {
            "key": key, **request, "status": "error", "raw_path": None,
            "sha256": None, "retrieved_at": None, "cached": False,
            "attempted": False, "error": None,
        }
        cached = self.cache.get(key)
        # Never follow a path supplied by a previous manifest. Recompute it.
        if cached and cached.get("raw_path") == relative and output.is_file() and not output.is_symlink():
            data = output.read_bytes()
            if _sha(data) == cached.get("sha256"):
                try:
                    payload = json.loads(data)
                except ValueError:
                    pass
                else:
                    output.chmod(0o600)
                    row.update({field: cached.get(field) for field in ("status", "raw_path", "sha256", "retrieved_at")})
                    row["cached"] = True
                    self.manifest["calls"].append(row)
                    self.checkpoint()
                    return True, payload
        operation = getattr(self.client, method, None)
        if not callable(operation) or method in self.missing_methods:
            self.missing_methods.add(method)
            row.update(status="unavailable", error={"type": "MissingMethod", "http_status": None, "reason": "client_method_unavailable"})
        else:
            if self.last_request_finished is not None:
                remaining = self.pause - (time.monotonic() - self.last_request_finished)
                if remaining > 0:
                    time.sleep(remaining)
            row["attempted"] = True
            try:
                payload = operation(*args, **kwargs)
            except Exception as exc:
                error, interrupt = _error(exc)
                self.interrupted = interrupt
                row.update(status="interrupted" if interrupt else "unavailable" if error["reason"] == "endpoint_or_resource_unavailable" else "error", error=error)
                if isinstance(exc, NotImplementedError):
                    self.missing_methods.add(method)
            else:
                row.update(status="empty" if payload is None or payload == [] or payload == {} else "ok", raw_path=relative, sha256=_atomic_json(output, payload), retrieved_at=_now())
                self.manifest["calls"].append(row)
                self.cache[key] = row.copy()
                self.last_request_finished = time.monotonic()
                self.checkpoint()
                return True, payload
            self.last_request_finished = time.monotonic()
        row["retrieved_at"] = _now()
        self.manifest["calls"].append(row)
        self.manifest["errors"].append({"key": key, "method": method, "args": list(args), "status": row["status"], **row["error"]})
        if self.interrupted:
            self.manifest["status"] = "interrupted"
        self.checkpoint()
        return False, None


def collect_snapshot(
    client: Any,
    snapshot_dir: Path,
    start: date,
    end: date,
    detail_start: date | None = None,
    max_details: int = 60,
    request_pause: float = 0.2,
) -> dict[str, Any]:
    """Collect a bounded training dataset, returning its persisted manifest.

    All date boundaries are inclusive. Activity inclusion uses startTimeLocal.
    By default detailed runs cover the last 84 days, limited to the newest 60;
    daily wellness calls cover at most the last 28 days. Range reads use 28-day
    windows. The latest 10,000 activity summaries are the pagination ceiling;
    hitting it is reported as incomplete. Empty successful responses are cached
    as observed, not treated as zero measurements. Reuse a snapshot directory to
    resume; use a new directory when an updated reading is required.

    No original FIT downloads are performed. Detail charts request 20,000 points
    but may still be sampled by Garmin; the manifest does not claim full streams.
    """
    if not isinstance(start, date) or isinstance(start, datetime) or not isinstance(end, date) or isinstance(end, datetime):
        raise TypeError("start and end must be calendar dates")
    if start > end:
        raise ValueError("start must not be after end")
    if detail_start is not None and (not isinstance(detail_start, date) or isinstance(detail_start, datetime)):
        raise TypeError("detail_start must be a calendar date")
    if detail_start is not None and detail_start > end:
        raise ValueError("detail_start must not be after end")
    if isinstance(max_details, bool) or not isinstance(max_details, int) or not 0 <= max_details <= 1000:
        raise ValueError("max_details must be an integer between 0 and 1000")
    if isinstance(request_pause, bool) or not isinstance(request_pause, (int, float)) or not math.isfinite(request_pause) or request_pause < 0:
        raise ValueError("request_pause must be a finite non-negative number")
    detail_start = max(start, detail_start or end - timedelta(days=83))
    daily_start = max(start, end - timedelta(days=27))
    folder = Path(snapshot_dir)
    _private_dir(folder)
    _private_dir(folder / "raw")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": {"library": "garminconnect", "repository": "https://github.com/cyberjunky/python-garminconnect"},
        "upstream_commit": UPSTREAM_COMMIT,
        "boundaries": {"start": start.isoformat(), "end": end.isoformat(), "detail_start": detail_start.isoformat(), "daily_start": daily_start.isoformat()},
        "status": "collecting", "created_at": _now(), "updated_at": _now(),
        "calls": [], "errors": [], "completeness": [], "artifacts": {},
        "counts": {"activities": 0, "selected_runs": 0, "eligible_runs": 0, "activity_pages": 0, "unclassified_local_dates": 0},
        "selection": {"max_details": max_details, "activity_page_size": ACTIVITY_PAGE_SIZE, "max_activity_pages": MAX_ACTIVITY_PAGES, "local_date_field": "startTimeLocal", "running_type_rule": "running, *_running, ultra_run, obstacle_run", "request_pause_seconds": request_pause},
        "sampling": {"maxChartSize": DETAIL_CHART_LIMIT, "maxPolylineSize": DETAIL_POLYLINE_LIMIT, "potentially_sampled": True, "original_fit_downloaded": False},
    }
    state = _Collection(client, folder, manifest, float(request_pause))
    state.checkpoint()
    activities: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous_page_hashes: set[str] = set()
    reached_boundary = False
    for page_index in range(MAX_ACTIVITY_PAGES):
        success, page = state.call("get_activities", page_index * ACTIVITY_PAGE_SIZE, ACTIVITY_PAGE_SIZE)
        if not success:
            state.problem("activity_history_request_failed", offset=page_index * ACTIVITY_PAGE_SIZE)
            break
        manifest["counts"]["activity_pages"] += 1
        if page is None or page == []:
            reached_boundary = True
            break
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            state.problem("unexpected_activity_list_shape", offset=page_index * ACTIVITY_PAGE_SIZE)
            break
        page_hash = _sha(json.dumps(page, sort_keys=True, separators=(",", ":")).encode())
        if page_hash in previous_page_hashes:
            state.problem("repeated_activity_page", offset=page_index * ACTIVITY_PAGE_SIZE)
            break
        previous_page_hashes.add(page_hash)
        dates: list[date] = []
        for activity in page:
            local = _local_date(activity)
            if local is None:
                unclassified.append(activity)
                continue
            dates.append(local)
            if not start <= local <= end:
                continue
            activity_id = activity.get("activityId")
            if activity_id is not None:
                identifier = str(activity_id)
                if identifier in seen_ids:
                    continue
                seen_ids.add(identifier)
            activities.append(activity)
        # A short page alone does not prove exhaustion. Keep reading until an
        # empty page or a complete page older than the requested local dates.
        if len(dates) == len(page) and all(local < start for local in dates):
            reached_boundary = True
            break
    else:
        state.problem("activity_pagination_limit_reached", maximum_pages=MAX_ACTIVITY_PAGES)
    manifest["activity_history_boundary_reached"] = reached_boundary
    activities.sort(key=lambda item: (item.get("startTimeLocal", ""), str(item.get("activityId", ""))))
    manifest["counts"]["activities"] = len(activities)
    manifest["artifacts"]["activities"] = {"path": "activities.json", "sha256": _atomic_json(folder / "activities.json", activities), "count": len(activities)}
    if unclassified:
        manifest["counts"]["unclassified_local_dates"] = len(unclassified)
        manifest["artifacts"]["activities_unclassified"] = {"path": "activities_unclassified.json", "sha256": _atomic_json(folder / "activities_unclassified.json", unclassified), "count": len(unclassified)}
        state.problem("activities_missing_valid_local_date", count=len(unclassified))
    runs = [item for item in reversed(activities) if _is_running(item) and _local_date(item) >= detail_start]
    manifest["counts"]["eligible_runs"] = len(runs)
    selected = runs[:max_details]
    manifest["counts"]["selected_runs"] = len(selected)
    manifest["selected_activity_ids"] = [item.get("activityId") for item in selected]
    manifest["detail_selection_truncated"] = len(runs) > max_details
    state.checkpoint()
    for first, last in _windows(start, end):
        if state.interrupted:
            break
        for method in RANGE_METHODS:
            state.call(method, first.isoformat(), last.isoformat())
            if state.interrupted:
                break
    for activity in selected:
        if state.interrupted:
            break
        identifier = activity.get("activityId")
        if identifier is None or isinstance(identifier, bool) or not str(identifier).isdigit() or int(identifier) <= 0:
            state.problem("selected_run_missing_valid_activity_id", local_date=activity.get("startTimeLocal"))
            continue
        for method in DETAIL_METHODS:
            options = {"maxchart": DETAIL_CHART_LIMIT, "maxpoly": DETAIL_POLYLINE_LIMIT} if method == "get_activity_details" else {}
            state.call(method, str(identifier), **options)
            if state.interrupted:
                break
    current = daily_start
    while current <= end and not state.interrupted:
        for method in DAILY_METHODS:
            state.call(method, current.isoformat())
            if state.interrupted:
                break
        current += timedelta(days=1)
    manifest["status"] = "interrupted" if state.interrupted else "partial" if manifest["errors"] or manifest["completeness"] else "complete"
    manifest["finished_at"] = _now()
    state.checkpoint()
    return manifest
