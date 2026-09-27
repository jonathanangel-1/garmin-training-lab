"""Offline tests for the capacity-first review and its publication/validation gates."""

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
from garmin_training.protocol import (
    CAPACITY_SCHEMA,
    FINAL_AUDIT_SCHEMA,
    FINAL_SCHEMA,
    PROPOSED_WEEK,
    REVIEW_SCHEMA,
    ROLES,
    SPECIALIST_SCHEMA,
)

CUTOFF = date(2026, 9, 27)


def example_goal(**updates):
    return {"description": "Assess readiness for a synthetic marathon", "race_date": "2026-11-01", "distance_km": 42.195,
            "max_stressors_per_week": 2, "constraints": ["No more than two stressors each week."], **updates}


def schema_fixture(schema):
    """Synthetic contract fixture; never evidence of physiological validity."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        return None if "null" in kind else schema_fixture({**schema, "type": kind[0]})
    if kind == "object":
        return {key: schema_fixture(value) for key, value in schema["properties"].items()}
    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind in {"number", "integer"}:
        return 0
    return "Synthetic fixture"


def example_final():
    kinds = ["easy", "rest", "steady", "easy", "rest", "easy", "long"]
    distances = [8, 0, 8, 5, 0, 5, 16]
    sessions = [{"date": date(2026, 9, 28 + index).isoformat() if index < 3 else date(2026, 10, index - 2).isoformat(),
                 "kind": kind, "description": "Synthetic example only", "distance_km": distances[index],
                 "duration_minutes": None, "is_stressor": kind in {"steady", "long"},
                 "adjustment_trigger": "Skip if the athlete reports renewed symptoms.", "evidence": ["synthetic-activity-1"]}
                for index, kind in enumerate(kinds)]
    result = {
        "summary": "Synthetic assessment for offline testing.", "confidence": "low", "goal_assessment": "insufficient_data",
        "current_status": {"fitness": "Unknown", "fatigue": "Unknown", "durability": "Unknown", "data_quality": "Synthetic fixture"},
        "key_findings": [{"claim": "Synthetic data cannot establish race fitness.", "kind": "unknown", "confidence": "high",
                          "evidence": ["2026-09-01 through 2026-09-27"], "alternative_explanations": [], "implication": "Collect real evidence."}],
        "finish_time_estimates": [{"method": "Race extrapolation", "eligible": False, "low_seconds": None, "high_seconds": None,
                                   "assumptions": [], "reason": "No actual race result supplied.", "evidence": [],
                                   "source_basis": "unknown", "validity_limits": []}],
        "weekly_plan": [{"week_start": "2026-09-28", "focus": "Conditional illustrative week", "distance_km": 42, "sessions": sessions}],
        "small_tweaks": ["Record perceived effort."], "constraints_checked": ["Maximum two stressors."],
        "unresolved_disagreements": [], "missing_information": ["Actual athlete records."],
    }
    defaults = schema_fixture(FINAL_SCHEMA)
    defaults.update(result)
    result = defaults
    result["race_strategy"]["hr_basis"] = "unknown"
    baseline = result["training_rationale"]["achieved_baseline"]
    baseline.update(window_start="2026-09-01", window_end="2026-09-27", weekly_distance_km=40)
    week = schema_fixture(PROPOSED_WEEK)
    week.update(week_start="2026-09-28", distance_km=42, running_days=5, stressors=2,
                longest_run_km=16, distance_delta_km=2, distance_delta_percent=5)
    result["training_rationale"]["planned_weeks"] = [week]
    return result


def stage_answer(stage, role):
    if stage == "independent":
        return {"role": role, "summary": f"Synthetic {role} review", "findings": [], "missing_data": ["Real athlete records"],
                "questions_for_other_roles": [], "suggested_changes": []}
    if stage == "roundtable":
        return {"role": role, "challenges": [], "revised_findings": [], "unresolved_questions": ["No real athlete data"],
                "recommendation": "Do not interpret synthetic fixtures as live proof."}
    if stage == "capacity":
        return schema_fixture(CAPACITY_SCHEMA)
    if stage == "audit":
        return schema_fixture(FINAL_AUDIT_SCHEMA)
    return example_final()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    evidence_path = tmp_path / "evidence.json"
    write_json(evidence_path, {"synthetic": True, "coverage": {"boundaries": {"start": "2026-09-01", "end": "2026-09-27"}},
                               "activities": [{"id": "synthetic-activity-1"}]})
    run_dir = tmp_path / "run"
    calls = []
    call_lock = Lock()
    behavior = {"failure": None, "final": None, "wrong_role": None, "revision": 0,
                "audit": None, "revision_final": None, "validation_seen": []}
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
        if stage in {"independent", "roundtable", "capacity"}:
            assert workspace.name == "capacity"
            assert not (workspace / "goal.json").exists()
            assert "target_time_seconds" not in str(read_json(workspace / "athlete_context.json"))
            assert not (workspace.parent / "planning").exists() or (run_dir / "capacity.json").exists()
        else:
            assert workspace.name == "planning"
            assert (workspace / "goal.json").is_file()
            assert (workspace / "capacity.json").is_file()
        if stage == "capacity":
            assert set(read_json(workspace / "roundtable.json")) == set(ROLES)
        result = deepcopy(behavior["final"]) if stage in {"planning", "revision"} and behavior["final"] is not None else stage_answer(stage, role)
        if stage in {"audit", "revision"}:
            assert "validation.json" in prompt
            behavior["validation_seen"].append((stage, deepcopy(read_json(workspace / "validation.json"))))
        if stage == "revision" and behavior["revision_final"] is not None:
            result = deepcopy(behavior["revision_final"])
        if stage == "capacity":
            result["summary"] += f"; fixture revision {behavior['revision']}"
        if stage == "audit" and behavior["audit"] is not None:
            result = deepcopy(behavior["audit"])
        if stage == "independent":
            result["summary"] += f"; fixture revision {behavior['revision']}"
        if behavior["wrong_role"] == (stage, role):
            result["role"] = "incorrect_role"
        jsonschema.validate(result, schema)
        write_json(output, result)
        return result

    monkeypatch.setattr(rt, "invoke_codex", fake_invoke)
    return evidence_path, run_dir, calls, behavior


def test_goal_blind_capacity_plan_audit_and_validated_resume(offline):
    evidence, run, calls, _ = offline
    result = rt.run_analysis(evidence, example_goal(), run, concurrency=3)
    assert len(calls) == 21
    assert {role for stage, role, _ in calls if stage == "independent"} == set(ROLES)
    assert {role for stage, role, _ in calls if stage == "roundtable"} == set(ROLES)
    assert [role for stage, role, _ in calls if stage == "planning"] == ["lead"]
    assert read_json(run / "run.json")["status"] == "complete"
    assert read_json(run / "run.json")["completed_calls"] == 21
    assert read_json(run / "final.json") == result
    assert "Synthetic assessment" in (run / "report.md").read_text()
    assert stat.S_IMODE((run / "final.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((run / "report.md").stat().st_mode) == 0o600
    assert rt.run_analysis(evidence, example_goal(), run) == result
    assert len(calls) == 21


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
    assert [role for stage, role, _ in new_calls if stage == "planning"] == ["lead"]
    assert len(new_calls) == 13


def test_invalid_final_does_not_publish(offline):
    evidence, run, calls, behavior = offline
    invalid = example_final()
    invalid["weekly_plan"][0]["sessions"][0]["date"] = CUTOFF.isoformat()
    behavior["final"] = invalid
    with pytest.raises(RuntimeError, match="audit did not pass"):
        rt.run_analysis(evidence, example_goal(), run)
    assert len(calls) == 25
    assert read_json(run / "run.json")["deterministic_validation_status"] == "fail"
    assert "future planning window" in read_json(run / "inputs" / "planning" / "validation.json")["errors"][0]
    assert all(not record["passed"] for _, record in behavior["validation_seen"])
    assert read_json(run / "run.json")["status"] == "failed"
    assert not (run / "final.json").exists()
    assert not (run / "report.md").exists()


def test_invalid_plan_resume_keeps_drafts_and_rechecks_changed_revision(offline):
    evidence, run, calls, behavior = offline
    invalid = example_final()
    invalid["weekly_plan"][0]["sessions"][0]["date"] = CUTOFF.isoformat()
    behavior["final"] = invalid
    with pytest.raises(RuntimeError, match="audit did not pass"):
        rt.run_analysis(evidence, example_goal(), run)
    assert len(calls) == 25
    assert (run / "planning" / "lead.json").is_file()
    assert (run / "planning" / "lead.cache.json").is_file()
    state = read_json(run / "run.json")
    assert state["stages"]["planning"]["lead"] == "complete"
    assert state["completed_calls"] == 25
    assert not (run / "final.json").exists()

    before = len(calls)
    # Explicitly invalidate the final repair; all earlier schema-valid drafts
    # stay cached, and its changed answer invalidates the dependent audit.
    (run / "revision" / "lead-2.json").unlink()
    behavior["revision_final"] = example_final()
    result = rt.run_analysis(evidence, example_goal(), run)
    assert calls[before:] == [("revision", "lead-2", None), ("audit", "reviewer-2", None)]
    assert read_json(run / "run.json")["status"] == "complete"
    assert read_json(run / "run.json")["completed_calls"] == 25
    assert read_json(run / "run.json")["new_calls_this_attempt"] == 2
    assert (run / "planning" / "lead.cache.json").is_file()
    assert read_json(run / "final.json") == result


def test_deterministic_error_is_repaired_before_publication_even_if_auditor_says_pass(offline):
    evidence, run, calls, behavior = offline
    invalid = example_final()
    invalid["weekly_plan"][0]["distance_km"] = 99
    behavior["final"] = invalid
    behavior["revision_final"] = example_final()
    result = rt.run_analysis(evidence, example_goal(), run)
    assert len(calls) == 23
    assert result["weekly_plan"][0]["distance_km"] == 42
    assert read_json(run / "audit" / "reviewer-0.validation.json")["passed"] is False
    assert read_json(run / "audit" / "reviewer-1.validation.json")["passed"] is True
    revision_inputs = [record for stage, record in behavior["validation_seen"] if stage == "revision"]
    assert "Weekly distance" in revision_inputs[0]["errors"][0]
    assert read_json(run / "run.json")["deterministic_validation_status"] == "pass"


def test_changed_validation_invalidates_audit_cache_even_with_unchanged_draft(offline, monkeypatch):
    evidence, run, calls, _ = offline
    rt.run_analysis(evidence, example_goal(), run)
    before = len(calls)

    def new_check(*args):
        raise ValueError("Synthetic new deterministic check")

    monkeypatch.setattr(rt, "validate_final", new_check)
    with pytest.raises(RuntimeError, match="audit did not pass"):
        rt.run_analysis(evidence, example_goal(), run)
    assert calls[before:] == [("audit", "reviewer-0", None), ("revision", "lead-1", None),
                             ("audit", "reviewer-1", None), ("revision", "lead-2", None),
                             ("audit", "reviewer-2", None)]
    assert not (run / "final.json").exists()


def test_failed_rerun_does_not_leave_stale_published_final(offline):
    evidence, run, _, behavior = offline
    rt.run_analysis(evidence, example_goal(), run)
    (run / "planning" / "lead.json").unlink()
    invalid = example_final()
    invalid["weekly_plan"][0]["sessions"][0]["date"] = CUTOFF.isoformat()
    behavior["final"] = invalid
    with pytest.raises(RuntimeError, match="audit did not pass"):
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


@pytest.mark.parametrize("verdict", ["revise", "blocked"])
def test_unresolved_audit_blocks_publication_after_bounded_revisions(offline, verdict):
    evidence, run, calls, behavior = offline
    audit = schema_fixture(FINAL_AUDIT_SCHEMA)
    audit.update(verdict=verdict, required_changes=["Synthetic unresolved claim"])
    behavior["audit"] = audit
    with pytest.raises(RuntimeError, match="audit did not pass"):
        rt.run_analysis(evidence, example_goal(), run)
    assert len(calls) == 25
    assert not (run / "final.json").exists()
    assert not (run / "report.md").exists()
    assert read_json(run / "run.json")["audit_status"] == verdict
    assert (run / "revision" / "lead-2.json").exists()


def test_pass_label_with_required_changes_cannot_publish(offline):
    evidence, run, _, behavior = offline
    audit = schema_fixture(FINAL_AUDIT_SCHEMA)
    audit["required_changes"] = ["Must actually resolve this"]
    behavior["audit"] = audit
    with pytest.raises(RuntimeError, match="audit did not pass"):
        rt.run_analysis(evidence, example_goal(), run)
    assert not (run / "final.json").exists()


def test_athlete_observations_reach_capacity_without_objective(offline, monkeypatch):
    evidence, run, _, _ = offline
    observation = {"date": "2026-09-27", "category": "symptoms", "observation": "No pain during the run.", "source": "Direct athlete report"}
    rt.run_analysis(evidence, example_goal(target_time_seconds=15000, notes="PRIVATE_GOAL_ONLY", athlete_observations=[observation]), run)
    context = read_json(run / "inputs" / "capacity" / "athlete_context.json")
    assert context["athlete_observations"] == [observation]
    for path in (run / "inputs" / "capacity").glob("*"):
        assert "PRIVATE_GOAL_ONLY" not in path.read_text()
        assert "15000" not in path.read_text()


@pytest.mark.parametrize("field,value", [("distance_km", 50), ("running_days", 7), ("stressors", 0),
                                         ("longest_run_km", 25), ("distance_delta_km", 10), ("distance_delta_percent", 25)])
def test_summary_arithmetic_cannot_contradict_sessions_or_baseline(field, value):
    result = example_final()
    result["training_rationale"]["planned_weeks"][0][field] = value
    with pytest.raises(ValueError):
        rt.validate_final(result, example_goal(), CUTOFF)


def test_unknown_calibration_cannot_publish_precise_race_hr():
    result = example_final()
    result["race_strategy"]["hr_guidance"]["low_bpm"] = 150
    with pytest.raises(ValueError, match="HR basis"):
        rt.validate_final(result, example_goal(), CUTOFF)


def test_observations_validate_source_date_and_category():
    for item in ({"category": "diagnosis", "observation": "x", "source": "athlete"},
                 {"category": "symptoms", "observation": "x", "source": ""},
                 {"category": "effort", "observation": "x", "source": "athlete", "date": "not-date"}):
        with pytest.raises(ValidationError):
            Goal.model_validate(example_goal(athlete_observations=[item]))


def test_failed_audit_check_cannot_be_hidden_by_pass_verdict(offline):
    evidence, run, _, behavior = offline
    audit = schema_fixture(FINAL_AUDIT_SCHEMA)
    audit['checks'] = [{'area': 'evidence', 'result': 'fail', 'reason': 'Synthetic unsupported claim', 'evidence': []}]
    behavior['audit'] = audit
    with pytest.raises(RuntimeError, match='audit did not pass'):
        rt.run_analysis(evidence, example_goal(), run)
    assert not (run / 'final.json').exists()


def test_null_cannot_hide_known_week_totals():
    result = example_final()
    result['weekly_plan'][0]['distance_km'] = None
    result['training_rationale']['planned_weeks'][0]['distance_km'] = None
    with pytest.raises(ValueError, match='weekly total'):
        rt.validate_final(result, example_goal(), CUTOFF)


def test_zero_cannot_be_a_calibrated_hr_target():
    result = example_final()
    result['race_strategy']['hr_basis'] = 'calibrated'
    result['race_strategy']['hr_guidance']['low_bpm'] = 0
    with pytest.raises(ValueError, match='positive'):
        rt.validate_final(result, example_goal(), CUTOFF)
