"""Synthetic time-alignment and association tests; no real athlete fixtures."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from garmin_training.capacity_metrics import build_capacity_metrics
from garmin_training.longitudinal import build_longitudinal_metrics


def stamp(day, hour=8):
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(hours=hour)


def sleep(day, hours=8, hrv=60, wake_hour=8, offset=0, label=None, fallback=False):
    end = stamp(day, wake_hour)
    start = end - timedelta(hours=hours)
    values = {"totalSleepTimeInSeconds": hours * 3600, "avgOvernightHrv": hrv,
              "restingHeartRate": 48,
              "gmtSleepStartTimeInMillis": start.timestamp() * 1000,
              "gmtSleepEndTimeInMillis": end.timestamp() * 1000,
              "localSleepStartTimeInMillis": (start.timestamp() + offset * 3600) * 1000,
              "localSleepEndTimeInMillis": (end.timestamp() + offset * 3600) * 1000}
    if fallback:
        mapping = {"totalSleepTimeInSeconds": "sleepTimeSeconds", "avgOvernightHrv": "avgSleepHRV",
                   "gmtSleepStartTimeInMillis": "sleepStartTimestampGMT", "gmtSleepEndTimeInMillis": "sleepEndTimestampGMT",
                   "localSleepStartTimeInMillis": "sleepStartTimestampLocal", "localSleepEndTimeInMillis": "sleepEndTimestampLocal"}
        values = {mapping.get(k, k): v for k, v in values.items()}
    return {"kind": "sleep", "date": label or day, "source_id": "call:100", "values": values}


def run(day, activity_id="101", hour=10, km=6, pace=360, hr=130):
    start = stamp(day, hour).strftime("%Y-%m-%d %H:%M:%S")
    segments = []
    for i in range(km):
        segment = {"bin_index": i, "sample_count": 200, "hr_bpm_sample_count": 200,
                   "start_distance_m": i * 1000, "end_distance_m": (i + 1) * 1000,
                   "mean_hr_bpm": hr, "max_hr_bpm": hr + 3,
                   "observed_ascent_m": 4, "observed_descent_m": 4,
                   "pace_from_mean_speed_s_per_km": pace}
        for key in ("duration_s", "elapsed_s", "moving_s"):
            segment[f"start_{key}"] = i * pace
            segment[f"end_{key}"] = (i + 1) * pace
        segments.append(segment)
    return {"activity_id": activity_id, "date": day, "start_gmt": start, "start_local": start,
            "is_running": True, "distance_m": km * 1000, "duration_s": km * pace,
            "elapsed_s": km * pace, "moving_s": km * pace, "elevation_gain_m": km * 4,
            "avg_hr_bpm": hr, "max_hr_bpm": hr + 3, "location_name": "Synthetic Park",
            "source_ids": ["call:101"], "stream": {"segments": segments, "source_id": "call:102"},
            "hr_zones": {"source_id": "call:103", "values": [{"zoneNumber": 1, "secsInZone": km * pace}]}}


def daily(kind, day, value):
    key = {"resting_hr": "value", "hrv": "lastNightAvg", "steps": "totalSteps"}[kind]
    return {"kind": kind, "date": day, "source_id": "call:104", "values": {key: value}}


def bundle(runs, rows, activities=None):
    return {"coverage": {"boundaries": {"start": "2027-01-01", "end": "2027-05-01"}},
            "runs": runs, "activities": activities or runs, "daily": rows,
            "capacity_metrics": build_capacity_metrics(runs, [])}


def test_future_sleep_is_excluded_even_when_calendar_label_claims_today():
    data = bundle([run("2027-03-10", hour=6)],
                  [sleep("2027-03-09"), sleep("2027-03-10", wake_hour=8, label="2027-03-09")])
    context = build_longitudinal_metrics(data)["run_contexts"][0]
    assert context["preceding_sleep"]["end_gmt"].startswith("2027-03-09T08:")
    assert context["pre_run_hrv"]["value_ms"] == 60
    assert context["prior_3_nights"]["completed_sleep_count"] == 1


def test_misdated_label_does_not_override_actual_gmt_completion():
    data = bundle([run("2027-03-10")], [sleep("2027-03-10", label="2027-03-11")])
    context = build_longitudinal_metrics(data)["run_contexts"][0]
    assert context["preceding_sleep"]["date_label"] == "2027-03-11"
    assert context["preceding_sleep"]["wake_local_date"] == "2027-03-10"
    assert context["preceding_sleep"]["date_label_matches_local_wake"] is False
    assert context["hours_since_sleep_end"] == 2


def test_same_day_daily_rhr_never_leaks_into_pre_run_value():
    rows = [daily("resting_hr", "2027-03-09", 48), daily("resting_hr", "2027-03-10", 99),
            daily("hrv", "2027-03-10", 90)]
    context = build_longitudinal_metrics(bundle([run("2027-03-10")], rows))["run_contexts"][0]
    assert context["pre_run_rhr"]["value_bpm"] == 48
    assert context["pre_run_rhr"]["date"] == "2027-03-09"
    assert context["pre_run_hrv"]["value_ms"] is None


def test_daily_overnight_hrv_needs_completed_matching_sleep_anchor():
    night = sleep("2027-03-10")
    night["values"]["avgOvernightHrv"] = None
    context = build_longitudinal_metrics(bundle([run("2027-03-10")],
                [night, daily("hrv", "2027-03-10", 75)]))["run_contexts"][0]
    assert context["pre_run_hrv"]["value_ms"] == 75
    night["date"] = "2027-03-11"
    context = build_longitudinal_metrics(bundle([run("2027-03-10")],
                [night, daily("hrv", "2027-03-11", 75)]))["run_contexts"][0]
    assert context["pre_run_hrv"]["value_ms"] is None


def test_fallback_sleep_fields_and_null_placeholders_are_handled():
    placeholder = {"kind": "sleep", "date": "2027-03-09", "values": {"sleepTimeSeconds": None, "sleepEndTimestampGMT": None}}
    result = build_longitudinal_metrics(bundle([run("2027-03-10")],
                [placeholder, sleep("2027-03-10", hrv=72, fallback=True)]))
    context = result["run_contexts"][0]
    assert context["preceding_sleep"]["duration_hours"] == 8
    assert context["pre_run_hrv"]["value_ms"] == 72
    assert result["coverage"]["sleep"]["usable_completed_sleep_records"] == 1


def test_duplicate_absence_merges_with_known_value_but_true_conflict_is_unknown():
    first, second = sleep("2027-03-10"), sleep("2027-03-10", hrv=75)
    first["values"]["avgOvernightHrv"] = None
    result = build_longitudinal_metrics(bundle([run("2027-03-10")], [first, second]))
    assert result["run_contexts"][0]["pre_run_hrv"]["value_ms"] == 75
    third = sleep("2027-03-10", hrv=80)
    result = build_longitudinal_metrics(bundle([run("2027-03-10")], [first, second, third, second]))
    assert result["run_contexts"][0]["pre_run_hrv"]["value_ms"] is None


def test_distinct_episodes_on_one_wake_date_do_not_count_as_three_nights():
    rows = [sleep("2027-03-10", hours=8), sleep("2027-03-10", hours=2, wake_hour=9),
            sleep("2027-03-09", hours=7)]
    context = build_longitudinal_metrics(bundle([run("2027-03-10")], rows))["run_contexts"][0]
    assert context["prior_3_nights"]["completed_sleep_count"] == 2
    assert context["prior_3_nights"]["mean_sleep_hours"] == 7.5


def test_circular_timing_handles_midnight_and_rejects_offset_changes():
    rows = [sleep("2027-03-08", hours=8, wake_hour=7.9),
            sleep("2027-03-09", hours=8, wake_hour=8.1), sleep("2027-03-10", hours=8)]
    context = build_longitudinal_metrics(bundle([run("2027-03-10")], rows))["run_contexts"][0]
    assert context["prior_3_nights"]["timing_regularity"]["bedtime_median_absolute_deviation_minutes"] == 6
    rows[0] = sleep("2027-03-08", offset=2)
    context = build_longitudinal_metrics(bundle([run("2027-03-10")], rows))["run_contexts"][0]
    assert context["prior_3_nights"]["timing_regularity"]["status"].startswith("clock_offset_changed")


def test_running_load_uses_completed_prior_sessions_and_never_today_step_totals():
    runs = [run("2027-03-02", "101"), run("2027-03-05", "102"), run("2027-03-10", "103"),
            run("2027-03-11", "104")]
    rows = [daily("steps", "2027-03-05", 12000), daily("steps", "2027-03-09", 8000), daily("steps", "2027-03-10", 90000)]
    context = next(r for r in build_longitudinal_metrics(bundle(runs, rows))["run_contexts"] if r["activity_id"] == "103")
    assert context["prior_running_load"]["7"]["run_count"] == 1
    assert context["prior_running_load"]["28"]["run_count"] == 2
    assert context["prior_steps"]["7"]["recorded_total_steps"] == 20000
    assert context["prior_steps"]["7"]["steps_on_dates_without_observed_runs"] == 8000
    assert context["prior_steps"]["7"]["nonrunning_steps"] is None


def test_missing_end_is_qualified_and_duplicate_runs_do_not_inflate_timeline():
    prior = run("2027-03-09", "101")
    prior["elapsed_s"] = None
    current = run("2027-03-10", "102")
    result = build_longitudinal_metrics(bundle([prior, prior, current], []))
    timeline = next(row for row in result["daily_timeline"] if row["date"] == "2027-03-09")
    assert timeline["recorded_running_km"] == 6
    load = result["run_contexts"][-1]["prior_running_load"]["7"]
    assert load["unconfirmed_end_count"] == 1
    assert load["complete_observation_window"] is False


def test_zero_daily_hrv_and_rhr_are_not_physiological_measurements():
    row = sleep("2027-03-10")
    row["values"]["avgOvernightHrv"] = None
    context = build_longitudinal_metrics(bundle([run("2027-03-10")],
                [row, daily("hrv", "2027-03-10", 0), daily("resting_hr", "2027-03-09", 0)]))["run_contexts"][0]
    assert context["pre_run_hrv"]["value_ms"] is None
    assert context["pre_run_rhr"]["value_bpm"] is None


def test_recovery_uses_pre_baseline_true_24_48h_and_intervening_load():
    index = run("2027-03-10", "101", hour=10, km=20)
    intervening = run("2027-03-11", "102", hour=12)
    rows = [sleep(f"2027-03-{d:02}", hrv=60) for d in (7, 8, 9, 10)]
    rows += [sleep("2027-03-11", hrv=40), sleep("2027-03-12", hrv=50)]
    result = build_longitudinal_metrics(bundle([index, intervening], rows))
    episode = result["post_session_recovery"]["episodes"][0]
    after = next(o for o in episode["outcomes"] if o["phase"] == "24_to_48_hours")
    assert after["hours_after_session_end"] == 44
    assert after["metrics"]["overnight_hrv_ms"]["pre_session_median"] == 60
    assert after["metrics"]["overnight_hrv_ms"]["delta"] == -10
    assert after["intervening_activity_ids"] == ["102"]
    assert after["intervening_load_observed"] is True


def test_overlapping_long_sessions_are_not_independent_recovery_episodes():
    result = build_longitudinal_metrics(bundle([run("2027-03-10", "101", km=20),
                                                run("2027-03-11", "102", km=20)], []))
    assert len(result["post_session_recovery"]["episodes"]) == 1
    assert result["post_session_recovery"]["overlapping_index_sessions_not_aggregated"] == ["102"]


def association_bundle():
    runs, rows = [], []
    for index in range(8):
        day = stamp("2027-03-02") + timedelta(days=index * 4)
        hours = 6 if index % 2 == 0 else 8
        for back in range(3):
            rows.append(sleep((day - timedelta(days=back)).date().isoformat(), hours=hours))
        runs.append(run(day.date().isoformat(), str(101 + index), pace=370 if hours == 6 else 350))
    return bundle(runs, rows)


def test_matched_pairs_are_disjoint_and_one_observation_per_run():
    result = build_longitudinal_metrics(association_bundle())
    assert len(result["pace_hr_observations"]) == 8
    association = result["matched_associations"][0]
    assert association["matched_pair_count"] == 4
    assert association["unique_run_count"] == 8
    assert association["median_pace_difference_seconds_per_km"] == -20
    ids = [p[key] for p in association["pairs"] for key in ("lower_exposure_activity_id", "higher_exposure_activity_id")]
    assert len(ids) == len(set(ids))
    exploratory = result["exploratory_associations"][0]
    assert exploratory["run_count"] == 8
    assert exploratory["unadjusted_spearman_pace_correlation"] == -1


def test_route_mismatch_blocks_strict_pairs_but_exploratory_view_remains():
    data = association_bundle()
    for index, item in enumerate(data["runs"]):
        item["route_context"] = {"route_group_id": "route-001" if index % 2 else "route-002"}
    result = build_longitudinal_metrics(data)
    assert result["matched_associations"][0]["matched_pair_count"] == 0
    assert result["matched_associations"][0]["median_pace_difference_seconds_per_km"] is None
    assert result["exploratory_associations"][0]["run_count"] == 8


def test_partial_sleep_history_not_mislabeled_as_three_night_exposure():
    data = association_bundle()
    data["daily"] = [r for r in data["daily"] if r["date"] in {item["date"] for item in data["runs"]}]
    result = build_longitudinal_metrics(data)
    assert result["exploratory_associations"][0]["run_count"] == 0


def test_empty_results_unknown_values_and_no_private_extra_fields():
    data = bundle([], [])
    data["preciseLatitude"] = 12.3456789
    result = build_longitudinal_metrics(data)
    assert result["run_contexts"] == []
    assert all(a["status"] == "insufficient_matched_pairs" for a in result["matched_associations"])
    assert "12.3456789" not in json.dumps(result)
    json.dumps(result, allow_nan=False)


def test_output_is_deterministic_and_does_not_mutate_inputs():
    data = association_bundle()
    before = copy.deepcopy(data)
    assert build_longitudinal_metrics(data) == build_longitudinal_metrics(data)
    assert data == before


@pytest.mark.parametrize("bad", [None, -1, 0, float("nan")])
def test_unusable_sleep_duration_cannot_anchor_pre_run_night(bad):
    row = sleep("2027-03-10")
    row["values"]["totalSleepTimeInSeconds"] = bad
    result = build_longitudinal_metrics(bundle([run("2027-03-10")], [row]))
    assert result["run_contexts"][0]["preceding_sleep"] is None
