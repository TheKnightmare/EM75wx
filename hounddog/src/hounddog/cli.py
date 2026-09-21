from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

from .db import claim_detail, connect, queue, transition
from .engine import load_settings, run_once
from .formatters import evidence_summary, ghostnet_message


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="hounddog", description="Provenance-first GhostNet claim collector")
    command.add_argument("--config", default="config.toml", help="TOML configuration path")
    sub = command.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create config.toml from the example")
    sub.add_parser("run-once", help="Poll enabled sources once")
    queue_parser = sub.add_parser("queue", help="List the human review queue")
    queue_parser.add_argument("--min-score", type=int, default=0)
    queue_parser.add_argument("--limit", type=int, default=50)
    show_parser = sub.add_parser("show", help="Show provenance for one claim")
    show_parser.add_argument("claim_id", type=int)
    format_parser = sub.add_parser("format", help="Format one claim for GhostNet review")
    format_parser.add_argument("claim_id", type=int)
    mark_parser = sub.add_parser("mark", help="Move a claim through the review workflow")
    mark_parser.add_argument("claim_id", type=int)
    mark_parser.add_argument("status", choices=["correlating", "review", "tx_candidate", "sent", "rejected"])
    return command


def _settings_or_die(path: str):
    try:
        return load_settings(path)
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}; run `hounddog init` first")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "init":
        target = Path(args.config)
        example = Path(__file__).resolve().parents[2] / "config.example.toml"
        if target.exists():
            print(f"refusing to overwrite {target}", file=sys.stderr)
            return 2
        shutil.copyfile(example, target)
        print(f"created {target}; set a real contact in collection.user_agent")
        return 0
    settings = _settings_or_die(args.config)
    if args.command == "run-once":
        result = run_once(settings)
        print(json.dumps(result, indent=2))
        return 1 if result["sources_ok"] == 0 and result["sources_failed"] else 0
    with connect(settings.database) as connection:
        if args.command == "queue":
            rows = queue(connection, args.min_score, args.limit)
            print("ID  SIG CONF STATUS        LABEL            FAMILIES TITLE")
            for row in rows:
                print(f"{row['id']:<3} {row['significance_score']:<3} {row['confidence_score']:<4} {row['status']:<13} {row['confidence_label']:<16} {row['independent_families']:<8} {row['title'][:90]}")
        elif args.command == "show":
            claim, observations = claim_detail(connection, args.claim_id)
            print(evidence_summary(claim, observations))
        elif args.command == "format":
            claim, observations = claim_detail(connection, args.claim_id)
            print(ghostnet_message(claim, observations, callsign=settings.callsign, network=settings.network, max_chars=settings.max_message_chars))
        elif args.command == "mark":
            transition(connection, args.claim_id, args.status.upper())
            print(f"claim {args.claim_id} -> {args.status.upper()}")
    return 0

