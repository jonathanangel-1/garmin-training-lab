"""Offline tests: local-date inclusion, bounded reads, resume, and interruptions."""

import hashlib
import json
import stat
from datetime import date, timedelta

import pytest

from garmin_training import collector


class FakeGarmin:
    def __init__(self, pages=None, responses=None):
        self.pages = pages or {}
        self.responses = responses or {}
        self.calls = []

    def get_activities(self, offset, limit):
        self.calls.append(("get_activities", (offset, limit), {}))
        result = self.pages.get(offset, [])
        if isinstance(result, Exception):
            raise result
        return result

    def __getattr__(self, name):
        if name not in collector.RANGE_METHODS + collector.DAILY_METHODS + collector.DETAIL_METHODS + collector.CALIBRATION_METHODS + collector.FALLBACK_METHODS:
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "get_sleep_daily" and name not in self.responses:
                first, last = map(date.fromisoformat, args)
                return [{"calendarDate": (first + timedelta(days=index)).isoformat(), "values": {"totalSleepTimeInSeconds": 28_800}} for index in range((last - first).days + 1)]
            result = self.responses.get(name, [])
            if isinstance(result, Exception):
                raise result
            return result

        return call


def activity(identifier, local, kind="running", **fields):
    return {"activityId": identifier, "startTimeLocal": local, "activityType": {"typeKey": kind}, **fields}


def collect(fake, folder, start=date(2026, 6, 1), end=date(2026, 6, 2), **kwargs):
    kwargs.setdefault("request_pause", 0)
    return collector.collect_snapshot(fake, folder, start, end, **kwargs)


def test_local_dates_all_types_pagination_dedup_and_missing_fields(tmp_path):
    run = activity(2, "2026-06-01 23:50:00", distance=1234, startTimeGMT="2026-06-02 03:50:00")
    walk = activity(3, "2026-06-02 06:30:00", "walking")
    missing_date = {"activityId": 5, "distance": None}
    fake = FakeGarmin(pages={
        0: [activity(4, "2026-06-03 00:05:00"), walk, run, missing_date],
        100: [run, activity(1, "2026-05-31 22:00:00", startTimeGMT="2026-06-01 02:00:00")],
    })
    manifest = collect(fake, tmp_path, max_details=1)
    saved = json.loads((tmp_path / "activities.json").read_text())
    assert saved == [run, walk]
    assert "averageHR" not in saved[0]
    assert manifest["counts"]["unclassified_local_dates"] == 1
    assert manifest["status"] == "partial"
    assert manifest["activity_history_boundary_reached"] is True
    assert manifest["selected_activity_ids"] == [2]
    assert [args[0] for name, args, _ in fake.calls if name == "get_activities"] == [0, 100, 200]
    assert json.loads((tmp_path / "activities_unclassified.json").read_text()) == [missing_date]


def test_windows_daily_bound_selection_and_stream_sampling_are_explicit(tmp_path):
    start, end = date(2026, 1, 1), date(2026, 3, 3)
    runs = [activity(i, f"2026-03-0{i} 07:00:00", "trail_running") for i in (3, 2, 1)]
    fake = FakeGarmin(pages={0: runs})
    manifest = collect(fake, tmp_path, start, end, max_details=2)
    for method in collector.RANGE_METHODS:
        bounds = [args for name, args, _ in fake.calls if name == method]
        assert bounds == [("2026-01-01", "2026-01-28"), ("2026-01-29", "2026-02-25"), ("2026-02-26", "2026-03-03")]
        assert all((date.fromisoformat(b) - date.fromisoformat(a)).days < 28 for a, b in bounds)
    daily_dates = [args[0] for name, args, _ in fake.calls if name == "get_user_summary"]
    assert len(daily_dates) == 28
    assert daily_dates[0] == (end - timedelta(days=27)).isoformat()
    assert daily_dates[-1] == end.isoformat()
    details = [(args, kwargs) for name, args, kwargs in fake.calls if name == "get_activity_details"]
    assert details == [(("3",), {"maxchart": 20_000, "maxpoly": 20_000}), (("2",), {"maxchart": 20_000, "maxpoly": 20_000})]
    assert manifest["detail_selection_truncated"] is True
    assert manifest["sampling"]["potentially_sampled"] is True
    assert manifest["status"] == "complete"


