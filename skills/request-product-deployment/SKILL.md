---
name: request-product-deployment
description: Plan, publish, deploy, promote, inspect, watch, or roll back Learny runtime releases through the standard immutable GitHub Actions delivery contract. Use when a user asks to deploy a product to dev, staging, stage, production, or prod; publish a runtime artifact; inspect deployment status; promote an exact release; or roll back to the previous healthy release.
---

# Request Product Deployment

Use the bundled deterministic wrapper. GitHub identity, pinned workflows, immutable OCI evidence,
GitHub Environments, and repository-owned executors are the authority; never create a Control Plane
deployment operation or call Dokploy directly.

## Workflow

1. Resolve a pushed frozen execution-record v3 reference with
   `delivery_contract: github-actions/v1`.
2. Run `plan`. Normalize `stage` to `staging` and `prod` to `production`.
3. If the plan reports `requires_publication`, show its exact source/components/fingerprint, obtain
   confirmation, run `publish`, then rerun `plan`.
4. Show the ready immutable plan including actor, source SHA, digests, definition hash, migration
   heads, environment, previous release when applicable, and fingerprint.
5. After a simple confirmation, run `promote` with the same fingerprint. The wrapper rebuilds the
   plan and rejects drift before dispatch.
6. Run `status --watch` and report the terminal GitHub run and canonical Deployment evidence.

Production initiation by an allowlisted technical owner after the immutable plan is the approval.
Do not request a separate Control Plane approval. A non-allowlisted GitHub actor fails before
dispatch. Access to an owner's authenticated GitHub session is equivalent to that owner's GitHub
authority.

## Commands

```bash
DEPLOY_SKILL="/absolute/path/to/skills/request-product-deployment/scripts/request_deployment.py"

python "$DEPLOY_SKILL" plan \
  --repository-root /path/to/component-repo \
  --environment dev \
  --pipeline backend \
  --execution-record /path/to/EXEC-TASK-...md \
  --reason "Deploy exact validated source"

python "$DEPLOY_SKILL" publish <same arguments> \
  --expected-fingerprint <publication-fingerprint>

python "$DEPLOY_SKILL" promote <same arguments> \
  --expected-fingerprint <deployment-fingerprint>

python "$DEPLOY_SKILL" status \
  --repository learny-technologies/trace-workspace \
  --run-id <github-run-id> --watch

python "$DEPLOY_SKILL" rollback-plan <same plan arguments>
python "$DEPLOY_SKILL" rollback <same plan arguments> \
  --expected-fingerprint <rollback-fingerprint>
```

Use `--source-sha` only for an exact remote SHA. Dev may use a remote branch SHA; staging and
production require main eligibility. Never deploy a mutable tag, arbitrary image, arbitrary
executor, or an expired/mismatched release artifact. Rollback selects only the previous healthy
canonical GitHub Deployment.
