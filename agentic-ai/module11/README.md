<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Module 11: MLOps and the Agent Development Lifecycle

Run commands from `agentic-ai/`.

Module 10 put the DevOps Companion on AgentCore Runtime behind API Gateway and Cognito. Module 11 gives it a
release process. Shipping an agent once is easy; this module is about shipping it the fiftieth time without fear.

## What Module 11 adds

- **The harness is the release unit.** Every input that shapes behavior is a file under `module11/harness/`:
  prompts, eight tools (schema, description, permissions, handler), orchestration, models, guardrail, retrieval
  snapshot, external surfaces, flags, evaluators, thresholds and scenarios. `module11 tree` lists them.
- **One content-addressed manifest.** `module11 manifest build` hashes 55 artifacts after normalization (50 in the
  behavior set). A release is sealed once, tagged `harness/v<n>` in GitLab with its package, and every session and
  side effect names its manifest version. `manifest diff` compares declared with deployed; the drift checker runs
  every 15 minutes and `drift revert` restores the declared state.
- **Infrastructure computed from the files.** `infrastructure/harness/` (Pulumi) turns each Lambda-backed tool into a
  role, a generated least-privilege policy, a log group and a published version. A CrossGuard pack enforces the rules.
- **A change-type-routed pipeline.** `release/detect.py` maps the changed artifacts to a rollout and an evaluation
  scope (`harness/pipeline/change_types.yaml`). GitLab merge requests get a fast gate and, for MRs by Vercel team
  members, a UI preview; `main` releases through CodePipeline V2 `m11-release` (GitLab source via CodeConnections),
  with a full staging gate, a GitLab protected-environment approval, and a canary.
- **Evaluation as a gate.** Hard gates for deterministic checks (trajectory, safety order, credential leakage) and
  statistical gates against a calibrated baseline under a named evaluator-set version; latency compared with the
  current version measured at the same time; online evaluation and a batch-evaluation shadow for model changes.
- **Progressive delivery.** Session-hash routing, a rollback controller with a latch, flags as code (a kill switch
  per team), side-effect stamping and Memory purge, a staging A/B test through AgentCore Gateway, Recommendations
  imported as merge requests, corpus snapshots as release events, and cost per successful session.

## Prerequisites

- **AWS.** The account and Region where Module 10's cloud mode is deployed (us-east-1 in the build), with Module 10's
  Pulumi state in an S3 backend (see `docs/MIGRATE_MODULE10_STATE.md`). The account ID is read from caller identity
  and checked against `M11_EXPECTED_ACCOUNT`. The account needs access to the inference profiles in
  `harness/agent/models.yaml`, AgentCore Runtime, Memory, Gateway and Evaluations in the Region, Transaction Search
  enabled, and room for Module 11's IAM roles (below). Module 11 adds no VPC.
