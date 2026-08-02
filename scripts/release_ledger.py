#!/usr/bin/env python3
"""Create one append-only product deployment ledger record."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE = re.compile(r"^ghcr\.io/learny-technologies/.+@sha256:[0-9a-f]{64}$")


def record(plan: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    if result.get("status") != "succeeded" or result.get("health") != "healthy":
        raise ValueError("only healthy successful deployments enter the release ledger")
    source = plan.get("source_revision")
    images = plan.get("images")
    actor = plan.get("actor")
    if not isinstance(source, str) or SHA.fullmatch(source) is None:
        raise ValueError("ledger source revision is invalid")
    if not isinstance(images, dict) or any(
        not isinstance(image, str) or IMAGE.fullmatch(image) is None
        for image in images.values()
    ):
        raise ValueError("ledger image map is invalid")
    if not isinstance(actor, dict) or not isinstance(actor.get("id"), int):
        raise TypeError("ledger actor is invalid")
    return {
        "apiVersion": "delivery.learny.technology/v1",
        "kind": "DeploymentRecord",
        "metadata": {"operationId": plan["operation_id"]},
        "spec": {
            "project": plan["project_id"],
            "environment": plan["environment_id"],
            "pipeline": plan["pipeline_id"],
            "actorId": actor["id"],
            "actorLogin": actor["login"],
            "sourceRevision": source,
            "images": dict(sorted(images.items())),
            "definitionHash": plan["definition_hash"],
            "migrationHeads": plan["migration_heads"],
            "health": result["health"],
            "workflowUrl": plan["run_url"],
            "recordedAt": datetime.now(UTC).isoformat(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = record(
            json.loads(args.plan.read_text()),
            json.loads(args.result.read_text()),
        )
        if args.output.exists():
            raise ValueError("deployment ledger records are append-only")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(yaml.safe_dump(document, sort_keys=False))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
