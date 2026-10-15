<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Module 11 operations

Runbooks for the release process. Run every command from `agentic-ai/` after `source module11/.local/env.sh`.
`module11` is `python -m module11`. Install the tools first: Pulumi CLI 3.267.0, Python 3.13.12, Node.js 24, AWS CLI v2.

## Everyday state

```bash
module11 status --all                      # endpoints READY, live == deployed manifest, drift, rollout state
module11 rollout show --stage prod         # the control item, both arms, the last signals
module11 versions --stage prod             # runtime versions, endpoints, bundle lineage, Lambda versions
module11 drift check --stage prod --external
module11 cost --stage prod --by manifest   # tokens, tool calls, cost per successful session
python -m module11.demo.live_check         # preflight (add --demo for the 16-item demo form)
```

## Baseline refresh after a promoted behavior change

Statistical gates compare a release with the calibrated baseline named in `harness/evaluation/thresholds.yaml`
(`baselineManifest`). After a behavior change reaches prod, recalibrate it, then refresh the baseline through a
bot MR so the gates compare with what prod serves:

```bash
module11 calibrate --manifest prod-current --n 8          # about 1 hour and $52 (measured, 1,008 sessions)
module11 baseline refresh                                  # opens the bot MR; --rederive-floors lowers only
                                                           # floors above their calibrated value
```

A change to any behavior artifact (prompts, tool descriptions, schemas or handlers, models, orchestration,
guardrail, retrieval snapshot, runtime code) needs a recalibration before its numbers are trusted. An evaluator-set
change follows `docs/EVALUATOR_SET_CHANGE.md`.

## Freeze

```bash
module11 freeze show
module11 freeze set planned --reason "model migration shadow"   # held at FreezeCheck; also blocks prod deploys
module11 freeze set emergency --reason "incident 1234"          # fails the FreezeCheck action
module11 freeze set none
```

Set the freeze first, then hold anything else. A planned freeze also makes a prod deploy refuse at validate, so lift
it before you retry a held Production stage.

## Latch clear

A rollback latches the release's behavior digest so the same bytes cannot advance again. Clear it only when you
mean to re-deploy those exact bytes (a demo reset, or a fix that turned out to be in another artifact):

```bash
module11 latch clear <behavior digest hex> --reason "<why>"      # audited in the ledger
```

## Break-glass approval

When no GitLab approver is available, an operator can release one execution's approval. It is audited and shows in
the execution's history:

```bash
module11 release approve --execution <id> --break-glass --reason "<why>"
```

## Prune

After a rollout ends, Bake runs `release prune` so the stage grants only `/current`'s pins. Run it by hand after a
manual recovery:

```bash
module11 release prune --stage prod
```

It refuses while another runtime version is still referenced by an endpoint or the control item.

## Drift revert

```bash
module11 drift check --stage staging
module11 drift revert --stage staging       # as the stage deployer: waits out UPDATING, retargets, polls READY
module11 release deploy --stage staging --reapply    # reverts hand-edited role policies (refreshes first)
```

When drift and `main` disagree, git is what you intended and SSM is what is running: revert the resource, never edit
the manifest to match reality.

## Main is ahead of prod

If `main` declares a release that prod does not serve (a failed or rolled-back release), the next release's change
plan is computed against prod `/current`, so it carries both changes. Main is ahead of prod; revert or fix forward.
A revert MR that restores `/current`'s content computes `noop` and runs nothing.

## Portkey stays stopped

Module 11 calls Bedrock directly through the OpenAI SDK; it never routes through Module 10's Portkey gateway. Keep
the gateway stopped (`python module10/gateway/lifecycle.py status`); start it only for a Module 10 smoke check and
stop it in the same session.

## Controller schedules

Each stage's controller (`m11-<stage>-controller`) runs on an EventBridge Scheduler schedule that is ENABLED only
while a rollout is active: `open` enables it, and a terminal state disables it. A schedule left ENABLED with an IDLE
control item costs one Lambda invocation a minute; disable it with the console or by re-running the controller's
`reset`.

