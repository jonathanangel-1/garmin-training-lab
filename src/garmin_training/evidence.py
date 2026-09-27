"""Offline, allowlisted evidence preparation; raw Garmin exports stay local.

No network or model calls occur here. Unknown response schemas are advertised as
available, never copied into model context. Source ids point back to manifest
calls; activity ids remain suitable citations without disclosing route tracks.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_SEGMENTS = 256
MAX_LAPS = 500
STATUSES = {"ok", "empty", "unavailable", "error", "interrupted"}
RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "virtual_run", "ultra_run", "ultra_running", "indoor_running", "street_running", "obstacle_run"}


def _num(value: Any) -> float | int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _rounded(value: float | int | None, places: int = 3) -> float | None:
    return round(value, places) if value is not None else None


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def _id(value: Any) -> str | None:
    value = str(value)
    return value if re.fullmatch(r"[0-9]{1,30}", value) else None


def _token(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_ .:/+-]{1,100}", value) else None


def _first(data: dict, *keys: str) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def _safe_write(path: Path, text: str) -> None:
    """Create with restrictive permissions, then atomically replace old content."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _metrics(data: dict) -> dict:
    """Summary/lap fields have documented Garmin SI units; never coerce null to 0."""
    mapping = {
        "distance_m": ("distance",),
        "duration_s": ("duration",),
        "elapsed_s": ("elapsedDuration",),
        "moving_s": ("movingDuration",),
        "avg_speed_mps": ("averageSpeed",),
        "max_speed_mps": ("maxSpeed",),
        "garmin_grade_adjusted_speed_mps": ("avgGradeAdjustedSpeed", "averageGradeAdjustedSpeed"),
        "avg_hr_bpm": ("averageHR", "averageHeartRate"),
        "max_hr_bpm": ("maxHR", "maxHeartRate"),
        "elevation_gain_m": ("elevationGain",),
        "elevation_loss_m": ("elevationLoss",),
        "avg_cadence_spm": ("averageRunningCadenceInStepsPerMinute",),
        "steps": ("steps",),
        "aerobic_training_effect": ("aerobicTrainingEffect", "trainingEffect"),
        "anaerobic_training_effect": ("anaerobicTrainingEffect",),
        "garmin_activity_training_load": ("activityTrainingLoad",),
        "calories_kcal": ("calories",),
        "avg_run_power_w": ("avgPower", "averagePower"),
        "avg_ground_contact_time_source": ("avgGroundContactTime",),
        "avg_vertical_oscillation_source": ("avgVerticalOscillation",),
        "avg_vertical_ratio_source": ("avgVerticalRatio",),
        "avg_stride_length_source": ("avgStrideLength",),
    }
    result = {dest: _num(_first(data, *keys)) for dest, keys in mapping.items()}
    distance = result["distance_m"]
    result["distance_km"] = _rounded(distance / 1000) if distance is not None else None
    speed = result["avg_speed_mps"]
    result["pace_s_per_km"] = _rounded(1000 / speed) if speed and speed > 0 else None
    result["pace_basis"] = "1000 / Garmin averageSpeed" if speed and speed > 0 else None
    if result["pace_s_per_km"] is None and distance and distance > 0 and result["duration_s"] and result["duration_s"] > 0:
        result["pace_s_per_km"] = _rounded(1000 * result["duration_s"] / distance)
        result["pace_basis"] = "Garmin duration / distance; not elapsed or moving time"
    gap = result["garmin_grade_adjusted_speed_mps"]
    result["garmin_grade_adjusted_pace_s_per_km"] = _rounded(1000 / gap) if gap and gap > 0 else None
    return result


def _normalize_activity(data: dict) -> dict:
    activity_type = data.get("activityType") or data.get("activityTypeDTO") or {}
    type_key = _token(activity_type.get("typeKey")) if isinstance(activity_type, dict) else _token(activity_type)
    start_local = _timestamp(data.get("startTimeLocal"))
    start_gmt = _timestamp(data.get("startTimeGMT"))
    timezone_value = data.get("timeZoneId")
    result = {
        "activity_id": _id(data.get("activityId")),
        "type": type_key,
        "is_running": bool(type_key in RUN_TYPES or type_key and type_key.endswith("_running")),
        "date": _date(start_local or start_gmt),
        "date_basis": "startTimeLocal" if start_local else "startTimeGMT" if start_gmt else None,
        "start_local": start_local,
        "start_gmt": start_gmt,
        "time_zone_id": timezone_value if isinstance(timezone_value, (int, float)) else _token(timezone_value),
        "location_name": data.get("locationName") if isinstance(data.get("locationName"), str) else None,
        **_metrics(data),
    }
    return result


# Descriptor units are interpreted only when recognized. Absent units retain the
# known Garmin key convention and explicitly appear in assumptions below.
STREAM_FIELDS = {
    "sumDistance": "distance_m",
    "sumDuration": "duration_s",
    "sumElapsedDuration": "elapsed_s",
    "sumMovingDuration": "moving_s",
    "directTimestamp": "timestamp_ms",
    "directHeartRate": "hr_bpm",
    "directSpeed": "speed_mps",
    "directElevation": "elevation_m",
    "directRunCadence": "run_cadence_source",
    "directDoubleCadence": "double_cadence_source",
    "directRunningCadence": "running_cadence_source",
    "directCadence": "cadence_source",
    "directGradeAdjustedSpeed": "garmin_grade_adjusted_speed_mps",
    "directPower": "power_w",
}


