"""Command-line setup, private collection, goal input, and local service."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from . import __version__
from .core import api_token, ensure_private_dir, read_json, validate_id, write_json
from .goals import Goal, parse_finish_time


def parser():
    root = argparse.ArgumentParser(description="Garmin evidence and nine-analyst training review")
    root.add_argument("--state-dir", type=Path, default=Path(os.environ.get("GTL_STATE_DIR", ".local")),
                      help="Private local data directory (default .local; use one per athlete)")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Connect Garmin interactively")
    commands.add_parser("status", help="Check local setup without displaying credentials")
    goal = commands.add_parser("goal", help="Write your goal and constraints")
    goal.add_argument("--description", required=True)
    goal.add_argument("--race-date", type=date.fromisoformat)
    goal.add_argument("--distance-km", type=float)
    goal.add_argument("--target-time", type=parse_finish_time, help="HH:MM:SS")
    goal.add_argument("--max-stressors", type=int)
    goal.add_argument("--constraint", action="append", default=[])
    goal.add_argument("--notes-file", type=Path)
    sync = commands.add_parser("sync", help="Read Garmin into a resumable private snapshot")
    sync.add_argument("--start", type=date.fromisoformat, default=date.today() - timedelta(days=183))
    sync.add_argument("--end", type=date.fromisoformat, default=date.today())
    sync.add_argument("--detail-start", type=date.fromisoformat)
    sync.add_argument("--max-details", type=int, default=1000)
    sync.add_argument("--snapshot", default=date.today().isoformat())
    evidence = commands.add_parser("evidence", help="Prepare and inspect the curated AI input")
    evidence.add_argument("--snapshot", required=True)
    analysis = commands.add_parser("analyze", help="Run nine analysts, their roundtable, and synthesis through Codex")
    analysis.add_argument("--snapshot", required=True)
    analysis.add_argument("--run", default=date.today().isoformat())
    analysis.add_argument("--concurrency", type=int, choices=[1, 2, 3], default=3)
    analysis.add_argument("--model", help="Optional explicit Codex model; otherwise CLI default")
    analysis.add_argument("--share-with-codex", action="store_true",
                          help="Send curated health/activity evidence and goal to your Codex account for analysis")
    serve = commands.add_parser("serve", help="Start the authenticated loopback-only API")
    serve.add_argument("--port", type=int, default=8765)
    commands.add_parser("api-token", help="Display the local API bearer token in your terminal")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    state = ensure_private_dir(args.state_dir)
    try:
        if args.command == "login":
            from .auth import login
            login(state)
        elif args.command == "status":
            import shutil
            print(json.dumps({"version": __version__,
                              "garmin_tokens_present": (state / "garmin-tokens" / "garmin_tokens.json").is_file(),
                              "goal_present": (state / "goal.json").is_file(),
                              "codex_installed": shutil.which("codex") is not None,
                              "private_state": str(state)}, indent=2))
        elif args.command == "goal":
            value = Goal(description=args.description, race_date=args.race_date,
                         distance_km=args.distance_km, target_time_seconds=args.target_time,
                         max_stressors_per_week=args.max_stressors, constraints=args.constraint,
                         notes=args.notes_file.read_text() if args.notes_file else "")
            write_json(state / "goal.json", value.model_dump(mode="json"))
            print("Goal saved privately.")
        elif args.command == "sync":
            from .auth import load_client
            from .collector import collect_snapshot
            destination = state / "snapshots" / validate_id(args.snapshot)
            print("Reading Garmin history. Progress is saved in the snapshot manifest.", flush=True)
            result = collect_snapshot(load_client(state), destination, args.start, args.end,
                                      args.detail_start, args.max_details)
            print(json.dumps({"snapshot": args.snapshot, "status": result["status"], "counts": result["counts"]}, indent=2))
            if result["status"] == "interrupted":
                return 1
        elif args.command in {"evidence", "analyze"}:
            from .evidence import build_evidence
            identifier = validate_id(args.snapshot)
            snapshot_dir = state / "snapshots" / identifier
            manifest = read_json(snapshot_dir / "manifest.json")
            if manifest.get("status") not in {"complete", "partial"}:
                raise ValueError("Collection must finish before evidence analysis")
            evidence_dir = state / "evidence" / identifier
            build_evidence(snapshot_dir, evidence_dir)
            if args.command == "evidence":
                print(f"Curated evidence: {evidence_dir / 'evidence.md'}")
            else:
                from .roundtable import run_analysis
                if not args.share_with_codex:
                    raise ValueError("Review the evidence, then add --share-with-codex to authorize AI processing")
                goal_value = read_json(state / "goal.json")
                run_dir = state / "runs" / validate_id(args.run)
                print("Starting 9 independent analyses, 9 cross-reviews, and final synthesis. This uses your Codex allowance.", flush=True)
                run_analysis(evidence_dir / "evidence.json", goal_value, run_dir, args.concurrency, args.model)
                print(f"Assessment and proposed plan: {run_dir / 'report.md'}")
        elif args.command == "serve":
            import uvicorn

            from .api import create_app
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            api_token(state)
            print(f"Local API: http://127.0.0.1:{args.port}. Use gtl api-token for your bearer token.")
            uvicorn.run(create_app(state), host="127.0.0.1", port=args.port, workers=1,
                        access_log=False, log_level="warning")
        elif args.command == "api-token":
            if not sys.stdout.isatty():
                raise ValueError("API token display requires an interactive terminal")
            print(api_token(state))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Stopped. Completed collection calls remain cached.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not emit remote exception bodies, tokens or user inputs into public logs.
        from pydantic import ValidationError
        if isinstance(exc, ValidationError):
            message = "Invalid goal. Check the documented fields and limits."
        elif type(exc) in {ValueError, RuntimeError}:
            message = str(exc)
        else:
            message = f"Operation failed ({type(exc).__name__}); inspect the private status files."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
