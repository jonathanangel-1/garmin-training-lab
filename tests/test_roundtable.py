"""Offline tests for the 9+9+1 review and its publication/validation gates."""

import stat
from copy import deepcopy
from datetime import date
from threading import Lock

import jsonschema
import pytest
from pydantic import ValidationError

from garmin_training import roundtable as rt
from garmin_training.core import read_json, write_json
from garmin_training.goals import Goal, parse_finish_time
from garmin_training.protocol import FINAL_SCHEMA, REVIEW_SCHEMA, ROLES, SPECIALIST_SCHEMA

CUTOFF = date(2026, 9, 27)


def example_goal(**updates):
    return {"description": "Assess readiness for a synthetic marathon", "race_date": "2026-11-01", "distance_km": 42.195,
            "max_stressors_per_week": 2, "constraints": ["No more than two stressors each week."], **updates}


def example_final():
    kinds = ["easy", "rest", "steady", "easy", "rest", "easy", "long"]
    distances = [8, 0, 8, 5, 0, 5, 16]
    sessions = [{"date": date(2026, 9, 28 + index).isoformat() if index < 3 else date(2026, 10, index - 2).isoformat(),
                 "kind": kind, "description": "Synthetic example only", "distance_km": distances[index],
                 "duration_minutes": None, "is_stressor": kind in {"steady", "long"},
                 "adjustment_trigger": "Skip if the athlete reports renewed symptoms.", "evidence": ["synthetic-activity-1"]}
                for index, kind in enumerate(kinds)]
    return {
        "summary": "Synthetic assessment for offline testing.", "confidence": "low", "goal_assessment": "insufficient_data",
        "current_status": {"fitness": "Unknown", "fatigue": "Unknown", "durability": "Unknown", "data_quality": "Synthetic fixture"},
        "key_findings": [{"claim": "Synthetic data cannot establish race fitness.", "kind": "unknown", "confidence": "high",
                          "evidence": ["2026-09-01 through 2026-09-27"], "alternative_explanations": [], "implication": "Collect real evidence."}],
        "finish_time_estimates": [{"method": "Race extrapolation", "eligible": False, "low_seconds": None, "high_seconds": None,
                                   "assumptions": [], "reason": "No actual race result supplied.", "evidence": []}],
        "weekly_plan": [{"week_start": "2026-09-28", "focus": "Conditional illustrative week", "distance_km": 42, "sessions": sessions}],
        "small_tweaks": ["Record perceived effort."], "constraints_checked": ["Maximum two stressors."],
        "unresolved_disagreements": [], "missing_information": ["Actual athlete records."],
    }


def stage_answer(stage, role):
    if stage == "independent":
        return {"role": role, "summary": f"Synthetic {role} review", "findings": [], "missing_data": ["Real athlete records"],
                "questions_for_other_roles": [], "suggested_changes": []}
    if stage == "roundtable":
        return {"role": role, "challenges": [], "revised_findings": [], "unresolved_questions": ["No real athlete data"],
                "recommendation": "Do not interpret synthetic fixtures as live proof."}
    return example_final()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    evidence_path = tmp_path / "evidence.json"
    write_json(evidence_path, {"synthetic": True, "coverage": {"boundaries": {"start": "2026-09-01", "end": "2026-09-27"}},
                               "activities": [{"id": "synthetic-activity-1"}]})
    run_dir = tmp_path / "run"
    calls = []
    call_lock = Lock()
    behavior = {"failure": None, "final": None, "wrong_role": None, "revision": 0}
    monkeypatch.setattr(rt, "check_codex", lambda: "offline-mocked-codex")

    def fake_invoke(prompt, schema, output, workspace, model=None, timeout=900):
        stage, role = output.parent.name, output.stem
        with call_lock:
            calls.append((stage, role, model))
        if behavior["failure"] == (stage, role):
            raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_ERROR")
        assert read_json(workspace / "evidence.json")["synthetic"] is True
        assert "evidence" in prompt.lower()
        if stage == "roundtable":
            assert set(read_json(workspace / "independent.json")) == set(ROLES)
        if stage == "synthesis":
            assert set(read_json(workspace / "roundtable.json")) == set(ROLES)
        result = deepcopy(behavior["final"]) if stage == "synthesis" and behavior["final"] is not None else stage_answer(stage, role)
        if stage == "independent":
            result["summary"] += f"; fixture revision {behavior['revision']}"
        if behavior["wrong_role"] == (stage, role):
            result["role"] = "incorrect_role"
        jsonschema.validate(result, schema)
        write_json(output, result)
        return result

    monkeypatch.setattr(rt, "invoke_codex", fake_invoke)
    return evidence_path, run_dir, calls, behavior