- **Tools.** Pulumi CLI 3.267.0 (pinned; `brew install pulumi` or the Pulumi installer); Python 3.13 (3.13.12 in the
  build) in a venv at `agentic-ai/.venvs/module11` (below); Node.js 24 for the edge and Vercel build tests (with
  Homebrew's keg-only `node@24`, prefix commands with `PATH=/opt/homebrew/opt/node@24/bin:$PATH`); AWS CLI v2 for
  the B2 hotfix only (every Module 11 tool uses boto3); git with SSH commit signing. Docker is not needed: images
  build in ARM CodeBuild.
- **Python environment.** From `agentic-ai/`:

  ```bash
  python3.13 -m venv .venvs/module11            # git-ignored; includes pip, which Pulumi's toolchain uses
  source .venvs/module11/bin/activate
  pip install -r module11/requirements.txt -r module11/requirements-pulumi.lock
  ```

  With uv: `uv venv --python 3.13 --seed .venvs/module11`, then `uv pip install --python .venvs/module11/bin/python
  -r module11/requirements.txt -r module11/requirements-pulumi.lock`. Every command below assumes the venv is active.
- **Partners.** A GitLab.com group on Premium or an Ultimate trial (deployment approvals, code-owner sections, push
  rules), three groups `harness-owners`, `platform` and `quality` (`quality` shares no member with the other two),
  and a Vercel Pro team. Limits that shaped the design (details in `PARTNER_SETUP.md`):
  - An Ultimate trial allows one project access token and no group token, so the token's bot user is the only bot
    and CI identity, and preview jobs reach GitLab through a broker Lambda instead of reading the token.
  - GitLab's documentation says code-owner groups count direct members only. In this build an Owner inherited from
    the top-level group was still accepted for the `quality` code-owner section, so one person could approve both
    sides of an evaluation change. Name the quality reviewers as users (`seed.py --quality-users`,
    `M11_APPROVER_USERS`), keep them out of the parent group's Owners, and give every code-owner group at least two
    people. Two-person approval is enforced at the `production-gated` environment (named users); treat MR-level
    quality separation as enforced only once the [Quality] section names users. You supply two secrets in Secrets Manager: `devops-companion/partners/gitlab-token` and
  `devops-companion/partners/vercel-token`. One-time console actions: the GitLab-Vercel link and the CodeConnections
  approval (`partners up` turns on GitLab web-based commit signing). `PARTNER_SETUP.md` has the order.
- **Not needed:** Pulumi Cloud, hosted LaunchDarkly, a running Portkey gateway, or any earlier module's partner
  account. Module 11 calls Bedrock directly; leave Module 10's Portkey gateway stopped.

## Quick start

```bash
mkdir -p module11/.local
cp module11/.env.example module11/.local/env.sh        # fill in the values (git-ignored; every line is exported)
source module11/.local/env.sh
for p in harness platform partners; do                 # each Pulumi program uses the venv above (virtualenv: venv)
  ln -sfn "$VENV" "module11/infrastructure/$p/venv"
done
python -m module11.demo.live_check --headroom          # IAM role quota before the first deploy
python -m module11.infrastructure.bootstrap.state_backend
# Platform foundation, then the stages (each through its own deployer role):
pulumi -C module11/infrastructure/platform stack init shared --secrets-provider "$M11_SECRETS_PROVIDER"   # once
pulumi -C module11/infrastructure/platform up --stack shared --policy-pack "$PWD/module11/infrastructure/policy"
python -m module11 release seal --operator --ref HEAD
python -m module11 release deploy --stage dev --manifest <manifestVersion printed by seal>
python -m module11 status --all
```

Then follow `PARTNER_SETUP.md` for the GitLab-sourced pipeline and `OPERATIONS.md` for day-to-day runbooks.

**Without an account.** Module 11 is live-only by design: its subject is a real release. The offline parts need no
credentials, no `.env` and no partner accounts. From `agentic-ai/`, with the venv above active:

```bash
python -m pytest tests/test_module11_*.py -q                   # about 3 minutes; Pulumi programs run under mocks
node --test tests/test_module11_*.mjs                          # Node.js 24
python -m module11 tree                                        # the harness files that make up the release
python -m module11 check                                       # build checks: expect 0 error(s), 0 warning(s)
python -m module11 manifest build                              # 55 artifacts / 50 in the behavior set / 39 runtime
python demos/module11_demo.py --replay-fixtures --no-pause     # committed, redacted captures of a real run
python demos/module11_demo.py --replay-fixtures --no-pause --cut 12   # the 12-minute edit (or --cut 15)
```

`--replay-fixtures` renders `tests/fixtures/module11/captures/` under the banner `REPLAY of real run <ids>, recorded
<timestamp>`. Captures are only ever written by a real run (`--record`) and exported with `module11 captures export`,
which redacts account IDs, ARNs, user names and URL hosts. A beat with no committed capture says so. Running the
same `manifest build` twice prints the same digests; only `manifestVersion` carries the time and commit.

About 30 tests skip offline: the live tests (`M11_LIVE_TESTS=1`), the slow statistical grids (`M11_SLOW_TESTS=1`),
and the runtime's model-client tests unless you also `pip install -r module11/requirements-runtime.txt`.

## The demo

`demos/module11_demo.py` is the run-of-show: eight beats (`--section 0-7`), 17:15 nominal, with edits of 15:15 and
11:55 (`--cut 15` skips C1-C5, `--cut 12` skips C1-C8). `module11 demo prep` writes the presenter's shell function
`d N` into `module11/.local/env.sh`; `docs/PRESENTER_CHEAT_SHEET.md` is the one-page sheet, and
`MODULE11_TALK_TRACK.md` the talk track.

There is exactly one live race, B5: the prod canary of the payments team's edit, a few words appended to the
companion prompt (`PAYMENTS_TWEAK` in `module11/demo/wording.py`: "For payments, never call `search_runbooks`."). The
payments team keeps its own runbooks elsewhere and wants cheaper, quicker payments investigations, so the shared
runbook lookup is the step it drops; but the shared payments timeout runbook is where the safe fix lives. The demo harness's suite deliberately has no payments-specific
runbook scenario: that is the gap B5 finds and B6 closes. Three scenarios that once depended on a payments runbook were
retargeted for that reason: `regression-runbook-skip-001` and `incident-triage-012` now target checkout-service, and
`edge-rollback-safety-001` targets reporting-service. The edit passes every gate; the canary catches it. B5's traffic
driver sends 180 sessions (`module11 traffic --stage prod --sessions 180 --concurrency 20 --mix payments=0.6`).

Everything not live prints `RAN BEFORE THE SESSION: real run <id> at <ts>`: the Friday MR pipeline, the staging gate,
the approval, the calibration, the evaluator-set record, the model comparison, the A/B test and the recommendation.
The model-migration shadow shown in B5 is a short real shadow run: 2 hours at 120 sessions an hour, 241 paired
sessions, verdict pass. For a real model change, run the shadow for a day or more (`docs/MODEL_MIGRATION.md`).

### Presenting the demo

`d N` runs one beat (`d 0` to `d 7`, each at the `M11_CUT` level). To run every part in order, call the driver
without `--section`: `python demos/module11_demo.py`. For a screen that an audience reads, `export M11_EXPLAIN=1` (or
add `--explain`, for example `d 5 --explain`). Explain mode puts a short panel in plain words before each step and
drops the presenter's own markers (beat ids, time budgets, cut levels and key hints). The commands and their output
stay as they are, and the recording and timings files keep the full text. It is off by default, so nothing changes
unless you turn it on.

### Demo-only relaxations

These exist only so the run-of-show fits a recorded session. Customer-facing bounds (the prod rollout profile's
false-rollback and power grids, `sim verify`, every quality and safety gate, IAM and security) are unchanged.

- Labelled demo settings: `rolloutProfile: demo` (50% then 100%, 1-minute ticks), `teamConcurrency: 14` and
  `onlineEval.samplingPct: 100` on prod. Defaults are `prod`, 10 and 10.
- MR pipeline durations are reported as measured numbers, not budgets (they are printed per job and per step; an
  operational ceiling of 20:00 from push to note is a WARN).
- Approve-to-rollout is reported as measured, with a 9:00 limit from `PutApprovalResult` to `m11-deploy` success
  inside the promote job's 10-minute wait (WARN above 8:00). The promote job's functional timeouts are unchanged.
- `module11 demo arm --stage dev` deploys a deliberately bad release to dev only, to rehearse the canary; refuses
  outside the demo rollout profile and for callers outside operatorPrincipals; writes an audit record first.
- `module11 demo rearm` re-runs an approved release's Production stage without a new approval; refuses outside the
  demo rollout profile, for any release other than the armed one, and for callers outside operatorPrincipals; writes
  an audit record first.
- The reserved CodeBuild fleet (lever 5) is documented in `OPERATIONS.md` as an option, not built.

## Cost

Two kinds of number appear below. **Computed** numbers come from the module's own cost model (token counts at the
planning price of $5 / $20 per 1M input/output model tokens, because the AWS Price List had no entry for the model,
plus the October 2026 list prices for Evaluations, Runtime and Gateway). **Billed** numbers come from Cost Explorer
for the build account (us-east-1, unblended, still marked estimated). Run `module11 prices refresh` before you rely
on a computed number.