def test_successful_and_empty_raw_calls_resume_without_network_and_verify_hashes(tmp_path):
    fake = FakeGarmin(responses={"get_daily_steps": [{"calendarDate": "2026-06-01", "totalSteps": None}]})
    first = collect(fake, tmp_path, max_details=0)
    assert first["status"] == "complete"
    assert first["counts"]["empty_calls"] > 0
    second_client = FakeGarmin()
    second = collect(second_client, tmp_path, max_details=0)
    assert second_client.calls == []
    assert second["counts"]["cached_calls"] == len(first["calls"])
    assert second["counts"]["network_calls"] == 0
    for call in second["calls"]:
        content = (tmp_path / call["raw_path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == call["sha256"]
        assert call["retrieved_at"] == next(row for row in first["calls"] if row["key"] == call["key"])["retrieved_at"]
    steps = next(row for row in second["calls"] if row["method"] == "get_daily_steps")
    (tmp_path / steps["raw_path"]).write_text("[]")
    third_client = FakeGarmin()
    third = collect(third_client, tmp_path, max_details=0)
    assert [name for name, _, _ in third_client.calls] == ["get_daily_steps"]
    assert third["counts"]["network_calls"] == 1


@pytest.mark.parametrize("exception", [
    type("GarminConnectTooManyRequestsError", (Exception,), {})("secret: do not persist"),
    type("GarminConnectAuthenticationError", (Exception,), {})("password: do not persist"),
    RuntimeError("API Error 429 - private response"),
    RuntimeError("HTTP 401 private response"),
])
def test_auth_or_rate_limit_stops_without_more_calls_or_sensitive_errors(tmp_path, exception):
    fake = FakeGarmin(responses={"get_sleep_daily": exception})
    manifest = collect(fake, tmp_path)
    assert manifest["status"] == "interrupted"
    assert [name for name, _, _ in fake.calls] == ["get_activities", "get_daily_steps", "get_sleep_daily"]
    failure = manifest["calls"][-1]
    assert failure["status"] == "interrupted"
    assert failure["raw_path"] is None
    rendered = (tmp_path / "manifest.json").read_text()
    assert "private response" not in rendered
    assert "do not persist" not in rendered
    resumed_client = FakeGarmin()
    resumed = collect(resumed_client, tmp_path)
    assert resumed["status"] == "complete"
    assert resumed["counts"]["cached_calls"] == 2
    assert resumed_client.calls[0][0] == "get_sleep_daily"


def test_unavailable_and_failed_calls_are_distinct_from_empty(tmp_path):
    fake = FakeGarmin(responses={"get_hydration_data": NotImplementedError(), "get_nutrition_daily_meals": RuntimeError("API Error 503 private diagnostic")})
    manifest = collect(fake, tmp_path)
    assert manifest["status"] == "partial"
    hydration = [row for row in manifest["calls"] if row["method"] == "get_hydration_data"]
    assert [row["status"] for row in hydration] == ["unavailable", "unavailable"]
    assert [row["attempted"] for row in hydration] == [True, False]
    failed = [row for row in manifest["calls"] if row["method"] == "get_nutrition_daily_meals"]
    assert all(row["status"] == "error" and row["raw_path"] is None for row in failed)
    assert all(row["error"]["http_status"] == 503 for row in failed)


def test_resume_interruption_preserves_successful_later_calls(tmp_path):
    first = collect(FakeGarmin(responses={"get_sleep_daily": RuntimeError("HTTP 503")}), tmp_path)
    assert first["status"] == "partial"
    interrupted = collect(FakeGarmin(responses={"get_sleep_daily": RuntimeError("HTTP 401")}), tmp_path)
    assert interrupted["status"] == "interrupted"
    assert len(interrupted["calls"]) == 3
    final_client = FakeGarmin()
    final = collect(final_client, tmp_path)
    assert final["status"] == "complete"
    assert [name for name, _, _ in final_client.calls] == ["get_sleep_daily"]
    # Repaired range summaries make the two earlier fallback reads unnecessary,
    # but their successful cache entries must survive the interrupted resume.
    fallback_count = sum(row["method"] == "get_sleep_data" for row in first["calls"])
    assert final["counts"]["cached_calls"] == first["counts"]["successful_calls"] - fallback_count
    assert {row["key"] for row in first["calls"] if row["status"] in {"ok", "empty"}} <= {row["key"] for row in final["resume_cache"]}


def test_missing_client_capabilities_are_explicit(tmp_path):
    class MinimalClient:
        def get_activities(self, *_):
            return []

    manifest = collect(MinimalClient(), tmp_path)
    assert manifest["status"] == "partial"
    assert manifest["counts"]["network_calls"] == 1
    assert manifest["counts"]["unavailable_calls"] > 0
    assert all(row["error"]["reason"] == "client_method_unavailable" for row in manifest["calls"][1:])


def test_private_atomic_files_and_exact_payload_preservation(tmp_path):
    payload = {"calendarDate": "2026-06-01", "newFirmwareField": {"unknown": [None, 1, "é"]}}
    folder = tmp_path / "snapshot"
    fake = FakeGarmin(responses={"get_user_summary": payload})
    manifest = collect(fake, folder)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "raw").stat().st_mode) == 0o700
    for file in folder.rglob("*.json"):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    row = next(row for row in manifest["calls"] if row["method"] == "get_user_summary")
    assert json.loads((folder / row["raw_path"]).read_text()) == payload
    assert not list(folder.rglob(".collect-*"))
    assert json.loads((folder / "manifest.json").read_text()) == manifest


