#!/usr/bin/env python3
"""Claim and complete a Control Plane OCI artifact publication operation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
OCI_DIGEST = re.compile(
    r"^ghcr\.io/learny-technologies/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)
STATE_PATH = Path(".artifact-operation.json")


class OperationError(RuntimeError):
    pass


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise OperationError(f"{name} is required")
    return value


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode()
        if payload is not None
        else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise OperationError(
            f"Control Plane request failed with HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise OperationError("Control Plane request failed") from exc
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise OperationError("Control Plane returned invalid JSON") from exc


def oidc_token() -> str:
    request_url = required_env("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = required_env("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    audience = required_env("CONTROL_PLANE_ARTIFACT_AUDIENCE")
    separator = "&" if "?" in request_url else "?"
    response = request_json(
        f"{request_url}{separator}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"Bearer {request_token}"},
    )
    token = response.get("value") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise OperationError("GitHub Actions OIDC token was not issued")
    return token


def control_request(path: str, payload: dict[str, object]) -> Any:
    base_url = required_env("CONTROL_PLANE_URL").rstrip("/")
    return request_json(
        f"{base_url}/v1/{path.lstrip('/')}",
        method="POST",
        headers={"Authorization": f"Bearer {oidc_token()}"},
        payload=payload,
    )


def write_output(name: str, value: str) -> None:
    output_path = Path(required_env("GITHUB_OUTPUT"))
    if "\n" in value or "\r" in value:
        raise OperationError(f"{name} output contains a newline")
    with output_path.open("a") as output:
        output.write(f"{name}={value}\n")


def run_url() -> str:
    return (
        f"{required_env('GITHUB_SERVER_URL')}/{required_env('GITHUB_REPOSITORY')}"
        f"/actions/runs/{required_env('GITHUB_RUN_ID')}"
    )


def claim(operation_id: str) -> None:
    plan = control_request(f"artifacts/operations/{operation_id}/claim", {})
    operation = plan.get("operation", {}) if isinstance(plan, dict) else {}
    source_revision = operation.get("source_revision")
    component_id = operation.get("component_id")
    if (
        not isinstance(source_revision, str)
        or SOURCE_SHA.fullmatch(source_revision) is None
    ):
        raise OperationError("Control Plane returned an invalid source revision")
    if not isinstance(component_id, str) or not component_id:
        raise OperationError("Control Plane returned an invalid component")
    STATE_PATH.write_text(
        json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n"
    )
    write_output("source_sha", source_revision)
    write_output("component_id", component_id)


def complete(operation_id: str, image: str, attestation_url: str) -> None:
    if OCI_DIGEST.fullmatch(image) is None:
        raise OperationError("artifact image must use an immutable GHCR digest")
    if not attestation_url.startswith("https://github.com/"):
        raise OperationError("artifact attestation URL is invalid")
    control_request(
        f"artifacts/operations/{operation_id}/complete",
        {
            "outcome": "succeeded",
            "image": image,
            "github_run_url": run_url(),
            "signature_verified": True,
            "provenance": {
                "builder": "github-actions",
                "repository": required_env("GITHUB_REPOSITORY"),
                "run_id": required_env("GITHUB_RUN_ID"),
                "attestation_url": attestation_url,
            },
        },
    )


def fail(operation_id: str, message: str) -> None:
    control_request(
        f"artifacts/operations/{operation_id}/complete",
        {
            "outcome": "failed",
            "github_run_url": run_url(),
            "signature_verified": False,
            "failure_code": "workflow_failed",
            "failure_message": message[:500],
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    claim_parser = subcommands.add_parser("claim")
    claim_parser.add_argument("--operation-id", required=True)
    complete_parser = subcommands.add_parser("complete")
    complete_parser.add_argument("--operation-id", required=True)
    complete_parser.add_argument("--image", required=True)
    complete_parser.add_argument("--attestation-url", required=True)
    fail_parser = subcommands.add_parser("fail")
    fail_parser.add_argument("--operation-id", required=True)
    fail_parser.add_argument("--message", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "claim":
            claim(args.operation_id)
        elif args.command == "complete":
            complete(args.operation_id, args.image, args.attestation_url)
        else:
            fail(args.operation_id, args.message)
    except OperationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
