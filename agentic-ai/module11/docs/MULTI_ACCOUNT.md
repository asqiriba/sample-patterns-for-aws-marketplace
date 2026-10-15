<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# One account per stage

This sample runs dev, staging and prod as three Pulumi stacks in one AWS account. That keeps the demo small, and it
has three costs you should not accept in production:

- **Quota.** Stages share the account's model and AgentCore quotas, so a staging gate can throttle production.
- **Attribution.** Cost and usage per stage need tags instead of falling out of the bill.
- **Blast radius.** An administrator of the account can hand-edit any stage (B2's unreviewed hotfix is exactly that).

## The recommended layout

| Account | Holds | Who gets in |
|---|---|---|
| Tooling | `m11-platform`: the release bucket, CodePipeline `m11-release`, CodeBuild projects, the GitLab connection and OIDC roles | Platform engineers (read-only day to day) |
| dev, staging, prod (one each) | That stage's `m11-harness` stack | Only the stage's deployer role; humans are read-only |

The pipeline's `m11-deploy` (and `m11-bake` for prod) in the tooling account assume each stage account's
`m11-<stage>-deployer`. Every Module 11 tool already takes the stage as configuration and reaches a stage only through
its deployer role, so the code does not change; the deployer role ARNs and account IDs do.

## Steps

1. In each stage account, create the deployer role with `m11-account-baseline`
   (`module11/infrastructure/platform/account_baseline.py`), the same templates the single-account build uses:
   `stage`, `toolingAccountId` and `operatorPrincipals` are its configuration. A Pulumi-mock test covers it
   (`tests/test_module11_account_baseline.py`).
2. Give each stage account its own state backend and KMS key (`infrastructure/bootstrap/state_backend.py`), or share
   the tooling account's with a bucket policy limited to that stage's stack prefix.
3. Point the platform's deployer-role outputs at the stage accounts' roles, and replace the single-account
   `aws:ResourceTag/Stage` conditions with per-account scoping (the account boundary does that work).
4. Keep humans read-only in stage accounts. The deployment role is the only way in; break-glass access is audited.

The run-of-show's B1 1e says this out loud on every edit of the recording: "Three stacks in one account today; one
account per stage in yours."
