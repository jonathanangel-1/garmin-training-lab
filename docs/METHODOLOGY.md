# Methodology

The goal is to turn recorded training into a traceable assessment and a proposed running plan. The software separates source collection, evidence preparation, independent interpretation, cross-review, and final validation. A target time is a user goal, not evidence of current fitness.

## 1. One shared dataset

The collector reads Garmin through the pinned `python-garminconnect` dependency. It never changes Garmin data. Raw successful responses are preserved as JSON with request arguments, retrieval time, and SHA-256 hashes in `manifest.json`.

Activity inclusion uses the recorded `startTimeLocal` calendar date. It does not silently substitute a UTC date, which could move a late-night run into another day. Activity IDs deduplicate overlapping pages. Records without a valid local date remain available privately and are counted as unclassified. All sports are retained in activity summaries, including walking and strength work.

The request schedule is serial and paced. Authentication and rate-limit failures interrupt the run. Other failures and unavailable resources remain explicit. A successful empty response is different from an error, and neither means a measured value of zero. A `complete` status means the configured collection finished without recorded gaps; it does not prove Garmin recorded every aspect of the athlete's life.

### Default coverage

| Data | Window or bound |
|---|---|
| Activity summaries, all types | CLI defaults to the last 183 days through today; pagination capped at 10,000 most recent records |
| Steps, sleep summaries, resting HR, HRV, maximum metrics, Body Battery | Requested date range, divided into windows of at most 28 days |
| Run summary, laps, chart details, recorded weather, gear, HR zones | Newest eligible runs since `detail_start`; default last 84 days |
| Number of detailed runs | CLI default 1,000; API default 60; configurable from 0 to 1,000 |
| Detailed daily summary, readiness, hydration, nutrition | Last 28 days of requested range |
| Activity chart request | Up to 20,000 chart points and 20,000 polyline points; still potentially sampled |
| Original FIT records, external historical weather, route geocoding | Not collected by this version |

The collector's selected-run count, truncation flag, endpoint outcomes, and sampling settings are carried into the evidence. Supply `--detail-start` explicitly when older runs need the same level of examination as recent ones.

Snapshots are observation records. Resuming reuses hash-verified successes, including empty responses; it retries failed calls. It does not refresh a previously successful reading. New observations require a new snapshot identifier.

## 2. Curated evidence

The evidence builder reads the manifest and verifies referenced local payloads. It creates an indexed Markdown overview and a JSON bundle containing normalized activities, dated wellness records, bounded running details, source references, and limitations. Source payloads with unknown shapes remain available for local inspection; they are not automatically copied into the model context.

Normalization uses explicit fields and units. Garmin activity summaries commonly express distance in meters, duration in seconds, and speed in meters per second; these must not be confused with kilometers or minutes per kilometer. Moving, timer, and elapsed duration remain different measures. Pace changes caused by stops cannot automatically be treated as fatigue.

Activity chart channels are mapped through Garmin's `metricDescriptors` rather than fixed array positions. The builder summarizes known channels into bounded profiles. Profiles may be sampled first by Garmin and then summarized locally. Sample-weighted means are not automatically time-weighted means, and coarse bins cannot reconstruct every short hill, stop, or surge. Use summary elevation totals for overall ascent/descent rather than assuming binned changes recover the full route.

Exact coordinates, route polylines, owner identifiers, and activity names/descriptions are excluded from the curated bundle. Provided `locationName` area context, dates, activity IDs, and relevant health metrics remain. The goal and athlete notes are also model input. This reduces unnecessary disclosure but does not make the dataset anonymous.

## 3. Nine independent questions

| Role | Required distinction |
|---|---|
| Training history and progression | Recorded training versus a complete history; missing records versus rest |
| Workout execution | Observed phases versus intended purpose; controlled work versus maximal effort |
| Terrain and environment | Terrain/context effects versus fitness or fatigue changes |
| Cardiovascular fitness | Comparable pace/HR trends versus sensor error or contextual variation |
| Long-run endurance | Ability to run target pace briefly versus ability to sustain the required event duration |
| Sleep and recovery | Personal patterns and associations versus demonstrated causes |
| Background physical load | Extra activity versus running already included in daily steps |
| Fueling and hydration | Documented intake/tolerance versus daily totals or missing logs |
| Symptoms, shoes, mechanics | Athlete-reported function and comparable dynamics versus inferred diagnosis or form defect |

The first nine analyses run without being given the other specialists' findings. Each returns consequential findings, evidence, confidence, alternative explanations, missing information, questions, and suggested changes. Independent roles share the same underlying measurements; their agreement does not create nine independent physiological observations.

## 4. Cross-review and synthesis

The next nine calls receive the independent findings and shared evidence. They challenge claims that could change the plan, revisit the underlying observations, and revise their conclusions where warranted. Unresolved disagreements remain visible.

For example, late-run slowing might reflect fatigue, a climb, a programmed cooldown, stops, or several factors together. The endurance specialist's observation is not resolved until workout and terrain context have been checked. Fueling or recovery explanations must be labeled uncertain unless the records support them.

The final call combines the evidence and cross-reviews into:

- Current fitness, fatigue, durability, and data-quality assessments.
- A goal assessment with confidence and cited observations.
- Finish-time ranges only when the proposed method has suitable inputs.
- A dated, conditional plan respecting supplied constraints.
- At most seven small tweaks, unresolved disagreements, and missing information.