def _unit_name(descriptor: dict) -> str | None:
    unit = descriptor.get("unit")
    if isinstance(unit, dict):
        unit = unit.get("key") or unit.get("unitKey")
    if unit is None:
        unit = descriptor.get("unitKey")
    return _token(unit)


def _stream_value(value: Any, key: str, unit: str | None) -> float | None:
    value = _num(value)
    if value is None:
        return None
    normalized = (unit or "").lower().replace(" ", "_")
    if key in {"distance_m", "elevation_m"}:
        if normalized in {"kilometer", "kilometers", "km"}:
            return value * 1000
        if normalized in {"foot", "feet", "ft"}:
            return value * 0.3048
        if normalized in {"mile", "miles", "mi"}:
            return value * 1609.344
        if normalized not in {"", "meter", "meters", "m"}:
            return None
    if key in {"duration_s", "elapsed_s", "moving_s"}:
        if normalized in {"millisecond", "milliseconds", "ms"}:
            return value / 1000
        if normalized in {"minute", "minutes", "min"}:
            return value * 60
        if normalized not in {"", "second", "seconds", "s"}:
            return None
    if key == "timestamp_ms" and normalized in {"second", "seconds", "s"}:
        return value * 1000
    if key in {"speed_mps", "garmin_grade_adjusted_speed_mps"}:
        if normalized in {"kilometer_per_hour", "kilometers_per_hour", "km/h", "kph"}:
            return value / 3.6
        if normalized in {"mile_per_hour", "miles_per_hour", "mph"}:
            return value * 0.44704
        if normalized not in {"", "meter_per_second", "meters_per_second", "m/s", "mps"}:
            return None
    return value


