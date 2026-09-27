"""Time-aligned, descriptive within-athlete evidence; no causal or race model.

Only normalized allowlisted evidence is consumed. No coordinates, raw records,
credentials, network calls, model calls or third-party dependencies are used.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

DAY = 86400
MAX_RUNS = 2000
MAX_DAYS = 3660
MAX_SLEEP_RECORDS = 5000
MIN_BASELINE_N = 3


def _number(value, positive=False):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0 or positive and value == 0):
        return None
    return float(value)


def _round(value):
    return round(value, 3) if value is not None else None


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _utc(value):
    """Garmin fields named GMT are UTC even when their string lacks an offset."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (ValueError, OverflowError):
        return None


def _millis(values, *keys):
    for key in keys:
        value = _number(values.get(key), positive=True)
        if value is not None:
            try:
                datetime.fromtimestamp(value / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            return value / 1000
    return None


def _first_number(values, *keys):
    for key in keys:
        value = _number(values.get(key))
        if value is not None:
            return value
    return None


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None


def _sources(records):
    result = set()
    for row in records:
        if isinstance(row.get("source_id"), str):
            result.add(row["source_id"])
        result.update(s for s in row.get("source_ids", []) if isinstance(s, str))
    return sorted(result)


def _dated_values(daily, kind, keys):
    grouped = defaultdict(list)
    for row in daily:
        if row.get("kind") == kind and _day(row.get("date")):
            value = _first_number(row.get("values", {}), *keys)
            if value is not None and (kind not in {"hrv", "resting_hr"} or value > 0):
                grouped[row["date"]].append((value, row))
    result = {}
    for day, rows in grouped.items():
        # Conflicting duplicate records are unknown, not arbitrarily averaged.
        unique = {value for value, _ in rows}
        result[day] = {"date": day, "value": rows[0][0] if len(unique) == 1 else None,
                       "source_ids": _sources([row for _, row in rows]),
                       "conflicting_records": len(unique) > 1}
    return result


def _sleep_records(daily):
    records, rejected, duplicates = {}, defaultdict(int), 0
    sleep_rows = [r for r in daily if r.get("kind") == "sleep"]
    for row in sleep_rows[:MAX_SLEEP_RECORDS]:
        values = row.get("values", {})
        start = _millis(values, "gmtSleepStartTimeInMillis", "sleepStartTimestampGMT")
        end = _millis(values, "gmtSleepEndTimeInMillis", "sleepEndTimestampGMT")
        if start is None or end is None or end <= start or end - start > 30 * 3600:
            rejected["missing_or_invalid_explicit_gmt_interval"] += 1
            continue
        duration = _first_number(values, "totalSleepTimeInSeconds", "sleepTimeSeconds", "totalSleepSeconds")
        if duration is not None and duration > end - start + 300:
            rejected["sleep_duration_exceeds_recorded_interval"] += 1
            duration = None
        local_start = _millis(values, "localSleepStartTimeInMillis", "sleepStartTimestampLocal")
        local_end = _millis(values, "localSleepEndTimeInMillis", "sleepEndTimestampLocal")
        offset_start = (local_start - start) / 60 if local_start is not None else None
        offset_end = (local_end - end) / 60 if local_end is not None else None
        local_valid = (offset_start is not None and offset_end is not None
                       and abs(offset_start) <= 14 * 60 and abs(offset_end) <= 14 * 60)
        wake_date = datetime.fromtimestamp(local_end, timezone.utc).date().isoformat() if local_valid else None
        identity = hashlib.sha256(f"{start}:{end}".encode()).hexdigest()[:16]
        night = {"night_id": f"sleep:{identity}", "date_label": row.get("date"),
                 "wake_local_date": wake_date, "start_gmt": _iso(start), "end_gmt": _iso(end),
                 "start_epoch_s": start, "end_epoch_s": end,
                 "duration_hours": _round(duration / 3600) if duration is not None else None,
                 "overnight_hrv_ms": _number(_first_number(values, "avgOvernightHrv", "avgSleepHRV"), positive=True),
                 "reported_resting_hr_bpm": _number(values.get("restingHeartRate"), positive=True),
                 "local_bedtime_minutes": (local_start % DAY) / 60 if local_valid else None,
                 "local_wake_minutes": (local_end % DAY) / 60 if local_valid else None,
                 "utc_offset_minutes": offset_end if local_valid and offset_start == offset_end else None,
                 "date_label_matches_local_wake": row.get("date") == wake_date if wake_date else None,
                 "source_ids": _sources([row]), "_conflicting_fields": []}
        key = (start, end)
        if key in records:
            duplicates += 1
            existing = records[key]
            existing["source_ids"] = sorted(set(existing["source_ids"] + night["source_ids"]))
            for field in ("duration_hours", "overnight_hrv_ms", "reported_resting_hr_bpm"):
                if field in existing["_conflicting_fields"] or night[field] is None:
                    continue
                if existing[field] is None:
                    existing[field] = night[field]
                elif existing[field] != night[field]:
                    existing[field] = None
                    existing["_conflicting_fields"].append(field)
                    rejected["conflicting_duplicate_sleep_value"] += 1
        else:
            records[key] = night
    usable = [n for n in records.values() if n["duration_hours"] is not None and n["duration_hours"] > 0]
    rejected["intervals_without_positive_sleep_duration"] = len(records) - len(usable)
    return sorted(usable, key=lambda n: n["end_epoch_s"]), {
        "input_sleep_records": len(sleep_rows), "usable_gmt_intervals": len(records),
        "usable_completed_sleep_records": len(usable),
        "duplicate_intervals_merged": duplicates, "rejected_or_qualified": dict(rejected),
        "sleep_records_omitted": max(0, len(sleep_rows) - MAX_SLEEP_RECORDS)}


def _night_key(night):
    return night["wake_local_date"] or night["end_gmt"][:10]


def _window_nights(nights, after, before, limit=None):
    eligible = [n for n in nights if after < n["end_epoch_s"] <= before]
    per_night = {}
    for night in eligible:
        key = _night_key(night)
        previous = per_night.get(key)
        if previous is None or (night["duration_hours"] or 0) > (previous["duration_hours"] or 0):
            per_night[key] = night
    selected = sorted(per_night.values(), key=lambda n: n["end_epoch_s"])
    return selected[-limit:] if limit else selected


def _sleep_history(nights, before, days):
    selected = _window_nights(nights, before - days * DAY, before, days)
    durations = [n["duration_hours"] for n in selected if n["duration_hours"] is not None]
    offsets = {n["utc_offset_minutes"] for n in selected}
    regularity = {"status": "insufficient_timing_records", "bedtime_median_absolute_deviation_minutes": None,
                  "wake_median_absolute_deviation_minutes": None}
    if len(selected) >= MIN_BASELINE_N and None not in offsets and len(offsets) == 1:
        def circular_mad(field):
            values = [n[field] for n in selected]
            def delta(a, b):
                return abs((a - b + 720) % 1440 - 720)
            center = min(values, key=lambda x: sum(delta(x, other) for other in values))
            return _round(median(delta(x, center) for x in values))
        regularity = {"status": "same_observed_utc_offset",
                      "bedtime_median_absolute_deviation_minutes": circular_mad("local_bedtime_minutes"),
                      "wake_median_absolute_deviation_minutes": circular_mad("local_wake_minutes")}
    elif len(offsets - {None}) > 1:
        regularity["status"] = "clock_offset_changed_do_not_compare_wall_times"
    return {"window_days": days, "completed_sleep_count": len(selected),
            "duration_record_count": len(durations),
            "mean_sleep_hours": _round(sum(durations) / len(durations)) if durations else None,
            "median_sleep_hours": _round(median(durations)) if durations else None,
            "night_ids": [n["night_id"] for n in selected], "source_ids": _sources(selected),
            "timing_regularity": regularity,
            "basis": "Completed main-sleep records within the preceding window, longest observed episode per wake date, capped at the requested night count; missing nights are not zero sleep."}


def _prior_daily(series, run_date, value_name):
    eligible = [row for day, row in sorted(series.items()) if day < run_date
                and _day(day) >= _day(run_date) - timedelta(days=2) and row["value"] is not None]
    if not eligible:
        return {value_name: None, "status": "no_recent_completed_calendar_day", "source_ids": []}
    latest = eligible[-1]
    base = [r for d, r in series.items() if _day(latest["date"]) - timedelta(days=7) <= _day(d) < _day(latest["date"])
            and r["value"] is not None]
    baseline = median(r["value"] for r in base) if len(base) >= MIN_BASELINE_N else None
    return {value_name: latest["value"], "date": latest["date"], "source_ids": latest["source_ids"],
            "baseline_count": len(base), "preceding_7_day_median": _round(baseline),
            "delta_from_baseline": _round(latest["value"] - baseline) if baseline is not None else None,
            "baseline_source_ids": _sources(base), "status": "prior_calendar_day_only",
            "basis": "Same-day daily aggregates are excluded because their measurement/availability time is unknown."}


def _load(prior_runs, before, days, coverage_start, run_date):
    in_window = [r for r in prior_runs if before - days * DAY <= r["_start"] < before]
    unknown_end = [r for r in in_window if r["_end"] is None]
    rows = [r for r in in_window if r["_end"] is not None and r["_end"] <= before]
    distances = [_number(r.get("distance_m")) for r in rows]
    durations = [_number(r.get("duration_s")) for r in rows]
    return {"window_days": days, "run_count": len(rows),
            "recorded_distance_km": _round(sum(d for d in distances if d is not None) / 1000),
            "recorded_duration_minutes": _round(sum(t for t in durations if t is not None) / 60),
            "distance_missing_count": distances.count(None), "duration_missing_count": durations.count(None),
            "unconfirmed_end_activity_ids": [r.get("activity_id") for r in unknown_end],
            "unconfirmed_end_count": len(unknown_end),
            "complete_observation_window": bool(not unknown_end and coverage_start and _day(run_date)
                                                 and _day(run_date) - timedelta(days=days) >= coverage_start),
            "activity_ids": [r.get("activity_id") for r in rows], "source_ids": _sources(rows),
            "basis": "Completed runs starting in the preceding rolling GMT interval; not an acute:chronic risk estimate."}


def _step_load(steps, run_dates, day, days):
    selected = [r for d, r in steps.items() if day - timedelta(days=days) <= _day(d) < day
                and r["value"] is not None]
    no_run = [r for r in selected if r["date"] not in run_dates]
    return {"calendar_days": days, "step_record_days": len(selected),
            "recorded_total_steps": sum(r["value"] for r in selected) if selected else None,
            "dates_without_observed_runs_count": len(no_run),
            "steps_on_dates_without_observed_runs": sum(r["value"] for r in no_run) if no_run else None,
            "nonrunning_steps": None, "source_ids": _sources(selected),
            "basis": "Total steps include running. No subtraction is performed; dates without observed runs are only a background-load proxy, not proven non-running days."}


def _run_context(run, all_runs, nights, daily_hrv, daily_rhr, steps, run_dates, coverage_start):
    before = run["_start"]
    prior_nights = _window_nights(nights, -math.inf, before)
    latest = prior_nights[-1] if prior_nights and before - prior_nights[-1]["end_epoch_s"] <= 36 * 3600 else None
    hrv = {"value_ms": None, "status": "no_timestamp_aligned_overnight_hrv", "source_ids": []}
    if latest:
        value = latest["overnight_hrv_ms"]
        sources = latest["source_ids"]
        provenance = "completed_sleep_avgOvernightHrv"
        # Calendar HRV is overnight-labelled but usable on the run date only
        # when a matching, already completed sleep independently anchors it.
        daily = daily_hrv.get(latest["date_label"])
        if value is None and latest["date_label_matches_local_wake"] and daily and daily["value"] is not None:
            value, sources = daily["value"], daily["source_ids"]
            provenance = "daily_lastNightAvg_linked_to_completed_sleep"
        prior = [n for n in _window_nights(nights, latest["end_epoch_s"] - 7 * DAY, latest["end_epoch_s"] - .001, 7)
                 if n["overnight_hrv_ms"] is not None and _night_key(n) != _night_key(latest)]
        base = median(n["overnight_hrv_ms"] for n in prior) if len(prior) >= MIN_BASELINE_N else None
        hrv = {"value_ms": value, "status": provenance if value is not None else "overnight_hrv_missing",
               "night_id": latest["night_id"], "source_ids": sources,
               "prior_7_night_baseline_count": len(prior), "prior_7_night_median_ms": _round(base),
               "deviation_percent": _round(100 * (value - base) / base) if value is not None and base else None,
               "baseline_source_ids": _sources(prior)}
    day = _day(run.get("date"))
    return {"activity_id": run.get("activity_id"), "date": run.get("date"),
            "start_gmt": _iso(before), "source_ids": _sources([run]),
            "preceding_sleep": ({k: latest[k] for k in ("night_id", "date_label", "wake_local_date", "start_gmt", "end_gmt", "duration_hours", "date_label_matches_local_wake", "source_ids")}
                                if latest else None),
            "hours_since_sleep_end": _round((before - latest["end_epoch_s"]) / 3600) if latest else None,
            "prior_3_nights": _sleep_history(nights, before, 3),
            "prior_7_nights": _sleep_history(nights, before, 7),
            "pre_run_hrv": hrv,
            "pre_run_rhr": _prior_daily(daily_rhr, run["date"], "value_bpm") if day else {"value_bpm": None, "status": "run_local_date_missing"},
            "prior_running_load": {str(days): _load(all_runs, before, days, coverage_start, run.get("date")) for days in (7, 28)},
            "prior_steps": {str(days): _step_load(steps, run_dates, day, days) for days in (7, 28)} if day else {},
            "alignment_status": "explicit_run_and_sleep_gmt_only"}


def _observation(run, windows, quality):
    valid = [w for w in windows if "longest_pace_screen_pass" in w.get("selection_labels", [])
             and _number(w.get("hr_sample_fraction")) is not None and w["hr_sample_fraction"] >= .95
             and _number(w.get("mean_hr_bpm"), positive=True)
             and not any(flag in w.get("flags", []) for flag in ("sparse_stream_samples", "high_observed_hr_requires_sensor_review", "source_profile_truncated"))]
    selected = max(valid, key=lambda w: w.get("duration_s", 0), default=None)
    flags = []
    if selected:
        pace, hr, duration = selected.get("pace_s_per_km"), selected.get("mean_hr_bpm"), selected.get("duration_s")
        source_ids = selected.get("source_ids", [])
        basis = "longest_pace_screen_pass_window"
    else:
        duration = _number(run.get("duration_s"), positive=True)
        distance = _number(run.get("distance_m"), positive=True)
        pace = 1000 * duration / distance if duration and distance else None
        hr, source_ids, basis = _number(run.get("avg_hr_bpm"), positive=True), _sources([run]), "whole_run_summary"
        elapsed, moving = _number(run.get("elapsed_s")), _number(run.get("moving_s"))
        segments = run.get("stream", {}).get("segments", [])
        speeds = [s.get("pace_from_mean_speed_s_per_km") for s in segments
                  if _number(s.get("pace_from_mean_speed_s_per_km"), positive=True)]
        if not speeds or max(speeds) / min(speeds) > 1.10:
            flags.append("whole_run_steady_pace_unestablished")
        if not duration or elapsed is None or moving is None or elapsed - duration > 2 or duration - moving > max(2, duration * .02):
            flags.append("whole_run_continuity_unestablished")
    if not quality.get("coverage_supports_full_run_average"):
        flags.append("activity_hr_coverage_limited_or_unknown")
    if not duration or duration < 1200 or not _number(pace, positive=True) or not _number(hr, positive=True):
        flags.append("insufficient_pace_hr_duration")
    gain, distance = _number(run.get("elevation_gain_m")), _number(run.get("distance_m"), positive=True)
    gain_per_km = gain / distance * 1000 if gain is not None and distance else None
    if gain_per_km is None:
        flags.append("terrain_unknown")
    route = run.get("route_context", {})
    group = route.get("route_group_id") if isinstance(route.get("route_group_id"), str) else None
    location = run.get("location_name") if isinstance(run.get("location_name"), str) else None
    if not group and not location:
        flags.append("route_and_location_unknown")
    try:
        local = datetime.fromisoformat(run.get("start_local") or "")
        local_hour = local.hour + local.minute / 60
    except (ValueError, TypeError):
        local_hour = None
    return {"activity_id": run.get("activity_id"), "date": run.get("date"), "source_ids": source_ids,
            "basis": basis, "window_id": selected.get("window_id") if selected else None,
            "pace_s_per_km": _round(pace), "mean_hr_bpm": _round(hr), "duration_s": _round(duration),
            "route_group_id": group, "location_name": location,
            "local_start_hour": _round(local_hour),
            "activity_ascent_m_per_km": _round(gain_per_km),
            "window_terrain_flags": [f for f in selected.get("flags", []) if "terrain" in f] if selected else [],
            "eligible_for_descriptive_matching": not flags, "flags": flags,
            "context_limit": "Whole-activity elevation only approximates selected-window terrain; weather, surface, workout intent and actual HR sensor may remain unverified."}


def _exposure_rows(observations, contexts):
    by_id = {r["activity_id"]: r for r in contexts}
    definitions = [
        ("prior_3_night_mean_sleep_hours", .5, lambda c: c["prior_3_nights"]["mean_sleep_hours"]
         if c["prior_3_nights"]["duration_record_count"] == 3 else None),
        ("overnight_hrv_deviation_percent", 5.0, lambda c: c["pre_run_hrv"].get("deviation_percent")),
        ("prior_7_day_running_minutes", 30.0, lambda c: c["prior_running_load"]["7"]["recorded_duration_minutes"]
         if c["prior_running_load"]["7"]["complete_observation_window"] and not c["prior_running_load"]["7"]["duration_missing_count"] else None),
    ]
    for label, contrast, extract in definitions:
        rows = [(o, extract(by_id[o["activity_id"]])) for o in observations
                if o["eligible_for_descriptive_matching"] and o["activity_id"] in by_id]
        rows = [(o, value) for o, value in rows if value is not None]
        yield label, contrast, rows


def _rank_correlation(left, right):
    if len(left) < 5:
        return None
    def ranks(values):
        ordered = sorted(range(len(values)), key=lambda i: values[i])
        output, cursor = [0.0] * len(values), 0
        while cursor < len(values):
            end = cursor + 1
            while end < len(values) and values[ordered[end]] == values[ordered[cursor]]:
                end += 1
            rank = (cursor + 1 + end) / 2
            for index in ordered[cursor:end]:
                output[index] = rank
            cursor = end
        return output
    x, y = ranks(left), ranks(right)
    center = (len(x) + 1) / 2
    numerator = sum((a - center) * (b - center) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - center) ** 2 for a in x) * sum((b - center) ** 2 for b in y))
    return _round(numerator / denominator) if denominator else None


