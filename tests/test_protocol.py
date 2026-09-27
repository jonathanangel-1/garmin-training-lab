"""Offline contracts for capability, uncertainty, quantitative plans and audit output."""

import jsonschema
import pytest

from garmin_training import protocol as p


def example_baseline():
    return {
        "window_start": "2026-08-31", "window_end": "2026-09-27",
        "aggregation_method": "Mean of four complete synthetic weeks",
        "weekly_distance_km": 36, "weekly_duration_minutes": 228,
        "weekly_running_days": 4, "weekly_stressors": 1,
        "longest_run_km": 18, "longest_run_minutes": 114,
        "basis": "observation", "evidence": ["synthetic-week-1-to-4"],
        "limitations": ["Synthetic fixture; not athlete evidence"],
    }


def example_capacity():
    return {
        "summary": "Synthetic capacity fixture with no goal or calibrated threshold.",
        "confidence": "low",
        "calibration": {
            "status": "unknown", "hr_sensor": "Not supplied",
            "hr_signal_quality": "limited", "threshold_records": [],
            "configured_zones": "Configured values have no documented calibration.",
            "usable_for_prescription": "No precise race HR is established.",
            "evidence": ["synthetic-device-settings"],
            "limitations": ["No threshold measurement"],
        },
        "observed_pace_hr": [{
            "description": "Synthetic steady section", "date_or_range": "2026-09-20",
            "pace_low_seconds_per_km": 360, "pace_high_seconds_per_km": 370,
            "hr_low_bpm": None, "hr_high_bpm": None,
            "duration_minutes": 40, "distance_km": 6.5,
            "effort_report": "No contemporaneous effort report",
            "conditions": "Terrain and weather unrecorded", "signal_quality": "missing",
            "basis": "observation", "evidence": ["synthetic-run-1"],
            "confidence": "low", "limitations": ["No usable HR coverage"],
        }],
        "durability": {
            "observed_longest_distance_km": 18,
            "observed_longest_duration_minutes": 114,
            "recent_repeatable_duration_minutes": 100,
            "late_run_response": "Unestablished", "sustained_effort_evidence": "Limited",
            "recovery_response": "No first-person recovery report",
            "basis": "observation", "evidence": ["synthetic-long-runs"],
            "limitations": ["Synthetic records only"], "confidence": "low",
        },
        "strength_and_mechanics": {
            "documented_strength_training": "No strength records supplied",
            "strength_capacity_status": "not_established", "supported_findings": [],
            "evidence": [], "limitations": ["Cadence does not establish strength"],
        },
        "established_training_baseline": example_baseline(),
        "key_findings": [], "unknowns": ["HR calibration", "Strength capacity"],
        "assessment_priorities": ["Verify sensor and any existing threshold record"],
    }


