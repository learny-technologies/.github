#!/usr/bin/env python3
"""Validate a repository automation manifest and its local file references."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml


class ValidationFailure(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--repository-root", type=Path)
    return parser.parse_args()


def default_schema() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "schemas"
        / "repository-automation-v1alpha1.schema.json"
    )


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationFailure(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationFailure(f"{path} must contain a YAML object")
    return value


def load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailure(f"cannot read schema {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationFailure(f"{path} must contain a JSON object")
    return value


def unique_ids(items: list[dict[str, Any]], label: str) -> None:
    identifiers = [str(item["id"]) for item in items]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        raise ValidationFailure(f"duplicate {label} ids: {', '.join(duplicates)}")


def validate_semantics(document: dict[str, Any], repository_root: Path | None) -> None:
    scopes = document["localValidation"]["scopes"]
    artifacts = document["artifacts"]
    pipelines = document["delivery"]["pipelines"]
    unique_ids(scopes, "local validation scope")
    unique_ids(artifacts, "artifact")
    unique_ids(pipelines, "delivery pipeline")

    artifact_ids = {str(item["id"]) for item in artifacts}
    for pipeline in pipelines:
        unknown = sorted(set(pipeline["components"]) - artifact_ids)
        if unknown:
            raise ValidationFailure(
                f"delivery pipeline {pipeline['id']} references unknown artifacts: "
                + ", ".join(unknown)
            )

    profiles = set(document["metadata"]["profiles"])
    runtime_profiles = {"service", "web-runtime", "platform-service"}
    if artifacts and not profiles.intersection(runtime_profiles):
        raise ValidationFailure("OCI artifacts require a runtime repository profile")
    if pipelines and not artifacts:
        raise ValidationFailure("delivery pipelines require at least one OCI artifact")
    if profiles == {"client-local"} and artifacts:
        raise ValidationFailure(
            "client-local repositories cannot publish runtime OCI artifacts"
        )

    if repository_root is None:
        return
    root = repository_root.resolve()
    manifest_repository = document["metadata"]["repository"]
    if "/" not in manifest_repository:
        raise ValidationFailure("metadata.repository must use owner/name")
    for artifact in artifacts:
        for field in ("context", "dockerfile"):
            candidate = (root / artifact[field]).resolve()
            if not candidate.is_relative_to(root) or not candidate.exists():
                raise ValidationFailure(
                    f"artifact {artifact['id']} {field} does not exist: {artifact[field]}"
                )
    for pipeline in pipelines:
        candidate = (root / pipeline["executor"]).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise ValidationFailure(
                f"delivery pipeline {pipeline['id']} executor does not exist: "
                f"{pipeline['executor']}"
            )


def validate(
    manifest: Path,
    *,
    schema: Path | None = None,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    document = load_yaml(manifest)
    schema_document = load_schema(schema or default_schema())
    try:
        jsonschema.Draft202012Validator(schema_document).validate(document)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "<root>"
        raise ValidationFailure(f"{manifest}:{location}: {exc.message}") from exc
    validate_semantics(document, repository_root)
    return document


def main() -> int:
    args = parse_args()
    try:
        document = validate(
            args.manifest.resolve(),
            schema=args.schema.resolve() if args.schema else None,
            repository_root=args.repository_root.resolve()
            if args.repository_root
            else None,
        )
    except ValidationFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"valid: {document['metadata']['repository']} "
        f"({', '.join(document['metadata']['profiles'])})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
