#!/usr/bin/env python3
"""Render thin repository workflows from automation.yaml."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

from validate_automation import ValidationFailure, validate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--shared-ref", required=True)
    return parser.parse_args()


def validate_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValidationFailure("--shared-ref must be a full 40-character Git SHA")
    return value


def validation_workflow(shared_ref: str) -> str:
    return f"""name: Validate repository automation

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read

concurrency:
  group: source-gate-${{{{ github.event.pull_request.number }}}}
  cancel-in-progress: true

jobs:
  validate:
    uses: learny-technologies/.github/.github/workflows/reusable-validate.yml@{shared_ref}
    with:
      automation_revision: {shared_ref}
"""


def image_workflow(shared_ref: str) -> str:
    return f"""name: Publish OCI artifact
run-name: Publish ${{{{ inputs.component_id }}}} @ ${{{{ inputs.source_sha }}}}

on:
  workflow_dispatch:
    inputs:
      source_sha:
        description: Exact Git source revision
        required: true
        type: string
      component_id:
        description: Artifact component declared in automation.yaml
        required: true
        type: string
      execution_record_json:
        description: Immutable execution-record v3 reference
        required: true
        type: string

permissions:
  contents: read
  packages: write
  id-token: write

jobs:
  publish:
    uses: learny-technologies/.github/.github/workflows/reusable-oci-publish.yml@{shared_ref}
    with:
      source_sha: ${{{{ inputs.source_sha }}}}
      component_id: ${{{{ inputs.component_id }}}}
      execution_record_json: ${{{{ inputs.execution_record_json }}}}
      automation_revision: {shared_ref}
"""


def deploy_workflow(shared_ref: str) -> str:
    return f"""name: Deploy runtime artifact
run-name: Deploy ${{{{ inputs.pipeline_id }}}} to ${{{{ inputs.environment }}}} @ ${{{{ inputs.source_sha }}}}

on:
  workflow_dispatch:
    inputs:
      source_sha:
        description: Exact Git source revision
        required: true
        type: string
      environment:
        description: Target GitHub Environment
        required: true
        type: string
      pipeline_id:
        description: Delivery pipeline declared in automation.yaml
        required: true
        type: string
      images_json:
        description: Immutable component-to-digest map
        required: true
        type: string
      migration_heads_json:
        description: Exact migration heads
        required: false
        default: "[]"
        type: string
      execution_record_json:
        description: Immutable execution-record v3 reference
        required: true
        type: string
      expected_fingerprint:
        description: Confirmed immutable deployment plan fingerprint
        required: true
        type: string
      reason:
        description: Bounded deployment reason
        required: true
        type: string
      operation_type:
        description: Promotion or previous-healthy rollback
        required: false
        default: promotion
        type: choice
        options: [promotion, rollback]
      staging_evidence_json:
        description: Successful immutable staging evidence for production
        required: false
        default: "{{}}"
        type: string
      break_glass:
        description: Technical-owner production exception
        required: false
        default: false
        type: boolean

permissions:
  actions: read
  contents: read
  deployments: write
  packages: read

jobs:
  deploy:
    uses: learny-technologies/.github/.github/workflows/reusable-deploy.yml@{shared_ref}
    with:
      source_sha: ${{{{ inputs.source_sha }}}}
      delivery_revision: ${{{{ github.sha }}}}
      automation_revision: {shared_ref}
      environment: ${{{{ inputs.environment }}}}
      pipeline_id: ${{{{ inputs.pipeline_id }}}}
      images_json: ${{{{ inputs.images_json }}}}
      migration_heads_json: ${{{{ inputs.migration_heads_json }}}}
      execution_record_json: ${{{{ inputs.execution_record_json }}}}
      expected_fingerprint: ${{{{ inputs.expected_fingerprint }}}}
      reason: ${{{{ inputs.reason }}}}
      operation_type: ${{{{ inputs.operation_type }}}}
      staging_evidence_json: ${{{{ inputs.staging_evidence_json }}}}
      break_glass: ${{{{ inputs.break_glass }}}}
    secrets:
      DOKPLOY_API_TOKEN: ${{{{ secrets.DOKPLOY_API_TOKEN }}}}
"""


def local_validation(repository: str, scopes: list[dict[str, object]]) -> str:
    rendered_repository = json.dumps(repository)
    scope_document = json.dumps(scopes, separators=(",", ":"))
    encoded_scope_document = base64.b64encode(scope_document.encode()).decode()
    rendered_scope_chunks = "\n        ".join(
        json.dumps(encoded_scope_document[offset : offset + 64])
        for offset in range(0, len(encoded_scope_document), 64)
    )
    return f'''#!/usr/bin/env python3
"""Run only the local validation scopes affected by the current Git diff."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = {rendered_repository}
SCOPES = json.loads(
    base64.b64decode(
        {rendered_scope_chunks}
    )
)


