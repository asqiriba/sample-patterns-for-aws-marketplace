<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Module 11 talk track (draft)

Draft for the recording. Clock budgets are the plan; measured per-beat times are **to be measured** in the Monday
rehearsals (R1 full, R2 at the 12-minute cut) and the automated dry run, and replace the budgets here afterwards.
The driver prints real tag and MR numbers from live state; say "the payments release" and "the Friday MR". Before
the recording, read from live state (`python -m module11.demo.live_check --demo`) which `harness/v<n>` prod serves,
which release is the payments release, and which MRs are the Friday MR and the fallback MR: any other release
shifts them.

**Shape.** Eight beats, `d 0` to `d 7`. Full version **17:15**; the 15-minute edit **15:15** (cuts C1-C5); the
12-minute edit **11:55** (cuts C1-C8). Hard ceiling 22:00. Exactly one live race: B5, the prod canary.

**"Ran before the session."** Anything that takes longer than a beat ran beforehand, for real, in this account. The
screen says `RAN BEFORE THE SESSION: real run <id> at <ts>`; when you switch to a browser tab that shows such a run,
say "this ran before the session". GitLab, Vercel and CodePipeline show the run's own timestamps. A replay (`r`) runs
under a yellow `REPLAY of real run` banner; say so if you use one.

---

## B0 Context (0:00-0:45, `d 0`)

- On screen: `module11 status --all`: dev, staging and prod, endpoints, manifest versions, drift, and prod's rollout
  WARMING with 0 sessions.
- Context line: "The DevOps Companion investigates incidents with read-only evidence tools and hands recovery planning
  to a second agent, the Recovery Planner. Module 10 put it on AgentCore Runtime behind API Gateway and Cognito. Today
  it gets a release process. Something is already waiting in production; we'll get to it."
- Takeaway: "Shipping an agent once is easy. Module 11 is about shipping it the fiftieth time without fear."

## B1 The harness is the release unit, declared in code (0:45-2:45, `d 1`)

- 1a (0:20) `module11 tree`: prompts, eight tools with schema, description, permissions and handler, orchestration,
  models, guardrail, retrieval, external surfaces, flags, CODEOWNERS.
- 1b (0:30) `infrastructure/harness/tools.py`: each of the seven Lambda-backed tools becomes a role, a generated
  policy, a log group and a published version. `delegate_recovery_planning` runs in-process and gets no role.
- 1c (0:35, **C5**) `pulumi preview --stack prod --diff --policy-pack`: "no changes", 0 policy violations, even with
  the rollout armed, because the program reads only the sealed inputs `/applied` names. Narrate while it runs.
- 1d (0:10) `toolPolicies` for `search_runbooks`: one `s3:GetObject` on the release bucket's policy snapshots.
- 1e (0:25, **never cut**) `module11 resources --stage prod --kind guardrail,identity,deployer`, then the account line:
  "Today dev, staging and prod are three stacks in one account, so a staging gate can eat production model quota. In
  your organization use one account per stage: quota, cost attribution and blast radius are per account, the
  deployment role is the only way in, and the code takes an account per stage as configuration."
- Context lines: "Module 5 said the DomainConfig is what an MLOps pipeline promotes. This directory keeps that
  promise." The one IaC sentence: "Pulumi is still the IaC from Module 10; the slides show the same harness shape in
  CDK and Terraform."
- Takeaway: "If it shapes behavior, it is a file, and the infrastructure is computed from it. Nobody hand-writes seven
  IAM policies."

## B2 The manifest: hash, declared vs. deployed, drift (2:45-5:00, `d 2`)

- 2a (0:20) `module11 manifest build`: 55 hashes in groups, `behaviorDigest` (50) and `releaseDigest` (55).
- 2b (0:15) `--explain-normalization` with a trailing space and CRLF endings on a scratch copy: both digests stay.
- 2c (0:25) two diffs: prod's tag against prod is "identical"; `main` against prod shows one changed artifact,
  `instructions/companion`: "main declares the payments team's edit, and prod still serves the previous release. The
  edit is the armed candidate."
- 2d (0:30, **C6**) the unreviewed hotfix as the admin profile, then `drift check --stage staging`: `DRIFT endpoint
  staging: target <older> (UPDATING) != declared <current>`. Context line, said with 2d and cut with it: "One account
  means my admin user could do that. In the recommended layout humans are read-only in each stage's account, and the
  deployment role is the only way in."