| Activity | Cost | How it was measured |
|---|---|---|
| One session, all in (model, two judges, code evaluator, runtime) | $0.048-0.052 | Computed from 5,800-6,400 input and 320-360 output model tokens per session, 13-17 s active |
| MR fast gate (12 scenarios x 2) | $1.02-1.14 for the scored suite (8 runs); about $5-6 with today's 12 warm-up and 40 + 40 latency-phase sessions | Suite computed and measured; the warm-up and latency phases estimated from the per-session cost |
| Full staging gate (136 sessions) | $6.43 mean (4 runs), plus about $4 for the latency phase and up to $0.60 for the warm-up | Suite computed; the two phases estimated (they are never scored) |
| Calibration, n = 8 at r = 3 (1,008 sessions) | $51.92 | Computed |
| Canary (demo profile, 180 sessions) | about $9 | Estimated: 180 x the per-session cost |
| One demo take | about $10; a B5 re-take adds about $9 | Estimated: one canary, the Vercel click and a few seconds-long commands. `demo rearm` itself runs no sessions |
| Idle per day (schedules disabled, no rollout, Portkey stopped) | about $0.40-0.60 | Estimated from Cost Explorer: KMS +$0.15, S3 +$0.15, Secrets Manager +$0.07 and SSM, DynamoDB and ECR about +$0.04 a day over the pre-build baseline, plus alarms and log storage. Not yet measured on an idle day |
| Building and validating the module (10/5-10/9) | about $205 | Billed, incremental over the day before the build for the services Module 11 uses: AgentCore $159 (runtime, evaluations, memory, gateway), CloudTrail $20, Bedrock models $14, CodeBuild and CodePipeline $4.50, CloudWatch $3.60, Lambda $2.90, the rest about $1.30 |

