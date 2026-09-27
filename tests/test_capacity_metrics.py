"""Synthetic observations exercise missing HR, continuity and calendar boundaries."""

import copy
import json

import pytest

from garmin_training.capacity_metrics import build_capacity_metrics


def synthetic_run(km=12, pace=300, hr=145, activity_id="101", day="2027-04-04"):
    segments = []
    for index in range(km):
        segments.append({
            "bin_index": index, "sample_count": 100, "hr_bpm_sample_count": 100,
            "start_distance_m": index * 1000, "end_distance_m": (index + 1) * 1000,
            "start_duration_s": index * pace, "end_duration_s": (index + 1) * pace,
            "start_elapsed_s": index * pace, "end_elapsed_s": (index + 1) * pace,
            "start_moving_s": index * pace, "end_moving_s": (index + 1) * pace,
            "mean_hr_bpm": hr, "max_hr_bpm": hr + 5,
            "observed_ascent_m": 2, "observed_descent_m": 2,
        })
    return {
        "activity_id": activity_id, "date": day, "distance_m": km * 1000,
        "distance_km": km, "duration_s": km * pace, "avg_hr_bpm": hr,
        "max_hr_bpm": hr + 5, "source_ids": ["call:00001"],
        "stream": {"source_id": "call:00002", "segments": segments, "segments_omitted": 0},
        "hr_zones": {"source_id": "call:00003", "values": [
            {"zoneNumber": 1, "zoneLowBoundary": 100, "secsInZone": km * pace}]},
    }


def build(*runs, weeks=None, calibration=None):
    return build_capacity_metrics(list(runs), weeks or [], calibration)


def test_uninterrupted_flat_run_is_only_a_numerical_candidate():
    result = build(synthetic_run())
    window = next(w for w in result["sustained_windows"] if "longest_continuous" in w["selection_labels"])
    assert (window["distance_m"], window["duration_s"], window["elapsed_s"], window["moving_s"]) == (12000, 3600, 3600, 3600)
    assert window["pace_s_per_km"] == 300
    assert window["mean_hr_bpm"] == 145
    assert window["hr_sample_fraction"] == 1
    assert window["comparison_status"] == "numerical_candidate_requires_context"
    assert window["effort_and_environment_validated"] is False
    assert window["source_ids"] == ["call:00001", "call:00002", "call:00003"]
    assert result["long_steady_candidates"][0]["window_id"] == window["window_id"]
    assert "drift_percent" not in json.dumps(result)


def test_stopped_segment_never_bridges_into_continuous_window():
    run = synthetic_run()
    for index, segment in enumerate(run["stream"]["segments"]):
        segment["start_elapsed_s"] += 120 if index > 5 else 0
        segment["end_elapsed_s"] += 120 if index >= 5 else 0
    result = build(run)
    assert result["sustained_windows"]
    assert all(not (w["first_bin"] <= 5 <= w["last_bin"]) for w in result["sustained_windows"])
    reasons = result["heart_rate_quality"]["runs"][0]["stream_screening"]["rejected_bin_reasons"]
    assert reasons["pause_or_nonmoving_time"] == 1


def test_pause_between_bins_is_not_hidden_by_clean_bin_spans():
    run = synthetic_run()
    for segment in run["stream"]["segments"][6:]:
        segment["start_elapsed_s"] += 60
        segment["end_elapsed_s"] += 60
    result = build(run)
    assert all(not (w["first_bin"] < 6 <= w["last_bin"]) for w in result["sustained_windows"])


def test_small_pauses_accumulate_and_nonmoving_time_blocks_windows():
    run = synthetic_run()
    for index, segment in enumerate(run["stream"]["segments"]):
        segment["start_elapsed_s"] += index
        segment["end_elapsed_s"] += index + 1
    assert build(run)["sustained_windows"] == []
    run = synthetic_run()
    for index, segment in enumerate(run["stream"]["segments"]):
        segment["start_moving_s"] -= index * 15
        segment["end_moving_s"] -= (index + 1) * 15
    assert build(run)["sustained_windows"] == []


def test_hr_only_in_first_kilometre_does_not_validate_run_average():
    run = synthetic_run(km=16)
    run["hr_zones"]["values"][0]["secsInZone"] = 90
    for segment in run["stream"]["segments"][1:]:
        segment["mean_hr_bpm"] = None
        segment["hr_bpm_sample_count"] = 0
    result = build(run)
    quality = result["heart_rate_quality"]["runs"][0]
    assert quality["hr_zone_time_fraction"] == pytest.approx(0.019)
    assert quality["coverage_supports_full_run_average"] is False
    assert "low_hr_zone_time_coverage" in quality["flags"]
    assert result["sustained_windows"] == []


def test_hr_means_use_valid_sample_counts_not_total_points():
    run = synthetic_run(km=4)
    for index, segment in enumerate(run["stream"]["segments"]):
        segment["mean_hr_bpm"] = 140 if index < 2 else 160
        segment["hr_bpm_sample_count"] = 100 if index < 2 else 95
    window = build(run)["sustained_windows"][0]
    assert window["mean_hr_bpm"] == pytest.approx((28000 + 30400) / 390, abs=0.001)
    assert window["hr_sample_fraction"] == 0.975


def test_sample_count_cannot_exceed_total_and_sparse_stream_requires_review():
    run = synthetic_run()
    for segment in run["stream"]["segments"]:
        segment["hr_bpm_sample_count"] = 101
    assert build(run)["sustained_windows"] == []
    for segment in run["stream"]["segments"]:
        segment["sample_count"] = segment["hr_bpm_sample_count"] = 2
    assert "sparse_stream_samples" in build(run)["sustained_windows"][0]["flags"]