def command(*args: str, capture: bool = True) -> str:
    completed = subprocess.run(
        args,
        check=True,
        text=True,
        capture_output=capture,
    )
    return completed.stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--task", required=True)
    gate = parser.add_mutually_exclusive_group(required=True)
    gate.add_argument("--execution-record", type=Path)
    gate.add_argument(
        "--exemption",
        choices=("typo", "formatting", "comment-only", "docs-nonbehavior"),
    )
    parser.add_argument("--pr", type=int)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--submit", action="store_true")
    return parser.parse_args()


def changed_files(base: str, head: str) -> tuple[str, list[str]]:
    merge_base = command("git", "merge-base", base, head)
    output = command(
        "git",
        "diff",
        "--name-only",
        "--diff-filter=ACMRD",
        merge_base,
        head,
    )
    return merge_base, [item for item in output.splitlines() if item]


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def scope_selected(scope: dict[str, object], changed: list[str], run_all: bool) -> bool:
    patterns = [str(item) for item in scope["paths"]]
    return run_all or any(matches_any(path, patterns) for path in changed)


def selected_scopes(changed: list[str], run_all: bool) -> list[dict[str, object]]:
    if run_all:
        return list(SCOPES)
    automation_scopes = []
    for scope in SCOPES:
        if str(scope["id"]) == "automation-contract":
            automation_scopes.append(scope)
    if automation_scopes and changed:
        contract_only = all(
            any(scope_selected(scope, [path], False) for scope in automation_scopes)
            for path in changed
        )
        if contract_only:
            return automation_scopes
    return [scope for scope in SCOPES if scope_selected(scope, changed, False)]


def rendered_command(value: object, merge_base: str, head: str) -> str:
    rendered = str(value)
    if rendered == "git diff --check":
        return f"git diff --check {{merge_base}}..{{head}}"
    return rendered


def exact_remote_source() -> tuple[str, str, str]:
    if command("git", "status", "--porcelain"):
        message = "commit or remove local changes before submitting validation evidence"
        raise RuntimeError(message)
    revision = command("git", "rev-parse", "HEAD").lower()
    branch = command("git", "branch", "--show-current")
    if not branch:
        raise RuntimeError("local validation submission requires a named branch")
    remote_ref = f"refs/heads/{{branch}}"
    remote_revision = command("git", "ls-remote", "origin", remote_ref).split()
    if not remote_revision or remote_revision[0].lower() != revision:
        raise RuntimeError(
            "push the exact validated HEAD to origin before submitting validation evidence"
        )
    tree = command("git", "rev-parse", f"{{revision}}^{{{{tree}}}}").lower()
    return revision, f"refs/heads/{{branch}}", tree


def execution_record_metadata(
    record_path: Path,
    task_id: str,
    source_revision: str,
    *,
    require_frozen: bool,
) -> dict[str, str]:
    record_path = record_path.expanduser().resolve()
    if not record_path.is_file():
        raise RuntimeError("execution record does not exist")
    root = Path(command("git", "-C", str(record_path.parent), "rev-parse", "--show-toplevel"))
    if command("git", "-C", str(root), "status", "--porcelain"):
        raise RuntimeError("execution record repository must be clean")
    content = record_path.read_text()
    linked = re.search(r"^linked_to:\\s*(\\S+)\\s*$", content, re.MULTILINE)
    status = re.search(r"^status:\\s*(\\S+)\\s*$", content, re.MULTILINE)
    contract = re.search(r"^record_contract:\\s*(\\S+)\\s*$", content, re.MULTILINE)
    delivery_contract = re.search(
        r"^delivery_contract:\\s*(\\S+)\\s*$", content, re.MULTILINE
    )
    if linked is None or linked.group(1) != task_id:
        raise RuntimeError("execution record linked_to does not match --task")
    if status is None:
        raise RuntimeError("execution record status is missing")
    if require_frozen and status.group(1) != "frozen":
        raise RuntimeError("validation submission requires a frozen execution record")
    if not re.search(
        rf"^- `{{re.escape(REPOSITORY)}}` — baseline `[0-9a-f]{{{{40}}}}`;",
        content,
        re.MULTILINE,
    ):
        raise RuntimeError("execution record has no baseline for this repository")
    if require_frozen and not re.search(
        rf"^- `{{re.escape(REPOSITORY)}}` — head `{{re.escape(source_revision)}}`;",
        content,
        re.MULTILINE,
    ):
        raise RuntimeError("frozen execution record does not describe the current HEAD")
    revision = command("git", "-C", str(root), "rev-parse", "HEAD").lower()
    branch = command("git", "-C", str(root), "branch", "--show-current")
    if not branch:
        raise RuntimeError("execution record must be on a named branch")
    remote = command("git", "-C", str(root), "ls-remote", "origin", f"refs/heads/{{branch}}").split()
    if not remote or remote[0].lower() != revision:
        raise RuntimeError("push the execution record before validation submission")
    record_repository = command("git", "-C", str(root), "remote", "get-url", "origin")
    match = re.search(r"github\\.com[:/]([^/]+/[^/]+?)(?:\\.git)?$", record_repository)
    if match is None:
        raise RuntimeError("execution record repository must be hosted on GitHub")
    contract_value = contract.group(1) if contract is not None else "v2"
    if contract_value not in {{"v2", "v3"}}:
        raise RuntimeError("execution record contract is unsupported")
    return {{
        "execution_record.contract": contract_value,
        "execution_record.delivery_contract": (
            delivery_contract.group(1) if delivery_contract is not None else ""
        ),
        "execution_record.task_id": task_id,
        "execution_record.repository": match.group(1),
        "execution_record.path": str(record_path.relative_to(root)),
        "execution_record.revision": revision,
        "execution_record.content_digest": hashlib.sha256(content.encode()).hexdigest(),
    }}