## Idle cost

With every schedule disabled and no rollout, the module costs the KMS keys, a few secrets and SSM parameters, ECR, S3
and log storage, the alarms, and the drift checkers (96 runs a day per stage). Cost Explorer's increments for those
services put it at about $0.40-0.60 a day (an estimate: there has been no idle day since the stages went live).
Module 10 adds about $0.05 a day. AgentCore Runtime and Evaluations bill per use, so they cost nothing idle.
`module11 teardown --dry-run` lists everything that would be removed.

## Evaluation cost and timing (measured)

| Activity | Measured | Wall time |
|---|---|---|
| One session, all in | $0.048-0.052 | 13-17 s active |
| MR fast gate (24 sessions), before warm-up and latency phases | $1.02-1.14 (about $5-6 with both phases, estimated) | 3:14-4:17 from a laptop |
| MR pipeline, push to `m11-gate` note (first MR run) | - | validate 1:29, preview:deploy 4:50, preview:gate 4:51; note 9:31 after the push |
| Full staging gate (136 sessions) | $6.43 mean, plus about $4 for the balanced latency phase | 5:22-6:38 |
| Calibration, n = 8 | $51.92 | about 47 minutes of runs |

Span readiness dominates a gate: polling starts 60 s after the last invocation, and a session is ready only after
its record count has been stable for 20 s. MR pipeline and Production-stage durations are recorded per job and per
Production step (`module11 pipeline timing --execution <id>`), not treated as budgets; approve-to-rollout has a 9:00
limit from `PutApprovalResult` to `m11-deploy` success (WARN above 8:00). Measured: 2:51 from the GitLab Run click to
the rollout opening, for one release.

**Lever 5, a reserved CodeBuild fleet**, is an option for customers who need faster pipelines: one ARM `g1.small`
reserved instance (about $3 per day it exists) removes CodeBuild's queue and provisioning time. This sample does not
build it; the levers it does use are the prebuilt build image pinned by digest, warm caches and parallel steps.

## Model changes: shadow first

A model change gets a shadow phase before any user sees it: `module11 shadow experiment --source model/<profile>
--stage prod --hours 24`, then `--finish <id>`. In production, shadow a model change for a day or more. The demo's
pre-session shadow was a short real run (`--hours 2`), shown with its measured duration. `docs/MODEL_MIGRATION.md`
is the playbook.

## Demo stacks only: `demo arm` and `demo rearm`

Both commands exist to rehearse and re-take the canary in a recorded demo. Use them on demo stacks only. Their guards
(the demo rollout profile, the armed release, operator principals, the dev lock) are defense in depth; IAM remains
the boundary, because an operator could call the same APIs directly.

**`module11 demo arm --stage dev [--rearm] [--reason]`** deploys the bad payments manifest (`PAYMENTS_TWEAK`) to dev
with the operator `release deploy` and opens a demo-profile rollout. `--stage` accepts only `dev`. It refuses (exit 3,
nothing written) outside the demo profile, for a non-operator caller, while `module11/.local/locks/dev.lock` exists
(a model comparison holds dev), with an active rollout or a latch on the bad digest (use `--rearm`), and when
dev already serves the bad release. `--rearm` stops a dev traffic driver, aborts, clears the latch, restores flags,
purges and resets (exit 4 on any failure: re-run with `--rearm`). It writes a `demo_arm` audit record before the
deploy (exit 5 if that fails; nothing deployed); a deploy error is exit 6.