def example_final():
    quantities = [
        ("2026-09-29", "easy", 6, 40, False),
        ("2026-10-01", "steady", 8, 48, True),
        ("2026-10-03", "easy", 6, 40, False),
        ("2026-10-04", "long", 20, 120, True),
    ]
    sessions = [{
        "date": day, "kind": kind, "description": "Synthetic session",
        "distance_km": distance, "duration_minutes": minutes, "is_stressor": stressor,
        "adjustment_trigger": "Adjust for actual effort and reported recovery",
        "evidence": ["synthetic-week-1-to-4"],
    } for day, kind, distance, minutes, stressor in quantities]
    return {
        "summary": "Synthetic proposal only", "confidence": "low",
        "goal_assessment": "insufficient_data",
        "current_status": {
            "fitness": "Unknown", "fatigue": "Unknown", "durability": "Limited evidence",
            "data_quality": "Synthetic fixture",
        },
        "key_findings": [],
        "finish_time_estimates": [{
            "method": "Race extrapolation", "eligible": False,
            "low_seconds": None, "high_seconds": None, "assumptions": [],
            "reason": "No qualifying race", "evidence": [], "source_basis": "unknown",
            "validity_limits": ["Submaximal training is not a maximal race"],
        }],
        "weekly_plan": [{
            "week_start": "2026-09-28", "focus": "Synthetic illustration",
            "distance_km": 40, "sessions": sessions,
        }],
        "small_tweaks": [], "constraints_checked": ["Two stressors maximum"],
        "unresolved_disagreements": [], "missing_information": ["Actual athlete data"],
        "capacity_summary": {
            "summary": "Capacity not calibrated", "confidence": "low",
            "hr_calibration_status": "unknown", "sustainable_effort_basis": "Not established",
            "durability_limit": "Longer-duration tolerance unestablished",
            "evidence": ["synthetic-long-runs"], "unknowns": ["Threshold calibration"],
        },
        "goal_feasibility": {
            "objective_present": False, "status": "insufficient_evidence",
            "explanation": "No numerical objective supplied", "confidence": "unknown",
            "supporting_evidence": [], "limiting_evidence": [],
            "what_would_change_assessment": ["A stated objective and suitable performance evidence"],
        },
        "race_strategy": {
            "status": "readiness_needs_verification", "hr_basis": "unknown",
            "hr_guidance": {
                "low_bpm": None, "high_bpm": None,
                "description": "No numerical race HR is established", "basis": "unknown",
                "evidence": [], "limitations": ["Uncalibrated HR"],
            },
            "pace_basis": "No numerical pace prescription", "effort_execution": [],
            "decision_points": [], "fueling_hydration": "Ask about practiced intake",
            "readiness_requirements": ["Establish duration tolerance"],
            "confidence": "unknown", "evidence": [],
        },
        "training_rationale": {
            "achieved_baseline": example_baseline(),
            "planned_weeks": [{
                "week_start": "2026-09-28", "distance_km": 40, "duration_minutes": 248,
                "running_days": 4, "stressors": 2, "longest_run_km": 20,
                "longest_run_minutes": 120, "distance_delta_km": 4,
                "distance_delta_percent": 100 * 4 / 36,
                "duration_delta_minutes": 20, "duration_delta_percent": 100 * 20 / 228,
                "comparison_basis": "Four completed synthetic weeks",
                "adaptation_target": "Increase duration tolerance",
                "load_tradeoff": "More time on feet creates additional fatigue",
                "why_this_dose": "Illustrative coaching judgment, not a validated threshold",
                "governing_limit": "Duration governs; distance is illustrative",
                "adjustment_basis": "Observed effort and subsequent recovery",
                "basis": "coaching_judgment", "evidence": ["synthetic-week-1-to-4"],
            }],
            "benefit_load_reasoning": "Synthetic example; no individual inference",
            "alternatives_considered": ["Maintain baseline if recovery is inadequate"],
        },
        "assessment_actions": [{
            "question": "Is HR calibrated?", "action": "Inspect existing threshold record",
            "purpose": "Resolve source quality", "interpretation": "Device settings are not a test",
            "load_cost": "No exercise load", "counts_as_stressor": False,
            "replaces_planned_session": None, "evidence": ["synthetic-device-settings"],
        }],
    }


def example_audit(verdict="pass"):
    result = {
        "verdict": verdict, "summary": "Synthetic audit", "confidence": "high",
        "checks": [{"area": "arithmetic", "result": "pass", "reason": "Recomputed",
                    "evidence": ["synthetic-week-1-to-4"]}],
        "blockers": [], "required_changes": [],
    }
    if verdict != "pass":
        result["blockers"] = [{
            "issue": "Unsupported numerical HR", "reason": "No calibrated source",
            "evidence": ["synthetic-device-settings"], "required_change": "Remove HR range",
        }]
        result["required_changes"] = ["Remove unsupported HR range"]
    return result


@pytest.mark.parametrize("schema", [p.SPECIALIST_SCHEMA, p.REVIEW_SCHEMA, p.CAPACITY_SCHEMA,
                                    p.FINAL_SCHEMA, p.FINAL_AUDIT_SCHEMA])