Notes:
- The build account already had a CloudTrail trail that logs data events, so Module 11's Lambda and S3 traffic
  added about $6 a day there while the build ran. An account without data-event trails does not pay this.
- Cost Explorer's model line for the build is well below what the planning price implies, so the computed
  per-session figures err high on the model share.
- Cost allocation by the `Project=m11-devops-companion` tag needs that tag activated as a cost allocation tag in
  Billing; the build account had not, which is why the billed row is an increment by service.

`module11 cost estimate --suite staging` prints a projection before a gate runs; `module11 cost --stage prod --by
manifest` reports tokens, tool calls and cost per successful session from real outcomes.

## One account today, one account per stage in yours

This sample runs dev, staging and prod as three stacks in one account, so a staging gate can eat production model
quota and an administrator could hand-edit a stage. In your organization use one account per stage: quota, cost
attribution and blast radius are per account, the deployment role is the only way in, and the code takes an account
per stage as configuration. `docs/MULTI_ACCOUNT.md` describes the layout and `infrastructure/platform/account_baseline.py`
(`m11-account-baseline`) creates the per-account deployer role from the same templates.

## Teardown

```bash
python -m module11 teardown --dry-run      # prints steps 1-10 and every ORPHAN?; deletes nothing
python -m module11 teardown --yes          # then runs the printed plan
```

Teardown deletes only what this project owns: resources tagged `Project=m11-devops-companion`, in an `m11-*` Pulumi
stack state, or recorded in the release bucket's ledger (read once, before step 1). Anything that only matches the
`m11` name prefix is listed `ORPHAN?` and kept unless you pass `--include-orphans` and type the confirmation. Module 10
is kept unless you pass `--include-module10`; the GitLab project is archived unless you pass `--delete-gitlab-project`.

