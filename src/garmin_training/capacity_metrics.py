"""Deterministic observations for capacity review, never a performance predictor.

The inputs are the allowlisted evidence schema, not Garmin responses. Screening
constants identify comparable-looking observations; they are not physiological
thresholds, injury limits, or proof that a workout was steady effort.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

MAX_RUNS = 10000
MAX_SEGMENTS = 256
MAX_WINDOWS_PER_RUN = 4
MIN_WINDOW_S = 1200
LONG_WINDOW_S = 2700
MIN_HR_COVERAGE = 0.95
MAX_PACE_RATIO = 1.10
MAX_PAUSE_S = 2.0
MAX_MOVING_DEFICIT_FRACTION = 0.02
MAX_BRIDGE_S = 30.0
MAX_BRIDGE_M = 100.0
TERRAIN_REVIEW_M_PER_KM = 10.0
HIGH_HR_REVIEW_BPM = 210.0
MIN_SAMPLE_DENSITY = 0.1


def _num(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def _positive(value):
    value = _num(value)
    return value if value is not None and value > 0 else None


def _nonnegative(value):
    value = _num(value)
    return value if value is not None and value >= 0 else None


def _round(value):
    return round(value, 3) if value is not None else None


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _sources(run):
    result = {value for value in run.get("source_ids", []) if isinstance(value, str)}
    for key in ("stream", "hr_zones"):
        value = run.get(key, {}).get("source_id")
        if isinstance(value, str):
            result.add(value)
    return sorted(result)


def _ref(run):
    return {"activity_id": run.get("activity_id"), "date": run.get("date"),
            "source_ids": _sources(run), "summary_source": "activities.json"}


def _weekly_load(runs, weeks):
    grouped = defaultdict(list)
    metadata = {}
    for row in weeks:
        if isinstance(row, dict) and _day(row.get("week_start")):
            metadata[row["week_start"]] = row
            grouped[row["week_start"]]
    undated = 0
    for run in runs:
        day = _day(run.get("date"))
        if day is None:
            undated += 1
            continue
        grouped[(day - timedelta(days=day.weekday())).isoformat()].append(run)
    rows = []
    for week, items in sorted(grouped.items()):
        distances = [_nonnegative(r.get("distance_m")) for r in items]
        durations = [_nonnegative(r.get("duration_s")) for r in items]
        present_d = [d for d in distances if d is not None]
        present_t = [t for t in durations if t is not None]
        longest_d = max((r for r in items if _nonnegative(r.get("distance_m")) is not None),
                        key=lambda r: r["distance_m"], default=None)
        longest_t = max((r for r in items if _nonnegative(r.get("duration_s")) is not None),
                        key=lambda r: r["duration_s"], default=None)
        rows.append({
            "week_start": week, "week_end": (_day(week) + timedelta(days=6)).isoformat(),
            "partial_boundary_week": metadata.get(week, {}).get("partial_boundary_week"),
            "run_count": len(items), "running_days": len({r.get("date") for r in items}),
            "recorded_distance_km": _round(sum(present_d) / 1000) if present_d or not items else None,
            "recorded_duration_s": _round(sum(present_t)) if present_t or not items else None,
            "recorded_duration_min": _round(sum(present_t) / 60) if present_t or not items else None,
            "distance_missing_count": len(items) - len(present_d),
            "duration_missing_count": len(items) - len(present_t),
            "longest_run_km": _round(longest_d["distance_m"] / 1000) if longest_d else None,
            "longest_run_activity_id": longest_d.get("activity_id") if longest_d else None,
            "longest_duration_s": _round(longest_t["duration_s"]) if longest_t else None,
            "longest_duration_activity_id": longest_t.get("activity_id") if longest_t else None,
            "activity_ids": [r.get("activity_id") for r in items],
            "source_ids": sorted({s for r in items for s in _sources(r)}),
        })
    complete = [r for r in rows if r["partial_boundary_week"] is False]
    comparable = [r for r in complete if not r["distance_missing_count"]
                  and not r["duration_missing_count"]]
    distances = [_nonnegative(r.get("distance_m")) for r in runs]
    durations = [_nonnegative(r.get("duration_s")) for r in runs]
    valid_d = [d for d in distances if d is not None]
    valid_t = [t for t in durations if t is not None]
    # Average exact per-run quantities, not rounded weekly display values.
    complete_keys = {r["week_start"] for r in comparable}
    complete_runs = [r for key, items in grouped.items() if key in complete_keys for r in items]
    return {
        "weeks": rows,
        "totals": {"run_count": len(runs), "undated_run_count": undated,
                   "recorded_distance_km": _round(sum(valid_d) / 1000) if valid_d or not runs else None,
                   "recorded_duration_s": _round(sum(valid_t)) if valid_t or not runs else None,
                   "recorded_duration_min": _round(sum(valid_t) / 60) if valid_t or not runs else None,
                   "distance_missing_count": len(runs) - len(valid_d),
                   "duration_missing_count": len(runs) - len(valid_t)},
        "complete_week_baseline": {
            "calendar_complete_week_count": len(complete),
            "usable_complete_week_count": len(comparable),
            "week_starts": [r["week_start"] for r in comparable],
            "mean_distance_km": _round(sum(r["distance_m"] for r in complete_runs)
                                       / 1000 / len(comparable)) if comparable else None,
            "mean_duration_min": _round(sum(r["duration_s"] for r in complete_runs)
                                        / 60 / len(comparable)) if comparable else None,
            "basis": "Supplied snapshot calendar boundaries, never today's date. Empty weeks mean no observed runs, not proven rest.",
        },
    }


def _hr_quality(run):
    duration = _positive(run.get("duration_s"))
    zones = run.get("hr_zones", {}).get("values")
    values = [z for z in zones if isinstance(z, dict)] if isinstance(zones, list) else []
    times = [_nonnegative(z.get("secsInZone")) for z in values]
    zone_s = sum(times) if times and all(t is not None for t in times) else None
    fraction = zone_s / duration if zone_s is not None and duration else None
    flags = []
    if fraction is None:
        flags.append("hr_zone_time_coverage_unknown")
    elif fraction < MIN_HR_COVERAGE:
        flags.append("low_hr_zone_time_coverage")
    elif fraction > 1.02:
        flags.append("hr_zone_time_exceeds_timer")
    maximum = _positive(run.get("max_hr_bpm"))
    if maximum and maximum > HIGH_HR_REVIEW_BPM:
        flags.append("high_observed_hr_requires_sensor_review")
    return {
        **_ref(run), "timer_duration_s": duration,
        "hr_zone_time_s": _round(zone_s), "hr_zone_time_fraction": _round(fraction),
        "coverage_basis": "Sum of configured-zone seconds / timer duration; excludes time below the lowest zone and is not exact sensor sample coverage.",
        "summary_avg_hr_bpm": _num(run.get("avg_hr_bpm")),
        "coverage_supports_full_run_average": bool(fraction is not None
                                                  and MIN_HR_COVERAGE <= fraction <= 1.02),
        "observed_max_hr_bpm": maximum,
        "observed_max_status": "Unvalidated observation, not true maximum HR or a zone-calibration input.",
        "configured_zone_lower_bounds_bpm": [
            {"zone_number": z.get("zoneNumber"), "lower_bpm": _num(z.get("zoneLowBoundary"))}
            for z in values],
        "flags": flags,
    }


def _span(segment, field):
    first = _nonnegative(segment.get(f"start_{field}"))
    last = _nonnegative(segment.get(f"end_{field}"))
    return last - first if first is not None and last is not None and last >= first else None


def _segment_check(segment):
    distance = _span(segment, "distance_m")
    timer = _span(segment, "duration_s")
    elapsed = _span(segment, "elapsed_s")
    moving = _span(segment, "moving_s")
    flags = []
    if not distance or not timer:
        flags.append("missing_or_invalid_distance_timer")
    if elapsed is None or moving is None:
        flags.append("continuity_channels_missing")
    elif timer is not None and (elapsed < timer - MAX_PAUSE_S or moving > timer + MAX_PAUSE_S):
        flags.append("inconsistent_time_channels")
    elif timer is not None and (elapsed - timer > MAX_PAUSE_S
                               or timer - moving > max(MAX_PAUSE_S, timer * MAX_MOVING_DEFICIT_FRACTION)):
        flags.append("pause_or_nonmoving_time")
    if not _positive(segment.get("mean_hr_bpm")):
        flags.append("hr_missing_in_bin")
    count = _nonnegative(segment.get("hr_bpm_sample_count"))
    total = _positive(segment.get("sample_count"))
    if count is not None and total:
        if count > total:
            flags.append("invalid_hr_sample_count")
        elif count / total < MIN_HR_COVERAGE:
            flags.append("low_hr_sample_coverage_in_bin")
    return flags


def _bridge_ok(previous, current):
    if current.get("bin_index") != previous.get("bin_index", -2) + 1:
        return False
    gaps = {}
    for field in ("distance_m", "duration_s", "elapsed_s", "moving_s"):
        start, end = _num(current.get(f"start_{field}")), _num(previous.get(f"end_{field}"))
        if start is None or end is None or start < end:
            return False
        gaps[field] = start - end
    return (gaps["distance_m"] <= MAX_BRIDGE_M and gaps["elapsed_s"] <= MAX_BRIDGE_S
            and abs(gaps["elapsed_s"] - gaps["duration_s"]) <= MAX_PAUSE_S
            and abs(gaps["duration_s"] - gaps["moving_s"]) <= MAX_PAUSE_S)


def _window(run, segments, quality):
    first, last = segments[0], segments[-1]
    spans = {field: last[f"end_{field}"] - first[f"start_{field}"]
             for field in ("distance_m", "duration_s", "elapsed_s", "moving_s")}
    counts_known = all(_nonnegative(s.get("hr_bpm_sample_count")) is not None for s in segments)
    total_samples = sum(_nonnegative(s.get("sample_count")) or 0 for s in segments)
    weights = [(_nonnegative(s.get("hr_bpm_sample_count")) if counts_known
                else _nonnegative(s.get("sample_count"))) or 0 for s in segments]
    mean_hr = sum(s["mean_hr_bpm"] * w for s, w in zip(segments, weights)) / sum(weights) if sum(weights) else None
    hr_coverage = sum(weights) / total_samples if counts_known and total_samples else None
    ascent = [_nonnegative(s.get("observed_ascent_m")) for s in segments]
    descent = [_nonnegative(s.get("observed_descent_m")) for s in segments]
    terrain_known = all(v is not None for v in ascent + descent)
    climb = sum(ascent) if terrain_known else None
    fall = sum(descent) if terrain_known else None
    gain_per_km = climb / spans["distance_m"] * 1000 if climb is not None else None
    loss_per_km = fall / spans["distance_m"] * 1000 if fall is not None else None
    flags = []
    if not counts_known:
        flags.append("within_bin_hr_sample_coverage_unknown")
    if mean_hr is None:
        flags.append("hr_weighting_unavailable")
    sample_density = total_samples / spans["duration_s"]
    if sample_density < MIN_SAMPLE_DENSITY:
        flags.append("sparse_stream_samples")
    if not quality["coverage_supports_full_run_average"]:
        flags.append("activity_hr_coverage_requires_review")
    if not terrain_known:
        flags.append("terrain_unknown")
    elif max(gain_per_km, loss_per_km) > TERRAIN_REVIEW_M_PER_KM:
        flags.append("terrain_variation_requires_comparison_review")
    stream = run.get("stream", {})
    if stream.get("segments_omitted", 0):
        flags.append("source_profile_truncated")
    maximums = [_positive(s.get("max_hr_bpm")) for s in segments]
    maximums = [v for v in maximums if v is not None]
    if maximums and max(maximums) > HIGH_HR_REVIEW_BPM:
        flags.append("high_observed_hr_requires_sensor_review")
    paces = [_span(s, "duration_s") / _span(s, "distance_m") * 1000 for s in segments]
    if max(paces) / min(paces) > MAX_PACE_RATIO:
        flags.append("variable_pace_requires_effort_review")
    return {
        **_ref(run), "window_id": f"{run.get('activity_id')}:{first['bin_index']}-{last['bin_index']}",
        "first_bin": first["bin_index"], "last_bin": last["bin_index"],
        "bin_count": len(segments), "start_distance_m": first["start_distance_m"],
        "end_distance_m": last["end_distance_m"],
        **{key: _round(value) for key, value in spans.items()},
        "pace_s_per_km": _round(spans["duration_s"] / spans["distance_m"] * 1000),
        "pace_basis": "Observed endpoint timer difference / distance difference; includes inter-bin gaps, not exact lap boundaries.",
        "bin_pace_max_min_ratio": _round(max(paces) / min(paces)),
        "mean_hr_bpm": _round(mean_hr), "hr_sample_fraction": _round(hr_coverage),
        "observed_samples_per_timer_s": _round(sample_density),
        "hr_mean_basis": ("Valid-HR-sample weighted bin means; source means were rounded."
                          if counts_known else "Approximate bin-total-sample weighted HR means; valid HR counts unavailable."),
        "continuity": "Timer, elapsed and moving channels passed numerical screen; within-bin stops and surges can remain hidden.",
        "terrain": {"observed_ascent_m": _round(climb), "observed_descent_m": _round(fall),
                    "observed_ascent_m_per_km": _round(gain_per_km),
                    "observed_descent_m_per_km": _round(loss_per_km),
                    "basis": "Binned stream elevations omit inter-bin edges and depend on sampling; these are not corrected course elevations."},
        "flags": flags,
        "comparison_status": "numerical_candidate_requires_context" if not flags else "review_required",
        "effort_and_environment_validated": False,
    }


def _run_windows(run, quality):
    stream = run.get("stream", {})
    raw = stream.get("segments", [])
    segments = [s for s in raw[:MAX_SEGMENTS] if isinstance(s, dict)] if isinstance(raw, list) else []
    checks = [_segment_check(s) for s in segments]
    best = {}
    # At most 256^2/2 endpoint comparisons per run. Only four records are built.
    for start, segment in enumerate(segments):
        if checks[start]:
            continue
        minimum, maximum = math.inf, 0.0
        for end in range(start, len(segments)):
            current = segments[end]
            if checks[end] or end > start and not _bridge_ok(segments[end - 1], current):
                break
            pace = _span(current, "duration_s") / _span(current, "distance_m") * 1000
            minimum, maximum = min(minimum, pace), max(maximum, pace)
            duration = current["end_duration_s"] - segment["start_duration_s"]
            elapsed = current["end_elapsed_s"] - segment["start_elapsed_s"]
            moving = current["end_moving_s"] - segment["start_moving_s"]
            if (abs(elapsed - duration) > MAX_PAUSE_S
                    or duration - moving > max(MAX_PAUSE_S, duration * MAX_MOVING_DEFICIT_FRACTION)):
                continue
            if duration < MIN_WINDOW_S:
                continue
            distance = current["end_distance_m"] - segment["start_distance_m"]
            mean_pace = duration / distance * 1000
            options = {"longest_continuous": (duration, -mean_pace)}
            if maximum / minimum <= MAX_PACE_RATIO:
                options["longest_pace_screen_pass"] = (duration, -mean_pace)
            for minutes in (20, 30):
                if duration >= minutes * 60:
                    options[f"fastest_at_least_{minutes}_min"] = (-mean_pace, duration)
            for label, score in options.items():
                if label not in best or score > best[label][0]:
                    best[label] = (score, start, end)
    selected = {}
    for label, (_, start, end) in best.items():
        selected.setdefault((start, end), []).append(label)
    rows = []
    for (start, end), labels in sorted(selected.items()):
        row = _window(run, segments[start:end + 1], quality)
        row["selection_labels"] = labels
        rows.append(row)
    reasons = defaultdict(int)
    for flags in checks:
        for flag in flags:
            reasons[flag] += 1
    return rows, {"checked_bin_count": len(segments), "rejected_bin_reasons": dict(reasons),
                  "capacity_bins_omitted": max(0, len(raw) - MAX_SEGMENTS) if isinstance(raw, list) else 0}


def build_capacity_metrics(runs, weeks, calibration=None):
    """Return bounded, source-cited observations from normalized evidence only.

    ``calibration`` is intentionally not interpreted here. Curated Garmin
    estimates/settings belong in their own evidence section and do not turn
    these observed windows into threshold measurements or marathon forecasts.
    """
    input_runs = [r for r in runs if isinstance(r, dict)]
    ordered = sorted(input_runs, key=lambda r: (r.get("date") or "", str(r.get("activity_id") or "")))
    selected = ordered[-MAX_RUNS:]
    qualities, windows, candidates = [], [], []
    for run in selected:
        quality = _hr_quality(run)
        observed, screening = _run_windows(run, quality)
        quality["stream_screening"] = screening
        qualities.append(quality)
        windows.extend(observed)
        long = [w for w in observed if w["duration_s"] >= LONG_WINDOW_S]
        if long:
            longest = max(long, key=lambda w: ("variable_pace_requires_effort_review" not in w["flags"],
                                              not w["flags"], w["duration_s"]))
            candidates.append({key: longest[key] for key in (
                "window_id", "activity_id", "date", "source_ids", "duration_s", "distance_m",
                "pace_s_per_km", "mean_hr_bpm", "comparison_status", "flags")})
    maxima = [q for q in qualities if q["observed_max_hr_bpm"] is not None]
    peak = max(maxima, key=lambda q: q["observed_max_hr_bpm"]) if maxima else None
    return {
        "schema_version": 1,
        "policy": {"minimum_window_s": MIN_WINDOW_S, "long_candidate_minimum_s": LONG_WINDOW_S,
                   "minimum_hr_coverage_fraction": MIN_HR_COVERAGE,
                   "maximum_bin_pace_ratio": MAX_PACE_RATIO, "maximum_pause_difference_s": MAX_PAUSE_S,
                   "maximum_moving_deficit_fraction": MAX_MOVING_DEFICIT_FRACTION,
                   "maximum_bridge_s": MAX_BRIDGE_S, "maximum_bridge_m": MAX_BRIDGE_M,
                   "terrain_review_m_per_km": TERRAIN_REVIEW_M_PER_KM,
                   "high_hr_review_bpm": HIGH_HR_REVIEW_BPM,
                   "minimum_stream_samples_per_timer_s": MIN_SAMPLE_DENSITY,
                   "meaning": "Transparent numerical review screens, not validated physiological or safety cutoffs."},
        "weekly_load": _weekly_load(selected, weeks),
        "heart_rate_quality": {"runs": qualities,
                               "observed_peak": ({"bpm": peak["observed_max_hr_bpm"], **{k: peak[k] for k in ("activity_id", "date", "source_ids")}} if peak else None),
                               "true_max_hr_status": "not_established"},
        "sustained_windows": windows, "long_steady_candidates": candidates,
        "calibration": {"provided": calibration is not None,
                        "interpretation": "Not used to infer marathon HR, threshold, or true maximum HR."},
        "truncation": {"input_run_count": len(input_runs), "analyzed_run_count": len(selected),
                       "runs_omitted": max(0, len(input_runs) - MAX_RUNS),
                       "max_bins_per_run": MAX_SEGMENTS, "max_windows_per_run": MAX_WINDOWS_PER_RUN},
        "limitations": [
            "No lactate threshold, marathon HR ceiling, finish prediction, or validated cardiac drift is inferred.",
            "Stable binned pace is not necessarily steady effort; hills, heat, wind, fueling, stops within bins and workout purpose require review.",
            "Zone seconds provide a coverage proxy. Time below the lowest configured boundary can be absent even with a working sensor.",
            "Configured HR zones are settings, not independent threshold or maximum-HR measurements.",
            "Stream windows may be sampled. Absence of detected stops does not prove uninterrupted raw recordings.",
            "Fastest-window selection is descriptive and biased toward favorable segments; it is not representative sustainable pace.",
        ],
    }
