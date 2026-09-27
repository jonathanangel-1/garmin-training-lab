"""Synthetic, offline tests for evidence accuracy and disclosure boundaries."""

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from garmin_training.evidence import build_evidence, summarize_stream


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        (self.snapshot / "raw").mkdir(parents=True)
        self.output = self.root / "evidence"
        self.activities = []
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def add_call(self, method, data, args=None, status="ok", **extra):
        raw = json.dumps(data).encode()
        raw_path = f"raw/{method}-{len(self.calls)}.json"
        (self.snapshot / raw_path).write_bytes(raw)
        self.calls.append({"method": method, "args": args or [], "kwargs": {}, "status": status,
                           "raw_path": raw_path, "sha256": hashlib.sha256(raw).hexdigest(),
                           "retrieved_at": "2026-09-27T12:00:00+00:00", **extra})

    def build(self):
        (self.snapshot / "activities.json").write_text(json.dumps(self.activities))
        (self.snapshot / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "status": "complete",
            "boundaries": {"start": "2026-09-01", "end": "2026-09-27", "detail_start": "2026-09-01", "daily_start": "2026-09-01"},
            "calls": self.calls}))
        return build_evidence(self.snapshot, self.output)

    def test_conversions_zero_and_unknown_remain_distinct(self):
        self.activities = [
            {"activityId": 101, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-21 07:00:00", "distance": 10000, "duration": 3000, "elapsedDuration": 3300, "movingDuration": 2900, "averageSpeed": 10 / 3, "averageHR": 0, "elevationGain": 0},
            {"activityId": 102, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-22 07:00:00", "distance": None, "averageHR": None},
        ]
        result = self.build()
        run = result["runs"][0]
        self.assertEqual(run["distance_km"], 10)
        self.assertEqual(run["pace_s_per_km"], 300)
        self.assertEqual((run["duration_s"], run["elapsed_s"], run["moving_s"]), (3000, 3300, 2900))
        self.assertEqual(run["avg_hr_bpm"], 0)
        self.assertEqual(run["elevation_gain_m"], 0)
        self.assertIsNone(result["runs"][1]["distance_km"])
        week = result["weeks"][-1]
        self.assertEqual(week["recorded_distance_km"], 10)
        self.assertEqual(week["distance_missing_count"], 1)
        self.assertEqual(week["running_days"], 2)

    def test_private_fields_never_enter_bundle(self):
        secret = "PRIVATE_OWNER_TEST_SENTINEL"
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"},
                            "activityName": secret, "description": secret, "ownerDisplayName": secret,
                            "ownerId": 983421, "userProfileId": 983421, "email": secret,
                            "profileImageUrl": secret, "startLatitude": 40.123456789,
                            "startLongitude": -73.123456789, "locationName": "New York"}]
        self.add_call("get_activity", {"summaryDTO": {"distance": 5000, "startLatitude": 40.123456789}, "activityName": secret}, [101])
        self.add_call("get_sleep_data", {"dailySleepDTO": {"userProfilePK": 983421, "calendarDate": "2026-09-26", "sleepTimeSeconds": 28800, "owner": secret}, "secret": secret}, ["2026-09-26"])
        self.add_call("get_unknown_profile", {"displayName": secret, "private": secret})
        self.add_call("get_activity_gear", [{"displayName": secret, "customMakeModel": secret}], [101])
        result = self.build()
        encoded = json.dumps(result)
        for private in (secret, "983421", "40.123456789", "-73.123456789", "ownerDisplayName", "profileImageUrl", "activityName"):
            self.assertNotIn(private, encoded)
        self.assertEqual(result["runs"][0]["location_name"], "New York")
        self.assertEqual(result["runs"][0]["distance_km"], 5)
        self.assertEqual(result["daily"][0]["values"]["sleepTimeSeconds"], 28800)
        unknown = next(row for row in result["resources"] if row["method"] == "get_unknown_profile")
        self.assertEqual(unknown["normalization"], "schema_not_supported")

    def test_descriptor_remapping_cannot_swap_coordinates_into_hr(self):
        details = {"metricDescriptors": [
            {"key": "directLatitude", "metricsIndex": 0},
            {"key": "directHeartRate", "metricsIndex": 3},
            {"key": "sumDistance", "metricsIndex": 4, "unit": {"key": "meter"}},
            {"key": "directSpeed", "metricsIndex": 2},
            {"key": "directElevation", "metricsIndex": 1},
            {"key": "sumElapsedDuration", "metricsIndex": 5},
            {"key": "sumMovingDuration", "metricsIndex": 6},
        ], "activityDetailMetrics": [
            {"metrics": [40.123456789, 10, 3, 140, 0, 0, 0]},
            {"metrics": [40.123456789, 30, 3, 150, 900, 320, 300]},
            {"metrics": [40.123456789, 20, 2, 160, 1100, 420, 400]},
        ]}
        profile = summarize_stream(details)
        first = profile["segments"][0]
        self.assertEqual(first["mean_hr_bpm"], 145)
        self.assertEqual(first["mean_speed_mps"], 3)
        self.assertEqual(first["observed_ascent_m"], 20)
        self.assertEqual(first["span_elapsed_s"], 320)
        self.assertEqual(first["span_moving_s"], 300)
        self.assertNotIn("40.123456789", json.dumps(profile))
        self.assertNotIn("directLatitude", json.dumps(profile))
        reversed_descriptors = [{**row, "metricsIndex": 6 - row["metricsIndex"]} for row in details["metricDescriptors"]]
        remapped = {**details, "metricDescriptors": reversed_descriptors,
                    "activityDetailMetrics": [{"metrics": list(reversed(row["metrics"]))} for row in details["activityDetailMetrics"]]}
        self.assertEqual(profile, summarize_stream(remapped))

    def test_no_decoupling_verdict_for_long_or_variable_run(self):
        details = {"metricDescriptors": [{"key": "sumDistance", "metricsIndex": 0}, {"key": "directHeartRate", "metricsIndex": 1}, {"key": "directSpeed", "metricsIndex": 2}],
                   "activityDetailMetrics": [{"metrics": [0, 120, 2]}, {"metrics": [10000, 150, 3]}, {"metrics": [20000, 180, 4]}]}
        profile = summarize_stream(details)
        self.assertEqual(profile["decoupling"]["status"], "not_computed")
        self.assertNotIn("percent", profile["decoupling"])

    def test_missing_stream_fields_and_explicit_millisecond_units(self):
        details = {"metricDescriptors": [
            {"key": "sumElapsedDuration", "metricsIndex": 0, "unit": "millisecond"},
            {"key": "directHeartRate", "metricsIndex": 1},
            {"key": "sumDistance", "metricsIndex": -1},
            {"key": "directSpeed", "metricsIndex": "1"},
        ], "activityDetailMetrics": [{"metrics": [0, None]}, {"metrics": [299000, 140]}, {"metrics": [301000]}]}
        profile = summarize_stream(details)
        self.assertEqual(profile["binning"], "5_min_observed_points")
        self.assertEqual(profile["segments"][0]["span_elapsed_s"], 299)
        self.assertEqual(profile["segments"][0]["mean_hr_bpm"], 140)
        self.assertIsNone(profile["segments"][1]["mean_hr_bpm"])
        self.assertEqual(summarize_stream({})["segments"], [])

    def test_failed_calls_unknown_resources_and_hash_mismatch_are_visible(self):
        self.add_call("get_daily_steps", [{"calendarDate": "2026-09-26", "totalSteps": 0}], ["2026-09-26", "2026-09-27"])
        self.add_call("get_hrv_data", None, ["2026-09-26"], status="unavailable", error="secret email and request URL")
        self.add_call("get_stats", {"totalSteps": 999}, ["2026-09-26"], sha256="a" * 64)
        self.add_call("get_sleep_data", {}, ["2026-09-26"], raw_path="../../private.json")
        result = self.build()
        self.assertEqual(result["daily"][0]["values"]["totalSteps"], 0)
        self.assertEqual(result["coverage"]["normalization_error_count"], 2)
        self.assertEqual(result["coverage"]["call_status_counts"]["unavailable"], 1)
        self.assertNotIn("secret email", json.dumps(result))
        self.assertNotIn("999", json.dumps(result["daily"]))
        self.assertEqual(stat.S_IMODE((self.output / "evidence.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.output / "evidence.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)

    def test_duplicate_ids_do_not_inflate_weekly_distance(self):
        summary = {"activityId": 10, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-21 10:00:00", "distance": 10000}
        self.activities = [summary, dict(summary)]
        result = self.build()
        self.assertEqual(result["coverage"]["duplicate_activity_ids_omitted"], ["10"])
        self.assertEqual(result["weeks"][-1]["recorded_distance_km"], 10)

    def test_laps_and_supplied_gap_are_kept_without_estimation(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"}, "distance": 1000, "duration": 300, "avgGradeAdjustedSpeed": 4}]
        self.add_call("get_activity_splits", {"lapDTOs": [{"lapIndex": 1, "distance": 1000, "duration": 300, "averageSpeed": 10 / 3, "averageHR": 145, "startLatitude": 40}]}, [101])
        result = self.build()
        run = result["runs"][0]
        self.assertEqual(run["garmin_grade_adjusted_pace_s_per_km"], 250)
        self.assertEqual(run["laps"][0]["pace_s_per_km"], 300)
        self.assertIsNone(run["laps"][0]["garmin_grade_adjusted_pace_s_per_km"])

    def test_range_sleep_and_hrv_preserve_dates_and_unknown_dates(self):
        self.add_call("get_sleep_daily", [
            {"calendarDate": "2026-09-21", "overallSleepScore": 75, "totalSleepSeconds": 25200},
            {"overallSleepScore": 80},
        ], ["2026-09-20", "2026-09-27"])
        self.add_call("get_hrv_data_range", {"hrvSummaries": [
            {"calendarDate": "2026-09-21", "lastNightAvg": 44, "baseline": {"balancedLow": 40, "balancedUpper": 60, "userProfilePK": 999}},
        ]}, ["2026-09-20", "2026-09-27"])
        result = self.build()
        sleeps = [row for row in result["daily"] if row["kind"] == "sleep"]
        self.assertIsNone(sleeps[0]["date"])
        self.assertEqual(sleeps[1]["date"], "2026-09-21")
        self.assertEqual(sleeps[1]["values"]["totalSleepSeconds"], 25200)
        self.assertEqual(result["coverage"]["daily_records_without_date"], 1)
        self.assertNotIn("999", json.dumps(result["daily"]))

    def test_date_only_unknown_payload_does_not_claim_daily_metric_coverage(self):
        self.add_call("get_nutrition_daily_meals", {"calendarDate": "2026-09-21", "unknownPrivateSchema": {"name": "SECRET"}}, ["2026-09-21"])
        result = self.build()
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["resources"][0]["normalization"], "schema_not_supported")

    def test_summary_disagreement_is_exposed_without_silent_replacement(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"}, "distance": 1000, "duration": 300}]
        self.add_call("get_activity", {"summaryDTO": {"distance": 1100, "duration": 300}}, [101])
        result = self.build()
        run = result["runs"][0]
        self.assertEqual(run["distance_m"], 1000)
        conflict = next(item for item in run["summary_conflicts"] if item["field"] == "distance_m")
        self.assertEqual(conflict["detail_value"], 1100)
        self.assertEqual(result["coverage"]["activities_with_summary_conflicts"], 1)

    def test_nested_sleep_values_preserve_duration_need_units_and_privacy(self):
        self.add_call("get_sleep_daily", [{"calendarDate": "2026-09-21", "values": {
            "totalSleepTimeInSeconds": 27000, "remTime": 6000, "lightTime": 17000,
            "deepTime": 4000, "awakeTime": 900, "sleepNeed": 480,
            "sleepScore": 82, "avgOvernightHrv": 50, "restingHeartRate": 48,
            "skinTempC": -0.2, "hrvStatus": "BALANCED", "ownerName": "SECRET_OWNER",
            "profileId": 987654321,
        }}], ["2026-09-20", "2026-09-27"])
        result = self.build()
        sleep = result["daily"][0]
        self.assertEqual(sleep["date"], "2026-09-21")
        self.assertEqual(sleep["values"]["totalSleepTimeInSeconds"], 27000)
        self.assertEqual(sleep["values"]["sleepNeed"], 480)
        self.assertEqual(result["daily_units"]["sleep"]["totalSleepTimeInSeconds"], "seconds")
        self.assertIn("not independently verified", result["daily_units"]["sleep"]["sleepNeed"])
        self.assertEqual(result["coverage"]["usable_metric_date_counts"]["sleep"]["totalSleepTimeInSeconds"], 1)
        self.assertEqual(result["coverage"]["usable_metric_date_counts"]["sleep"]["skinTempC"], 1)
        self.assertNotIn("SECRET_OWNER", json.dumps(result))
        self.assertNotIn("987654321", json.dumps(result))

    def test_vo2_nested_generic_and_cycling_dates_without_owner_id(self):
        self.add_call("get_max_metrics_range", [{"userId": 987654321,
            "generic": {"calendarDate": "2026-09-21", "vo2MaxPreciseValue": 51.2, "vo2MaxValue": 51, "fitnessAgeDescription": "PRIVATE_DESCRIPTION"},
            "cycling": {"calendarDate": "2026-09-22", "vo2MaxPreciseValue": 49.3},
            "heatAltitudeAcclimation": {"currentAltitude": 42},
        }], ["2026-09-20", "2026-09-27"])
        result = self.build()
        rows = result["daily"]
        self.assertEqual([row["date"] for row in rows], ["2026-09-21", "2026-09-22"])
        self.assertEqual([row["values"]["source_sport_category"] for row in rows], ["generic", "cycling"])
        self.assertEqual(result["coverage"]["usable_metric_date_counts"]["vo2_estimate"]["vo2MaxPreciseValue"], 2)
        self.assertNotIn("987654321", json.dumps(result))
        self.assertNotIn("PRIVATE_DESCRIPTION", json.dumps(result))

    def test_stream_factor_metadata_is_not_reapplied_and_cadences_are_distinct(self):
        details = {"metricDescriptors": [
            {"key": "sumDistance", "metricsIndex": 0, "unit": {"key": "meter", "factor": 100}},
            {"key": "sumDuration", "metricsIndex": 1, "unit": {"key": "second", "factor": 1000}},
            {"key": "directSpeed", "metricsIndex": 2, "unit": {"key": "mps", "factor": 0.1}},
            {"key": "directRunCadence", "metricsIndex": 3, "unit": {"key": "stepsPerMinute", "factor": 1}},
            {"key": "directDoubleCadence", "metricsIndex": 4, "unit": {"key": "stepsPerMinute", "factor": 1}},
            {"key": "directGradeAdjustedSpeed", "metricsIndex": 5, "unit": {"key": "mps", "factor": 0.1}},
        ], "activityDetailMetrics": [{"metrics": [0, 0, 3.2, 80, 160, 3.4]}, {"metrics": [960, 300, 3.2, 82, 164, 3.4]}]}
        result = summarize_stream(details)
        segment = result["segments"][0]
        self.assertEqual(segment["span_distance_m"], 960)
        self.assertEqual(segment["span_duration_s"], 300)
        self.assertEqual(segment["mean_speed_mps"], 3.2)
        self.assertEqual(segment["mean_run_cadence_source"], 81)
        self.assertEqual(segment["mean_double_cadence_source"], 162)
        self.assertEqual(segment["mean_garmin_grade_adjusted_speed_mps"], 3.4)
        self.assertNotIn("mean_cadence_spm", segment)

    def test_body_battery_observed_extrema_resolve_descriptor_and_ignore_missing(self):
        self.add_call("get_body_battery", [{"date": "2026-09-21", "charged": 25, "drained": 40,
            "bodyBatteryValueDescriptorDTOList": [
                {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "timestamp"},
                {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
            ], "bodyBatteryValuesArray": [[50, 1000000000000], [0, 1000000100000], [None, 1000000200000], [-1, 1000000300000], [75, 1000000400000]],
            "bodyBatteryDynamicFeedbackEvent": {"feedbackText": "PRIVATE_TEXT"},
        }], ["2026-09-20", "2026-09-27"])
        result = self.build()
        values = result["daily"][0]["values"]
        self.assertEqual(values["observed_min_level"], 0)
        self.assertEqual(values["observed_max_level"], 75)
        self.assertEqual(values["valid_level_sample_count"], 3)
        self.assertEqual(values["source_level_sample_count"], 5)
        self.assertNotIn("PRIVATE_TEXT", json.dumps(result))
        self.assertNotIn("1000000000000", json.dumps(result))

    def test_gear_keeps_vendor_make_model_and_omits_custom_names_and_identifiers(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"}}]
        self.add_call("get_activity_gear", [{"brandName": "Example Brand", "modelName": "Road 2",
            "uuid": "PRIVATE_UUID", "displayName": "PRIVATE_NAME", "customMakeModel": "PRIVATE_CUSTOM",
            "userProfilePk": 987654321}], [101])
        result = self.build()
        self.assertEqual(result["runs"][0]["gear"]["items"], [{"brandName": "Example Brand", "modelName": "Road 2"}])
        for secret in ("PRIVATE_UUID", "PRIVATE_NAME", "PRIVATE_CUSTOM", "987654321"):
            self.assertNotIn(secret, json.dumps(result))

    def test_negative_stress_becomes_null_with_audit_and_signed_fields_survive(self):
        for day, stress in (("2026-09-21", -1), ("2026-09-22", 0), ("2026-09-23", 42)):
            self.add_call("get_user_summary", {"calendarDate": day, "averageStressLevel": stress}, [day])
        self.add_call("get_sleep_daily", [{"calendarDate": "2026-09-21", "values": {
            "skinTempC": -0.4, "skinTempF": -0.7, "bodyBatteryChange": -12,
            "totalSleepTimeInSeconds": 24000,
        }}], ["2026-09-20", "2026-09-27"])

        result = self.build()

        summaries = [row for row in result["daily"] if row["kind"] == "daily_summary"]
        self.assertEqual([row["values"]["averageStressLevel"] for row in summaries], [None, 0, 42])
        counts = result["coverage"]["usable_metric_date_counts"]
        self.assertEqual(counts["daily_summary"]["averageStressLevel"], 2)
        self.assertEqual(result["coverage"]["daily_values_normalized_to_null"], 1)
        note = result["data_quality_notes"][0]
        self.assertEqual(note["date"], "2026-09-21")
        self.assertEqual(note["source_id"], summaries[0]["source_id"])
        self.assertEqual(note["field"], "averageStressLevel")
        self.assertEqual(note["original_value"], -1)
        self.assertIsNone(note["replacement"])
        sleep = next(row for row in result["daily"] if row["kind"] == "sleep")
        self.assertEqual(sleep["values"]["skinTempC"], -0.4)
        self.assertEqual(sleep["values"]["skinTempF"], -0.7)
        self.assertEqual(sleep["values"]["bodyBatteryChange"], -12)
        for field in ("skinTempC", "skinTempF", "bodyBatteryChange"):
            self.assertEqual(counts["sleep"][field], 1)
        rendered = (self.output / "evidence.md").read_text()
        self.assertIn("2026-09-21", rendered)
        self.assertIn("call:00000", rendered)
        self.assertIn("represented as null", rendered)

    def test_sub_centimetre_and_sub_centisecond_rounding_is_not_a_conflict(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"},
            "distance": 1000.499999999, "duration": 300.0000003,
            "elapsedDuration": 305.001, "movingDuration": 298.004,
            "elevationGain": 5.00000001, "averageHR": 145}]
        self.add_call("get_activity", {"summaryDTO": {
            "distance": 1000.500000001, "duration": 300,
            "elapsedDuration": 305.002, "movingDuration": 298.005,
            "elevationGain": 5, "averageHR": 145}}, [101])

        result = self.build()

        self.assertEqual(result["coverage"]["activities_with_summary_conflicts"], 0)
        self.assertNotIn("summary_conflicts", result["runs"][0])
        self.assertEqual(result["runs"][0]["distance_m"], 1000.499999999)

    def test_conflicts_at_tolerance_and_meaningful_hr_changes_are_retained(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"},
            "distance": 1000.5, "duration": 300, "averageHR": 145}]
        self.add_call("get_activity", {"summaryDTO": {
            "distance": 1000.51, "duration": 300.01, "averageHR": 146}}, [101])

        result = self.build()

        fields = {row["field"] for row in result["runs"][0]["summary_conflicts"]}
        self.assertEqual(fields, {"distance_m", "duration_s", "avg_hr_bpm"})
        self.assertEqual(result["coverage"]["activities_with_summary_conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