def _exploratory_associations(observations, contexts):
    output = []
    for label, _, rows in _exposure_rows(observations, contexts):
        strata = defaultdict(list)
        for observation, value in rows:
            key = (observation["route_group_id"] or observation["location_name"],
                   int(observation["mean_hr_bpm"] // 5) * 5,
                   int(observation["activity_ascent_m_per_km"] // 5) * 5,
                   int(observation["duration_s"] // 1200) * 20)
            strata[key].append((observation, value))
        summaries = []
        for key, members in sorted(strata.items(), key=lambda item: str(item[0])):
            if len(members) < 4:
                continue
            center = median(value for _, value in members)
            lower = [o for o, v in members if v < center]
            higher = [o for o, v in members if v >= center]
            if len(lower) < 2 or len(higher) < 2:
                continue
            summaries.append({"route_or_location": key[0], "hr_band_low_bpm": key[1],
                              "ascent_band_low_m_per_km": key[2], "duration_band_low_minutes": key[3],
                              "run_count": len(members), "lower_exposure_count": len(lower), "higher_exposure_count": len(higher),
                              "exposure_split_median": _round(center),
                              "higher_minus_lower_median_pace_seconds_per_km": _round(median(o["pace_s_per_km"] for o in higher) - median(o["pace_s_per_km"] for o in lower)),
                              "activity_ids": [o["activity_id"] for o, _ in members]})
        correlation = _rank_correlation([v for _, v in rows], [o["pace_s_per_km"] for o, _ in rows])
        output.append({"exposure": label, "run_count": len(rows),
                       "status": "exploratory_within_person_description" if len(rows) >= 5 else "insufficient_observations",
                       "unadjusted_spearman_pace_correlation": correlation,
                       "observed_date_range": [min(o["date"] for o, _ in rows), max(o["date"] for o, _ in rows)] if rows else None,
                       "activity_ids": [o["activity_id"] for o, _ in rows], "stratified_comparisons": summaries,
                       "interpretation": "Unadjusted rank correlation uses seconds/km: positive means higher exposure accompanies slower observed pace. It does not control HR, weather, time trend, intensity choice or recovery. Strata add coarse HR/duration/terrain/location controls, not causal identification; overlapping nights and loads remain dependent. No p-value or personal effect estimate is justified."})
    return output


def _matched_associations(observations, contexts):
    output = []
    for label, contrast, rows in _exposure_rows(observations, contexts):
        center = median(v for _, v in rows) if rows else None
        low = sorted([(o, v) for o, v in rows if v < center], key=lambda x: (x[0]["date"], str(x[0]["activity_id"]))) if rows else []
        high = [(o, v) for o, v in rows if v >= center] if rows else []
        used_dates, used_ids, pairs = set(), set(), []
        for left, lv in low:
            if left["date"] in used_dates or left["activity_id"] in used_ids:
                continue
            choices = []
            for right, rv in high:
                if right["activity_id"] in used_ids or right["date"] in used_dates or right["date"] == left["date"] or rv - lv < contrast:
                    continue
                gap = abs((_day(left["date"]) - _day(right["date"])).days)
                if gap > 42 or abs(left["mean_hr_bpm"] - right["mean_hr_bpm"]) > 3:
                    continue
                if max(left["duration_s"], right["duration_s"]) / min(left["duration_s"], right["duration_s"]) > 1.25:
                    continue
                if abs(left["activity_ascent_m_per_km"] - right["activity_ascent_m_per_km"]) > 5:
                    continue
                if left["local_start_hour"] is not None and right["local_start_hour"] is not None:
                    if abs((left["local_start_hour"] - right["local_start_hour"] + 12) % 24 - 12) > 3:
                        continue
                same_route = left["route_group_id"] and left["route_group_id"] == right["route_group_id"]
                if left["route_group_id"] and right["route_group_id"] and not same_route:
                    continue
                same_location = left["location_name"] and left["location_name"] == right["location_name"]
                if not same_route and not same_location:
                    continue
                # Matching never uses the pace outcome to select a favorable pair.
                score = (not bool(same_route), gap, abs(left["mean_hr_bpm"] - right["mean_hr_bpm"]), str(right["activity_id"]))
                choices.append((score, right, rv))
            if not choices:
                continue
            _, right, rv = min(choices, key=lambda x: x[0])
            used_ids.update((left["activity_id"], right["activity_id"]))
            used_dates.update((left["date"], right["date"]))
            pairs.append({"lower_exposure_activity_id": left["activity_id"], "higher_exposure_activity_id": right["activity_id"],
                          "lower_exposure": _round(lv), "higher_exposure": _round(rv),
                          "higher_minus_lower_pace_seconds_per_km": _round(right["pace_s_per_km"] - left["pace_s_per_km"]),
                          "hr_difference_bpm": _round(right["mean_hr_bpm"] - left["mean_hr_bpm"]),
                          "location_only_match": not bool(left["route_group_id"] and left["route_group_id"] == right["route_group_id"]),
                          "source_ids": sorted(set(left["source_ids"] + right["source_ids"]))})
        output.append({"exposure": label, "eligible_run_count": len(rows), "split_median": _round(center),
                       "minimum_exposure_contrast": contrast, "matched_pair_count": len(pairs),
                       "unique_run_count": len(used_ids), "pairs": pairs,
                       "status": "descriptive_matched_association" if len(pairs) >= 3 else "insufficient_matched_pairs",
                       "median_pace_difference_seconds_per_km": _round(median(p["higher_minus_lower_pace_seconds_per_km"] for p in pairs)) if len(pairs) >= 3 else None,
                       "interpretation": "Higher exposure minus lower exposure at similar recorded HR; positive means slower. Deterministic greedy matching is order-sensitive, with no favorable pace selection. No causal or independent-sample inference; pairs can still share nights, rolling loads and unmeasured conditions."})
    return output


def _recovery(runs, activities, nights, daily_rhr):
    episodes, overlap_skipped, previous_end = [], [], -math.inf
    for run in runs:
        if run["_end"] is None:
            continue
        reasons = []
        if (_number(run.get("duration_s")) or 0) >= 5400:
            reasons.append("recorded_duration_at_least_90_minutes")
        if (_number(run.get("aerobic_training_effect")) or 0) >= 4:
            reasons.append("garmin_aerobic_training_effect_at_least_4")
        if (_number(run.get("anaerobic_training_effect")) or 0) >= 2.5:
            reasons.append("garmin_anaerobic_training_effect_at_least_2_5")
        if not reasons:
            continue
        if run["_start"] < previous_end:
            overlap_skipped.append(run.get("activity_id"))
            continue
        previous_end = run["_end"] + 48 * 3600
        baseline = _window_nights(nights, run["_start"] - 7 * DAY, run["_start"], 7)
        after = [n for n in _window_nights(nights, run["_end"], previous_end)
                 if n["start_epoch_s"] >= run["_end"]]
        outcomes = []
        for phase, low, high in (("0_to_24_hours", 0, 24), ("24_to_48_hours", 24, 48)):
            candidates = [n for n in after if low * 3600 < n["end_epoch_s"] - run["_end"] <= high * 3600]
            night = candidates[0] if candidates else None
            if night is None:
                outcomes.append({"phase": phase, "status": "no_completed_sleep_observed"})
                continue
            intervening = [a for a in activities if a.get("activity_id") != run.get("activity_id")
                           and run["_end"] <= a["_start"] < night["end_epoch_s"]]
            deltas = {}
            for field in ("overnight_hrv_ms", "duration_hours"):
                values = [n[field] for n in baseline if n[field] is not None]
                base = median(values) if len(values) >= MIN_BASELINE_N else None
                value = night[field]
                deltas[field] = {"value": value, "pre_session_7_night_count": len(values),
                                 "pre_session_median": _round(base),
                                 "delta": _round(value - base) if value is not None and base is not None else None}
            day = _day(run.get("date"))
            rhr_base = [r["value"] for d, r in daily_rhr.items() if day and day - timedelta(days=7) <= _day(d) < day and r["value"] is not None]
            rhr_row = daily_rhr.get(night.get("wake_local_date"))
            rhr_median = median(rhr_base) if len(rhr_base) >= MIN_BASELINE_N else None
            rhr_value = rhr_row["value"] if rhr_row else None
            outcomes.append({"phase": phase, "status": "observed", "night_id": night["night_id"],
                             "sleep_end_gmt": night["end_gmt"], "hours_after_session_end": _round((night["end_epoch_s"] - run["_end"]) / 3600),
                             "metrics": deltas,
                             "date_only_rhr_context": {"date": night.get("wake_local_date"), "value_bpm": rhr_value,
                                                       "pre_session_baseline_count": len(rhr_base),
                                                       "delta_bpm": _round(rhr_value - rhr_median) if rhr_value is not None and rhr_median is not None else None,
                                                       "timing": "Calendar-day aggregate; not an exact 24–48h measurement or a pre-run value.",
                                                       "source_ids": rhr_row["source_ids"] if rhr_row else []},
                             "intervening_activity_ids": [a.get("activity_id") for a in intervening],
                             "intervening_recorded_duration_minutes": _round(sum(_number(a.get("duration_s")) or 0 for a in intervening) / 60),
                             "intervening_duration_missing_count": sum(_number(a.get("duration_s")) is None for a in intervening),
                             "intervening_running_distance_km": _round(sum((_number(a.get("distance_m")) or 0) for a in intervening if a.get("is_running")) / 1000),
                             "intervening_other_activity_count": sum(not a.get("is_running") for a in intervening),
                             "intervening_load_observed": bool(intervening),
                             "source_ids": _sources([night] + baseline + intervening)})
        episodes.append({"activity_id": run.get("activity_id"), "date": run.get("date"),
                         "session_end_gmt": _iso(run["_end"]), "selection_reasons": reasons,
                         "source_ids": _sources([run]), "outcomes": outcomes})
    summaries = []
    for field in ("overnight_hrv_ms", "duration_hours"):
        for confounded in (False, True):
            rows = [(e, o) for e in episodes for o in e["outcomes"] if o["phase"] == "24_to_48_hours"
                    and o["status"] == "observed" and o["intervening_load_observed"] is confounded
                    and o["metrics"][field]["delta"] is not None]
            summaries.append({"metric": field, "intervening_load_observed": confounded, "episode_count": len(rows),
                              "median_delta_from_pre_session_baseline": _round(median(o["metrics"][field]["delta"] for _, o in rows)) if len(rows) >= 3 else None,
                              "status": "descriptive_only" if len(rows) >= 3 else "insufficient_episodes",
                              "activity_ids": [e["activity_id"] for e, _ in rows]})
    return {"episodes": episodes, "overlapping_index_sessions_not_aggregated": overlap_skipped,
            "summary_24_to_48_hours": summaries,
            "method": "Chronological index sessions have non-overlapping48h follow-up windows. Baselines precede session start; one completed sleep per phase. Intervening activities remain explicit; no claim that the index run caused the change."}


def build_longitudinal_metrics(bundle):
    """Build a reproducible timeline and qualified associations without new reads."""
    input_runs = [r for r in bundle.get("runs", []) if isinstance(r, dict)]
    raw_runs, seen_ids, duplicate_count = [], set(), 0
    for row in input_runs:
        identity = row.get("activity_id")
        if identity is not None and identity in seen_ids:
            duplicate_count += 1
            continue
        if identity is not None:
            seen_ids.add(identity)
        raw_runs.append(row)
    daily = [r for r in bundle.get("daily", []) if isinstance(r, dict)]
    nights, sleep_coverage = _sleep_records(daily)
    hrv = _dated_values(daily, "hrv", ("lastNightAvg",))
    rhr = _dated_values(daily, "resting_hr", ("value", "restingHeartRate"))
    steps = _dated_values(daily, "steps", ("totalSteps", "steps"))
    runs, excluded = [], defaultdict(int)
    for raw in sorted(raw_runs, key=lambda r: (r.get("start_gmt") or "", str(r.get("activity_id") or "")))[-MAX_RUNS:]:
        start = _utc(raw.get("start_gmt"))
        if start is None or not _day(raw.get("date")):
            excluded["run_missing_gmt_start_or_local_date"] += 1
            continue
        elapsed = _number(raw.get("elapsed_s"))
        runs.append({**raw, "_start": start, "_end": start + elapsed if elapsed is not None else None})
    runs.sort(key=lambda r: r["_start"])
    excluded["duplicate_activity_id"] = duplicate_count
    activity_map = {r.get("activity_id"): r for r in runs}
    for raw in bundle.get("activities", []):
        if not isinstance(raw, dict) or raw.get("activity_id") in activity_map:
            continue
        start = _utc(raw.get("start_gmt"))
        if start is not None:
            activity_map[raw.get("activity_id")] = {**raw, "_start": start}
    boundaries = bundle.get("coverage", {}).get("boundaries", {})
    coverage_start, coverage_end = _day(boundaries.get("start")), _day(boundaries.get("end"))
    run_dates = {r.get("date") for r in raw_runs}
    contexts = [_run_context(r, runs, nights, hrv, rhr, steps, run_dates, coverage_start) for r in runs]
    capacity = bundle.get("capacity_metrics", {})
    windows = defaultdict(list)
    for window in capacity.get("sustained_windows", []):
        windows[window.get("activity_id")].append(window)
    quality = {r.get("activity_id"): r for r in capacity.get("heart_rate_quality", {}).get("runs", [])}
    observations = [_observation(r, windows[r.get("activity_id")], quality.get(r.get("activity_id"), {})) for r in runs]
    dates = {_day(row.get("date")) for row in daily + raw_runs} - {None}
    if coverage_start and coverage_end:
        dates.update(coverage_start + timedelta(days=i) for i in range(min(MAX_DAYS, (coverage_end - coverage_start).days + 1)))
    ordered_dates = sorted(dates)[-MAX_DAYS:]
    timeline = []
    for day in ordered_dates:
        text_day = day.isoformat()
        observed = [r for r in raw_runs if r.get("date") == text_day]
        durations = [_number(r.get("duration_s")) for r in observed]
        distances = [_number(r.get("distance_m")) for r in observed]
        timeline.append({"date": text_day, "activity_ids": [r.get("activity_id") for r in observed],
                         "recorded_running_km": _round(sum(v for v in distances if v is not None) / 1000),
                         "recorded_running_minutes": _round(sum(v for v in durations if v is not None) / 60),
                         "distance_missing_count": distances.count(None), "duration_missing_count": durations.count(None),
                         "steps": steps.get(text_day), "calendar_day_hrv": hrv.get(text_day),
                         "calendar_day_rhr": rhr.get(text_day),
                         "completed_sleep_ids": [n["night_id"] for n in nights if n["wake_local_date"] == text_day],
                         "may_be_incomplete_boundary_day": day == coverage_end,
                         "source_ids": _sources(observed)})
    return {"schema_version": 1, "daily_timeline": timeline, "run_contexts": contexts,
            "pace_hr_observations": observations,
            "exploratory_associations": _exploratory_associations(observations, contexts),
            "matched_associations": _matched_associations(observations, contexts),
            "post_session_recovery": _recovery(runs, list(activity_map.values()), nights, rhr),
            "coverage": {"input_run_count": len(input_runs), "unique_run_count": len(raw_runs), "aligned_run_count": len(runs),
                         "run_exclusions": dict(excluded), "runs_omitted_by_limit": max(0, len(raw_runs) - MAX_RUNS),
                         "sleep": sleep_coverage, "daily_timeline_days": len(timeline),
                         "dates_omitted_by_limit": max(0, len(dates) - MAX_DAYS)},
            "matching_policy": {"one_observation_per_run": True, "maximum_pair_date_gap_days": 42,
                                "maximum_hr_difference_bpm": 3, "maximum_duration_ratio": 1.25,
                                "maximum_local_start_hour_difference_when_known": 3,
                                "maximum_ascent_difference_m_per_km": 5, "minimum_pairs_to_summarize": 3,
                                "same_activity_or_calendar_date_reused_within_association": False},
            "limitations": [
                "Associations are descriptive, not causal. Same-person observations, overlapping exposure windows, fitness trends and unmeasured factors remain dependent.",
                "No HR-to-marathon curve, race prediction, threshold estimate, diagnosis or individualized safety boundary is fitted.",
                "Sleep is aligned by explicit GMT completion, not its date label; sleep without a usable GMT interval cannot be a pre-run exposure.",
                "Retrospective record timestamps describe the observed period, not proof that a particular metric was available on the device at that moment.",
                "Date-only RHR is not a precisely timed overnight measurement. Same-day RHR never enters a pre-run predictor.",
                "Steps include running. The number of truly non-running steps is not established by this dataset.",
                "A route group or shared location and similar gain do not establish identical terrain, direction, surface, weather or effort.",
                "Matched runs are used once per exposure analysis and once per date, but different analyses are not independent replications.",
                "Numerical selection screens and minimum sample counts are transparent analysis choices, not validated coaching or clinical cutoffs.",
            ]}
