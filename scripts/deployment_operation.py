#!/usr/bin/env python3
"""Claim and fail a Control Plane deployment operation before executor selection."""

from __future__ import annotations

import argparse
import contextlib
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
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
PLAN_PATH = Path(".delivery-plan.json")
STATE_PATH = Path(".delivery-state.json")


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
    audience = required_env("CONTROL_PLANE_DEPLOYMENT_AUDIENCE")
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
    return request_json(
        f"{required_env('CONTROL_PLANE_URL').rstrip('/')}/v1/{path.lstrip('/')}",
        method="POST",
        headers={"Authorization": f"Bearer {oidc_token()}"},
        payload=payload,
    )


def write_output(name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise OperationError(f"{name} output contains a newline")
    with Path(required_env("GITHUB_OUTPUT")).open("a") as output:
        output.write(f"{name}={value}\n")


def run_url() -> str:
    return (
        f"{required_env('GITHUB_SERVER_URL')}/{required_env('GITHUB_REPOSITORY')}"
        f"/actions/runs/{required_env('GITHUB_RUN_ID')}"
    )


def validated_plan(
    plan: object, operation_id: str
) -> tuple[dict[str, Any], str, str, str]:
    operation = plan.get("operation", {}) if isinstance(plan, dict) else {}
    release = plan.get("release", {}) if isinstance(plan, dict) else {}
    returned_operation_id = operation.get("id")
    pipeline_id = operation.get("pipeline_id")
    environment_id = operation.get("environment_id")
    source_revision = release.get("source_revision")
    if returned_operation_id != operation_id:
        raise OperationError("Control Plane returned a different deployment operation")
    if not isinstance(pipeline_id, str) or IDENTIFIER.fullmatch(pipeline_id) is None:
        raise OperationError("Control Plane returned an invalid pipeline")
    if (
        not isinstance(environment_id, str)
        or IDENTIFIER.fullmatch(environment_id) is None
    ):
        raise OperationError("Control Plane returned an invalid environment")
    if (
        not isinstance(source_revision, str)
        or SOURCE_SHA.fullmatch(source_revision) is None
    ):
        raise OperationError("Control Plane returned an invalid source revision")
    assert isinstance(plan, dict)
    return plan, pipeline_id, environment_id, source_revision


def store_plan(plan: dict[str, Any]) -> None:
    PLAN_PATH.write_text(json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n")
    STATE_PATH.write_text('{"completed":false,"sequence":0}\n')


def claim(
    operation_id: str,
    expected_pipeline: str,
    expected_environment: str,
) -> None:
    plan, pipeline_id, environment_id, source_revision = validated_plan(
        control_request(f"deployments/operations/{operation_id}/claim", {}),
        operation_id,
    )
    if pipeline_id != expected_pipeline or environment_id != expected_environment:
        raise OperationError(
            "Control Plane authorization does not match the requested pipeline and environment"
        )
    store_plan(plan)
    write_output("pipeline_id", pipeline_id)
    write_output("environment", environment_id)
    write_output("source_sha", source_revision)


def sequence() -> int:
    try:
        return int(json.loads(STATE_PATH.read_text()).get("sequence", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def fail(operation_id: str, message: str) -> None:
    if not PLAN_PATH.exists():
        return
    with contextlib.suppress(OSError, json.JSONDecodeError, AttributeError):
        if json.loads(STATE_PATH.read_text()).get("completed") is True:
            return
    control_request(
        f"deployments/operations/{operation_id}/complete",
        {
            "sequence": sequence() + 1,
            "outcome": "failed",
            "github_run_url": run_url(),
            "provenance_verified": False,
            "rollback_eligible": False,
            "evidence": {"failure_stage": "workflow_failed"},
            "failure_code": "workflow_failed",
            "failure_message": message[:500],
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    claim_parser = subcommands.add_parser("claim")
    claim_parser.add_argument("--operation-id", required=True)
    claim_parser.add_argument("--expected-pipeline", required=True)
    claim_parser.add_argument("--expected-environment", required=True)
    fail_parser = subcommands.add_parser("fail")
    fail_parser.add_argument("--operation-id", required=True)
    fail_parser.add_argument("--message", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "claim":
            claim(
                args.operation_id,
                args.expected_pipeline,
                args.expected_environment,
            )
        else:
            fail(args.operation_id, args.message)
    except OperationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