- 2e (0:45, **C6**) `drift revert --stage staging`: the stage deployer waits out UPDATING, retargets, polls READY.
  If it runs past 0:40 the driver says "revert continuing in background" and moves on; B6's first line reports it.
- Takeaway: "Git says what we intended. SSM says what is running. When they disagree, we revert the resource; we never
  edit the manifest to match reality."

## B3 Friday, four words: the merge request (5:00-8:00, `d 3`)

Describe only what the pre-session MR pipeline shows; the script reads correctly for both outcomes.

- 3a (0:20) the Friday commit: "It's Friday afternoon. Someone wants shorter diagnoses and appends four words":
  "Skip the runbook search." Signed, pushed before the session.
- 3b (0:20) `module11 detect --base main`: instructions, so a bundle version and a config-only runtime version, no
  image build, eval scope full, trajectory hard gate.
- 3c (0:30, **C4**) the consistency check both ways on a scratch copy: remove the `search_runbooks` mention (C5 fails),
  write `restart_service` (C4 fails).
- 3d (0:40) tab 1, the MR: Verified commit; the code-owner line for `agent/prompts/` (`harness-owners`). On the
  Friday MR it reads as satisfied, because the presenter is the only harness-owner and is the author: name what it
  maps and don't say an approval is pending. `validate` and `preview:deploy` passed; `preview:gate` failed; the
  `m11-gate` note: the trajectory gate FAIL on regressions naming the repeats that skipped `search_runbooks`, and
  every other row as the note shows it (on the Friday MR the trajectory gate is usually the only failing row); merge
  blocked because the pipeline must succeed. The merge box may also list "Merge conflicts must be resolved": the MR
  was opened Friday, before main moved with the payments change. Say: "It also conflicts with this morning's payments
  change, but what blocks the merge is the gate."
- 3e (1:00) tab 2, the Vercel preview: click the Example "Checkout returns 502s since the 14:20 deploy" and talk over
  the 20-40 s live run. Usually the answer is fluent and Agent activity lists no `search_runbooks`. If this run does
  call it: "This time it took the runbook path. That is why the gate repeats every case and blocks on any repeat that
  skips it."
- Context lines: "Code owners map each behavioral surface to the people who review behavior, and nobody can push to
  main, so this MR is the only way in." Optional, if asked why the MR is not rebased: instructions interact, so
  evaluate the combination you ship. In our runs, on Friday's base the four words failed the trajectory gate on
  every gate run (the Friday MR and the fallback MR, 7 of 10 repeats each); rebased onto the payments sentence the
  same four words failed one gate run of two. "Every MR gets its own front end, pointed at its own harness."
- 3f (0:10) the takeaway that matches the note:
  - inside tolerance: "Same answer, wrong path. Answer scoring would have shipped this on Friday. The trajectory gate
    and the session assertions blocked the merge, and the reviewer saw it on a preview of the real UI."
  - below tolerance: "Answer scoring is noisy: sometimes it notices, sometimes it doesn't. The trajectory check is red
    every time. That is why deterministic checks are hard gates, and the reviewer saw it on a preview of the real UI."

## B4 Designing the gate (8:00-9:30, `d 4`)

- 4a (0:25, **C8**) `incident-triage-014`'s expected trajectory and assertions; `requiredTools` requires only
  `get_deployment_history`: "Skipping a runbook is a behavior we measure, not a crash we prevent."
- 4b (0:25, **C8**) `thresholds.yaml`: hard gates, statistical gates, budgets. On screen: "Latency is compared against
  the current version measured at the same time, so a slow provider doesn't fail a good release."
- 4c (0:25, **C8**) `module11 calibration show`: 8 full-suite runs of the baseline, the noise floor at r = 3, and the
  rule that a tolerance is at least the scaled noise.
- 4d (0:15, **C3**) `module11 evalset show`: the v2 to v3 change record, old and new ruler on the same artifact.
- Context line: "Module 3 taught evaluator design. Here those evaluators become a release gate."
- Takeaway: "Deterministic checks are hard gates. Judge scores are statistical gates against a named baseline under
  the same evaluator-set version, calibrated before they are enforced."

## B5 The one that got through: canary and automatic rollback (9:30-15:00, `d 5`, LIVE RACE, never cut)