def main() -> int:
    args = parse_args()
    try:
        head_revision = command("git", "rev-parse", args.head).lower()
        if head_revision != command("git", "rev-parse", "HEAD").lower():
            raise RuntimeError("--head must resolve to the current HEAD")
        merge_base, changed = changed_files(args.base, head_revision)
        selected = selected_scopes(changed, args.all)
        if not selected:
            raise RuntimeError("no local validation scope matches the current diff")
        results: list[dict[str, object]] = []
        commands: list[str] = []
        seen_commands: set[str] = set()
        for scope in selected:
            for value in scope["commands"]:
                rendered = rendered_command(value, merge_base, head_revision)
                if rendered in seen_commands:
                    continue
                seen_commands.add(rendered)
                print(f"+ {{rendered}}", flush=True)
                completed = subprocess.run(rendered, shell=True, text=True, check=False)
                results.append(
                    {{
                        "command": rendered,
                        "outcome": "PASS" if completed.returncode == 0 else "FAIL",
                        "exit_code": completed.returncode,
                    }}
                )
                commands.append(rendered)
                if completed.returncode != 0:
                    return completed.returncode
        revision, source_ref, tree = exact_remote_source()
        repository_id = command("gh", "api", f"repos/{{REPOSITORY}}", "--jq", ".id")
        record_metadata = (
            execution_record_metadata(
                args.execution_record,
                args.task,
                revision,
                require_frozen=args.submit,
            )
            if args.execution_record is not None
            else {{
                "execution_record.contract": "v2",
                "execution_record.exemption": str(args.exemption),
            }}
        )
        evidence = {{
            "repository": REPOSITORY,
            "repository_id": repository_id,
            "source_revision": revision,
            "source_ref": source_ref,
            "source_tree": tree,
            "task_id": args.task,
            "pull_request_number": args.pr,
            "changed_scopes": [str(scope["id"]) for scope in selected],
            "commands": commands,
            "results": results,
            "toolchain": {{
                "python": platform.python_version(),
                "platform": platform.platform(),
                **record_metadata,
            }},
        }}
        target = args.evidence
        if target is None:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="learny-validation-", suffix=".json"
            )
            os.close(descriptor)
            target = Path(temporary_name)
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\\n")
        print(f"Validation evidence: {{target}}")
        if args.submit:
            subprocess.run(
                ["controlpctl", "validation", "submit", str(target)],
                check=True,
            )
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {{exc}}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if executable:
        path.chmod(path.stat().st_mode | 0o111)
    print(path)


def main() -> int:
    args = parse_args()
    try:
        shared_ref = validate_sha(args.shared_ref)
        manifest = args.manifest.resolve()
        output_root = args.output_root.resolve()
        document = validate(manifest, repository_root=output_root)
    except ValidationFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    workflows = output_root / ".github" / "workflows"
    write(
        output_root / "scripts" / "validate_local.py",
        local_validation(
            document["metadata"]["repository"], document["localValidation"]["scopes"]
        ),
        executable=True,
    )
    if document["sourceGate"]["enabled"]:
        write(workflows / "validate.yml", validation_workflow(shared_ref))
    if document["artifacts"]:
        write(workflows / "image.yml", image_workflow(shared_ref))
    if document["delivery"]["pipelines"]:
        write(workflows / "deploy.yml", deploy_workflow(shared_ref))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