**`module11 demo rearm [--reason]`** re-runs the armed release's Production stage for a B5 re-take, with no new
approval. It validates first (demo-state `armed`, the demo profile, the execution's sealed tag, an operator caller),
stops the traffic driver, aborts a live rollout, waits for the Production stage to fail (Bake prunes and posts its
note), cleans up (latch clear, flags restore, purge, reset), writes a `demo_rearm` audit record, then calls
`RetryStageExecution(ALL_ACTIONS)` and waits for a new WARMING rollout. About 10-15 minutes.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Re-armed; the `--armed` preflight is green | Re-take `d 5` |
| 2 | Nothing to re-arm: no `armed` record, PROMOTED, or IDLE with nothing to resume | The full reset cycle (below) |
| 3 | A guard refused; nothing changed | Check `AWS_PROFILE` (the operator) and the printed reason, fix it, re-run |
| 4 | A wait timed out, cleanup is incomplete, the retry was refused for now, or credentials failed before it | Re-run `demo rearm`; it resumes |
| 5 | The audit write failed; nothing retried | Re-run `demo rearm` |
| 6 | `RetryStageExecution` refused for good (StageNotRetryable, NotLatestPipelineExecution, other errors) | The full reset cycle |
| 7 | Retried, but no rollout opened within 15 minutes | When it opens, run `live_check --demo --armed`; otherwise the full reset cycle |
| 8 | Re-armed, but the `--armed` preflight is red | Fix the red items it printed |

**The full reset cycle** (about 1:45, presenter only): `module11 demo prep --reset` (stop executions, abort, one
revert MR of every merge since prod `/current`, staging resync, latch clear, flags restore, purge, reset, close demo
MRs, prune), then `module11 demo prep --payments` with a harness-owner approval, the staging gate, then Approve + Run
in GitLab. Both bot MRs merge only once their pipeline passed and the approvals the design requires are there, so
`M11_APPROVER_USERS` must be set (`module11/.env.example`).

## Demo-day runbook (recording day; T = the recording start)

| T- | Step | If red |
|---|---|---|
| T-60 | `source module11/.local/env.sh`; `python -m module11.demo.live_check --demo`: all green, pre-approve state | Follow each item's remediation. A Friday MR re-push needs about 15 min; a staging gate re-run about 45 min, so start later |
| T-50 | Warm-up: `module11 demo prep --warm`, then `d 0 --no-pause`, `d 1 --no-pause`, `d 2 --no-pause` in a scratch terminal (2e reverts the staging drift). Not B3-B6: B5 would consume the armed canary and B6 opens MRs | Fix, or plan `r` for that beat |
| T-40 | Portkey: confirm STOPPED (`python module10/gateway/lifecycle.py status`) | `lifecycle.py stop` |
| T-25 | GitLab > Operate > Environments > `production`: the payments release's blocked deployment. Approve, then Run `promote:production`; wait until `module11 rollout show --stage prod` shows WARMING with 0 sessions (about 3 minutes) | Re-check the CodePipeline wait (preflight 7) |
| T-15 | `python -m module11.demo.live_check --demo --armed`: WARMING, a new rollout ID (written to `demo-state.armed`), 0 outcome items, schedule ENABLED, candidate READY, Bake in progress, `promote:production` succeeded. The rollout opens within 9:45 of the click | Not armed by T-10: hold the start. If `promote:production` failed on its 10-minute wait, run the full reset cycle and record later |
| T-10 | Open the six tabs; sign in to Cognito on the Vercel preview as the platform user; terminal font size; `unset M11_CUT`; `clear`. For a take that an audience will read, `export M11_EXPLAIN=1` (explain mode, off by default; see "Presenting the demo" in `README.md`) | - |
| T-5 | Quiet: no gate, calibration or traffic driver running, no `module11/.local/locks/*.lock`, notifications off | - |
| T-0 | Record `d 0` to `d 7`. Re-take any beat with `d N`; B5 needs `module11 demo rearm` first | `r` on any beat |

**Re-take rules.** Re-take a failed beat with `d N`. Do not re-take 1c while `m11-deploy` or Bake's prune holds the
prod stack (the driver offers `r` instead). A B5 take with no ROLLED_BACK at t0 + 5:30 is stopped, re-armed and
re-taken, never kept; a replay is the last resort.

## Teardown

`module11 teardown --dry-run` prints steps 1-10 and every `ORPHAN?`; `--yes` runs the plan. Steps 8-10 (Module 10,
the bootstrap state buckets and KMS keys, and the partner secrets) are printed for you to run by hand after exporting
state. Revoke the GitLab and Vercel tokens in their consoles before deleting the secrets.