def test_full_nine_plus_nine_plus_lead_and_validated_resume(offline):
    evidence, run, calls, _ = offline
    result = rt.run_analysis(evidence, example_goal(), run, concurrency=3)
    assert len(calls) == 19
    assert {role for stage, role, _ in calls if stage == "independent"} == set(ROLES)
    assert {role for stage, role, _ in calls if stage == "roundtable"} == set(ROLES)
    assert [role for stage, role, _ in calls if stage == "synthesis"] == ["lead"]
    assert read_json(run / "run.json")["status"] == "complete"
    assert read_json(run / "run.json")["completed_calls"] == 19
    assert read_json(run / "final.json") == result
    assert "Synthetic assessment" in (run / "report.md").read_text()
    assert stat.S_IMODE((run / "final.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((run / "report.md").stat().st_mode) == 0o600
    assert rt.run_analysis(evidence, example_goal(), run) == result
    assert len(calls) == 19


def test_failed_specialist_prevents_publication_and_can_resume(offline):
    evidence, run, calls, behavior = offline
    behavior["failure"] = ("independent", "training_history")
    with pytest.raises(RuntimeError, match="SYNTHETIC_PRIVATE_PROVIDER_ERROR"):
        rt.run_analysis(evidence, example_goal(), run)
    state = read_json(run / "run.json")
    assert state["status"] == "failed"
    assert "PRIVATE_PROVIDER_ERROR" not in str(state)
    assert not (run / "final.json").exists()
    assert not (run / "report.md").exists()
    successful_roles = {path.stem for path in (run / "independent").glob("*.json")}
    assert successful_roles
    call_count = len(calls)
    behavior["failure"] = None
    rt.run_analysis(evidence, example_goal(), run)
    resumed_calls = calls[call_count:]
    assert not any(stage == "independent" and role in successful_roles for stage, role, _ in resumed_calls)
    assert read_json(run / "run.json")["status"] == "complete"


def test_wrong_role_from_provider_is_rejected(offline):
    evidence, run, _, behavior = offline
    behavior["wrong_role"] = ("independent", "training_history")
    with pytest.raises(ValueError, match="role"):
        rt.run_analysis(evidence, example_goal(), run)
    assert not (run / "final.json").exists()


def test_wrong_role_in_cache_is_not_silently_accepted(offline):
    evidence, run, calls, _ = offline
    rt.run_analysis(evidence, example_goal(), run)
    cached = run / "independent" / "training_history.json"
    value = read_json(cached)
    value["role"] = "workout_execution"
    write_json(cached, value)
    before = len(calls)
    try:
        rt.run_analysis(evidence, example_goal(), run)
    except ValueError:
        assert read_json(run / "run.json")["status"] == "failed"
    else:
        assert ("independent", "training_history", None) in calls[before:]
        assert read_json(cached)["role"] == "training_history"


@pytest.mark.parametrize("change", ["evidence", "evidence_markdown", "goal", "model", "protocol_schema"])
def test_changed_inputs_cannot_silently_reuse_cached_analysis(offline, monkeypatch, change):
    evidence, run, calls, _ = offline
    rt.run_analysis(evidence, example_goal(), run)
    goal, model = example_goal(), None
    if change == "evidence":
        value = read_json(evidence)
        value["new_observation"] = "Synthetic updated evidence"
        write_json(evidence, value)
    elif change == "evidence_markdown":
        evidence.with_suffix(".md").write_text("Synthetic changed summary supplied to analysts.\n")
    elif change == "goal":
        goal["notes"] = "New explicit athlete constraint"
    elif change == "model":
        model = "synthetic-alternate-model"
    else:
        schema = deepcopy(rt.SPECIALIST_SCHEMA)
        schema["properties"]["role"]["minLength"] = 1
        monkeypatch.setattr(rt, "SPECIALIST_SCHEMA", schema)
    before = len(calls)
    with pytest.raises(ValueError, match="inputs changed"):
        rt.run_analysis(evidence, goal, run, model=model)
    assert len(calls) == before


def test_regenerated_upstream_answer_invalidates_downstream_caches(offline):
    evidence, run, calls, behavior = offline
    rt.run_analysis(evidence, example_goal(), run)
    write_json(run / "independent" / "training_history.json", {"invalid": "cached answer"})
    behavior["revision"] = 1
    before = len(calls)
    rt.run_analysis(evidence, example_goal(), run)
    new_calls = calls[before:]
    assert [(stage, role) for stage, role, _ in new_calls if stage == "independent"] == [("independent", "training_history")]
    assert {role for stage, role, _ in new_calls if stage == "roundtable"} == set(ROLES)
    assert [role for stage, role, _ in new_calls if stage == "synthesis"] == ["lead"]
    assert len(new_calls) == 11


def test_invalid_final_does_not_publish(offline):
    evidence, run, _, behavior = offline
    invalid = example_final()
    invalid["weekly_plan"][0]["sessions"][0]["date"] = CUTOFF.isoformat()
    behavior["final"] = invalid
    with pytest.raises(ValueError, match="future planning window"):
        rt.run_analysis(evidence, example_goal(), run)
    assert read_json(run / "run.json")["status"] == "failed"
    assert not (run / "final.json").exists()
    assert not (run / "report.md").exists()


def test_semantically_invalid_synthesis_resume_reruns_only_lead(offline):
    evidence, run, calls, behavior = offline
    invalid = example_final()
    invalid["weekly_plan"][0]["sessions"][0]["date"] = CUTOFF.isoformat()
    behavior["final"] = invalid
    with pytest.raises(ValueError, match="future planning window"):
        rt.run_analysis(evidence, example_goal(), run)
    assert len(calls) == 19
    assert (run / "synthesis" / "lead.json").is_file()
    assert not (run / "synthesis" / "lead.cache.json").exists()
    state = read_json(run / "run.json")
    assert state["stages"]["synthesis"]["lead"] == "invalid"
    assert state["completed_calls"] == 18
    assert not (run / "final.json").exists()

    before = len(calls)
    behavior["final"] = None
    result = rt.run_analysis(evidence, example_goal(), run)
    assert calls[before:] == [("synthesis", "lead", None)]
    assert read_json(run / "run.json")["status"] == "complete"
    assert read_json(run / "run.json")["completed_calls"] == 19
    assert (run / "synthesis" / "lead.cache.json").is_file()
    assert read_json(run / "final.json") == result


def test_failed_rerun_does_not_leave_stale_published_final(offline):
    evidence, run, _, behavior = offline
    rt.run_analysis(evidence, example_goal(), run)
    (run / "synthesis" / "lead.json").unlink()
    invalid = example_final()
    invalid["weekly_plan"][0]["sessions"][0]["date"] = CUTOFF.isoformat()
    behavior["final"] = invalid
    with pytest.raises(ValueError):
        rt.run_analysis(evidence, example_goal(), run)
    assert read_json(run / "run.json")["status"] == "failed"
    assert not (run / "final.json").exists()
    assert not (run / "report.md").exists()


def test_report_failure_does_not_leave_a_published_final(offline, monkeypatch):
    evidence, run, _, _ = offline

    def fail_report(_result):
        raise ValueError("Synthetic report rendering failure")

    monkeypatch.setattr(rt, "render_report", fail_report)
    with pytest.raises(ValueError, match="report rendering"):
        rt.run_analysis(evidence, example_goal(), run)
    assert read_json(run / "run.json")["status"] == "failed"
    assert not (run / "final.json").exists()
    assert not (run / "report.md").exists()


def test_missing_cutoff_prevents_publication(offline):
    evidence, run, calls, _ = offline
    write_json(evidence, {"synthetic": True})
    with pytest.raises(ValueError, match="cutoff"):
        rt.run_analysis(evidence, example_goal(), run)
    assert not (run / "final.json").exists()
    assert calls == []


@pytest.mark.parametrize("mutation", ["non_monday", "outside_week", "past", "after_race", "duplicate", "negative_distance", "negative_duration", "rest_stressor", "too_many_stressors", "too_many_tweaks"])
def test_final_validates_schedule_and_explicit_limits(mutation):
    result, goal = example_final(), example_goal()
    week, sessions = result["weekly_plan"][0], result["weekly_plan"][0]["sessions"]
    if mutation == "non_monday":
        week["week_start"] = "2026-09-29"
    elif mutation == "outside_week":
        sessions[0]["date"] = "2026-10-05"
    elif mutation == "past":
        sessions[0]["date"] = "2026-09-27"
    elif mutation == "after_race":
        sessions[0]["date"] = "2026-11-02"
    elif mutation == "duplicate":
        sessions.append(deepcopy(sessions[0]))
    elif mutation == "negative_distance":
        sessions[0]["distance_km"] = -1
    elif mutation == "negative_duration":
        sessions[0]["duration_minutes"] = -1
    elif mutation == "rest_stressor":
        sessions[1]["is_stressor"] = True
    elif mutation == "too_many_stressors":
        goal["max_stressors_per_week"] = 1
    else:
        result["small_tweaks"] = ["One tweak"] * 8
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        rt.validate_final(result, goal, CUTOFF)


@pytest.mark.parametrize("kind", ["long", "race"])
def test_long_runs_and_races_cannot_bypass_stressor_limit(kind):
    result, goal = example_final(), example_goal(max_stressors_per_week=0)
    for session in result["weekly_plan"][0]["sessions"]:
        session["is_stressor"] = False
    result["weekly_plan"][0]["sessions"][-1]["kind"] = kind
    with pytest.raises(ValueError):
        rt.validate_final(result, goal, CUTOFF)


@pytest.mark.parametrize("race_date,session_date", [(None, "2026-10-26"), ("2027-11-01", "2026-12-21")])
def test_planning_horizon_is_bounded(race_date, session_date):
    result = example_final()
    session = result["weekly_plan"][0]["sessions"][0]
    session["date"] = session_date
    result["weekly_plan"] = [{"week_start": session_date, "focus": "Synthetic overly distant plan", "distance_km": 8, "sessions": [session]}]
    with pytest.raises(ValueError):
        rt.validate_final(result, example_goal(race_date=race_date), CUTOFF)


@pytest.mark.parametrize("eligible,low,high", [(True, None, None), (True, 0, 100), (True, 200, 100), (False, 100, 200)])
def test_prediction_ranges_are_honest(eligible, low, high):
    result = example_final()
    result["finish_time_estimates"][0].update(eligible=eligible, low_seconds=low, high_seconds=high)
    with pytest.raises(ValueError):
        rt.validate_final(result, example_goal(), CUTOFF)


def test_valid_range_and_strict_output_schemas():
    result = example_final()
    result["finish_time_estimates"][0].update(eligible=True, low_seconds=12600, high_seconds=13200)
    rt.validate_final(result, example_goal(), CUTOFF)
    for schema, payload in [(SPECIALIST_SCHEMA, stage_answer("independent", "training_history")),
                            (REVIEW_SCHEMA, stage_answer("roundtable", "training_history")), (FINAL_SCHEMA, result)]:
        payload["unexpected_private_field"] = "must not be accepted"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(payload, schema)


@pytest.mark.parametrize("changes", [{"distance_km": 0}, {"target_time_seconds": 0}, {"distance_km": None, "target_time_seconds": 12000},
                                      {"max_stressors_per_week": 8}, {"max_stressors_per_week": -1}, {"race_date": "not-a-date"},
                                      {"constraints": ["x" * 2001]}, {"unknown_key": "value"}])
def test_goal_validation_rejects_invalid_inputs(changes):
    with pytest.raises(ValidationError):
        Goal.model_validate(example_goal(**changes))


@pytest.mark.parametrize("value", ["3:30", "03:60:00", "03:30:60", "00:00:00", "-1:30:00", "garbage"])
def test_finish_time_parser_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_finish_time(value)


def test_finish_time_parser_and_codex_environment(monkeypatch):
    assert parse_finish_time("03:30:00") == 12600
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMINTOKENS"):
        monkeypatch.setenv(key, "SYNTHETIC_SECRET")
    env = rt._codex_environment()
    assert "SYNTHETIC_SECRET" not in env.values()


@pytest.mark.parametrize("concurrency", [0, 4])
def test_concurrency_is_bounded(offline, concurrency):
    evidence, run, calls, _ = offline
    with pytest.raises(ValueError):
        rt.run_analysis(evidence, example_goal(), run, concurrency=concurrency)
    assert calls == []
