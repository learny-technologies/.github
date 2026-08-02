# Learny GitHub Automation Agent Guide

This repository owns shared executable GitHub automation for Learny Technologies. Company policy
belongs in `engineering-handbook-workspace`; repository-specific validation, migration, and health
logic belongs in the component repository.

## Rules

- Write every committed artifact in English.
- Keep reusable workflows provider-neutral and parameterized by validated repository manifests.
- Pin every external action to a full commit SHA.
- Never accept mutable image tags as deployment inputs.
- Never put Dokploy credentials in image-publication jobs.
- Never compile source in deployment jobs.
- Keep framework test suites local unless the handbook explicitly grants an exception.
- Validate schema, generator, workflow syntax, and tests before publishing changes.

## Delivery

For non-exempt implementation, fix, merge, build, deployment, release, rollback, or migration
work, use `manage-product-delivery`. Create or resume the canonical execution record in
`engineering-handbook-workspace/docs/execution/` before any mutation, including a branch.
Goal mode is optional; update the record before a phase change, handoff, or stop.