def test_repeated_activity_pages_cannot_loop_or_claim_complete_history(tmp_path):
    page = [activity(1, "2026-06-01 07:00:00")]
    fake = FakeGarmin(pages={0: page, 100: page})
    manifest = collect(fake, tmp_path, max_details=0)
    assert manifest["status"] == "partial"
    assert manifest["activity_history_boundary_reached"] is False
    assert {issue["reason"] for issue in manifest["completeness"]} == {"repeated_activity_page"}
    assert manifest["counts"]["activities"] == 1


@pytest.mark.parametrize("options", [
    {"start": date(2026, 6, 3)},
    {"detail_start": date(2026, 6, 3)},
    {"max_details": -1},
    {"max_details": 1001},
    {"request_pause": float("nan")},
])
def test_invalid_inputs_fail_before_files_or_network(tmp_path, options):
    fake = FakeGarmin()
    folder = tmp_path / "should-not-exist"
    with pytest.raises(ValueError):
        collect(fake, folder, **options)
    assert fake.calls == []
    assert not folder.exists()


def test_calibration_calls_bound_history_and_device_settings_and_resume(tmp_path):
    devices = [{"deviceId": value} for value in (101, 101, "202", True, None, "../private", 303, 404)]
    fake = FakeGarmin(responses={"get_devices": devices})
    first = collect(fake, tmp_path, start=date(2026, 3, 1), end=date(2026, 6, 2), max_details=0)

    threshold = [(args, kwargs) for method, args, kwargs in fake.calls if method == "get_lactate_threshold"]
    assert threshold == [
        ((), {"latest": True}),
        ((), {"latest": False, "start_date": "2026-03-01", "end_date": "2026-06-02", "aggregation": "daily"}),
    ]
    device_reads = [args for method, args, _ in fake.calls if method == "get_device_settings"]
    assert device_reads == [("101",), ("202",), ("303",)]
    assert first["calibration_policy"]["device_settings_truncated"] is True
    assert first["calibration_policy"]["eligible_device_settings"] == 4
    assert all(method.startswith("get_") for method, _, _ in fake.calls)

    resumed_client = FakeGarmin()
    resumed = collect(resumed_client, tmp_path, start=date(2026, 3, 1), end=date(2026, 6, 2), max_details=0)
    assert resumed_client.calls == []
    assert resumed["counts"]["cached_calls"] == len(first["calls"])


def test_adding_calibration_to_old_snapshot_fetches_only_new_resources(tmp_path):
    first = collect(FakeGarmin(), tmp_path, max_details=0)
    old_calls = [row for row in first["calls"] if row["method"] not in collector.CALIBRATION_METHODS]
    first["calls"] = old_calls
    first["resume_cache"] = old_calls
    (tmp_path / "manifest.json").write_text(json.dumps(first))
    updated_client = FakeGarmin()

    updated = collect(updated_client, tmp_path, max_details=0)

    assert updated["counts"]["cached_calls"] == len(old_calls)
    assert [method for method, _, _ in updated_client.calls] == [
        "get_lactate_threshold", "get_lactate_threshold", *collector.CALIBRATION_CURRENT_METHODS,
    ]


def test_calibration_missing_or_failed_methods_are_recorded_without_aborting_other_reads(tmp_path):
    fake = FakeGarmin(responses={
        "get_lactate_threshold": NotImplementedError(),
        "get_heart_rate_zones": RuntimeError("HTTP 503 private response"),
        "get_devices": [{"deviceId": 123}],
    })
    manifest = collect(fake, tmp_path, max_details=0)
    assert manifest["status"] == "partial"
    lt_rows = [row for row in manifest["calls"] if row["method"] == "get_lactate_threshold"]
    assert [row["status"] for row in lt_rows] == ["unavailable", "unavailable"]
    assert [row["attempted"] for row in lt_rows] == [True, False]
    assert any(method == "get_device_settings" for method, _, _ in fake.calls)
    assert "private response" not in (tmp_path / "manifest.json").read_text()