- 5a (0:20) `rollout show --stage prod`: WARMING, canary 50% (demo profile, labelled), 0 sessions. Say: "This is the
  payments team's edit, a few words: 'For payments, never call search_runbooks.' They keep their own runbooks
  elsewhere and wanted cheaper, quicker payments investigations, so the shared runbook lookup looked like the step
  to drop. Reasonable on its face, but the payments
  timeout runbook is where the safe fix lives: switch the fraud-score call off instead of rolling back. It passed
  every gate, because our suite has no payments-specific runbook scenario. It's armed as a canary, and no traffic is
  on it yet. Let's send some." **Enter = t0.**
- F1 (0:35) tab 3, the payments release's CodePipeline execution: Image and ExtendedApproval SKIPPED, the staging gate
  passed. "A model, evaluator-set or unknown change would also stop at ExtendedApproval, which needs two people to
  approve in GitLab: a quality reviewer and a harness owner."
- F2 (0:35) tabs 4-6: the approved `production` environment, the package, the protected tag. "The human decision
  lives in a protected GitLab environment. Only that job's role can release the AWS approval."
- F3 (0:25) `module11 versions --stage prod`, then the five tiers: IaC, manifest, configuration bundle, runtime
  parameters, flags.
- F4 (0:45) `shadow report model-migration` (a real 2-hour shadow run, 241 paired sessions, verdict pass; say "a
  shadow run", never "24 hours"), `model compare show`, `abtest show`. "A model change gets a shadow first; in production,
  a day or more. An instruction change starts at canary."
- F5 (0:15) `flags kill module10-recovery-planning --team payments --stage prod`: "One capability off for one team,
  with no release. Flags are stage-wide, so both arms change together and the comparison holds."
- F6 (until ROLLED_BACK) the tick table. Narrate the rules as rows arrive: minimum samples per arm and new sessions per
  counted tick; the candidate warm-up: latency and cost wait 4 minutes after the candidate's first session, because a
  new version starts on cold capacity, while tool selection, errors and quality count from the first session; two
  consecutive breaches; canary to 0 first, then one retarget, then the READY poll; the latch.
- Timing: measured on dev, ROLLED_BACK lands at t0 + 1:28 to 2:09, usually during F1-F5: finish F5, then `v`.
  The driver prompts `t` at t0 + 4:30 and the stop at t0 + 5:30.
- 5b (0:40) verdict: ROLLED_BACK with its reason, the latch, Bake's status. "Bake now prunes prod's retained pins and
  posts its outcome note; the pipeline turns FAILED (rolled back) when that finishes." Tab 3 only if Bake already
  shows Failed. "Why did this pass staging? The suite has no payments-specific runbook scenario. That's the gap; we'll
  close it in a minute."
- 5c (1:05) `side-effects`, `memory purge --dry-run`, then the purge. It may print "waiting for k of N notes to
  reach Memory" for a few seconds; that is the reconciliation, not a fault. Say: "Memory indexes writes a little after they
  land, so the purge counts what the canary actually wrote and waits until every one is gone. Rollback doesn't undo
  side effects; you reconcile them."
- 5d (0:15) takeaway: "Rollback restores configuration, not consequences. Limit exposure, stamp every side effect, and
  make a bad version impossible to advance again."
- Context line: "A canary sends a slice of real traffic to the new version. Here it's half the sessions on a compressed
  demo profile, labelled on screen; the production profile takes hours."

## B6 Coordination and closing the loop (15:00-16:45, `d 6`)

- (C6) If B2's revert went to the background, the first line reports it. The 12-minute edit drops it with 2d-2e.
- 6a (0:15, never cut) Bake's status line, then `scenario add --from-session <canary session> --category
  regressions`: "Every incident produces a regression scenario. The next payments edit meets this one at the gate."
- 6b (0:25, **C7**) `drift check --stage prod --external`: model lifecycle ACTIVE, the built-in evaluator list
  unchanged, the external surface's hash matches its pin (a stand-in target, labelled). One line on
  `docs/MODEL_MIGRATION.md`.
- 6c (0:25, **C7**) `cost --stage prod --by manifest`: cost per successful session, previous release vs the payments
  edit.
- 6d (0:20, **C1**) `corpus publish --corpus policy --change runbook-checkout-v2`: an immutable snapshot, an
  EventBridge event, an MR. "Module 9 built the ingestion pipeline. Here its output becomes a release event."
- 6e (0:20, **C2**) `recommendation show`, then `import`: "A recommendation is a hypothesis. It goes through the same
  gate."
- Takeaway: "Models, corpora, evaluators, and other teams' tools change on someone else's schedule. Make each change a
  visible release event." At the 12-minute cut only 6a remains; its line is the takeaway.

## B7 Wrap (16:45-17:15, `d 7`)

- Close: "A completed DevOps agent: every behavior input pinned in one manifest, every change through one gated path,
  every session and side effect naming its manifest version, and a bad release rolled back with one call."
- Bridge: "Module 12 goes deeper on security and observability: securing this pipeline, prompt injection, guardrail
  versioning, tool credentials, and auditing agent-initiated actions."

---

## Cut list and edit points

Record the full version once; cut the 15- and 12-minute edits in post at the cut points the driver writes to
`module11/.local/timings/<run>.json`. No narration refers back to a cuttable sub-beat.

| Cut | Sub-beats | Saves | Running total | Edit point |
|---|---|---|---|---|
| C1 | 6d corpus event | 0:20 | 16:55 | from 6c's end to 6e's start |
| C2 | 6e recommendation | 0:20 | 16:35 | from 6d's (or 6c's) end to B6's takeaway |
| C3 | 4d evaluator-set record | 0:15 | 16:20 | from 4c's end to B4's takeaway |
| C4 | 3c consistency check | 0:30 | 15:50 | from 3b's end to 3d's start |
| C5 | 1c `pulumi preview` | 0:35 | **15:15** | from 1b's end to 1d's start |
| C6 | 2d-2e drift hotfix and revert, plus B6's revert line | 1:15 | 14:00 | from 2c's end to B2's takeaway; B6's first line |
| C7 | 6b-6c external drift and cost | 0:50 | 13:10 | from 6a's end to 6d's start |
| C8 | 4a-4c gate design (B4 drops out) | 1:15 | **11:55** | from B3's end to B5's start |

Never cut: B0's context line and B7's wrap; B1's tree, `tools.py`, `toolPolicies` and 1e; B2's build, normalization
and diff; B3's edit, detect, MR and preview; all of B5 including F1-F5; B6's `scenario add`.

## Fallback ladder (B5)

1. **Not armed at 5a.** The driver starts no traffic and says why. Stop the take, `module11 demo rearm`, re-take `d 5`.
2. **The traffic driver fails** (edge errors in its first minute, or it exits without finishing). Stop the take,
   `demo rearm`, re-take.
3. **Not ROLLED_BACK at t0 + 4:30.** Press `t` (`module11 rollout tick`); once more 20 s later.
4. **Not ROLLED_BACK at t0 + 5:30, or PROMOTED.** Stop the take, `demo rearm`, re-take; the edit uses the re-take.
   Only if the re-arm and the full reset both fail: `r` (the recorded canary), and say it is a replay.

Other beats: `r` replays the beat's capture of a real run. B3's GitLab page failing: the terminal summary from the API.
The Vercel click erroring or taking over 60 s: the fallback MR's preview, then `r`.

## Slide corrections (the slides vs. the current docs)

1. A/B CLI: the docs use `agentcore run ab-test --mode target-based|config-bundle ... --control-weight 80
   --treatment-weight 20` and `agentcore view ab-test <id>`; API variant names are `C` and `T1`.
2. Evaluations are available in 22 Regions, not 9.
3. `ListEvaluators` returns 18 built-in evaluators, not 13.
4. Ground truth by mode: online evaluation takes none; on-demand takes `evaluationReferenceInputs`; batch takes session
   metadata. Batch also serves baselines and regression tests, not only shadow scoring.
5. Configuration bundle version IDs are UUIDs, components are keyed by ARN, names allow letters, digits and
   underscores; propagation uses the W3C baggage keys `aws.agentcore.configbundle_arn` and
   `aws.agentcore.configbundle_version`.
6. CDK: the L2 artifact class is `AgentRuntimeArtifact`, not `RuntimeArtifact`.
7. The manifest uses inference-profile IDs; a base-model ID such as `anthropic.claude-sonnet-4-5-20250929-v1:0`
   cannot be called on demand.
8. Token usage comes from the agent's run usage, not a built-in AgentCore metric.
9. Endpoint `targetVersion` is omitted when idle; the status enum includes `DELETING`.
10. Slide note: the demo uses one runtime per stage (and recommends one account per stage), not one runtime with two
    endpoints.
11. Slide note: "do not re-implement session routing" stands; the demo routes sessions itself only for the rollback
    controller.
12. Pulumi Cloud features are not used: per-MR environments are GitLab-driven `mr-<iid>` stacks, and CrossGuard runs as
    open source.
