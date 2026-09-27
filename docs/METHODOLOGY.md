# Methodology

The goal is to turn recorded training into a traceable assessment and a proposed running plan. The software separates source collection, evidence preparation, goal-blind interpretation, cross-review, capacity synthesis, goal feasibility, race strategy, training and reassessment. It publishes only after a separate model audit and deterministic checks. A target time is a user goal, not evidence of current fitness.

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
| Lactate-threshold estimate history | Requested dates, daily aggregation; latest estimate also requested |
| HR zones/profile/device configuration | Retrieval-time state, at most three device-settings reads; explicitly not historical configuration |
| Original FIT records, external historical weather, route geocoding | Not collected by this version |

The collector's selected-run count, truncation flag, endpoint outcomes, and sampling settings are carried into the evidence. Supply `--detail-start` explicitly when older runs need the same level of examination as recent ones.

Snapshots are observation records. Resuming reuses hash-verified successes, including empty responses; it retries failed calls. It does not refresh a previously successful reading. New observations require a new snapshot identifier.

## 2. Curated evidence

The evidence builder reads the manifest and verifies referenced local payloads. It creates an indexed Markdown overview and a JSON bundle containing normalized activities, dated wellness records, bounded running details, source references, and limitations. Source payloads with unknown shapes remain available for local inspection; they are not automatically copied into the model context.

Normalization uses explicit fields and units. Garmin activity summaries commonly express distance in meters, duration in seconds, and speed in meters per second; these must not be confused with kilometers or minutes per kilometer. Moving, timer, and elapsed duration remain different measures. Pace changes caused by stops cannot automatically be treated as fatigue.

Activity chart channels are mapped through Garmin's `metricDescriptors` rather than fixed array positions. The builder summarizes known channels into bounded profiles. Profiles may be sampled first by Garmin and then summarized locally. Sample-weighted means are not automatically time-weighted means, and coarse bins cannot reconstruct every short hill, stop, or surge. Use summary elevation totals for overall ascent/descent rather than assuming binned changes recover the full route.

Exact coordinates, route polylines, owner identifiers, and activity names/descriptions are excluded from the curated bundle. Provided `locationName` area context, dates, activity IDs, and relevant health metrics remain. Dated athlete observations are supplied throughout analysis. The objective, constraints and planning notes are introduced only after capacity synthesis. This reduces unnecessary disclosure but does not make the dataset anonymous.

## 3. Nine independent questions

| Role | Required distinction |
|---|---|
| Training history and progression | Recorded training versus a complete history; missing records versus rest |
| Calibration and quality | Configured zones/device estimates versus measured calibration; coverage and sensor artifacts |
| Terrain and environment | Terrain/context effects versus fitness or fatigue changes |
| Pace and effort | Comparable pace/HR observations and duration versus inferred sustainability |
| Long-run endurance | Ability to run target pace briefly versus ability to sustain the required event duration |
| Sleep and recovery | Personal patterns and associations versus demonstrated causes |
| Background load and strength | Extra activity versus running included in steps; documented strength versus invented weakness |
| Fueling and hydration | Documented intake/tolerance versus daily totals or missing logs |
| Symptoms, shoes, mechanics | Athlete-reported function and comparable dynamics versus inferred diagnosis or form defect |

The first nine analyses receive neither the objective nor the other specialists' findings. Goal description, target, race date, constraints and planning notes are absent from their supplied working directory. Structured athlete observations carry dated testimony; callers must not mix aspirations or prior coaching schedules into those observations. This separation reduces anchoring, but does not make the process blind to every contextual hint or isolate other OS-readable files. Each returns consequential findings, evidence, confidence, alternative explanations, missing information, questions, and suggested changes. Independent roles share the same underlying measurements; their agreement does not create nine independent physiological observations.

## 4. Cross-review and synthesis

The next nine calls receive the independent findings and shared evidence. They challenge claims that could change the plan, revisit the underlying observations, and revise their conclusions where warranted. Unresolved disagreements remain visible.

For example, late-run slowing might reflect fatigue, a climb, a programmed cooldown, stops, or several factors together. The endurance specialist's observation is not resolved until workout and terrain context have been checked. Fueling or recovery explanations must be labeled uncertain unless the records support them.

The nineteenth call synthesizes current capacity without the goal: calibration, observed pace/HR for explicit durations and conditions, durability, documented strength, achieved training baseline and unknowns. It is saved as `capacity.json` and remains unchanged when the objective is introduced.

The twentieth call reads that assessment before receiving the goal. It separates goal feasibility from current capacity, develops a provisional race strategy, and explains each training dose against the completed baseline. A goal is neither a predicted finish time nor a reason to force arbitrary overload or a blanket rest plan.