## Security notes, in passing

Module 12 goes deeper. Here: no human or MR pipeline can push to `main`; a red gate blocks a merge even with every
approval; commits are signed; preview roles cannot read the GitLab token or touch another stage's resources (checked
by `live_check` item 16 with IAM policy simulation); secrets are read by ARN at run time and never printed; GitLab
CI reaches AWS only through OIDC roles bound to protected environments; and `demo arm` / `demo rearm` are guards
for demo stacks. IAM remains the boundary.

Two local details: `python -m module11.runtime.app` without `--agentcore` binds 127.0.0.1:8091 and refuses any other
host, and that port has no authentication (loopback is the access control); only the AgentCore image binds
0.0.0.0:8080, inside the Runtime microVM. Module 11's Node.js code (the edge Lambda and the Vercel build) has no npm
dependencies: it uses Node built-ins and the AWS SDK for JavaScript v3 that the Lambda runtime provides.
`pip-audit` found no known vulnerabilities in `module11/requirements*.txt` or `requirements-pulumi.lock`
(pip-audit 2.9.0, 2026-10-09).

### IAM role headroom

IAM limits the number of roles in an account ("Roles per account", Service
Quotas code `L-FE177D64`, default 1,000). Shared accounts are often close to
it. Module 11 gives every Lambda its own least-privilege role, so a full
deployment holds 87 roles:

| Stack | Roles |
|---|---|
| `m11-platform` | 25: image build, three stage deployers, the preview deployer, three eval runners; with the pipeline also the ten CodeBuild project roles, `m11-pipeline`, three GitLab OIDC roles, `m11-operator-experiment`, and the freeze-check and notes-broker Lambdas |
| `m11-harness` dev, staging, prod | 12 each: runtime, 7 tool Lambdas, adapter, authorizer, control, code-based evaluator |
| `m11-harness` `mr-<iid>` (one per open merge request) | 13 each: the same 12 plus its own eval runner |

The total assumes two preview stacks open at a time. Check the headroom before
you deploy anything:

```bash
python -m module11.demo.live_check --headroom          # add --preview-stacks N for more open MRs
```

The check fails when the roles still to be created do not fit, and warns when
fewer than 10 would be left. `module11 release deploy` runs the same check
before a stack's first `pulumi up`, so a full account stops the deploy before
any resource is created. To raise the quota (no charge, takes effect when
approved):

```bash
aws service-quotas request-service-quota-increase --region us-east-1 \
  --service-code iam --quota-code L-FE177D64 --desired-value 1500
```

## Evaluation gates

`python -m module11 gate --stage <stage> --profile staging|preview` runs the
scenario suite against the stage's endpoint and compares it with the
calibrated baseline (`harness/evaluation/thresholds.yaml`). The pipeline's
Staging stage and every MR's `preview:gate` job run the same command.

Latency and the other budgets are measured warm. A gate runs right after a
deploy created a new runtime version, and the baseline comes from prod's warm
`stable` endpoint, so the gate first sends 12 untimed warm-up sessions (one per
worker slot; `--warmup N` or `M11_GATE_WARMUP`, 0 turns it off). They run in
shadow mode, write no incident note, Memory record or outcome item, and count
in no statistic or cost. The report still shows the cold start in an
informational warm-up block: the number of warm-up sessions and their time to
first event (median and max).

Two more informational blocks follow the table. Guard-blocked attempts counts
tool calls the runtime refused, such as recovery planning requested before
deployment history. Model provider health helps you tell a provider incident
from a regression. It shows the sessions that ended on a model throttle (429)
or a provider 5xx, the retries, and the median session latency. It also shows
the model's `AWS/Bedrock` invocations, throttles, server errors and maximum
latency during the run. Those metrics are account-wide, so they include
other callers. Neither block changes the verdict, and nothing is retried or
excluded because of them.

