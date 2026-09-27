# Garmin Training Lab

Connect Garmin, establish what your completed training supports, then assess your goal and build a provisional race strategy and training plan.

Nine specialists first review capacity without receiving your goal. They cross-examine the evidence, synthesize current capacity, and only then consider your goal. A separate model audit challenges the proposed strategy and plan before publication. You can inspect the evidence, individual findings, disagreements, and final proposal. The app does not upload workouts or change your Garmin calendar.

This is a local Python application and authenticated local API. Garmin collection stays on your computer. **AI analysis sends curated training and health information to Codex using your own ChatGPT login.** It uses your Codex allowance and is not an offline AI system.

## Start here

You need Python 3.12 or later, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), a Garmin account, and a current [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) with ChatGPT access. Install Codex separately using its official instructions. This project checks for the non-interactive CLI features it uses before starting analysis. CLI 0.157.1 was verified with `gpt-6-astra`; older CLIs may reject newer models even when login works.

This release is tested on macOS and Linux. On Windows, use a Linux environment such as WSL; native Windows operation is not validated.

```sh
git clone https://github.com/jonathanangel-1/garmin-training-lab.git
cd garmin-training-lab
uv sync
codex login
uv run gtl login
```

Complete the Codex browser sign-in with your own ChatGPT account. Garmin credentials are entered in the terminal; the password and verification code are hidden. Garmin session tokens are stored locally. Run `uv run gtl status` to check setup. See the [official Codex authentication documentation](https://learn.chatgpt.com/docs/auth) for sign-in details.

### 1. Describe your goal

This example is a fictional 10 km goal. Change the date, target, and constraints to your own:

```sh
uv run gtl goal \
  --description "Prepare for a 10 km race with consistent, sustainable training" \
  --race-date 2027-05-09 \
  --distance-km 10 \
  --target-time 00:55:00 \
  --max-stressors 2 \
  --constraint "At most four running days per week" \
  --constraint "Keep Friday as a rest day"
```

Use `--observations-file /path/to/observations.json` for a JSON list of dated athlete reports: symptoms, effort, actual fueling, equipment or context. See [the synthetic example](examples/observations.json). These reports reach the capacity analysts. Put aspirations, schedule limits and prior coaching proposals in the goal/constraints or `--notes-file /path/to/notes.md`; those are withheld until planning. Existing notes are not automatically reclassified as evidence. Do not put a desired finish time or proposed schedule inside an observation. A time target also requires a distance. Goal input is freeform, but this version's specialist protocol and plan format focus on running.

### 2. Collect and inspect your evidence

```sh
uv run gtl sync --snapshot baseline
uv run gtl evidence --snapshot baseline
```

Collection defaults to the last 183 days through today. Activity summaries include every sport. Detailed running reads default to the last 84 days, with at most 1,000 runs in the CLI. Use `--start`, `--end`, and `--detail-start` with `YYYY-MM-DD` dates to change those windows; set `--detail-start` to the collection start if you want running details for the whole period. A smaller `--max-details` reduces collection work.

Open `.local/evidence/baseline/evidence.md` and `.local/evidence/baseline/evidence.json`. The first is an index; the second is the actual curated dataset. Review missing fields and coverage before analysis. Raw Garmin responses remain in `.local/snapshots/baseline/` for private inspection.

### 3. Run the specialists and roundtable

```sh
uv run gtl analyze \
  --snapshot baseline \
  --run first-review \
  --concurrency 3 \
  --share-with-codex
```

`--share-with-codex` authorizes sending the curated evidence and goal to your Codex account. A new full analysis uses **21–25 Codex calls**: nine independent reviews, nine cross-reviews, a goal-blind capacity synthesis, a strategy/plan proposal, and an independent audit. Up to two revisions and re-audits are allowed. Unresolved audit findings prevent publication of a final plan. Concurrency can be 1, 2, or 3; this changes simultaneous work, not the number of roles. Analysis uses Codex's default model unless you pass `--model` explicitly, and requests high reasoning effort. Choose a model your Codex account supports; unavailable models fail explicitly.

Read `.local/runs/first-review/report.md` for the assessment and proposed plan. `final.json` holds the structured result only after a passing audit and structural checks. `capacity.json` preserves the assessment made before seeing the goal, and `audit.json` records the final challenge. Rejected drafts remain private and are not served as final API results. The `independent/` and `roundtable/` directories retain each specialist's contribution; `run.json` records progress. Model logs stay inside the private run directory.

The plan begins after the collection cutoff. With a race date, it covers up to the race or 12 weeks, whichever comes first. Without a date, it covers four weeks. Refresh old evidence before asking for a current plan.

## What the nine specialists cover

| Specialist | Main question |
|---|---|
| Training history | What preparation have you actually accumulated? |
| Calibration and quality | Which sensors, settings and threshold estimates are actually trustworthy for this inference? |
| Terrain and environment | How do slopes, routes, and recorded weather affect comparisons? |
| Pace and effort | What pace/HR relationships are demonstrated, for how long, under what conditions? |
| Long-run endurance | Can you sustain the required work late in long efforts? |
| Sleep and recovery | Which personal recovery patterns accompany training? |
| Background load and strength | What extra load or measured strength is documented, and what remains unknown? |
| Fueling and hydration | What intake and tolerance are actually documented? |
| Symptoms and mechanics | What do your notes, shoes, cadence, and available dynamics establish? |

Each specialist must distinguish observations, estimates, heuristics, and unknowns. The capacity and planning reviewers resolve disagreements using source evidence, not vote counts. The final report explains feasibility, HR calibration limits, provisional race execution, and why each proposed week differs from the achieved baseline. A current pain-free report is not converted into an injury-return plan. See [Methodology](docs/METHODOLOGY.md) for data coverage, interpretation rules, and limits.

## Data coverage and limits

- Six-month default collection includes activity summaries and range reads for steps, sleep summaries, resting heart rate, HRV, maximum metrics, and Body Battery where available. Range requests use windows of at most 28 days.
- Detailed daily summaries, readiness, hydration, and nutrition reads cover only the final 28 days of the requested period.
- Selected runs include summary, laps, sampled chart details, recorded weather, gear, and heart-rate zones. Charts request up to 20,000 points; this is not a guarantee of full-resolution streams. Original FIT files are not downloaded.
- Collection also requests latest and date-bounded lactate-threshold estimates, configured HR zones/profile settings, and bounded device metadata. Current settings are retrieval-time observations, not a reconstruction of past settings. Device estimates and configured HRmax are not measured physiological calibration. Unknown units stay unknown.
- Deterministic capacity summaries report exact weekly load, HR coverage, and bounded sustained-window candidates. Selection thresholds are processing heuristics, not validated marathon-readiness tests.
- A raw endpoint being collected does not mean every field reaches the model. The evidence builder uses known fields and marks resources it cannot normalize. Some metrics depend on device, account features, recording habits, and Garmin availability.
- Running contributes to daily steps. Missing food logs do not mean no food; missing runs do not prove rest. Garmin measurements cannot establish injury recovery, workout intent, or in-run fueling without supporting notes.
- This is an initial implementation, not a clinically validated coaching system or a performance guarantee. Watch metrics and model agreement do not establish a diagnosis or prove a goal is achievable.

## Resume, refresh, and another athlete

Repeat `sync` with the same snapshot identifier and the same date arguments to resume. Successful raw calls are reused only after their file hashes match. Authentication failure or rate limiting stops collection. Errors and unavailable resources remain visible in `manifest.json`; a `partial` snapshot is not complete coverage.

Cached successful responses, including empty responses, are observations from the original collection. **Use a new snapshot identifier to refresh data**, especially today's activities. The CLI's default snapshot name is today's date, so use explicit names when making multiple fresh snapshots in one day.

Repeat analysis with the same run identifier to reuse valid stage outputs under identical inputs. If the goal, evidence, model, or protocol changes, choose a new run identifier.

The default `.local/` directory is ignored by Git. To keep all personal state outside the checkout, put the global option **before** the command:

```sh
uv run gtl --state-dir /absolute/path/to/private-athlete-state login
uv run gtl --state-dir /absolute/path/to/private-athlete-state status
```

Use that same option for subsequent commands, or set `GTL_STATE_DIR`. Each athlete needs a separate state directory. An existing Garmin connection is verified and resumed; `login` does not silently replace it with another athlete's credentials.

## Privacy and local service

Private state contains Garmin session tokens, raw health/activity responses, goals, curated evidence, and generated reports. Directories and files use owner-only permissions on systems supporting them. Do not commit this state or attach raw data or private logs to public issues. A custom state directory placed elsewhere inside the checkout needs its own ignore rule.

The curated model input excludes precise coordinates, route polylines, owner identifiers, and activity names/descriptions. Activity IDs, dates, health metrics, and provided `locationName` area context remain. This is sensitive personal information, not an anonymous dataset. The runner supplies the curated input directory and uses Codex's read-only sandbox, but **read-only is not filesystem isolation**; it does not guarantee the process cannot read other files accessible to your operating-system account.

Start the optional API with:

```sh
uv run gtl serve
```

It binds to `127.0.0.1:8765`, runs one worker, and requires the bearer token in `.local/api-token` for every `/v1` endpoint. Only `/health` is public. `uv run gtl api-token` displays the token in an interactive terminal. The service accepts no Garmin passwords and serializes collection/analysis jobs. See [API reference](docs/API.md) for requests and error behavior.

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv build
```

Tests use synthetic data and fake clients for collection, evidence normalization, plan validation, and API behavior. Passing them verifies software behavior within those cases; it does not validate a personalized prescription or establish that every Garmin feature works for every account.

## Credits

Garmin access uses Ron Klinkien's community-maintained [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), pinned to commit [`c3c1c0d66579696e3843cba20f985c66069140b9`](https://github.com/cyberjunky/python-garminconnect/tree/c3c1c0d66579696e3843cba20f985c66069140b9). That library is an unofficial Garmin Connect client. This repository adds collection, evidence preparation, and an analysis workflow; it is not a fork or an official Garmin integration. No Garmin, OpenAI, TrainingPeaks, Strava, or V.O2 endorsement is implied.

Project code is MIT licensed. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