A full new run therefore makes 19 Codex calls. Concurrency is limited to 1–3. Stage outputs are reusable only under matching input/protocol/cache fingerprints and valid schemas.

## 5. Interpretation rules and their basis

### Pace and race equivalence

Race-equivalence tools require a defensible performance input. The [V.O2 calculator](https://vdoto2.com/) produces equivalent race performances from a supplied event distance and time. This project instructs analysts not to treat an ordinary controlled training segment as an all-out race. It does not contain a separately validated VDOT implementation.

Extrapolation to a marathon adds endurance assumptions. A [study of recreational endurance runners](https://doi.org/10.1186/s13102-016-0052-y) found systematic overoptimism in a common race-time formula for marathon prediction. That motivates checking actual training and longer-effort evidence; it does not justify a universal correction to every athlete's estimate.

Two numerical methods are useful only if both have valid inputs. Reusing the same workout and heart-rate measurements does not provide independent confirmation. If suitable inputs are missing, estimates should remain unavailable instead of being invented to satisfy a requested count.

### Heart-rate drift and decoupling

The familiar 5% threshold is a coaching heuristic. [TrainingPeaks' explanation](https://www.trainingpeaks.com/blog/efficiency-factor-and-decoupling/) specifies steady aerobic work and comparable conditions. In this workflow, analysts must account for workout phase, warmup, hills, stops, weather, and sensor quality before interpreting a first-half/second-half comparison. Passing a threshold is not proof of marathon readiness; exceeding it across a progression run is not proof of failure.

### Terrain and grade-adjusted pace

Elevation totals and kilometer splits do not reconstruct the slopes inside each kilometer. [Strava's GAP methodology](https://medium.com/strava-engineering/an-improved-gap-model-8b07ae8886c3) illustrates the use of detailed windows and the variability in estimating equivalent flat effort. This app does not reproduce Strava's model or independently calculate a validated hill correction. Any vendor-provided adjusted speed remains a vendor estimate. Sampled altitude profiles support contextual interpretation, not exact claims about seconds lost to terrain.

### Recovery scores

[Garmin's Training Readiness documentation](https://www8.garmin.com/manuals/webhelp/GUID-31D23DBB-57C2-4DF7-A0C9-8D1A00AB4BE7/EN-US/GUID-C21BE0C8-A08E-4DA1-B6C6-2E0E2DDDB372.html) lists sleep, recovery time, HRV, acute load, and recent sleep/stress history among its inputs. These signals overlap. Agreement between readiness and its component metrics partly reflects score construction and must not be presented as several independent findings.

The analysts compare personal baselines and dated patterns. They cannot diagnose an illness, injury, or cause of poor performance from these metrics alone. Athlete notes about symptoms and tolerance remain necessary when they materially affect a prescription.

### Tapering and remaining workload

The [2023 endurance taper meta-analysis](https://pubmed.ncbi.nlm.nih.gov/37163550/) supports tapering while showing that results depend on several protocol components. An [observational marathon study](https://pubmed.ncbi.nlm.nih.gov/34651125/) associated disciplined longer tapers with better performance in recreational runners; it does not establish one optimal duration for an individual.

The workflow therefore asks the lead to choose a taper from completed training, race timing, and recovery context instead of imposing a fixed two-week template. A user-specified maximum of hard sessions is a ceiling, not a quota. Long runs and races count toward the plan's stressor limit.

## 6. What is checked automatically

JSON schemas validate specialist, review, and final result shapes. Additional final-plan checks enforce:

- Valid dated sessions after the dataset cutoff and within the planning horizon.
- Monday week boundaries and sessions inside their declared week.
- Nonnegative distances and durations, and no duplicate session of the same kind on a date.
- Rest is not labeled a stressor; long runs, marathon-pace sessions, and races are.
- The supplied maximum weekly stressor count is not exceeded.
- Eligible finish-time estimates have ordered positive ranges; ineligible methods have null times.
- At most seven small tweaks.

The horizon is the race date or 84 days after cutoff, whichever is earlier; without a race date, it is 28 days. A historical snapshot therefore produces a plan relative to its historical cutoff. Collect fresh evidence for current recommendations.

These checks catch structural contradictions. They do not prove physiological appropriateness, verify every cited claim, guarantee that freeform constraints were followed, or validate predictive accuracy. A schema-valid answer can still be wrong. Review the evidence and prescribed sessions before using them.

## 7. Processing boundary

Garmin credentials are entered only in the terminal. Collection is read-only, and no workout or calendar write is included in this version. Raw snapshots remain local unless the user moves them.

Analysis invokes Codex with the user's ChatGPT login. The supplied inputs are curated evidence and the goal, and the output/log directories are private. The runner requests a read-only sandbox and tells analysts not to inspect credentials or unrelated files. This is not operating-system isolation from all other readable files. The workflow should not be described as offline-only, anonymous, or a fully isolated health-data enclave.

Recorded outcomes, model interpretations, and software verification are separate evidence categories. Offline tests can validate collection/resume logic and rejection rules; a live Garmin read validates an account connection; a completed model run validates that the workflow executed. None alone validates the training plan's effectiveness.
