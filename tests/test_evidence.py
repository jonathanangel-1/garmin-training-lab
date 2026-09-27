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

    def test_threshold_latest_and_history_have_source_dates_and_explicit_uncertainty(self):
        self.add_call("get_lactate_threshold", {
            "speed_and_heart_rate": {"calendarDate": "2026-10-05", "heartRate": 169,
                "speed": 4.25, "userProfilePK": 918273645, "displayName": "PRIVATE_OWNER"},
            "power": {"calendarDate": "2026-10-05", "value": 300, "userProfilePK": 918273645},
        }, kwargs={"latest": True}, retrieved_at="2026-10-06T12:00:00+00:00")
        self.add_call("get_lactate_threshold", {
            "heart_rate": [{"calendarDate": "2026-09-12", "value": 166, "userId": 918273645}],
            "speed": {"values": [{"date": "2026-09-12", "value": 4.1}]},
            "power": [{"calendarDate": "2026-09-12", "value": 295}],
        }, kwargs={"latest": False, "start_date": "2026-09-01", "end_date": "2026-09-27", "aggregation": "daily"})

        result = self.build()

        latest = next(row for row in result["calibration"] if row["kind"] == "lactate_threshold_latest")
        self.assertEqual(latest["values"]["threshold_hr_bpm"], 169)
        self.assertEqual(latest["values"]["threshold_speed_source"], 4.25)
        self.assertEqual(latest["classification"], "garmin_reported_estimate")
        self.assertEqual(latest["temporal_scope"], "latest_at_retrieval")
        self.assertTrue(latest["observation_after_cutoff"])
        self.assertIn("unit unverified", latest["units"]["threshold_speed_source"])
        history = [row for row in result["calibration"] if row["kind"] == "lactate_threshold_history"]
        self.assertEqual(len(history), 3)
        self.assertTrue(all(row["date"] == "2026-09-12" for row in history))
        self.assertTrue(all(row["temporal_scope"] == "historical_daily" for row in history))
        source = next(row for row in result["sources"] if row["source_id"] == history[0]["source_id"])
        self.assertEqual(source["requested_date"], "2026-09-01")
        self.assertEqual(source["requested_end"], "2026-09-27")
        self.assertNotIn("918273645", json.dumps(result))
        self.assertNotIn("PRIVATE_OWNER", json.dumps(result))

    def test_configured_zones_and_profile_are_not_inferred_maximum_hr(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"}, "maxHR": 217}]
        self.add_call("get_heart_rate_zones", [{"sport": "RUNNING", "zone1Floor": 125,
            "zone5Floor": 166, "calculationMethod": "PERCENT_MAX_HR", "userProfilePK": 918273645}])
        self.add_call("get_user_profile", {"userData": {"maxHeartRate": 185, "restingHeartRate": 48,
            "email": "private@example.invalid", "birthDate": "1900-01-01", "weight": 74000,
            "userProfilePK": 918273645}})

        result = self.build()

        zones = next(row for row in result["calibration"] if row["kind"] == "configured_heart_rate_zones")
        self.assertEqual(zones["values"]["zone5Floor"], 166)
        self.assertNotIn("maxHeartRate", zones["values"])
        configured = next(row for row in result["calibration"] if row["kind"] == "profile_heart_rate_configuration")
        self.assertEqual(configured["values"]["maxHeartRate"], 185)
        self.assertEqual(configured["classification"], "configured_value")
        self.assertEqual(configured["temporal_scope"], "latest_at_retrieval")
        self.assertEqual(result["runs"][0]["max_hr_bpm"], 217)
        self.assertNotIn("217", json.dumps(result["calibration"]))
        for secret in ("private@example.invalid", "1900-01-01", "74000", "918273645"):
            self.assertNotIn(secret, json.dumps(result))

    def test_device_and_activity_sensor_metadata_remove_identifiers_and_custom_names(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"}}]
        self.add_call("get_devices", [{"deviceId": 918273645, "displayName": "PRIVATE_OWNER_WATCH",
            "productDisplayName": "Example Runner 2", "manufacturerName": "Example",
            "serialNumber": "PRIVATE_SERIAL", "email": "private@example.invalid"}])
        self.add_call("get_device_settings", {"heartRateSettings": {"maxHeartRate": 185},
            "pairedSensors": [{"sensorType": "HEART_RATE", "connectionType": "ANT_PLUS",
                "serialNumber": "PRIVATE_SERIAL", "deviceId": 918273645}]}, [918273645])
        self.add_call("get_activity", {"metadataDTO": {"deviceManufacturer": "Example",
            "heartRateSource": "CHEST_STRAP", "deviceId": 918273645,
            "sensors": [{"sensorType": "HEART_RATE", "serialNumber": "PRIVATE_SERIAL"}]}}, [101])

        result = self.build()

        device = next(row for row in result["calibration"] if row["kind"] == "device_metadata")
        self.assertEqual(device["values"]["productDisplayName"], "Example Runner 2")
        self.assertEqual(device["classification"], "device_metadata")
        self.assertTrue(any(row["values"].get("heartRateSource") == "CHEST_STRAP" for row in result["runs"][0]["sensor_metadata"]))
        for secret in ("918273645", "PRIVATE_OWNER_WATCH", "PRIVATE_SERIAL", "private@example.invalid"):
            self.assertNotIn(secret, json.dumps(result))

    def test_unknown_calibration_schema_advertises_availability_without_dumping(self):
        self.add_call("get_primary_training_device", {"accountName": "PRIVATE_OWNER",
            "newPrivateSchema": {"secret": "PRIVATE_SECRET"}})
        result = self.build()
        self.assertEqual(result["calibration"], [])
        self.assertEqual(result["resources"][0]["normalization"], "schema_not_supported")
        self.assertNotIn("PRIVATE_OWNER", json.dumps(result))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    def test_hr_sample_count_excludes_missing_and_nonpositive_samples(self):
        details = {"metricDescriptors": [
            {"key": "sumDistance", "metricsIndex": 0},
            {"key": "directHeartRate", "metricsIndex": 1},
        ], "activityDetailMetrics": [{"metrics": [0, 0]}, {"metrics": [100, 145]},
            {"metrics": [200, None]}, {"metrics": [300, 155]}]}
        segment = summarize_stream(details)["segments"][0]
        self.assertEqual(segment["sample_count"], 4)
        self.assertEqual(segment["hr_bpm_sample_count"], 2)
        self.assertEqual(segment["mean_hr_bpm"], 150)
        self.assertEqual(segment["min_hr_bpm"], 145)

    def test_actual_history_period_schema_and_training_method_are_preserved(self):
        self.add_call("get_lactate_threshold", {
            "heart_rate": [{"from": "2026-07-11", "until": "2026-07-11", "series": "running",
                "value": 171, "updatedDate": "2026-07-12"}],
            "speed": [{"from": "2026-07-11", "until": "2026-07-11", "series": "running",
                "value": 0.42, "updatedDate": "2026-07-12"}], "power": [],
        }, kwargs={"latest": False, "start_date": "2026-07-01", "end_date": "2026-09-27", "aggregation": "daily"})
        self.add_call("get_heart_rate_zones", [{"trainingMethod": "LACTATE_THRESHOLD",
            "maxHeartRateUsed": 189, "lactateThresholdHeartRateUsed": 171,
            "zone1Floor": 100, "restingHrAutoUpdateUsed": False, "sport": "DEFAULT"}])
        self.add_call("get_user_profile", {"userData": {"lactateThresholdHeartRate": 171,
            "lactateThresholdSpeed": 0.42, "thresholdHeartRateAutoDetected": True}})

        result = self.build()

        history = [row for row in result["calibration"] if row["kind"] == "lactate_threshold_history"]
        self.assertEqual(len(history), 2)
        for row in history:
            self.assertEqual(row["date"], "2026-07-11")
            self.assertEqual(row["date_basis"], "from")
            self.assertEqual(row["period_end"], "2026-07-11")
            self.assertEqual(row["source_updated_date"], "2026-07-12")
        speed = next(row for row in history if "threshold_speed_source" in row["values"])
        self.assertEqual(speed["values"]["threshold_speed_source"], 0.42)
        self.assertIn("unverified", speed["units"]["threshold_speed_source"])
        zones = next(row for row in result["calibration"] if row["kind"] == "configured_heart_rate_zones")
        self.assertEqual(zones["values"]["trainingMethod"], "LACTATE_THRESHOLD")
        profile = next(row for row in result["calibration"] if row["kind"] == "profile_heart_rate_configuration")
        self.assertTrue(profile["values"]["thresholdHeartRateAutoDetected"])

    def test_primary_device_join_preserves_unicode_model_without_exposing_device_identity(self):
        self.add_call("get_devices", [{"deviceId": 918273645, "productDisplayName": "Example fēnix – AMOLED",
            "hasOpticalHeartRate": True, "displayName": "PRIVATE_CUSTOM_NAME", "serialNumber": "PRIVATE_SERIAL"}])
        self.add_call("get_primary_training_device", {"PrimaryTrainingDevice": {"deviceId": 918273645}})
        self.add_call("get_device_settings", {"deviceId": 918273645, "opticalHeartRateEnabled": False}, [918273645])

        result = self.build()

        primary = next(row for row in result["calibration"] if row["kind"] == "primary_training_device")
        self.assertEqual(primary["values"]["productDisplayName"], "Example fēnix – AMOLED")
        self.assertIn("supporting_source_id", primary)
        settings = next(row for row in result["calibration"] if row.get("values", {}).get("opticalHeartRateEnabled") is False)
        self.assertEqual(settings["device_context"]["values"]["productDisplayName"], "Example fēnix – AMOLED")
        for secret in ("918273645", "PRIVATE_CUSTOM_NAME", "PRIVATE_SERIAL"):
            self.assertNotIn(secret, json.dumps(result))

    def test_capacity_metrics_integration_uses_observations_without_inferring_true_maximum(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-09-21 08:00:00", "distance": 10500.25,
            "duration": 3200, "maxHR": 217, "averageHR": 148}]
        result = self.build()
        capacity = result["capacity_metrics"]
        self.assertEqual(capacity["heart_rate_quality"]["true_max_hr_status"], "not_established")
        self.assertEqual(capacity["heart_rate_quality"]["observed_peak"]["bpm"], 217)
        self.assertFalse(capacity["calibration"]["provided"])
        self.assertEqual(len(capacity["heart_rate_quality"]["runs"]), 1)
        self.assertIn("Capacity evidence index", (self.output / "evidence.md").read_text())

    def test_longitudinal_integration_requires_completed_sleep_and_preserves_provenance(self):
        from datetime import datetime, timezone

        def millis(value):
            return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)

        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-09-02 08:00:00", "startTimeGMT": "2026-09-02 12:00:00", "distance": 10_000, "duration": 3300, "elapsedDuration": 3400}]
        self.add_call("get_sleep_data", {"dailySleepDTO": {"calendarDate": "2026-09-02", "sleepTimeSeconds": 25_200,
            "sleepStartTimestampGMT": millis("2026-09-02T01:00:00"), "sleepEndTimestampGMT": millis("2026-09-02T08:00:00")}}, ["2026-09-02"])
        self.add_call("get_sleep_data", {"dailySleepDTO": {"calendarDate": "2026-09-03", "sleepTimeSeconds": None}}, ["2026-09-03"])
        result = self.build()
        longitudinal = result["longitudinal_metrics"]
        self.assertEqual(longitudinal["coverage"]["aligned_run_count"], 1)
        self.assertEqual(longitudinal["coverage"]["sleep"]["usable_completed_sleep_records"], 1)
        self.assertEqual(longitudinal["coverage"]["sleep"]["rejected_or_qualified"]["missing_or_invalid_explicit_gmt_interval"], 1)
        self.assertIn("call:00000", json.dumps(longitudinal["run_contexts"]))
        self.assertNotIn("call:00001", json.dumps(longitudinal["run_contexts"]))
        self.assertIn("Longitudinal evidence index", (self.output / "evidence.md").read_text())

    def test_stream_coverage_separates_response_samples_bins_hr_and_unavailable(self):
        self.activities = [{"activityId": identifier, "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-09-21 08:00:00", "distance": 200, "duration": 60}
            for identifier in range(101, 107)]
        self.add_call("get_activity_details", {"measurementCount": 0, "metricsCount": 0,
            "metricDescriptors": [], "activityDetailMetrics": []}, [101])
        self.add_call("get_activity_details", {
            "metricDescriptors": [{"key": "directLatitude", "metricsIndex": 0}],
            "activityDetailMetrics": [{"metrics": [41.123456789]}, {"metrics": [41.223456789]}],
        }, [102])
        self.add_call("get_activity_details", {
            "metricDescriptors": [{"key": "directHeartRate", "metricsIndex": 0}],
            "activityDetailMetrics": [{"metrics": [140]}, {"metrics": [145]}],
        }, [103])
        descriptors = [{"key": "sumDistance", "metricsIndex": 0},
                       {"key": "directHeartRate", "metricsIndex": 1}]
        self.add_call("get_activity_details", {"metricDescriptors": descriptors,
            "activityDetailMetrics": [{"metrics": [0, None]}, {"metrics": [100, 0]}]}, [104])
        self.add_call("get_activity_details", {"metricDescriptors": descriptors,
            "activityDetailMetrics": [{"metrics": [0, 140]}, {"metrics": [100, 145]}]}, [105])
        self.add_call("get_activity_details", None, [106], status="unavailable")

        result = self.build()

        coverage = result["coverage"]
        self.assertEqual(coverage["running_count"], 6)
        self.assertEqual(coverage["runs_with_streams"], 5)
        self.assertEqual(coverage["runs_with_raw_stream_samples"], 4)
        self.assertEqual(coverage["runs_with_usable_stream_samples"], 3)
        self.assertEqual(coverage["runs_with_usable_stream_profiles"], 2)
        self.assertEqual(coverage["runs_with_hr_stream_profiles"], 1)
        self.assertEqual(coverage["runs_with_empty_stream_profiles"], 3)
        self.assertEqual(coverage["call_status_counts"]["unavailable"], 1)
        self.assertNotIn("stream", result["runs"][-1])
        self.assertEqual(result["runs"][-1]["distance_m"], 200)
        rendered = (self.output / "evidence.md").read_text()
        self.assertIn("stream responses: 5", rendered)
        self.assertIn("usable stream profiles: 2", rendered)
        self.assertIn("responses without segments: 3", rendered)
        self.assertNotIn("41.123456789", json.dumps(result))


    def test_time_context_uses_event_offset_across_utc_midnight_and_dst(self):
        self.activities = [
            {"activityId": 101, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-01-02 23:30:00", "startTimeGMT": "2026-01-03 04:30:00", "locationName": "  "},
            {"activityId": 102, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-02 23:30:00", "startTimeGMT": "2026-09-03 03:30:00"},
            {"activityId": 103, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-03 07:00:00", "startTimeGMT": "2026-09-03 04:00:00"},
        ]
        for identifier in (101, 102, 103):
            self.add_call("get_activity", {"timeZoneUnitDTO": {"unitKey": "America/New_York", "unitId": 998877}}, [identifier])
        result = self.build()
        first, second, third = result["runs"]
        self.assertEqual(first["date"], "2026-01-02")
        self.assertIsNone(first["location_name"])
        self.assertEqual(first["time_context"]["observed_utc_offset_minutes"], -300)
        self.assertEqual(second["time_context"]["observed_utc_offset_minutes"], -240)
        self.assertEqual(first["time_context"]["timestamp_consistency"], "consistent_with_explicit_timezone")
        self.assertEqual(third["time_context"]["timestamp_consistency"], "conflicts_with_explicit_timezone")
        self.assertNotIn("country", first["time_context"])
        self.assertNotIn("998877", json.dumps(result))

    def test_anonymous_route_groups_match_full_corridors_and_keep_coordinates_private(self):
        self.activities = [{"activityId": identifier, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-02 08:00:00"} for identifier in range(101, 106)]
        route = [{"lat": 40.123456789 + index * .001, "lon": -73.123456789} for index in range(21)]
        parallel = [{"lat": row["lat"], "lon": row["lon"] + .0005} for row in route]
        different = [{"lat": route[0]["lat"], "lon": route[0]["lon"] + index * .001} for index in range(21)]
        for identifier, points in ((101, route), (102, list(reversed(parallel))), (103, different), (104, route[:2])):
            self.add_call("get_activity_details", {"geoPolylineDTO": {"polyline": points}}, [identifier])
        self.add_call("get_activity_details", {"geoPolylineDTO": None, "metricDescriptors": None, "activityDetailMetrics": None}, [105])
        result = self.build()
        runs = result["runs"]
        self.assertEqual(runs[0]["route_context"]["route_group_id"], runs[1]["route_context"]["route_group_id"])
        self.assertEqual(runs[0]["route_context"]["matched_activity_ids"], ["102"])
        self.assertEqual(runs[2]["route_context"]["status"], "no_matching_corridor")
        self.assertEqual(runs[3]["route_context"]["status"], "insufficient_coordinates")
        self.assertEqual(runs[4]["route_context"]["status"], "insufficient_coordinates")
        self.assertEqual(result["coverage"]["route_context"]["runs_with_matched_corridors"], 2)
        for private in ("40.123456789", "-73.123456789", "geoPolylineDTO", '"lat"', '"lon"'):
            self.assertNotIn(private, json.dumps(result))

    def test_route_groups_do_not_chain_nearby_but_incomparable_corridors(self):
        self.activities = [{"activityId": identifier, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-02 08:00:00"} for identifier in range(101, 104)]
        for identifier, shift in ((101, 0), (102, .0012), (103, .0024)):
            points = [{"lat": 40 + index * .001, "lon": -73 + shift} for index in range(21)]
            self.add_call("get_activity_details", {"geoPolylineDTO": {"polyline": points}}, [identifier])
        result = self.build()
        groups = [row["route_context"]["route_group_id"] for row in result["runs"]]
        self.assertEqual(groups[0], groups[1])
        self.assertIsNone(groups[2])

    def test_route_descriptor_fallback_uses_named_coordinate_indexes(self):
        self.activities = [{"activityId": identifier, "activityType": {"typeKey": "running"}} for identifier in (101, 102)]
        descriptors = [{"key": "directLatitude", "metricsIndex": 3, "unit": {"key": "dd"}}, {"key": "directLongitude", "metricsIndex": 1, "unit": {"key": "dd"}}]
        rows = [{"metrics": [140, -73.123456789, 5, 40.123456789 + index * .001]} for index in range(21)]
        self.add_call("get_activity_details", {"metricDescriptors": descriptors, "activityDetailMetrics": rows}, [101])
        self.add_call("get_activity_details", {"metricDescriptors": [{**descriptor, "metricsIndex": 3 - descriptor["metricsIndex"]} for descriptor in descriptors], "activityDetailMetrics": [{"metrics": list(reversed(row["metrics"]))} for row in rows]}, [102])
        result = self.build()
        self.assertEqual(result["runs"][0]["route_context"]["route_group_id"], result["runs"][1]["route_context"]["route_group_id"])
        self.assertIsNotNone(result["runs"][0]["route_context"]["route_group_id"])
        self.assertNotIn("40.123456789", json.dumps(result))
        self.assertNotIn("-73.123456789", json.dumps(result))

    def test_activity_hr_sensor_categories_survive_without_serial_identifiers(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"}}]
        self.add_call("get_activity", {"metadataDTO": {"sensors": [
            {"manufacturer": "POLAR_ELECTRO", "sourceType": "ANTPLUS", "antplusDeviceType": "HEART_RATE", "serialNumber": "PRIVATE_SERIAL", "displayName": "PRIVATE_SENSOR_NAME"},
            {"sourceType": "ANTPLUS", "antplusDeviceType": "RUN"},
        ]}}, [101])
        result = self.build()
        values = [row["values"] for row in result["runs"][0]["sensor_metadata"]]
        self.assertTrue(any(row.get("antplusDeviceType") == "HEART_RATE" and row.get("sourceType") == "ANTPLUS" for row in values))
        self.assertEqual(result["coverage"]["runs_with_explicit_hr_sensor_metadata"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_weather_keeps_observation_timing_without_guessing_units_from_values(self):
        self.activities = [{"activityId": 101, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-02 06:30:00", "startTimeGMT": "2026-09-02 10:30:00"}]
        self.add_call("get_activity_weather", {"issueDate": "2026-09-02T10:51:00+00:00", "temp": 63, "windSpeed": 13, "latitude": 40.123456789, "weatherStationDTO": {"id": "PRIVATE_STATION"}}, [101])
        weather = self.build()["runs"][0]["weather"]
        self.assertEqual(weather["values"]["temp"], 63)
        self.assertEqual(weather["observation_offset_from_run_start_minutes"], 21)
        self.assertTrue(weather["unit_verification"].startswith("unverified:"))
        self.assertNotIn("temperature_c", weather)
        self.assertNotIn("PRIVATE_STATION", json.dumps(weather))
        self.assertNotIn("40.123456789", json.dumps(weather))

    def test_sleep_coverage_counts_usable_dates_across_range_and_fallback(self):
        self.add_call("get_sleep_daily", [
            {"calendarDate": "2026-09-01", "values": {"totalSleepTimeInSeconds": 28_800}},
            {"calendarDate": "2026-09-02", "values": {"totalSleepTimeInSeconds": None}},
            {"calendarDate": "2026-09-03", "values": {"totalSleepTimeInSeconds": 0}},
        ], ["2026-09-01", "2026-09-27"])
        self.add_call("get_sleep_data", {"dailySleepDTO": {"calendarDate": "2026-09-02", "sleepTimeSeconds": 25_200, "sleepEndTimestampGMT": 1788325200000}}, ["2026-09-02"])
        self.add_call("get_sleep_data", None, ["2026-09-04"], status="empty")
        self.add_call("get_sleep_data", {"dailySleepDTO": {"calendarDate": "2026-09-03", "sleepTimeSeconds": None, "sleepEndTimestampGMT": None}}, ["2026-09-03"])
        result = self.build()
        coverage = result["coverage"]["sleep"]
        self.assertEqual(coverage["expected_date_count"], 27)
        self.assertEqual(coverage["dates_with_records_count"], 3)
        self.assertEqual(coverage["dates_with_positive_total_sleep_count"], 2)
        self.assertEqual(coverage["record_dates_without_positive_total_sleep"], ["2026-09-03"])
        self.assertEqual(len(coverage["missing_usable_dates"]), 25)
        self.assertNotIn("2026-09-02", coverage["missing_usable_dates"])
        self.assertEqual(coverage["fallback_call_status_counts"], {"ok": 2, "empty": 1})
        self.assertEqual(result["resources"][-1]["normalization"], "curated_daily_no_positive_sleep")
        self.assertEqual(result["resources"][-1]["positive_total_sleep_record_count"], 0)
        self.assertEqual(result["daily_units"]["sleep"]["sleepEndTimestampGMT"], "UTC timestamp milliseconds")


if __name__ == "__main__":
    unittest.main()