### Latency is compared with the current version

Latency is compared against the current version measured at the same time,
so a slow provider doesn't fail a good release. After the main suite, each
gate runs a balanced latency phase. It sends the happy_path scenarios twice each
to the candidate (40 sessions) and to the release on prod `stable`
(40 sessions) at the same time, with 12 workers per arm, so both runtimes carry
the same load in the same window. Equal load matters: session latency
roughly doubles between 3 and 12 concurrent sessions on a runtime. These
sessions run in shadow mode, are never evaluated, and are excluded from the
gate's statistics and per-session cost; the report shows their cost
separately, about $4.

The line `p95LatencyMs vs control` fails only when the candidate arm's p95 is
more than 1.25 times the control's and the exact exceedance test gives
p < 0.01. That is the same test the canary controller uses. The absolute p95
budget stays in the report as the informational `user-experience budget`
line.

### Model calls retry provider errors

The runtime retries a model call up to twice on a provider 5xx or a throttle
(429), with exponential backoff and jitter, only while the session deadline
leaves time for another attempt. It never retries a 4xx validation error, and
never retries a tool call. Each retry is a span attribute
(`m11.model_retries`), a count in the result (`m11.modelRetries`) and the
gate's provider-health block, and the CloudWatch metric `m11/ModelRetries`.
If the retries run out, the answer says that the model provider failed.

### Transport failures before the agent runs

Rarely, AgentCore rejects an invocation before it reaches the container,
for example with `RuntimeClientError`. In 5,830 gate and calibration sessions
this happened twice. The gate re-sends such a session once, with the same
input and a new session ID, only when it can prove that the request never
ran: a pre-dispatch error on the first turn, no tool or model activity, and
no span document or runtime log line for the session after a 3-minute
ingestion delay. The report's `Transport re-invokes` line lists each
original session, its error, the evidence and the replacement. If more than
two sessions need it, or a re-invoke fails again, nothing more is re-sent and
the gate stays INCONCLUSIVE. That pattern is a service incident. The traffic
driver never re-sends, because the edge cannot prove pre-dispatch; it counts
those failures as `invocation failed`.

### Provider incidents vs regressions

A gate that fails on latency alone can be a model provider incident rather
than a regression. Read the provider-health block before you decide.

- A regression usually moves the session median and clusters in the scenarios
  the change touches. Provider errors stay at 0.
- A provider incident leaves the median unchanged and spreads slow sessions
  across scenarios. The block shows provider 5xx (for example
  `InternalServerError`) and a high model p99, often with 0 throttles.
- Throttles (429) mean your own load is over the model's quota. Fix that by
  lowering gate concurrency or requesting a quota increase. Retrying does not
  help.

Investigate a latency-only failure during provider 5xx: classify the errors,
compare the model's p99 with a passing run, and record what you find. Do not
retry it automatically. A retry without that evidence hides the cause.

The control arm and the model-call retries absorb most provider incidents. A
reading of the model's recent health still tells you whether the window is
a good one to start a full-scope gate in:

```bash
python -m module11 pipeline provider-health --record          # exit 0 = healthy
python -m module11 pipeline provider-health --probe 150 --record   # quiet window
```

It reads the prior 30 minutes of `AWS/Bedrock` metrics for every model in
`models.yaml`. It reports healthy only when the p99 is at most 7 s and there
were no provider 5xx, the range the passing runs had. `--probe N` sends N small
model calls first, for a window with too few invocations to give a p99, and
costs about $2 for 150 calls. `--record` writes the reading to the release
ledger (`--execution` links it to a pipeline execution). Each gate report also
carries the same reading for the 30 minutes before its first session. The gate
itself does not use it.

An unchanged release that fails the latency-vs-control line, or fails a hard
gate on a session that ended with a provider error after its retries, is
still a failure to investigate. Classify the errors in the provider-health
block first. Do not re-run it to get a pass.