The twenty-first call independently challenges the draft against source evidence. It checks unsupported HR targets, projections, pain assumptions, constraint violations, load rationale, weekly arithmetic, and goal anchoring. A pass requires no blockers, required changes or failed checks. Otherwise, at most two revision/re-audit cycles are allowed. Deterministic draft errors are supplied to the audit and revision stages as `validation.json`, so the same bounded repair loop can correct them. Failure retains private drafts but publishes no final report or API result. A passing model audit is a review outcome, not clinical or predictive validation.

A new run therefore uses 21–25 Codex calls, with concurrency 1–3 for the specialist stages. Resumption reuses schema-valid, semantically valid stage outputs only when inputs, prompts, schemas, implementation and upstream output fingerprints match. Progress records distinguish completed stages from new calls made in that attempt.

### Deterministic capacity summaries

The evidence bundle also contains `capacity_metrics`: weekly loads recomputed from normalized metres/seconds, complete-week baselines, longest runs, HR-zone coverage and bounded continuous-window candidates. It does not generate a race prediction or calibrated HR zone. Coverage, pause, pace-variability and outlier flags expose selection limits. They are operational filters and not physiological pass/fail thresholds. HR means derived from profile bins remain sample-weighted; repeated terrain can still confound comparisons. The run-level and segment source records remain available for challenge.

## 5. Interpretation rules and their basis

### Pace and race equivalence

Race-equivalence tools require a defensible performance input. The [V.O2 calculator](https://vdoto2.com/) produces equivalent race performances from a supplied event distance and time. This project instructs analysts not to treat an ordinary controlled training segment as an all-out race. It does not contain a separately validated VDOT implementation.

Extrapolation to a marathon adds endurance assumptions. A [study of recreational endurance runners](https://doi.org/10.1186/s13102-016-0052-y) found systematic overoptimism in a common race-time formula for marathon prediction. That motivates checking actual training and longer-effort evidence; it does not justify a universal correction to every athlete's estimate.

Two numerical methods are useful only if both have valid inputs. Reusing the same workout and heart-rate measurements does not provide independent confirmation. If suitable inputs are missing, estimates should remain unavailable instead of being invented to satisfy a requested count.

### HR calibration and sustainability

Garmin zones are configured settings. A configured maximum HR, the highest observed sample and a measured maximum test are different things. A device-estimated lactate threshold is useful evidence with source date and uncertainty; it is not a laboratory measurement or a direct marathon-HR prescription. The collector records latest and historical estimates separately, preserves retrieval dates, and refuses to invent pace when the source unit is unspecified.

A [smartwatch threshold validation study](https://pubmed.ncbi.nlm.nih.gov/40740423/) illustrates why individual device estimates need qualification. A [large marathon decoupling study](https://pubmed.ncbi.nlm.nih.gov/35511416/) also shows why the pace/HR relationship over duration matters. Neither supplies a universal BPM progression or proves that a brief target-pace segment can extend to a full marathon.

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

JSON schemas validate specialist, cross-review, capacity, proposal and audit shapes. Additional final-plan checks enforce:

- Valid dated sessions after the dataset cutoff and within the planning horizon.
- Monday week boundaries and sessions inside their declared week.
- Nonnegative distances and durations, and no duplicate session of the same kind on a date.
- Rest is not labeled a stressor; long runs, marathon-pace sessions, and races are.
- The supplied maximum weekly stressor count is not exceeded.
- Eligible finish-time estimates have ordered positive ranges; ineligible methods have null times.
- Weekly distance equals the listed running distances; rationale totals, frequency, stressors, longest sessions and baseline deltas must agree. Unknown durations/distances propagate to aggregate fields.
- An unknown race-HR basis cannot coexist with numeric HR bounds.
- At most seven small tweaks.
- Publication requires an independent audit marked pass with zero blockers, required changes or failed checks.

The horizon is the race date or 84 days after cutoff, whichever is earlier; without a race date, it is 28 days. A historical snapshot therefore produces a plan relative to its historical cutoff. Collect fresh evidence for current recommendations.

These checks catch structural contradictions. They do not prove physiological appropriateness, verify every cited claim, guarantee that freeform constraints were followed, or validate predictive accuracy. A schema-valid answer can still be wrong. Review the evidence and prescribed sessions before using them.

## 7. Processing boundary

Garmin credentials are entered only in the terminal. Collection is read-only, and no workout or calendar write is included in this version. Raw snapshots remain local unless the user moves them.

Analysis invokes Codex with the user's ChatGPT login. The supplied inputs are curated evidence and athlete testimony, followed by the goal during planning, and the output/log directories are private. The runner requests a read-only sandbox and tells analysts not to inspect credentials or unrelated files. This is not operating-system isolation from all other readable files. The workflow should not be described as offline-only, anonymous, or a fully isolated health-data enclave.

Recorded outcomes, model interpretations, and software verification are separate evidence categories. Offline tests can validate collection/resume logic and rejection rules; a live Garmin read validates an account connection; a completed model run validates that the workflow executed. None alone validates the training plan's effectiveness.
