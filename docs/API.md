# Local API

The API runs on your computer and uses the Garmin connection already established by `gtl login`. It does not accept Garmin credentials, upload workouts, or publish reports.

```sh
uv run gtl login
codex login
uv run gtl serve --port 8765
```

The default URL is `http://127.0.0.1:8765`. The CLI binds only to loopback and uses one worker. Every `/v1` request requires `Authorization: Bearer <token>` using the token saved in the selected state directory's `api-token` file. With default state, that is `.local/api-token`. Only `GET /health` is public. Browser requests with an Origin header must match the local service origin; cross-origin access is not enabled.

Use a separate `--state-dir` for each athlete, before the command. This service is for a trusted local user, not a public or multi-tenant deployment. The bearer token grants access to local data and permission to start analysis using the signed-in Codex account.

## Routes

| Method | Route | Result |
|---|---|---|
| GET | `/health` | App name and version; no authentication |
| GET | `/v1/status` | Setup indicator, active job, snapshot and analysis counts |
| GET | `/v1/snapshots` | Snapshot identifiers and compact status/coverage summaries |
| POST | `/v1/sync` | Queue a Garmin collection job; returns HTTP 202 |
| POST | `/v1/analyses` | Queue curated-evidence preparation and Codex analysis; returns HTTP 202 |
| GET | `/v1/jobs/{identifier}` | Job state and its snapshot or analysis identifier |
| GET | `/v1/analyses/{identifier}` | Structured final result after successful completion |

There are no raw-data download, credential, Garmin write, cancellation, or HTTP resume endpoints. Collection can be resumed locally with the CLI and the existing snapshot identifier.

## Request examples

This Python example reads the bearer token into the process instead of putting its value in a command or URL. Run it from the project directory after starting the service:

```python
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8765"
token = Path(".local/api-token").read_text().strip()

def request(method, route, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(
        base + route,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with urlopen(req) as response:
        return json.load(response)

print(request("GET", "/v1/status"))
end = date.today()
job = request("POST", "/v1/sync", {
    "start": (end - timedelta(days=183)).isoformat(),
    "end": end.isoformat(),
    "detail_start": (end - timedelta(days=83)).isoformat(),
    "max_details": 60,
})
print(job)
print(request("GET", "/v1/jobs/" + job["id"]))
```

Poll the job endpoint until it has finished. A queued response is not evidence that Garmin collection succeeded.

### Collection body

| Field | Required | Meaning |
|---|---|---|
| `start` | Yes | Inclusive `YYYY-MM-DD` start date |
| `end` | Yes | Inclusive end date, on or after `start` |
| `detail_start` | No | First date eligible for running details; must be inside the range when supplied |
| `max_details` | No | Newest eligible runs to detail; integer 0–1,000; API default **60** |

The CLI's detail count default is **1,000**, while the API's is **60**. Both default the detail window to the last 84 days, bounded by the collection start. To request details for the whole period, set `detail_start` equal to `start`. Daily detailed wellness/nutrition remains capped to the final 28 days; range reads cover the requested period. Unknown request fields are rejected.

Each API sync creates a new random snapshot identifier. A completed collection job may reference a snapshot whose manifest is `partial` because some resources were unavailable. Inspect `/v1/snapshots` and the local manifest before interpreting coverage.

### Analysis body and cloud processing

**Posting to `/v1/analyses` starts cloud AI processing.** It authorizes sending the curated evidence and supplied goal to Codex under the server user's ChatGPT login. There is no separate consent field on this endpoint. Inspect evidence locally first:

```sh
uv run gtl evidence --snapshot SNAPSHOT_ID
```

Then, using the Python helper above and your completed snapshot identifier:

```python
analysis_job = request("POST", "/v1/analyses", {
    "snapshot_id": "REPLACE_WITH_COMPLETED_SNAPSHOT_ID",
    "goal": json.loads(Path("examples/goal.json").read_text()),
})
print(analysis_job)
```

Replace the fictional example goal before use. This request uses the goal in its body, not the CLI's saved `goal.json`.

The API uses the runner's defaults: concurrency 3 and Codex's default model. Use the CLI for a different concurrency or explicit model. A full new run makes nine independent calls, nine cross-reviews, and one synthesis.

### Goal object

| Field | Type and limits |
|---|---|
| `description` | Required string, 5–4,000 characters |
| `race_date` | Optional ISO date; choose a date after the evidence cutoff |
| `distance_km` | Optional number greater than 0 and at most 300 |
| `target_time_seconds` | Optional integer, 1–604,800; requires `distance_km` |
| `max_stressors_per_week` | Optional integer, 0–7; a ceiling, not a required count |
| `constraints` | Up to 30 strings, each at most 2,000 characters; defaults to `[]` |
| `notes` | String up to 20,000 characters; defaults to `""` |

Unknown fields are rejected. Model instructions use stated constraints, but only selected structural rules are checked automatically. Freeform constraints still require human review of the proposed sessions.

## Jobs and results

A newly queued job includes `id`, `kind`, `status`, `created_at`, and either `snapshot_id` or `analysis_id`. Subsequent records can include `started_at`, `finished_at`, and a generic `error`.

States are `queued`, `running`, `completed`, `failed`, and `interrupted`. On service startup, previous queued/running jobs are marked interrupted. The server allows one job at a time; starting another while busy returns 409. It does not resume background work automatically.

After an analysis job completes, request `/v1/analyses/{analysis_id}`. The final object includes:

- `summary`, `confidence`, and `goal_assessment` (`supported`, `conditional`, `not_supported`, or `insufficient_data`).
- `current_status` describing fitness, fatigue, durability, and data quality.
- `key_findings` with evidence, alternatives, observation/estimate/heuristic/unknown labels, and confidence.
- `finish_time_estimates` with eligibility, lower/upper seconds, assumptions, reasons, and evidence. Ineligible methods have null times.
- `weekly_plan` containing dated sessions, optional numeric distances/durations, stressor labels, adjustment triggers, and supporting evidence.
- `small_tweaks`, `constraints_checked`, `unresolved_disagreements`, and `missing_information`.

The local run also contains `report.md`, source stage outputs, and private model logs. An HTTP job marked failed does not imply no files were created; partial artifacts may remain locally, but the API serves a final result only after successful completion.

## Errors

| HTTP status | Typical cause |
|---|---|
| 400 | Invalid identifier or rejected Host header |
| 401 | Missing or incorrect bearer token |
| 403 | Disallowed browser Origin |
| 404 | Snapshot, job, or finished analysis not found |
| 409 | Another job is running, the snapshot is unfinished/unreadable, or a requested analysis has not completed |
| 422 | Invalid JSON fields, dates, values, or goal |
| 500 | Unexpected local service failure |

Validation errors do not echo submitted content. Job errors are deliberately generic; inspect private status files and model logs on the same computer. Auth/rate-limit failures stop collection rather than repeatedly calling Garmin.

For a stopped collection, use the CLI with the same snapshot ID and original dates. For refreshed readings, choose a new snapshot ID: successful cached responses are immutable observations. See [README](../README.md#resume-refresh-and-another-athlete).