def test_explicit_primary_training_device_is_prioritized_over_old_registered_devices(tmp_path):
    fake = FakeGarmin(responses={
        "get_devices": [{"deviceId": value} for value in (101, 202, 303, 404)],
        "get_primary_training_device": {"PrimaryTrainingDevice": {"deviceId": 404}},
    })
    manifest = collect(fake, tmp_path, max_details=0)
    settings = [args for method, args, _ in fake.calls if method == "get_device_settings"]
    assert settings == [("404",), ("101",), ("202",)]
    assert manifest["calibration_policy"]["explicit_primary_device_prioritized"] is True
    assert len(settings) == collector.MAX_DEVICE_SETTINGS


def test_missing_sleep_fallback_recovers_only_requested_dates_and_resumes(tmp_path):
    class SleepClient(FakeGarmin):
        def get_sleep_data(self, day):
            self.calls.append(("get_sleep_data", (day,), {}))
            return {"dailySleepDTO": {"calendarDate": day, "sleepTimeSeconds": 25_200}}

    client = SleepClient(responses={"get_sleep_daily": [
        {"calendarDate": "2026-06-01", "values": {"totalSleepTimeInSeconds": 28_800}},
        {"calendarDate": "2026-06-02", "values": {"totalSleepTimeInSeconds": None}},
        {"calendarDate": "2026-06-03", "values": {"totalSleepTimeInSeconds": 0}},
        {"calendarDate": "2026-05-30", "values": {"totalSleepTimeInSeconds": 28_800}},
    ]})
    first = collect(client, tmp_path, end=date(2026, 6, 4), max_details=0)
    assert [args[0] for method, args, _ in client.calls if method == "get_sleep_data"] == ["2026-06-04", "2026-06-03", "2026-06-02"]
    policy = first["sleep_fallback_policy"]
    assert policy["range_usable_date_count"] == 1
    assert policy["recovered_dates"] == ["2026-06-02", "2026-06-03", "2026-06-04"]
    assert policy["missing_dates_after_fallback"] == []
    resumed_client = SleepClient()
    resumed = collect(resumed_client, tmp_path, end=date(2026, 6, 4), max_details=0)
    assert resumed_client.calls == []
    assert resumed["sleep_fallback_policy"] == policy
    raw = next(row for row in resumed["calls"] if row["method"] == "get_sleep_data")
    (tmp_path / raw["raw_path"]).write_text("{}")
    refreshed_client = SleepClient()
    collect(refreshed_client, tmp_path, end=date(2026, 6, 4), max_details=0)
    assert refreshed_client.calls == [("get_sleep_data", tuple(raw["args"]), {})]


def test_sleep_fallback_ceiling_is_newest_first_and_reports_remaining_dates(tmp_path):
    start, end = date(2026, 1, 1), date(2026, 3, 3)
    client = FakeGarmin(responses={"get_sleep_daily": []})
    manifest = collect(client, tmp_path, start=start, end=end, max_details=0)
    reads = [args[0] for method, args, _ in client.calls if method == "get_sleep_data"]
    assert len(reads) == collector.MAX_SLEEP_FALLBACK_DAYS == 60
    assert reads[0] == end.isoformat()
    assert reads[-1] == (end - timedelta(days=59)).isoformat()
    policy = manifest["sleep_fallback_policy"]
    assert policy["truncated"] is True
    assert len(policy["missing_dates_after_fallback"]) == 62
    assert manifest["status"] == "partial"


@pytest.mark.parametrize("response,expected_status", [
    ({"dailySleepDTO": {"calendarDate": "2026-05-31", "sleepTimeSeconds": 28_800}}, "complete"),
    (RuntimeError("HTTP 503 private sleep response"), "partial"),
    (NotImplementedError(), "partial"),
    (RuntimeError("HTTP 429 private sleep response"), "interrupted"),
])
def test_sleep_fallback_does_not_turn_wrong_dates_or_failures_into_recovery(tmp_path, response, expected_status):
    client = FakeGarmin(responses={"get_sleep_daily": [], "get_sleep_data": response})
    manifest = collect(client, tmp_path, max_details=0)
    assert manifest["status"] == expected_status
    assert manifest["sleep_fallback_policy"]["recovered_dates"] == []
    assert manifest["sleep_fallback_policy"]["missing_dates_after_fallback"] == ["2026-06-01", "2026-06-02"]
    assert "private sleep response" not in (tmp_path / "manifest.json").read_text()
    if expected_status == "interrupted":
        assert sum(method == "get_sleep_data" for method, _, _ in client.calls) == 1
