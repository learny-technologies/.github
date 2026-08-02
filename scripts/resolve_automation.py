#!/usr/bin/env python3
"""Resolve one exact artifact or delivery executor from automation.yaml."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from validate_automation import ValidationFailure, validate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--artifact")
    selectors.add_argument("--pipeline")
    return parser.parse_args()


def write_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a") as output:
        for key, value in values.items():
            if "\r" in value:
                raise ValidationFailure(f"output {key} contains a carriage return")
            if "\n" not in value:
                output.write(f"{key}={value}\n")
                continue
            delimiter = f"LEARNY_{key.upper()}_EOF"
            if delimiter in value.splitlines():
                raise ValidationFailure(f"output {key} contains its output delimiter")
            output.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def main() -> int:
    args = parse_args()
    try:
        document = validate(
            args.manifest.resolve(),
            repository_root=args.repository_root.resolve(),
        )
        if args.artifact:
            artifact = next(
                (item for item in document["artifacts"] if item["id"] == args.artifact),
                None,
            )
            if artifact is None:
                raise ValidationFailure(f"unknown artifact: {args.artifact}")
            build_args = {
                **artifact.get("buildArgs", {}),
                "SOURCE_REVISION": os.environ.get("SOURCE_REVISION", ""),
            }
            if not build_args["SOURCE_REVISION"]:
                raise ValidationFailure(
                    "SOURCE_REVISION is required when resolving an artifact"
                )
            write_outputs(
                args.github_output,
                {
                    "image": artifact["image"],
                    "context": artifact["context"],
                    "dockerfile": artifact["dockerfile"],
                    "platforms": ",".join(artifact["platforms"]),
                    "build_args": "\n".join(
                        f"{key}={value}" for key, value in sorted(build_args.items())
                    ),
                },
            )
        else:
            pipeline = next(
                (
                    item
                    for item in document["delivery"]["pipelines"]
                    if item["id"] == args.pipeline
                ),
                None,
            )
            if pipeline is None:
                raise ValidationFailure(f"unknown delivery pipeline: {args.pipeline}")
            write_outputs(
                args.github_output,
                {
                    "executor": pipeline["executor"],
                    "definition": pipeline["definition"],
                    "components": json.dumps(
                        pipeline["components"], separators=(",", ":")
                    ),
                    "environments": json.dumps(
                        pipeline["environments"], separators=(",", ":")
                    ),
                },
            )
    except ValidationFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