def summarize_stream(details: Any) -> dict:
    """Descriptor-mapped, bounded stream profile; never a raw-coordinate export.

    Bins contain observed points, not interpolated exact kilometre laps. Means
    are explicitly sample weighted. Decoupling is intentionally not computed:
    steady effort, environmental comparability and workout intent are unproven.
    """
    if not isinstance(details, dict):
        details = {}
    mapping = {}
    assumptions = set()
    for descriptor in details.get("metricDescriptors") or []:
        if not isinstance(descriptor, dict):
            continue
        source = descriptor.get("key")
        index = descriptor.get("metricsIndex")
        if source not in STREAM_FIELDS or not isinstance(index, int) or isinstance(index, bool) or index < 0:
            continue
        unit = _unit_name(descriptor)
        mapping[source] = (index, STREAM_FIELDS[source], unit)
        if unit is None:
            assumptions.add(source)
    samples = []
    raw_samples = details.get("activityDetailMetrics") or []
    if not isinstance(raw_samples, list):
        raw_samples = []
    for record in raw_samples:
        values = record.get("metrics") if isinstance(record, dict) else None
        if not isinstance(values, list):
            continue
        point = {dest: _stream_value(values[index], dest, unit) for index, dest, unit in mapping.values() if index < len(values)}
        if any(value is not None for value in point.values()):
            samples.append(point)
    distance_values = [p["distance_m"] for p in samples if p.get("distance_m") is not None]
    distance_ok = len(distance_values) >= 2 and all(b >= a for a, b in zip(distance_values, distance_values[1:])) and distance_values[-1] > distance_values[0]
    time_key = next((key for key in ("elapsed_s", "duration_s", "timestamp_ms") if sum(p.get(key) is not None for p in samples) >= 2), None)
    origin = next((p[time_key] for p in samples if time_key and p.get(time_key) is not None), None)
    grouped = defaultdict(list)
    skipped = 0
    for point in samples:
        if distance_ok and point.get("distance_m") is not None:
            bucket = math.floor(point["distance_m"] / 1000)
        elif not distance_ok and time_key and point.get(time_key) is not None:
            time = (point[time_key] - origin) / (1000 if time_key == "timestamp_ms" else 1)
            if time < 0:
                skipped += 1
                continue
            bucket = math.floor(time / 300)
        else:
            skipped += 1
            continue
        grouped[bucket].append(point)
    segments = []
    for bucket, points in sorted(grouped.items()):
        segment = {"bin_index": bucket, "sample_count": len(points)}
        for key in ("distance_m", "duration_s", "elapsed_s", "moving_s"):
            vals = [point[key] for point in points if point.get(key) is not None]
            if not any(dest == key for _, dest, _ in mapping.values()):
                continue
            segment[f"start_{key}"] = _rounded(vals[0]) if vals else None
            segment[f"end_{key}"] = _rounded(vals[-1]) if vals else None
            segment[f"span_{key}"] = _rounded(vals[-1] - vals[0]) if len(vals) > 1 and vals[-1] >= vals[0] else None
        for key in ("hr_bpm", "speed_mps", "garmin_grade_adjusted_speed_mps", "run_cadence_source", "double_cadence_source", "running_cadence_source", "cadence_source", "power_w", "elevation_m"):
            vals = [point[key] for point in points if point.get(key) is not None]
            if not any(dest == key for _, dest, _ in mapping.values()):
                continue
            segment[f"mean_{key}"] = _rounded(sum(vals) / len(vals)) if vals else None
            segment[f"min_{key}"] = _rounded(min(vals)) if vals else None
            segment[f"max_{key}"] = _rounded(max(vals)) if vals else None
        elevations = [point["elevation_m"] for point in points if point.get("elevation_m") is not None]
        segment["observed_ascent_m"] = _rounded(sum(max(b - a, 0) for a, b in zip(elevations, elevations[1:]))) if len(elevations) > 1 else None
        segment["observed_descent_m"] = _rounded(sum(max(a - b, 0) for a, b in zip(elevations, elevations[1:]))) if len(elevations) > 1 else None
        speed = segment.get("mean_speed_mps")
        segment["pace_from_mean_speed_s_per_km"] = _rounded(1000 / speed) if speed and speed > 0 else None
        segments.append(segment)
    segment_count = len(segments)
    if segment_count > MAX_SEGMENTS:
        segments = segments[:MAX_SEGMENTS // 2] + segments[-MAX_SEGMENTS // 2:]
    return {
        "binning": "1_km_observed_points" if distance_ok else "5_min_observed_points" if time_key else "unavailable",
        "raw_sample_count": len(raw_samples), "usable_sample_count": len(samples),
        "unbinned_sample_count": skipped,
        "segment_count_before_limit": segment_count,
        "segments_omitted": max(0, segment_count - len(segments)),
        "reported_measurement_count": _num(details.get("measurementCount")),
        "reported_metrics_count": _num(details.get("metricsCount")),
        "source_metrics": [{"key": key, "unit": unit} for key, (_, _, unit) in mapping.items()],
        "assumed_garmin_units_for": sorted(assumptions),
        "cadence_convention": "directRunCadence and directDoubleCadence are retained as separate source channels even when both advertise stepsPerMinute. Do not treat the former as total steps/min or silently double it. Summary averageRunningCadenceInStepsPerMinute remains the explicit total-step field.",
        "descriptor_factor_policy": "Metric values are already decoded; descriptor unit.factor is metadata and is never multiplied or divided into samples. Recognized explicit unit keys alone control unit conversion.",
        "sampling_note": "May already be downsampled by Garmin. Means are sample weighted, bin edges are observed points, ascent/descent omit inter-bin edges and depend on sampling; use summary gain/loss for total terrain.",
        "decoupling": {"status": "not_computed", "reason": "Steady effort, comparable terrain/weather, stops, fueling and workout purpose have not been independently validated; length alone does not establish eligibility."},
        "segments": segments,
    }


def _select(data: Any, schema: dict) -> dict:
    """Strict field and value allowlist; unknown dicts/strings cannot pass through."""
    if not isinstance(data, dict):
        return {}
    selected = {}
    for key, kind in schema.items():
        if key not in data:
            continue
        value = data[key]
        if value is None:
            selected[key] = None
        elif isinstance(kind, dict):
            nested = _select(value, kind)
            if nested:
                selected[key] = nested
        elif kind == "num":
            selected[key] = _num(value)
        elif kind == "bool" and isinstance(value, bool):
            selected[key] = value
        elif kind == "date":
            selected[key] = _date(value)
        elif kind == "time":
            selected[key] = _timestamp(value)
        elif kind == "token":
            selected[key] = _token(value)
    return selected


def _numeric_schema(*keys: str) -> dict:
    return dict.fromkeys(keys, "num")


DAILY_BASE = {"calendarDate": "date", "date": "date", "timestamp": "time", "timestampLocal": "time"}
DAILY_STATS = {**DAILY_BASE, **_numeric_schema("totalSteps", "dailyStepGoal", "totalDistanceMeters", "restingHeartRate", "minHeartRate", "maxHeartRate", "sleepingSeconds", "activeSeconds", "highlyActiveSeconds", "sedentarySeconds", "moderateIntensityMinutes", "vigorousIntensityMinutes", "floorsAscended", "floorsDescended", "averageStressLevel", "maxStressLevel", "stressDuration", "restStressDuration", "bodyBatteryChargedValue", "bodyBatteryDrainedValue", "bodyBatteryHighestValue", "bodyBatteryLowestValue", "totalKilocalories", "activeKilocalories", "bmrKilocalories")}
SLEEP = {**DAILY_BASE, **_numeric_schema("sleepTimeSeconds", "napTimeSeconds", "deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds", "awakeSleepSeconds", "sleepStartTimestampGMT", "sleepEndTimestampGMT", "sleepStartTimestampLocal", "sleepEndTimestampLocal", "avgSleepHRV", "avgSpO2", "averageRespirationValue", "lowestRespirationValue", "highestRespirationValue"), "sleepWindowConfirmed": "bool", "sleepScores": {key: {"value": "num", "qualifierKey": "token"} for key in ("overall", "totalDuration", "stress", "awakeCount", "remPercentage", "restlessness", "lightPercentage", "deepPercentage")}}
HRV = {**DAILY_BASE, **_numeric_schema("weeklyAvg", "lastNightAvg", "lastNight5MinHigh"), "status": "token", "baseline": _numeric_schema("lowUpper", "balancedLow", "balancedUpper", "markerValue")}
READINESS = {**DAILY_BASE, **_numeric_schema("score", "sleepScore", "recoveryTime", "sleepScoreFactorPercent", "recoveryTimeFactorPercent", "acwrFactorPercent", "hrvFactorPercent", "stressHistoryFactorPercent"), "level": "token", "inputContext": "token", "recoveryTimeChangePhrase": "token"}
HYDRATION = {**DAILY_BASE, **_numeric_schema("valueInML", "goalInML", "baseGoalInML", "sweatLossInML", "activityIntakeInML", "lastEntryTimestampLocal"), "hydrationMeasurementUnit": "token"}
NUTRITION = {**DAILY_BASE, **_numeric_schema("totalCalories", "totalCarbs", "totalProtein", "totalFat", "totalCarbohydrates", "calories", "carbohydrates", "protein", "fat", "sodium", "fiber", "water"), "totals": _numeric_schema("calories", "carbohydrates", "protein", "fat", "sodium", "fiber", "water")}
WEATHER = {**_numeric_schema("temp", "apparentTemp", "dewPoint", "relativeHumidity", "windSpeed", "windDirection", "windGust", "temperature", "feelsLike", "humidity"), "issueDate": "time", "weatherTypeDTO": {"weatherTypePk": "num", "desc": "token"}}
SLEEP_RANGE_VALUES = {
    **_numeric_schema("remTime", "restingHeartRate", "respiration", "localSleepEndTimeInMillis", "awakeTime", "spO2", "localSleepStartTimeInMillis", "sleepScore", "lightTime", "avgOvernightHrv", "totalSleepTimeInSeconds", "deepTime", "sleepNeed", "bodyBatteryChange", "gmtSleepStartTimeInMillis", "gmtSleepEndTimeInMillis", "skinTempF", "skinTempC", "avgHeartRate", "hrv7dAverage"),
    "sleepAlignmentStatus": "token", "hrvStatus": "token", "sleepScoreQuality": "token",
}


def _body_battery_observations(item: dict) -> dict:
    """Extract observed levels by descriptor, not by an assumed array position."""
    descriptors = item.get("bodyBatteryValueDescriptorDTOList")
    if not isinstance(descriptors, list):
        return {}
    index = next((row.get("bodyBatteryValueDescriptorIndex") for row in descriptors
                  if isinstance(row, dict) and row.get("bodyBatteryValueDescriptorKey") == "bodyBatteryLevel"), None)
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        return {}
    samples = item.get("bodyBatteryValuesArray")
    if not isinstance(samples, list):
        return {}
    levels = [row[index] for row in samples if isinstance(row, list) and index < len(row)
              and _num(row[index]) is not None and 0 <= row[index] <= 100]
    return {"observed_min_level": min(levels) if levels else None,
            "observed_max_level": max(levels) if levels else None,
            "valid_level_sample_count": len(levels), "source_level_sample_count": len(samples)}


def _daily_units(kind: str) -> dict:
    if kind == "sleep":
        return {
            "totalSleepTimeInSeconds": "seconds", "sleepTimeSeconds": "seconds", "totalSleepSeconds": "seconds",
            "remTime": "source seconds; stage sums agree with totalSleepTimeInSeconds in the audited schema",
            "lightTime": "source seconds; stage sums agree with totalSleepTimeInSeconds in the audited schema",
            "deepTime": "source seconds; stage sums agree with totalSleepTimeInSeconds in the audited schema",
            "awakeTime": "source duration; not included in total sleep",
            "sleepNeed": "source value, likely minutes; unit not independently verified. Do not compare directly with sleep seconds or calculate a deficit from this field.",
            "localSleepStartTimeInMillis": "local timestamp milliseconds; not UTC",
            "localSleepEndTimeInMillis": "local timestamp milliseconds; not UTC",
            "gmtSleepStartTimeInMillis": "UTC timestamp milliseconds",
            "gmtSleepEndTimeInMillis": "UTC timestamp milliseconds",
            "restingHeartRate": "bpm", "avgHeartRate": "bpm", "avgOvernightHrv": "milliseconds",
            "hrv7dAverage": "milliseconds", "respiration": "breaths/min", "spO2": "percent",
            "skinTempC": "Garmin source Celsius field; absolute-versus-change semantics not assumed",
            "skinTempF": "Garmin source Fahrenheit field; absolute-versus-change semantics not assumed",
        }
    if kind == "vo2_estimate":
        return {"vo2MaxValue": "mL/kg/min; Garmin estimate, not a laboratory measurement",
                "vo2MaxPreciseValue": "mL/kg/min; vendor decimal precision is not proven measurement accuracy"}
    if kind == "body_battery":
        return {"observed_min_level": "Garmin 0-100 scale; observed samples only",
                "observed_max_level": "Garmin 0-100 scale; observed samples only"}
    return {}


def _daily_records(method: str, data: Any) -> tuple[str | None, list[dict]]:
    schema = None
    kind = None
    if method in {"get_stats", "get_user_summary"}:
        kind, schema = "daily_summary", DAILY_STATS
    elif method == "get_daily_steps":
        kind, schema = "steps", {**DAILY_BASE, **_numeric_schema("totalSteps", "steps", "totalDistance", "totalDistanceMeters", "stepGoal", "dailyStepGoal")}
    elif method == "get_sleep_data":
        kind, schema = "sleep", SLEEP
        data = data.get("dailySleepDTO") if isinstance(data, dict) else None
    elif method == "get_sleep_daily":
        kind, schema = "sleep", {**SLEEP, **SLEEP_RANGE_VALUES, **_numeric_schema("overallSleepScore", "totalSleepSeconds", "totalSleepTimeInSeconds", "sleepDurationInSeconds", "deepSleepTimeInSeconds", "lightSleepTimeInSeconds", "remSleepTimeInSeconds", "awakeTimeInSeconds", "sleepStartTimestamp", "sleepEndTimestamp")}
        if isinstance(data, dict):
            data = data.get("individualStats")
        if isinstance(data, list):
            data = [{**row, **_select(row.get("values"), SLEEP_RANGE_VALUES)} for row in data if isinstance(row, dict)]
    elif method in {"get_hrv_data", "get_hrv_data_range"}:
        kind, schema = "hrv", HRV
        if isinstance(data, dict):
            data = data.get("hrvSummary") or data.get("hrvSummaries")
    elif method in {"get_rhr_day", "get_rhr_daily"}:
        kind, schema = "resting_hr", {**DAILY_BASE, "value": "num", "restingHeartRate": "num"}
        if isinstance(data, dict) and isinstance(data.get("allMetrics"), dict):
            data = (data["allMetrics"].get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE")
    elif method in {"get_training_readiness", "get_morning_training_readiness"}:
        kind, schema = "readiness", READINESS
    elif method == "get_stress_data":
        kind, schema = "stress", {**DAILY_BASE, **_numeric_schema("avgStressLevel", "averageStressLevel", "maxStressLevel", "stressDuration", "restStressDuration")}
    elif method == "get_hydration_data":
        kind, schema = "hydration", HYDRATION
    elif method in {"get_nutrition_daily_food_log", "get_nutrition_daily_meals"}:
        kind, schema = "nutrition", NUTRITION
    elif method == "get_body_battery":
        kind, schema = "body_battery", {**DAILY_BASE, **_numeric_schema("charged", "drained", "highestValue", "lowestValue", "observed_min_level", "observed_max_level", "valid_level_sample_count", "source_level_sample_count")}
        if isinstance(data, list):
            data = [{**row, **_body_battery_observations(row)} for row in data if isinstance(row, dict)]
    elif method in {"get_max_metrics", "get_max_metrics_range"}:
        kind, schema = "vo2_estimate", {**DAILY_BASE, **_numeric_schema("vo2MaxPreciseValue", "vo2MaxValue"), "source_sport_category": "token"}
        items = data if isinstance(data, list) else [data]
        data = [{**row[sport], "source_sport_category": sport} for row in items if isinstance(row, dict)
                for sport in ("generic", "cycling") if isinstance(row.get(sport), dict)]
    elif method == "get_calories_daily":
        kind, schema = "calories", {**DAILY_BASE, **_numeric_schema("active", "resting", "total")}
    if schema is None:
        return None, []
    items = data if isinstance(data, list) else [data]
    return kind, [selected for item in items if (selected := _select(item, schema)) and any(key not in DAILY_BASE for key in selected)]


def _weeks(runs: list[dict], boundaries: dict) -> list[dict]:
    grouped = defaultdict(list)
    for run in runs:
        if not run["date"]:
            continue
        day = date.fromisoformat(run["date"])
        grouped[(day - timedelta(days=day.weekday())).isoformat()].append(run)
    # Empty weeks remain visible; zero observed activity is not proof of rest.
    start, end = boundaries.get("start"), boundaries.get("end")
    if start and end:
        day, last = date.fromisoformat(start), date.fromisoformat(end)
        day -= timedelta(days=day.weekday())
        while day <= last:
            grouped.setdefault(day.isoformat(), [])
            day += timedelta(days=7)
    rows = []
    for week, items in sorted(grouped.items()):
        distances = [run["distance_km"] for run in items if run["distance_km"] is not None]
        durations = [run["duration_s"] for run in items if run["duration_s"] is not None]
        rows.append({
            "week_start": week,
            "run_count": len(items), "running_days": len({run["date"] for run in items}),
            "recorded_distance_km": _rounded(sum(distances)) if distances or not items else None,
            "distance_missing_count": len(items) - len(distances),
            "recorded_duration_s": _rounded(sum(durations)) if durations or not items else None,
            "duration_missing_count": len(items) - len(durations),
            "longest_run_km": max(distances) if distances else None,
            "longest_duration_s": max(durations) if durations else None,
            "activity_ids": [run["activity_id"] for run in items],
            "partial_boundary_week": bool(start and week < start or end and (date.fromisoformat(week) + timedelta(days=6)).isoformat() > end),
        })
    return rows


def _metric_date_counts(daily: list[dict]) -> dict:
    counts = defaultdict(lambda: defaultdict(set))
    def leaves(value: dict, prefix: str = ""):
        for key, child in value.items():
            label = f"{prefix}.{key}" if prefix else key
            if isinstance(child, dict):
                yield from leaves(child, label)
            elif _num(child) is not None and (child >= 0 or key in {"skinTempC", "skinTempF", "bodyBatteryChange"}):
                yield label
    for row in daily:
        if row["date"]:
            for metric in leaves(row["values"]):
                counts[row["kind"]][metric].add(row["date"])
    return {kind: {metric: len(dates) for metric, dates in sorted(metrics.items())}
            for kind, metrics in sorted(counts.items())}


def _summary_values_differ(field: str, original: Any, detailed: Any) -> bool:
    if _num(original) is None or _num(detailed) is None:
        return False
    # Compare supplied quantities, not rounded derivatives of those quantities.
    # A sub-millimetre difference can straddle distance_km's rounding boundary.
    if field in {"distance_km", "pace_s_per_km", "garmin_grade_adjusted_pace_s_per_km"}:
        return False
    if field in {"distance_m", "elevation_gain_m", "elevation_loss_m", "duration_s", "elapsed_s", "moving_s"}:
        return abs(Decimal(str(original)) - Decimal(str(detailed))) >= Decimal("0.01")
    return original != detailed


def _read_call(snapshot: Path, call: dict) -> tuple[Any, str | None, str | None]:
    raw_path = call.get("raw_path")
    if not isinstance(raw_path, str) or not re.fullmatch(r"raw/[A-Za-z0-9_-]+\.json", raw_path):
        return None, None, "invalid_raw_path"
    path = snapshot / raw_path
    try:
        if not path.resolve().is_relative_to(snapshot.resolve()):
            return None, None, "invalid_raw_path"
        raw = path.read_bytes()
        expected = call.get("sha256")
        if isinstance(expected, str) and hashlib.sha256(raw).hexdigest() != expected:
            return None, raw_path, "hash_mismatch"
        return json.loads(raw), raw_path, None
    except (OSError, ValueError):
        return None, raw_path, "raw_unreadable"


def build_evidence(snapshot_dir: Path, output_dir: Path) -> dict:
    """Write ``evidence.json`` and ``evidence.md`` (0600) and return the bundle.

    Input is a collector snapshot containing manifest.json, activities.json and
    raw method responses. This API does not modify the raw snapshot. It refuses
    unsafe raw paths and hash-mismatched responses and reports coverage gaps.
    """
    snapshot_dir, output_dir = Path(snapshot_dir), Path(output_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported snapshot manifest schema_version; expected 1")
    activity_data = json.loads((snapshot_dir / "activities.json").read_text(encoding="utf-8"))
    if not isinstance(activity_data, list):
        raise ValueError("activities.json must contain a list")
    boundaries = {key: _date((manifest.get("boundaries") or {}).get(key)) for key in ("start", "end", "detail_start", "daily_start")}
    sources, resources, daily, loaded = [], [], [], defaultdict(list)
    quality_notes = []
    calls = manifest.get("calls") or []
    if not isinstance(calls, list):
        raise ValueError("manifest calls must contain a list")
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        method = call.get("method")
        if not isinstance(method, str) or not re.fullmatch(r"get_[a-z0-9_]+", method):
            method = "unknown_method"
        status = call.get("status") if call.get("status") in STATUSES else "unavailable"
        args = call.get("args") if isinstance(call.get("args"), list) else []
        kwargs = call.get("kwargs") if isinstance(call.get("kwargs"), dict) else {}
        activity_id = _id(args[0]) if args and method.startswith("get_activity") else None
        requested_date = _date(args[0]) if args and not activity_id else None
        source = {"source_id": f"call:{index:05d}", "method": method, "status": status,
                  "activity_id": activity_id, "requested_date": requested_date,
                  "requested_end": _date(args[1]) if len(args) > 1 and not activity_id else None,
                  "retrieved_at": _timestamp(call.get("retrieved_at"))}
        if method == "get_activity_details":
            source["sampling_request"] = {key: _num(kwargs.get(key)) for key in ("maxchart", "maxpoly") if key in kwargs}
        data, raw_path, failure = (None, None, None)
        if status == "ok":
            data, raw_path, failure = _read_call(snapshot_dir, call)
        source["raw_path"] = raw_path
        source["sha256"] = call.get("sha256") if isinstance(call.get("sha256"), str) and re.fullmatch(r"[a-fA-F0-9]{64}", call["sha256"]) else None
        if failure:
            source["normalization_error"] = failure
        if isinstance(call.get("error"), dict):
            source["collection_error"] = _select(call["error"], {"type": "token", "http_status": "num", "reason": "token"})
        sources.append(source)
        resource = {"source_id": source["source_id"], "method": method, "status": status,
                    "normalization": "unavailable" if status != "ok" or failure else "schema_not_supported"}
        if data is not None and not failure:
            if activity_id:
                loaded[activity_id].append((method, data, source, resource))
            else:
                kind, records = _daily_records(method, data)
                if records:
                    for record in records:
                        record_date = _date(record.get("calendarDate") or record.get("date"))
                        if not record_date and (len(args) == 1 or source["requested_end"] == requested_date):
                            record_date = requested_date
                        if kind in {"daily_summary", "stress"}:
                            for field in ("averageStressLevel", "avgStressLevel"):
                                value = _num(record.get(field))
                                if value is not None and value < 0:
                                    record[field] = None
                                    quality_notes.append({
                                        "issue": "negative_stress_sentinel",
                                        "date": record_date, "kind": kind,
                                        "source_id": source["source_id"], "field": field,
                                        "original_value": value, "replacement": None,
                                        "reason": "Negative stress scores are missing/invalid observations; exclude them from arithmetic and recovery comparisons.",
                                    })
                        daily.append({"date": record_date,
                                      "kind": kind, "source_id": source["source_id"], "values": record})
                    resource["normalization"] = "curated_daily"
                    resource["record_count"] = len(records)
        resources.append(resource)
    activities, duplicate_ids, seen = [], [], set()
    for summary in activity_data:
        if not isinstance(summary, dict):
            continue
        activity_id = _id(summary.get("activityId"))
        if activity_id and activity_id in seen:
            duplicate_ids.append(activity_id)
            continue
        if activity_id:
            seen.add(activity_id)
        combined = dict(summary)
        related = loaded.get(activity_id, [])
        conflicts = []
        for method, data, _, resource in related:
            if method == "get_activity" and isinstance(data, dict):
                # Full summary fills missing fields but never silently overwrites
                # a conflicting activity-list observation.
                full = {**data, **(data.get("summaryDTO") if isinstance(data.get("summaryDTO"), dict) else {})}
                original_metrics, detailed_metrics = _metrics(summary), _metrics(full)
                for field in original_metrics:
                    original, detailed = original_metrics[field], detailed_metrics[field]
                    if _summary_values_differ(field, original, detailed):
                        conflicts.append({"field": field, "activities_value": original, "detail_value": detailed,
                                          "source_id": resource["source_id"]})
                for key, value in full.items():
                    if combined.get(key) is None:
                        combined[key] = value
                resource["normalization"] = "activity_summary"
        activity = _normalize_activity(combined)
        activity["source_ids"] = [source["source_id"] for _, _, source, _ in related]
        activity["summary_source"] = "activities.json"
        if conflicts:
            activity["summary_conflicts"] = conflicts
        for method, data, source, resource in related:
            if method == "get_activity_details":
                activity["stream"] = {"source_id": source["source_id"], **summarize_stream(data)}
                resource["normalization"] = "stream_profile"
            elif method in {"get_activity_splits", "get_activity_typed_splits"}:
                laps = data.get("lapDTOs") if isinstance(data, dict) else data
                if isinstance(laps, list):
                    activity["laps"] = [{"lap_index": _num(row.get("lapIndex")) if _num(row.get("lapIndex")) is not None else index + 1, **_metrics(row)} for index, row in enumerate(laps[:MAX_LAPS]) if isinstance(row, dict)]
                    activity["laps_omitted"] = max(0, len(laps) - MAX_LAPS)
                    activity["laps_source_id"] = source["source_id"]
                    resource["normalization"] = "laps"
            elif method == "get_activity_weather":
                weather = _select(data, WEATHER)
                if weather:
                    activity["weather"] = {"source_id": source["source_id"], "values": weather, "units": "Garmin source units retained; verify endpoint units before temperature/wind comparisons."}
                    resource["normalization"] = "curated_weather"
            elif method == "get_activity_gear":
                rows = data if isinstance(data, list) else data.get("gear", []) if isinstance(data, dict) else []
                if isinstance(rows, list):
                    # Custom names and customMakeModel may contain identifying
                    # free text. Only vendor brand/model fields are considered.
                    gear = [_select(row, {"brandName": "token", "modelName": "token", "makeName": "token", "gearTypeName": "token"}) for row in rows]
                    gear = [row for row in gear if row]
                    if gear:
                        activity["gear"] = {"source_id": source["source_id"], "items": gear}
                        resource["normalization"] = "curated_gear"
            elif method == "get_activity_hr_in_timezones":
                rows = data if isinstance(data, list) else data.get("hrTimeInZones", []) if isinstance(data, dict) else []
                if isinstance(rows, list):
                    zones = [_select(row, _numeric_schema("zoneNumber", "secsInZone", "zoneLowBoundary", "zoneHighBoundary")) for row in rows]
                    zones = [row for row in zones if row]
                    if zones:
                        activity["hr_zones"] = {"source_id": source["source_id"], "values": zones, "note": "Garmin configured zones; not an independent threshold measurement."}
                        resource["normalization"] = "curated_hr_zones"
        activities.append(activity)
    activities.sort(key=lambda row: (row["date"] or "", row["activity_id"] or ""))
    daily.sort(key=lambda row: (row["date"] or "", row["kind"], row["source_id"]))
    runs = [activity for activity in activities if activity["is_running"]]
    coverage = {
        "snapshot_status": _token(manifest.get("status")), "boundaries": boundaries,
        "activities_source": {"raw_path": "activities.json", "sha256": hashlib.sha256((snapshot_dir / "activities.json").read_bytes()).hexdigest()},
        "detail_selection_truncated": manifest.get("detail_selection_truncated") if isinstance(manifest.get("detail_selection_truncated"), bool) else None,
        "collection_counts": _select(manifest.get("counts"), _numeric_schema("activities", "selected_runs", "eligible_runs", "activity_pages", "unclassified_local_dates", "calls", "network_calls", "cached_calls", "successful_calls", "empty_calls", "failed_calls", "unavailable_calls")),
        "source_sampling": _select(manifest.get("sampling"), {"maxChartSize": "num", "maxPolylineSize": "num", "potentially_sampled": "bool", "original_fit_downloaded": "bool"}),
        "collection_completeness": [_select(item, {"reason": "token", "count": "num", "offset": "num", "selected": "num", "eligible": "num"}) for item in (manifest.get("completeness") or []) if isinstance(item, dict)],
        "call_status_counts": dict(Counter(source["status"] for source in sources)),
        "normalization_error_count": sum("normalization_error" in source for source in sources),
        "activity_count": len(activities), "running_count": len(runs),
        "duplicate_activity_ids_omitted": duplicate_ids,
        "activities_without_date": sum(row["date"] is None for row in activities),
        "activities_without_type": sum(row["type"] is None for row in activities),
        "activities_with_summary_conflicts": sum(bool(row.get("summary_conflicts")) for row in activities),
        "summary_conflict_policy": "Compare supplied fields; omit rounded distance/pace derivatives. Ignore distance/elevation differences below 0.01 metre and duration differences below 0.01 second. Other supplied numeric fields use exact comparison.",
        "runs_with_streams": sum("stream" in row for row in runs),
        "runs_with_laps": sum("laps" in row for row in runs),
        "daily_dates_by_kind": {kind: sorted({row["date"] for row in daily if row["kind"] == kind and row["date"]}) for kind in sorted({row["kind"] for row in daily})},
        "daily_records_without_date": sum(row["date"] is None for row in daily),
        "daily_values_normalized_to_null": len(quality_notes),
        "usable_metric_date_counts": _metric_date_counts(daily),
        "metric_count_definition": "Distinct dated finite numeric observations; negative sentinels excluded except signed temperature/body-battery-change values. This establishes field availability, not physiological validity.",
        "no_record_is_not_zero_or_rest": True,
    }
    limitations = [
        "Observed Garmin data only: no medical diagnosis, injury clearance, lactate-threshold estimate, finish-time forecast or causal claim is computed.",
        "Activity names/descriptions, owner identifiers, precise coordinates and route polylines are excluded. Provided locationName remains as authorized area context; no geocoding occurs.",
        "Daily total steps include running; do not add them to running load or infer non-running steps without aligned evidence.",
        "Garmin readiness, load, HRV and sleep may share inputs; agreement does not imply independent confirmation.",
        "Missing values remain null. Empty weeks mean no observed runs, not proof of no activity. Boundary weeks may be partial.",
        "Negative average-stress sentinels become null with a dated source audit note; signed temperature and Body Battery change fields are retained.",
        "Activity purpose, perceived effort, pain, illness, actual fueling, adherence, strength details and complete sensor quality require athlete input.",
        "Garmin grade-adjusted pace is retained only when supplied, as a vendor model estimate. Stream cadence convention is not guessed.",
        "Running-dynamics fields ending in _source retain vendor values; verify their endpoint units before comparisons.",
        "Daily nutrition/hydration are recorded entries, not verified consumption or absorption; absent entries do not mean zero intake.",
        "Unknown schemas expose availability only. Unsupported weather units and downsampled stream terrain must be checked before quantitative interpretation.",
    ]
    bundle = {"schema_version": SCHEMA_VERSION,
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "coverage": coverage, "sources": sources,
              "activities": [{key: value for key, value in activity.items() if key not in {"stream", "laps", "laps_omitted", "laps_source_id", "weather", "hr_zones", "gear"}} for activity in activities],
              "runs": runs, "weeks": _weeks(runs, boundaries), "daily": daily,
              "daily_units": {kind: _daily_units(kind) for kind in sorted({row["kind"] for row in daily}) if _daily_units(kind)},
              "resources": resources, "data_quality_notes": quality_notes, "limitations": limitations}
    _safe_write(output_dir / "evidence.json", json.dumps(bundle, indent=2, allow_nan=False) + "\n")
    _safe_write(output_dir / "evidence.md", _markdown(bundle))
    return bundle


def _markdown(bundle: dict) -> str:
    coverage = bundle["coverage"]
    lines = ["# Garmin training evidence", "", "This is an offline evidence bundle, not a coaching verdict.", "",
             f"Activities: {coverage['activity_count']}; runs: {coverage['running_count']}; runs with stream profiles: {coverage['runs_with_streams']}.",
             f"Call coverage: {json.dumps(coverage['call_status_counts'], sort_keys=True)}. Normalization errors: {coverage['normalization_error_count']}.",
             "", "## Observed weekly running", "", "| Week starting | Runs | Days | Recorded km | Longest km | Missing distances | Partial week |", "|---|---:|---:|---:|---:|---:|---|"]
    for week in bundle["weeks"]:
        values = (week["week_start"], week["run_count"], week["running_days"], week["recorded_distance_km"], week["longest_run_km"], week["distance_missing_count"], week["partial_boundary_week"])
        lines.append("| " + " | ".join("unknown" if value is None else str(value) for value in values) + " |")
    if bundle["data_quality_notes"]:
        lines += ["", "## Data quality corrections", ""]
        for note in bundle["data_quality_notes"]:
            lines.append(f"- {note['date'] or 'Date unknown'}: {note['kind']}.{note['field']} from {note['source_id']} was {note['original_value']} and is represented as null. {note['reason']}")
    lines += ["", "## Evidence usage", "", "Use evidence.json for dated activities, laps, bounded terrain/pace/HR profiles, curated daily records and source ids. Unknown resources are available for local review but not copied into model context.", "", "## Limitations", ""]
    lines += [f"- {limitation}" for limitation in bundle["limitations"]]
    return "\n".join(lines) + "\n"
