#!/usr/bin/env python3
"""Verify portable OCI provenance and SBOM evidence for an immutable image."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
OCI_DIGEST = re.compile(
    r"^ghcr\.io/learny-technologies/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)
REPOSITORY = re.compile(r"^learny-technologies/[a-z0-9._-]+$")
BUILDKIT_BUILD_TYPE = (
    "https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md"
)
LABELS_FORMAT = "{{json .Image.Config.Labels}}"
PROVENANCE_FORMAT = "{{json .Provenance.SLSA}}"
SBOM_VERSION_FORMAT = '{{index .SBOM.SPDX "spdxVersion"}}'


class VerificationError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--automation-revision", required=True)
    return parser.parse_args()


def inspect_value(image: str, format_template: str) -> str:
    completed = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            format_template,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise VerificationError("registry evidence is unavailable")
    return completed.stdout.strip()


def parse_object(value: str, name: str) -> dict[str, Any]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"registry {name} is invalid") from exc
    if not isinstance(document, dict):
        raise VerificationError(f"registry {name} is invalid")
    return document


def registry_evidence(
    image: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    labels = parse_object(inspect_value(image, LABELS_FORMAT), "labels")
    provenance = parse_object(
        inspect_value(image, PROVENANCE_FORMAT),
        "provenance",
    )
    sbom_version = inspect_value(image, SBOM_VERSION_FORMAT)
    return labels, provenance, sbom_version


def nested(document: dict[str, Any], *path: str) -> object:
    value: object = document
    for name in path:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    return value


def verify(
    *,
    image: str,
    repository: str,
    source_revision: str,
    automation_revision: str,
) -> None:
    if OCI_DIGEST.fullmatch(image) is None:
        raise VerificationError("image must use an immutable Learny GHCR digest")
    if REPOSITORY.fullmatch(repository) is None:
        raise VerificationError("repository is invalid")
    if SOURCE_SHA.fullmatch(source_revision) is None:
        raise VerificationError("source revision is invalid")
    if SOURCE_SHA.fullmatch(automation_revision) is None:
        raise VerificationError("automation revision is invalid")

    source_url = f"https://github.com/{repository}"
    labels, provenance, sbom_version = registry_evidence(image)
    if labels.get("org.opencontainers.image.revision") != source_revision:
        raise VerificationError(
            "registry provenance does not match source and automation revisions"
        )
    if labels.get("org.opencontainers.image.source") != source_url:
        raise VerificationError(
            "registry provenance does not match source and automation revisions"
        )
    if labels.get("io.learny.automation.revision") != automation_revision:
        raise VerificationError(
            "registry provenance does not match source and automation revisions"
        )
    if (
        nested(provenance, "buildDefinition", "buildType") != BUILDKIT_BUILD_TYPE
        or nested(
            provenance,
            "runDetails",
            "metadata",
            "buildkit_metadata",
            "vcs",
            "revision",
        )
        != source_revision
        or nested(
            provenance,
            "runDetails",
            "metadata",
            "buildkit_metadata",
            "vcs",
            "source",
        )
        != source_url
    ):
        raise VerificationError(
            "registry provenance does not match source and automation revisions"
        )
    builder = nested(provenance, "runDetails", "builder", "id")
    if not isinstance(builder, str) or not builder.startswith(
        f"{source_url}/actions/runs/"
    ):
        raise VerificationError(
            "registry provenance has an unexpected builder identity"
        )
    if not sbom_version.startswith("SPDX-"):
        raise VerificationError("registry SBOM evidence is unavailable")


def main() -> int:
    args = parse_args()
    try:
        verify(
            image=args.image,
            repository=args.repository,
            source_revision=args.source_revision,
            automation_revision=args.automation_revision,
        )
    except VerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
