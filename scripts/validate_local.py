#!/usr/bin/env python3
"""Run only the local validation scopes affected by the current Git diff."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

# fmt: off
REPOSITORY = 'learny-technologies/.github'
SCOPES = [{'id': 'automation',
  'paths': ['.github/**',
            'actions/**',
            'schemas/**',
            'scripts/**',
            'tests/**',
            'automation.yaml'],
  'commands': ['python -m unittest discover -s tests -v',
               'python scripts/validate_automation.py automation.yaml '
               '--repository-root .']}]
# fmt: on


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
    parser.add_argument("--pr", type=int)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--submit", action="store_true")
    return parser.parse_args()


def changed_files(base: str, head: str) -> list[str]:
    merge_base = command("git", "merge-base", base, head)
    output = command("git", "diff", "--name-only", "--diff-filter=ACMR", merge_base, head)
    return [item for item in output.splitlines() if item]


def scope_selected(scope: dict[str, object], changed: list[str], run_all: bool) -> bool:
    if run_all:
        return True
    patterns = [str(item) for item in scope["paths"]]
    return any(fnmatch.fnmatch(path, pattern) for path in changed for pattern in patterns)


def exact_remote_source() -> tuple[str, str, str]:
    if command("git", "status", "--porcelain"):
        raise RuntimeError("commit or remove local changes before submitting validation evidence")
    revision = command("git", "rev-parse", "HEAD").lower()
    branch = command("git", "branch", "--show-current")
    if not branch:
        raise RuntimeError("local validation submission requires a named branch")
    remote_revision = command("git", "ls-remote", "origin", f"refs/heads/{branch}").split()
    if not remote_revision or remote_revision[0].lower() != revision:
        raise RuntimeError("push the exact validated HEAD to origin before submitting evidence")
    tree = command("git", "rev-parse", f"{revision}^{{tree}}").lower()
    return revision, f"refs/heads/{branch}", tree


def main() -> int:
    args = parse_args()
    try:
        if command("git", "rev-parse", args.head) != command("git", "rev-parse", "HEAD"):
            raise RuntimeError("--head must resolve to the current HEAD")
        changed = changed_files(args.base, args.head)
        selected = [scope for scope in SCOPES if scope_selected(scope, changed, args.all)]
        if not selected:
            raise RuntimeError("no local validation scope matches the current diff")
        results: list[dict[str, object]] = []
        commands: list[str] = []
        seen_commands: set[str] = set()
        for scope in selected:
            for value in scope["commands"]:
                rendered = str(value)
                if rendered in seen_commands:
                    continue
                seen_commands.add(rendered)
                print(f"+ {rendered}", flush=True)
                completed = subprocess.run(rendered, shell=True, text=True)
                results.append(
                    {
                        "command": rendered,
                        "outcome": "PASS" if completed.returncode == 0 else "FAIL",
                        "exit_code": completed.returncode,
                    }
                )
                commands.append(rendered)
                if completed.returncode != 0:
                    return completed.returncode
        revision, source_ref, tree = exact_remote_source()
        repository_id = command("gh", "api", f"repos/{REPOSITORY}", "--jq", ".id")
        evidence = {
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
            "toolchain": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
        }
        target = args.evidence
        if target is None:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="learny-validation-", suffix=".json"
            )
            os.close(descriptor)
            target = Path(temporary_name)
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(f"Validation evidence: {target}")
        if args.submit:
            subprocess.run(
                ["controlpctl", "validation", "submit", str(target)],
                check=True,
            )
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