def test_legacy_bins_never_claim_known_within_bin_hr_coverage():
    run = synthetic_run()
    for segment in run["stream"]["segments"]:
        del segment["hr_bpm_sample_count"]
    window = build(run)["sustained_windows"][0]
    assert window["hr_sample_fraction"] is None
    assert "Approximate" in window["hr_mean_basis"]
    assert "within_bin_hr_sample_coverage_unknown" in window["flags"]


def test_outlier_max_is_retained_unvalidated_and_never_sets_threshold():
    run = synthetic_run()
    run["max_hr_bpm"] = 230
    result = build(run, calibration={"maxHR": 190, "private_owner": "DO_NOT_COPY"})
    assert result["heart_rate_quality"]["observed_peak"]["bpm"] == 230
    assert result["heart_rate_quality"]["true_max_hr_status"] == "not_established"
    assert "high_observed_hr_requires_sensor_review" in result["heart_rate_quality"]["runs"][0]["flags"]
    assert "DO_NOT_COPY" not in json.dumps(result)
    assert result["calibration"]["provided"] is True


def test_hills_require_review_despite_even_pace_and_full_hr():
    run = synthetic_run()
    for segment in run["stream"]["segments"]:
        segment["observed_ascent_m"] = 25
        segment["observed_descent_m"] = 25
    candidate = build(run)["long_steady_candidates"][0]
    assert candidate["comparison_status"] == "review_required"
    assert "terrain_variation_requires_comparison_review" in candidate["flags"]


def test_unknown_terrain_and_missing_moving_channel_are_not_flat_continuity():
    run = synthetic_run()
    for segment in run["stream"]["segments"]:
        segment["observed_ascent_m"] = None
    assert "terrain_unknown" in build(run)["sustained_windows"][0]["flags"]
    for segment in run["stream"]["segments"]:
        del segment["start_moving_s"]
    assert build(run)["sustained_windows"] == []


def test_variable_pace_is_preserved_as_continuous_but_never_called_steady():
    run = synthetic_run()
    # A new time base for each bin maintains continuity but changes its pace.
    elapsed = 0
    for index, segment in enumerate(run["stream"]["segments"]):
        pace = 260 if index % 2 else 340
        for field in ("duration_s", "elapsed_s", "moving_s"):
            segment[f"start_{field}"] = elapsed
            segment[f"end_{field}"] = elapsed + pace
        elapsed += pace
    result = build(run)
    assert result["sustained_windows"]
    for window in result["sustained_windows"]:
        assert "variable_pace_requires_effort_review" in window["flags"]
        assert window["comparison_status"] == "review_required"
        assert "longest_pace_screen_pass" not in window["selection_labels"]


def test_weekly_uses_exact_metres_and_snapshot_boundary_not_wall_clock():
    first = synthetic_run(km=1, activity_id="101", day="2027-03-28")
    second = synthetic_run(km=1, activity_id="102", day="2027-03-29")
    third = synthetic_run(km=1, activity_id="103", day="2027-03-30")
    second["distance_m"] = third["distance_m"] = 1000.49
    second["distance_km"] = third["distance_km"] = 1
    weeks = [{"week_start": "2027-03-22", "partial_boundary_week": True},
             {"week_start": "2027-03-29", "partial_boundary_week": False},
             {"week_start": "2027-04-05", "partial_boundary_week": False}]
    result = build(first, second, third, weeks=weeks)["weekly_load"]
    assert result["weeks"][1]["recorded_distance_km"] == 2.001
    assert result["weeks"][1]["recorded_duration_min"] == 10
    assert result["complete_week_baseline"]["mean_distance_km"] == 1.0
    assert result["complete_week_baseline"]["usable_complete_week_count"] == 2
    assert result["complete_week_baseline"]["week_starts"] == ["2027-03-29", "2027-04-05"]
    assert result["weeks"][2]["run_count"] == 0


def test_missing_and_invalid_numeric_values_are_not_zero_observations():
    run = synthetic_run()
    run["date"] = None
    run["distance_m"] = float("nan")
    run["duration_s"] = -3
    result = build(run)["weekly_load"]
    assert result["totals"]["distance_missing_count"] == 1
    assert result["totals"]["duration_missing_count"] == 1
    assert result["totals"]["recorded_distance_km"] is None
    assert result["totals"]["recorded_duration_s"] is None
    assert result["totals"]["undated_run_count"] == 1


def test_longest_distance_and_duration_can_be_different_runs():
    first = synthetic_run(km=12, pace=280, activity_id="101")
    second = synthetic_run(km=11, pace=350, activity_id="102")
    row = build(first, second)["weekly_load"]["weeks"][0]
    assert row["longest_run_activity_id"] == "101"
    assert row["longest_duration_activity_id"] == "102"
    assert row["longest_run_km"] == 12
    assert row["longest_duration_s"] == 3850


def test_missing_zones_remain_unknown_not_zero_and_excess_time_is_flagged():
    run = synthetic_run()
    del run["hr_zones"]
    quality = build(run)["heart_rate_quality"]["runs"][0]
    assert quality["hr_zone_time_s"] is None
    assert quality["hr_zone_time_fraction"] is None
    run["hr_zones"] = {"values": [{"secsInZone": 8000}]}
    assert "hr_zone_time_exceeds_timer" in build(run)["heart_rate_quality"]["runs"][0]["flags"]


def test_deterministic_no_input_mutation_and_bounded_windows():
    run = synthetic_run(km=80)
    original = copy.deepcopy(run)
    a, b = build(run), build(run)
    assert a == b
    assert run == original
    assert len(a["sustained_windows"]) <= 4
    json.dumps(a, allow_nan=False)