def test_schemas_are_valid_and_recursively_strict(schema):
    jsonschema.Draft202012Validator.check_schema(schema)

    def inspect(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                inspect(child)
        elif node.get("type") == "array":
            inspect(node["items"])

    inspect(schema)


def test_unknown_capacity_remains_expressible_without_fabricated_numbers():
    result = example_capacity()
    jsonschema.validate(result, p.CAPACITY_SCHEMA)
    assert result["calibration"]["threshold_records"] == []
    assert result["observed_pace_hr"][0]["hr_low_bpm"] is None


@pytest.mark.parametrize("goal_field", ["goal", "goal_feasibility", "target_time_seconds",
                                      "weekly_plan", "race_strategy"])
def test_goal_blind_capacity_contract_rejects_planning_payload(goal_field):
    result = example_capacity()
    result[goal_field] = "An objective must not enter the capacity output"
    with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
        jsonschema.validate(result, p.CAPACITY_SCHEMA)


@pytest.mark.parametrize("missing", ["basis", "evidence", "limitations", "signal_quality",
                                     "duration_minutes", "effort_report"])
def test_pace_hr_observation_requires_context_and_provenance(missing):
    result = example_capacity()
    del result["observed_pace_hr"][0][missing]
    with pytest.raises(jsonschema.ValidationError, match="required property"):
        jsonschema.validate(result, p.CAPACITY_SCHEMA)


def test_calibration_status_is_not_an_untyped_zone_label():
    result = example_capacity()
    result["calibration"]["status"] = "configured_zones"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result, p.CAPACITY_SCHEMA)


def test_device_estimate_can_be_distinguished_from_measurement():
    result = example_capacity()
    result["calibration"]["status"] = "device_estimated"
    result["calibration"]["threshold_records"] = [{
        "name": "Synthetic device estimate", "date": "2026-09-01",
        "method": "Device algorithm", "hr_bpm": 165, "pace_seconds_per_km": None,
        "status": "device_estimated", "basis": "model_estimate",
        "evidence": ["synthetic-device-estimate"],
        "limitations": ["Not an independent laboratory measurement"],
    }]
    jsonschema.validate(result, p.CAPACITY_SCHEMA)


def test_strength_measurement_status_uses_explicit_vocabulary():
    result = example_capacity()
    result["strength_and_mechanics"]["strength_capacity_status"] = "cadence_validated"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result, p.CAPACITY_SCHEMA)


def test_full_proposal_accepts_unquantified_race_and_source_bounded_forecast():
    jsonschema.validate(example_final(), p.FINAL_SCHEMA)


@pytest.mark.parametrize("missing", ["capacity_summary", "goal_feasibility", "race_strategy",
                                     "training_rationale", "assessment_actions"])
def test_legacy_goal_led_output_cannot_skip_capability_and_load_assessment(missing):
    result = example_final()
    del result[missing]
    with pytest.raises(jsonschema.ValidationError, match="required property"):
        jsonschema.validate(result, p.FINAL_SCHEMA)


@pytest.mark.parametrize("missing", ["source_basis", "validity_limits"])
def test_forecast_requires_source_basis_and_limits(missing):
    result = example_final()
    del result["finish_time_estimates"][0][missing]
    with pytest.raises(jsonschema.ValidationError, match="required property"):
        jsonschema.validate(result, p.FINAL_SCHEMA)


@pytest.mark.parametrize("field", ["distance_km", "duration_minutes", "longest_run_km",
                                   "longest_run_minutes", "running_days", "stressors"])
def test_proposed_week_rejects_negative_training_quantities(field):
    result = example_final()
    result["training_rationale"]["planned_weeks"][0][field] = -1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result, p.FINAL_SCHEMA)


def test_signed_deltas_and_fractional_achieved_averages_are_allowed():
    result = example_final()
    baseline = result["training_rationale"]["achieved_baseline"]
    baseline["weekly_running_days"] = 3.5
    baseline["weekly_stressors"] = 1.5
    week = result["training_rationale"]["planned_weeks"][0]
    for field in ("distance_delta_km", "distance_delta_percent", "duration_delta_minutes",
                  "duration_delta_percent"):
        week[field] = -5
    jsonschema.validate(result, p.FINAL_SCHEMA)


@pytest.mark.parametrize("field", ["running_days", "stressors"])
def test_proposed_session_counts_must_be_integers(field):
    result = example_final()
    result["training_rationale"]["planned_weeks"][0][field] = 2.5
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result, p.FINAL_SCHEMA)


@pytest.mark.parametrize("verdict", ["pass", "revise", "blocked"])
def test_audit_verdicts_have_structured_actionable_outputs(verdict):
    jsonschema.validate(example_audit(verdict), p.FINAL_AUDIT_SCHEMA)


@pytest.mark.parametrize("missing", ["issue", "reason", "evidence", "required_change"])
def test_audit_blocker_requires_reason_source_and_correction(missing):
    result = example_audit("revise")
    del result["blockers"][0][missing]
    with pytest.raises(jsonschema.ValidationError, match="required property"):
        jsonschema.validate(result, p.FINAL_AUDIT_SCHEMA)
