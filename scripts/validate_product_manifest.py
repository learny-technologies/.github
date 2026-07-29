#!/usr/bin/env python3
"""Validate deployment-critical invariants in a canonical product manifest."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


class ValidationFailure(RuntimeError):
    pass


def objects(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValidationFailure(f"{label} must be a list of objects")
    return value


def indexed(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValidationFailure(f"{label} id is required")
        if identifier in result:
            raise ValidationFailure(f"duplicate {label} id: {identifier}")
        result[identifier] = item
    return result


def validate(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationFailure(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValidationFailure("product manifest must contain a YAML object")
    if (
        document.get("apiVersion") != "platform.learny.technology/v1alpha1"
        or document.get("kind") != "Product"
    ):
        raise ValidationFailure("unsupported product manifest kind or API version")

    repositories = indexed(
        objects(document.get("repositories"), "repositories"), "repository"
    )
    invalid_roles = sorted(
        identifier
        for identifier, repository in repositories.items()
        if repository.get("role") not in {"source", "component-source"}
    )
    if invalid_roles:
        raise ValidationFailure(
            "repositories have unsupported roles: " + ", ".join(invalid_roles)
        )
    source_repositories = [
        identifier
        for identifier, repository in repositories.items()
        if repository.get("role") == "source"
    ]
    if len(source_repositories) != 1:
        raise ValidationFailure(
            "product manifest must have exactly one source repository"
        )

    components = indexed(objects(document.get("components"), "components"), "component")
    for component_id, component in components.items():
        repository_id = component.get("repository")
        if not isinstance(repository_id, str) or repository_id not in repositories:
            raise ValidationFailure(
                f"component {component_id} must reference a known repository"
            )
    environments = indexed(
        objects(document.get("environments"), "environments"), "environment"
    )
    delivery = document.get("delivery", {})
    if not isinstance(delivery, dict):
        raise ValidationFailure("delivery must be an object")
    pipelines = indexed(
        objects(delivery.get("pipelines", []), "delivery.pipelines"), "pipeline"
    )

    for pipeline_id, pipeline in pipelines.items():
        automation_revision = pipeline.get("automation_revision")
        if (
            not isinstance(automation_revision, str)
            or SOURCE_SHA.fullmatch(automation_revision) is None
        ):
            raise ValidationFailure(
                f"pipeline {pipeline_id} must pin a full automation revision"
            )
        repository_id = pipeline.get("repository")
        repository = repositories.get(repository_id)
        if repository is None:
            raise ValidationFailure(
                f"pipeline {pipeline_id} references unknown repository {repository_id}"
            )
        if pipeline.get("repository_id") != repository.get("repository_id"):
            raise ValidationFailure(
                f"pipeline {pipeline_id} repository ID does not match its repository"
            )
        pipeline_components = pipeline.get("components")
        pipeline_environments = pipeline.get("environments")
        if not isinstance(pipeline_components, list) or not pipeline_components:
            raise ValidationFailure(f"pipeline {pipeline_id} must declare components")
        if not isinstance(pipeline_environments, list) or not pipeline_environments:
            raise ValidationFailure(f"pipeline {pipeline_id} must declare environments")
        for component_id in pipeline_components:
            if component_id not in components:
                raise ValidationFailure(
                    f"pipeline {pipeline_id} references unknown component {component_id}"
                )
        for environment_id in pipeline_environments:
            environment = environments.get(environment_id)
            if environment is None:
                raise ValidationFailure(
                    f"pipeline {pipeline_id} references unknown environment {environment_id}"
                )
            if environment.get("state") != "managed":
                raise ValidationFailure(
                    f"pipeline {pipeline_id} references non-managed environment {environment_id}"
                )
            deployments = {
                deployment.get("component"): deployment
                for deployment in objects(
                    environment.get("deployments", []),
                    f"environment {environment_id} deployments",
                )
            }
            for component_id in pipeline_components:
                deployment = deployments.get(component_id)
                if deployment is None or deployment.get("state") != "managed":
                    raise ValidationFailure(
                        f"pipeline {pipeline_id} component {component_id} is not managed "
                        f"in environment {environment_id}"
                    )
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        document = validate(args.manifest)
    except ValidationFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"valid product manifest: {document['metadata']['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
