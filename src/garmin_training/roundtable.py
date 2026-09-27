"""Goal-blind capacity assessment, goal-aware planning, and publication audit."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jsonschema

from .core import ensure_private_dir, read_json, write_json, write_text
from .goals import Goal
from .protocol import (
    AUDIT_PROMPT,
    CAPACITY_PROMPT,
    CAPACITY_SCHEMA,
    COACHING_QUALITY_CRITERIA,
    FINAL_AUDIT_SCHEMA,
    FINAL_SCHEMA,
    METHOD,
    PLAN_CANDIDATE_PROMPTS,
    PLAN_SELECTION_PROMPT,
    PROTOCOL_VERSION,
    REVIEW_SCHEMA,
    REVISION_PROMPT,
    ROLES,
    SPECIALIST_SCHEMA,
)
from .research import RESEARCH_CONTEXT


def _now():
    return datetime.now(timezone.utc).isoformat()


def _codex_environment():
    env = os.environ.copy()
    # Honor the user's selected ChatGPT login rather than an inherited API key.
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMINTOKENS"):
        env.pop(name, None)
    return env


def check_codex() -> str:
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Install Codex CLI and run codex login before analysis")
    result = subprocess.run([executable, "login", "status"], capture_output=True,
                            text=True, timeout=20, env=_codex_environment())
    if result.returncode or "chatgpt" not in (result.stdout + result.stderr).lower():
        raise RuntimeError("Sign into Codex CLI with ChatGPT using codex login")
    help_result = subprocess.run([executable, "exec", "--help"], capture_output=True,
                                text=True, timeout=20, env=_codex_environment())
    for flag in ("--ignore-user-config", "--ephemeral", "--output-schema"):
        if flag not in help_result.stdout:
            raise RuntimeError("Update Codex CLI; this workflow needs current non-interactive flags")
    return executable


def invoke_codex(prompt: str, schema: dict, output: Path, workspace: Path,
                 model: str | None = None, timeout: int = 900) -> dict:
    """Run with saved CLI login and intentionally supplied curated evidence only."""
    output = output.absolute()
    schema_path = output.with_suffix(".schema.json")
    write_json(schema_path, schema)
    args = ["codex", "exec", "--ignore-user-config", "--ephemeral",
            "-c", 'model_reasoning_effort="high"',
            "--sandbox", "read-only", "--skip-git-repo-check", "--color", "never",
            "-C", str(workspace.absolute()), "--output-schema", str(schema_path),
            "--output-last-message", str(output)]
    if model:
        args += ["--model", model]
    args += ["-"]
    log_path = output.with_suffix(".log")
    with log_path.open("w", encoding="utf-8") as log:
        log_path.chmod(0o600)
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                   text=True, env=_codex_environment(),
                                   start_new_session=os.name == "posix")
        try:
            process.communicate(prompt, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            raise RuntimeError("Codex run stopped before completion") from None
    if process.returncode or not output.is_file():
        raise RuntimeError("Codex analysis failed; inspect the private run log")
    output.chmod(0o600)
    result = read_json(output)
    jsonschema.validate(result, schema)
    return result


def validate_final(result: dict, goal: dict, cutoff: date) -> None:
    jsonschema.validate(result, FINAL_SCHEMA)
    option_ids = [option["option_id"] for option in result["planning_comparison"]["options_considered"]]
    if len(option_ids) != len(set(option_ids)):
        raise ValueError("Planning comparison option IDs must be unique")
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite numerical claim")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)
    finite(result)
    target = Goal.model_validate(goal)
    horizon = min(target.race_date or cutoff + timedelta(days=28), cutoff + timedelta(days=84))
    stressors = {}
    seen = set()
    seen_weeks = set()
    for week in result["weekly_plan"]:
        week_start = date.fromisoformat(week["week_start"])
        if week_start.weekday() != 0:
            raise ValueError("Plan weeks must begin on Monday")
        if week_start in seen_weeks:
            raise ValueError("Duplicate plan week")
        seen_weeks.add(week_start)
        distance = week["distance_km"]
        if distance is not None and distance < 0:
            raise ValueError("Negative weekly distance")
        for session in week["sessions"]:
            day = date.fromisoformat(session["date"])
            if day <= cutoff or day > horizon:
                raise ValueError("Session falls outside the future planning window")
            if not week_start <= day <= week_start + timedelta(days=6):
                raise ValueError("Session date is outside its stated week")
            key = (day, session["kind"])
            if key in seen:
                raise ValueError("Duplicate session")
            seen.add(key)
            for field in ("distance_km", "duration_minutes"):
                if session[field] is not None and session[field] < 0:
                    raise ValueError("Negative session quantity")
            if session["kind"] == "rest" and session["is_stressor"]:
                raise ValueError("Rest cannot be a stressor")
            if session["kind"] in {"long", "marathon_pace", "race"} and not session["is_stressor"]:
                raise ValueError("Long runs, marathon-pace sessions and races must count as stressors")
            if session["is_stressor"]:
                iso = day.isocalendar()[:2]
                stressors[iso] = stressors.get(iso, 0) + 1
        running = [s for s in week["sessions"] if s["kind"] not in {"rest", "strength", "cross_training"}]
        if distance is None and all(s["distance_km"] is not None for s in running):
            raise ValueError("Known running distances require a weekly total")
        if distance is not None:
            if any(s["distance_km"] is None for s in running):
                raise ValueError("Weekly distance requires numeric distances for all running sessions")
            if abs(sum(s["distance_km"] for s in running) - distance) > 0.01:
                raise ValueError("Weekly distance does not match running sessions")
    maximum = target.max_stressors_per_week
    if maximum is not None and any(count > maximum for count in stressors.values()):
        raise ValueError("Plan exceeds the user's maximum weekly stressors")
    if len(result["small_tweaks"]) > 7:
        raise ValueError("At most seven small tweaks are permitted")
    for estimate in result["finish_time_estimates"]:
        low, high = estimate["low_seconds"], estimate["high_seconds"]
        if estimate["eligible"]:
            if low is None or high is None or low <= 0 or high < low:
                raise ValueError("Eligible prediction requires an ordered positive range")
        elif low is not None or high is not None:
            raise ValueError("Ineligible prediction must not invent a finish time")
    hr = result["race_strategy"]["hr_guidance"]
    if any(hr[key] is not None and hr[key] <= 0 for key in ("low_bpm", "high_bpm")):
        raise ValueError("Numerical race HR must be positive")
    if result["race_strategy"]["hr_basis"] == "unknown" and (hr["low_bpm"] is not None or hr["high_bpm"] is not None):
        raise ValueError("Unknown HR basis cannot support numerical race HR")
    if hr["low_bpm"] is not None and hr["high_bpm"] is not None and hr["high_bpm"] < hr["low_bpm"]:
        raise ValueError("Reversed heart-rate bounds")
    summaries = result["training_rationale"]["planned_weeks"]
    if len(summaries) != len(result["weekly_plan"]) or {w["week_start"] for w in summaries} != seen_weeks_as_text(seen_weeks):
        raise ValueError("Training rationale must cover each planned week exactly once")
    baseline = result["training_rationale"]["achieved_baseline"]
    for summary in summaries:
        week = next(w for w in result["weekly_plan"] if w["week_start"] == summary["week_start"])
        running = [s for s in week["sessions"] if s["kind"] not in {"rest", "strength", "cross_training"}]
        quantities = {
            "distance_km": week["distance_km"],
            "duration_minutes": sum(s["duration_minutes"] for s in running) if all(s["duration_minutes"] is not None for s in running) else None,
            "running_days": len({s["date"] for s in running}),
            "stressors": sum(s["is_stressor"] for s in week["sessions"]),
            "longest_run_km": max((s["distance_km"] for s in running), default=0) if all(s["distance_km"] is not None for s in running) else None,
            "longest_run_minutes": max((s["duration_minutes"] for s in running), default=0) if all(s["duration_minutes"] is not None for s in running) else None,
        }
        for field, actual in quantities.items():
            declared = summary[field]
            if actual is not None and declared is None:
                raise ValueError(f"Known planned week {field} must be summarized")
            if declared is not None and (actual is None or abs(declared - actual) > 0.1):
                raise ValueError(f"Planned week {field} contradicts its sessions")
        for field, base_key, delta_key, percent_key in [
            ("distance_km", "weekly_distance_km", "distance_delta_km", "distance_delta_percent"),
            ("duration_minutes", "weekly_duration_minutes", "duration_delta_minutes", "duration_delta_percent"),
        ]:
            amount, base = summary[field], baseline[base_key]
            delta, percent = summary[delta_key], summary[percent_key]
            if amount is not None and base is not None:
                if delta is None or (base > 0 and percent is None):
                    raise ValueError("Known baseline and plan quantities require deltas")
            if delta is not None and (amount is None or base is None or abs(delta - (amount - base)) > 0.1):
                raise ValueError("Planned change contradicts the achieved baseline")
            if percent is not None and (amount is None or base is None or base <= 0 or abs(percent - 100 * (amount - base) / base) > 0.2):
                raise ValueError("Planned percentage contradicts the achieved baseline")


def seen_weeks_as_text(weeks):
    return {week.isoformat() for week in weeks}


def coaching_audit_errors(audit: dict) -> list[str]:
    """Require explicit review of coaching usefulness, not only factual defensibility."""
    errors = []
    for area in COACHING_QUALITY_CRITERIA:
        checks = [check for check in audit["checks"] if check["area"] == area]
        if len(checks) != 1 or checks[0]["result"] != "pass":
            errors.append(
                f"Workflow check: audit must explicitly pass {area} exactly once, "
                "with evidence-based reasoning; revise the proposal if that criterion is unmet."
            )
    return errors


def _clock(seconds):
    if seconds is None:
        return "unavailable"
    seconds = round(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def _render_value(value, depth=0):
    """Readable rendering of the structured assessment without hiding its basis."""
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            label = key.replace("_", " ").capitalize()
            if isinstance(item, (dict, list)):
                lines += [f"**{label}:**", ""] + _render_value(item, depth + 1) + [""]
            else:
                lines += [f"**{label}:** {'Unknown' if item is None else item}", ""]
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines += _render_value(item, depth + 1) + ["---", ""]
            else:
                lines.append(f"- {item}")
        return lines or ["None recorded."]
    return [str(value)]


def render_report(result: dict) -> str:
    lines = ["# Training assessment and proposed plan", "", result["summary"], "",
             f"**Goal assessment:** {result['goal_assessment']} · **Confidence:** {result['confidence']}", ""]
    for key, value in result["current_status"].items():
        lines += [f"**{key.replace('_', ' ').title()}:** {value}", ""]
    for title, key in [("Capacity before considering the goal", "capacity_summary"),
                       ("Goal feasibility", "goal_feasibility"),
                       ("Provisional race strategy", "race_strategy"),
                       ("Why this approach was selected", "planning_comparison"),
                       ("Why this training", "training_rationale"),
                       ("What would change the assessment", "assessment_actions")]:
        lines += [f"## {title}", ""] + _render_value(result[key]) + [""]
    lines += ["## Evidence", ""]
    for item in result["key_findings"]:
        lines += [f"- **{item['claim']}** ({item['kind']}; {item['confidence']}). {item['implication']} Evidence: {', '.join(item['evidence'])}."]
    lines += ["", "## Finish-time estimates", ""]
    for estimate in result["finish_time_estimates"]:
        value = f"{_clock(estimate['low_seconds'])}–{_clock(estimate['high_seconds'])}" if estimate["eligible"] else "insufficient evidence"
        lines += [f"- **{estimate['method']}: {value}.** {estimate['reason']} Assumptions: {'; '.join(estimate['assumptions'])}. Evidence: {', '.join(estimate['evidence'])}."]
    for week in result["weekly_plan"]:
        lines += ["", f"## Week of {week['week_start']}", "", week["focus"], "",
                  "| Date | Session | Prescription | Adjustment trigger |", "|---|---|---|---|"]
        for session in week["sessions"]:
            quantity = ""
            if session["distance_km"] is not None:
                quantity += f" {session['distance_km']:g} km."
            if session["duration_minutes"] is not None:
                quantity += f" {session['duration_minutes']:g} min."
            cells = [session["date"], session["kind"].replace("_", " "), quantity + " " + session["description"], session["adjustment_trigger"]]
            lines.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in cells) + " |")
    for title, key in [("Small tweaks", "small_tweaks"), ("Constraints checked", "constraints_checked"),
                       ("Unresolved disagreements", "unresolved_disagreements"), ("Missing information", "missing_information")]:
        lines += ["", f"## {title}", ""] + [f"- {text}" for text in result[key]]
    lines += ["", "This proposal passed the workflow's independent model audit and structural checks; that is not physiological validation. The workflow processed curated evidence through Codex using your ChatGPT login and saved local reports. No Garmin workout or calendar entry was changed.", ""]
    return "\n".join(lines)


def run_analysis(evidence_path: Path, goal: dict, run_dir: Path,
                 concurrency: int = 3, model: str | None = None) -> dict:
    if not 1 <= concurrency <= 3:
        raise ValueError("Use between one and three concurrent analysts")
    goal = Goal.model_validate(goal).model_dump(mode="json")
    evidence = read_json(evidence_path)
    if not isinstance(evidence, dict):
        raise ValueError("Evidence must be an object")
    coverage = evidence.get("coverage", {})
    boundaries = coverage.get("boundaries", {}) if isinstance(coverage, dict) else {}
    cutoff_text = boundaries.get("end") or evidence.get("boundaries", {}).get("end")
    if not cutoff_text:
        raise ValueError("Evidence must identify its collection cutoff date")
    cutoff = date.fromisoformat(cutoff_text)
    check_codex()
    root = ensure_private_dir(run_dir)
    capacity_workspace = ensure_private_dir(root / "inputs" / "capacity")
    markdown = Path(evidence_path).with_suffix(".md")
    markdown_text = markdown.read_text() if markdown.exists() else "Read evidence.json and athlete_context.json.\n"
    athlete_context = {"athlete_observations": goal["athlete_observations"],
                       "interpretation": "Athlete testimony, not independent device validation. Unknown symptoms are not an injury finding."}
    semantic_evidence = {key: value for key, value in evidence.items() if key != "generated_at"}
    protocol_path = Path(__file__).with_name("protocol.py")
    protocol_material = [PROTOCOL_VERSION, METHOD, ROLES, CAPACITY_PROMPT,
                         PLAN_CANDIDATE_PROMPTS, PLAN_SELECTION_PROMPT, RESEARCH_CONTEXT,
                         COACHING_QUALITY_CRITERIA,
                         AUDIT_PROMPT, REVISION_PROMPT, SPECIALIST_SCHEMA, REVIEW_SCHEMA,
                         CAPACITY_SCHEMA, FINAL_SCHEMA, FINAL_AUDIT_SCHEMA]
    fingerprint = hashlib.sha256((json.dumps(semantic_evidence, sort_keys=True)
                                 + json.dumps(goal, sort_keys=True) + markdown_text + str(model)
                                 + Path(__file__).read_text() + protocol_path.read_text()
                                 + json.dumps(protocol_material, sort_keys=True)).encode()).hexdigest()
    manifest_path = root / "run.json"
    previous = read_json(manifest_path) if manifest_path.exists() else None
    if previous and previous.get("fingerprint") != fingerprint:
        raise ValueError("Run inputs changed; choose a new run directory")
    write_json(capacity_workspace / "evidence.json", evidence)
    write_json(capacity_workspace / "athlete_context.json", athlete_context)
    write_json(capacity_workspace / "research_context.json", RESEARCH_CONTEXT)
    write_text(capacity_workspace / "evidence.md", markdown_text)
    # Prevent prior aggregate answers appearing in the first stage on resume.
    for filename in ("independent.json", "roundtable.json", "capacity.json", "goal.json"):
        (capacity_workspace / filename).unlink(missing_ok=True)
    for filename in ("final.json", "report.md"):
        (root / filename).unlink(missing_ok=True)
    state = {"schema_version": PROTOCOL_VERSION, "fingerprint": fingerprint, "status": "running",
             "started_at": previous.get("started_at") if previous else _now(),
             "model": model or "Codex CLI default", "stages": {}, "completed_calls": 0,
             "new_calls_this_attempt": 0, "audit_status": "pending",
             "privacy": "Curated evidence and observations go to Codex. Goal/constraints are supplied only after capacity synthesis. Local files and read-only execution are not OS filesystem isolation."}
    lock = threading.Lock()

    def record(stage, role, status):
        with lock:
            state["stages"].setdefault(stage, {})[role] = status
            state["updated_at"] = _now()
            state["completed_calls"] = sum(v == "complete" for roles in state["stages"].values() for v in roles.values())
            write_json(manifest_path, state)

    def work(stage, role, prompt, schema, workspace, dependencies=()):
        folder = ensure_private_dir(root / stage)
        output = folder / f"{role}.json"
        meta_path = folder / f"{role}.cache.json"
        dependency_text = "".join(json.dumps(read_json(workspace / name), sort_keys=True) for name in dependencies)
        cache_key = hashlib.sha256((fingerprint + prompt + json.dumps(schema, sort_keys=True)
                                   + dependency_text).encode()).hexdigest()

        def validate_answer(result):
            jsonschema.validate(result, schema)
            if stage in {"independent", "roundtable"} and result.get("role") != role:
                raise ValueError("Analyst returned an incorrect role identifier")

        if output.exists() and meta_path.exists():
            try:
                if read_json(meta_path).get("cache_key") != cache_key:
                    raise ValueError("Cache dependencies changed")
                result = read_json(output)
                validate_answer(result)
                record(stage, role, "complete")
                return role, result
            except (ValueError, jsonschema.ValidationError):
                pass
        record(stage, role, "running")
        with lock:
            state["new_calls_this_attempt"] += 1
        try:
            result = invoke_codex(prompt, schema, output, workspace, model)
            validate_answer(result)
        except Exception:
            meta_path.unlink(missing_ok=True)
            record(stage, role, "invalid")
            raise
        write_json(meta_path, {"cache_key": cache_key})
        record(stage, role, "complete")
        return role, result

    def fanout(stage, make_prompt, schema, dependencies=()):
        answers = {}
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(work, stage, role, make_prompt(role), schema,
                                       capacity_workspace, dependencies): role for role in ROLES}
            for future in as_completed(futures):
                role, value = future.result()
                answers[role] = value
        return answers

    write_json(manifest_path, state)
    try:
        first = fanout("independent", lambda role: METHOD + f"\nYour role ID is {role}.\n{ROLES[role]}\n"
                       "This is the goal-blind capacity stage. Read evidence.md, evidence.json and athlete_context.json. "
                       "The objective and constraints are intentionally withheld. Assess what current records establish, "
                       "not a desired finish time. Return 3–6 consequential findings, missing data and questions. "
                       "Do not read other analysts' answers or files outside this working directory. "
                       "Recommend evidence-gathering priorities, not a goal-specific training schedule.", SPECIALIST_SCHEMA)
        write_json(capacity_workspace / "independent.json", first)
        second = fanout("roundtable", lambda role: METHOD + f"\nYour role ID is {role}.\n{ROLES[role]}\n"
                        "This is still goal-blind. Read independent.json, evidence.json and athlete_context.json. "
                        "Cross-examine the most consequential claims against source observations. Separate repeated "
                        "interpretation of shared data from corroboration. Preserve unresolved disagreements. "
                        "Do not infer an objective or prescribe a goal-specific schedule.", REVIEW_SCHEMA, ("independent.json",))
        write_json(capacity_workspace / "roundtable.json", second)
        _, capacity = work("capacity", "lead", METHOD + CAPACITY_PROMPT, CAPACITY_SCHEMA,
                           capacity_workspace, ("independent.json", "roundtable.json"))
        write_json(root / "capacity.json", capacity)
        # Only now introduce aspirations and constraints to a separate planning workspace.
        planning = ensure_private_dir(root / "inputs" / "planning")
        shared_names = ("evidence.json", "athlete_context.json", "research_context.json",
                        "independent.json", "roundtable.json")
        for name in shared_names:
            write_json(planning / name, read_json(capacity_workspace / name))
        write_text(planning / "evidence.md", markdown_text)
        write_json(planning / "goal.json", goal)
        write_json(planning / "capacity.json", capacity)
        for name in ("draft.json", "audit.json", "validation.json", "planning_candidates.json"):
            (planning / name).unlink(missing_ok=True)
        # Separate inputs prevent candidates from being shown one another's proposals.
        # This is evidence separation, not an operating-system isolation boundary.
        candidate_inputs = {}
        for candidate_id in PLAN_CANDIDATE_PROMPTS:
            folder = ensure_private_dir(root / "inputs" / "candidates" / candidate_id)
            for name in shared_names + ("goal.json", "capacity.json"):
                write_json(folder / name, read_json(planning / name))
            write_text(folder / "evidence.md", markdown_text)
            for name in ("draft.json", "audit.json", "planning_candidates.json"):
                (folder / name).unlink(missing_ok=True)
            candidate_inputs[candidate_id] = folder
        candidates = {}
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(
                work, "planning_candidates", candidate_id, METHOD + candidate_prompt,
                FINAL_SCHEMA, candidate_inputs[candidate_id],
                ("independent.json", "roundtable.json", "capacity.json"),
            ) for candidate_id, candidate_prompt in PLAN_CANDIDATE_PROMPTS.items()]
            for future in as_completed(futures):
                candidate_id, proposal = future.result()
                errors = []
                try:
                    validate_final(proposal, goal, cutoff)
                except (ValueError, jsonschema.ValidationError) as exc:
                    errors.append(str(exc)[:4000])
                candidates[candidate_id] = {
                    "plan": proposal,
                    "structural_validation": {"passed": not errors, "errors": errors},
                }
        write_json(root / "planning_candidates.json", candidates)
        write_json(planning / "planning_candidates.json", candidates)
        _, draft = work("planning", "lead", METHOD + PLAN_SELECTION_PROMPT, FINAL_SCHEMA, planning,
                        ("independent.json", "roundtable.json", "capacity.json",
                         "planning_candidates.json"))
        validation_instruction = (
            "\nRead validation.json. It contains deterministic checks of this exact draft. "
            "Every listed error must be corrected before publication, even if an earlier "
            "model audit says pass. These checks do not replace the evidence audit. "
        )
        for attempt in range(3):
            write_json(planning / "draft.json", draft)
            # Keep schema-valid drafts available for bounded repair instead of
            # aborting before the auditor or reviser can see numerical defects.
            validation_errors = []
            try:
                validate_final(draft, goal, cutoff)
                compared = {option["option_id"] for option in draft["planning_comparison"]["options_considered"]}
                if not set(PLAN_CANDIDATE_PROMPTS) <= compared:
                    raise ValueError("Selected plan must compare both independently generated candidates")
            except (ValueError, jsonschema.ValidationError) as exc:
                validation_errors.append(str(exc)[:4000])
            validation = {"passed": not validation_errors, "errors": validation_errors,
                          "basis": "Deterministic validate_final checks; a pass is not physiological validation."}
            write_json(planning / "validation.json", validation)
            audit_folder = ensure_private_dir(root / "audit")
            write_json(audit_folder / f"reviewer-{attempt}.validation.json", validation)
            state["deterministic_validation_status"] = "fail" if validation_errors else "pass"
            _, audit = work("audit", f"reviewer-{attempt}", METHOD + AUDIT_PROMPT + validation_instruction
                            + "If errors are present, do not return a passing verdict; identify the required corrections.",
                            FINAL_AUDIT_SCHEMA, planning, ("capacity.json", "draft.json", "validation.json",
                                                         "planning_candidates.json"))
            # Keep the provider response intact in audit/reviewer-N.json, while
            # recording deterministic publication-gate failures in audit.json.
            for error in coaching_audit_errors(audit):
                if error not in audit["required_changes"]:
                    audit["required_changes"].append(error)
            write_json(root / "audit.json", audit)
            passed = (audit["verdict"] == "pass" and not audit["blockers"]
                      and not audit["required_changes"]
                      and not any(check["result"] == "fail" for check in audit["checks"])
                      and not validation_errors)
            state["provider_audit_verdict"] = audit["verdict"]
            state["audit_status"] = "pass" if passed else "blocked" if audit["verdict"] == "blocked" else "revise"
            if passed:
                validate_final(draft, goal, cutoff)
                report = render_report(draft)
                write_json(root / "final.json", draft)
                write_text(root / "report.md", report)
                state["status"] = "complete"
                state["finished_at"] = _now()
                write_json(manifest_path, state)
                return draft
            if attempt == 2:
                raise RuntimeError("Independent audit did not pass after two revisions; inspect the private drafts and audit. No final plan was published.")
            write_json(planning / "audit.json", audit)
            _, draft = work("revision", f"lead-{attempt + 1}", METHOD + REVISION_PROMPT + validation_instruction
                            + "Repair every deterministic error as well as the audit findings. Recompute all affected totals and deltas.",
                            FINAL_SCHEMA, planning, ("capacity.json", "draft.json", "audit.json", "validation.json",
                                                   "planning_candidates.json"))
        raise RuntimeError("Analysis ended without an audited proposal")
    except Exception:
        for name in ("final.json", "report.md"):
            (root / name).unlink(missing_ok=True)
        state["status"] = "failed"
        state["error"] = "Analysis did not complete or its proposed plan failed validation/audit. Private logs retain details."
        state["updated_at"] = _now()
        write_json(manifest_path, state)
        raise
