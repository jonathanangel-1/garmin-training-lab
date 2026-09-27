"""Nine independent Codex reviews, cross-examination, then one checked proposal."""

from __future__ import annotations

import hashlib
import json
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
from .protocol import FINAL_SCHEMA, METHOD, REVIEW_SCHEMA, ROLES, SPECIALIST_SCHEMA


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
    target = Goal.model_validate(goal)
    horizon = min(target.race_date or cutoff + timedelta(days=28), cutoff + timedelta(days=84))
    stressors = {}
    seen = set()
    for week in result["weekly_plan"]:
        week_start = date.fromisoformat(week["week_start"])
        if week_start.weekday() != 0:
            raise ValueError("Plan weeks must begin on Monday")
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


def _clock(seconds):
    if seconds is None:
        return "unavailable"
    seconds = round(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def render_report(result: dict) -> str:
    lines = ["# Training assessment and proposed plan", "", result["summary"], "",
             f"**Goal assessment:** {result['goal_assessment']} · **Confidence:** {result['confidence']}", ""]
    for key, value in result["current_status"].items():
        lines += [f"**{key.replace('_', ' ').title()}:** {value}", ""]
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
    lines += ["", "This is a proposal. No Garmin workout or calendar entry was changed.", ""]
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
    workspace = ensure_private_dir(root / "inputs")
    markdown = Path(evidence_path).with_suffix(".md")
    markdown_text = markdown.read_text() if markdown.exists() else "Read evidence.json and goal.json.\n"
    semantic_evidence = {key: value for key, value in evidence.items() if key != "generated_at"}
    fingerprint = hashlib.sha256((json.dumps(semantic_evidence, sort_keys=True) + json.dumps(goal, sort_keys=True) + markdown_text
                                 + str(model) + Path(__file__).read_text() + METHOD + str(ROLES)
                                 + json.dumps([SPECIALIST_SCHEMA, REVIEW_SCHEMA, FINAL_SCHEMA], sort_keys=True)).encode()).hexdigest()
    manifest_path = root / "run.json"
    previous = read_json(manifest_path) if manifest_path.exists() else None
    if previous and previous.get("fingerprint") != fingerprint:
        raise ValueError("Run inputs changed; choose a new run directory")
    write_json(workspace / "evidence.json", evidence)
    write_json(workspace / "goal.json", goal)
    write_text(workspace / "evidence.md", markdown_text)
    # Only a newly verified successful run may advertise a final proposal.
    (root / "final.json").unlink(missing_ok=True)
    (root / "report.md").unlink(missing_ok=True)
    state = {"schema_version": 1, "fingerprint": fingerprint, "status": "running",
             "started_at": previous.get("started_at") if previous else _now(),
             "model": model or "Codex CLI default", "stages": {}, "completed_calls": 0,
             "privacy": "Curated evidence and goal are sent to Codex using your ChatGPT login. Raw Garmin credentials are not supplied."}
    lock = threading.Lock()

    def record(stage, role, status):
        with lock:
            state["stages"].setdefault(stage, {})[role] = status
            state["updated_at"] = _now()
            state["completed_calls"] = sum(v == "complete" for roles in state["stages"].values() for v in roles.values())
            write_json(manifest_path, state)

    def work(stage, role, prompt, schema):
        folder = ensure_private_dir(root / stage)
        output = folder / f"{role}.json"
        meta_path = folder / f"{role}.cache.json"
        dependencies = ""
        for dependency in (["independent.json"] if stage == "roundtable" else
                           ["independent.json", "roundtable.json"] if stage == "synthesis" else []):
            dependencies += json.dumps(read_json(workspace / dependency), sort_keys=True)
        cache_key = hashlib.sha256((fingerprint + prompt + json.dumps(schema, sort_keys=True)
                                   + dependencies).encode()).hexdigest()
        # Resume only under an identical input fingerprint and validated output.
        if output.exists() and meta_path.exists():
            try:
                if read_json(meta_path).get("cache_key") != cache_key:
                    raise ValueError("Cache dependencies changed")
                result = read_json(output)
                jsonschema.validate(result, schema)
                if stage != "synthesis" and result.get("role") != role:
                    raise ValueError("Incorrect cached role")
                record(stage, role, "complete")
                return role, result
            except (ValueError, jsonschema.ValidationError):
                pass
        record(stage, role, "running")
        result = invoke_codex(prompt, schema, output, workspace, model)
        if stage != "synthesis" and result.get("role") != role:
            raise ValueError("Analyst returned an incorrect role identifier")
        write_json(meta_path, {"cache_key": cache_key})
        record(stage, role, "complete")
        return role, result

    def fanout(stage, make_prompt, schema):
        answers = {}
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(work, stage, role, make_prompt(role), schema): role for role in ROLES}
            for future in as_completed(futures):
                role, value = future.result()
                answers[role] = value
        return answers

    write_json(manifest_path, state)
    try:
        first = fanout("independent", lambda role: METHOD + f"\nYour role ID is {role}.\n{ROLES[role]}\n"
                       "Read evidence.md, evidence.json, and goal.json. Return 3–6 consequential findings with evidence, missing data, and questions for the other specialists. Do not read other analysts' answers at this stage.", SPECIALIST_SCHEMA)
        write_json(workspace / "independent.json", first)
        second = fanout("roundtable", lambda role: METHOD + f"\nYour role ID is {role}.\n{ROLES[role]}\n"
                        "Read independent.json, evidence.json, and goal.json. Cross-examine the other specialists' most consequential claims. Check source observations, overlapping evidence, competing explanations and proposals. Revise your own findings where evidence warrants it. Preserve unresolved disagreements. Do not force consensus.", REVIEW_SCHEMA)
        write_json(workspace / "roundtable.json", second)
        synthesis_prompt = METHOD + """\nYou are the lead reviewer. Read goal.json, evidence.json,
independent.json and roundtable.json. Produce one integrated assessment and an actionable proposed
running plan. Resolve disagreements using underlying observations, not vote counts. State what is
unknown. Distinguish overall fitness from marathon durability and current recovery. Use two finish-time
methods only if their inputs are valid; mark ineligible methods unavailable with null times. Never treat
controlled training as an all-out VDOT race or a preset HR as validated marathon effort. Give scenarios
with assumptions, not an invented probability. If evidence cannot support a number, say so.
Build a dated plan from the day AFTER the dataset cutoff to the race date, or 4 weeks when no date was
provided (maximum 12 weeks at a time for distant goals). Week starts must be Mondays. Include rest
days, numeric distance or duration when justified, and concrete triggers to reduce/skip sessions.
Do not schedule new workouts in the past. Keep stressors at or below the user's maximum including
long runs and races, and do not disguise hard sessions as easy. Preserve all explicit constraints.
If symptoms/current tolerance or evidence make a full prescription unjustifiable, keep the immediate
plan conservative and conditional, explicitly list information needed, and do not fabricate certainty.
Choose taper duration from actual history. Add at most 7 small tweaks. No unsolicited cadence targets
or novel shoes/fueling prescriptions unsupported by the athlete's records. Cite dated evidence for
material conclusions and training adjustments. This is a proposal; no Garmin writes occur.
"""
        _, final = work("synthesis", "lead", synthesis_prompt, FINAL_SCHEMA)
        try:
            validate_final(final, goal, cutoff)
        except (ValueError, jsonschema.ValidationError):
            # Keep the rejected answer for audit, but regenerate it on resume.
            (root / "synthesis" / "lead.cache.json").unlink(missing_ok=True)
            record("synthesis", "lead", "invalid")
            raise
        write_json(root / "final.json", final)
        write_text(root / "report.md", render_report(final))
        state["status"] = "complete"
        state["finished_at"] = _now()
        write_json(manifest_path, state)
        return final
    except Exception:
        (root / "final.json").unlink(missing_ok=True)
        (root / "report.md").unlink(missing_ok=True)
        state["status"] = "failed"
        state["error"] = "Analysis did not complete or its proposed plan failed validation. Private logs retain details."
        state["updated_at"] = _now()
        write_json(manifest_path, state)
        raise
