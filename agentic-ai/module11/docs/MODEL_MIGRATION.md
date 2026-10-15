<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Model migration playbook

When a model you depend on is deprecated, or a better one ships, the change arrives on the provider's schedule, not yours. This playbook turns it into a release like any other: measured against a calibrated baseline, shadowed on real traffic, approved by a person, and rolled out with a canary. It follows the six steps of the source material and maps each one to a `module11` command.

Model IDs are never typed into this document or the commands below. The current model is the `modelId` of each agent in `module11/harness/agent/models.yaml`, and the candidate is the inference profile you pass once to `model compare`, which writes it into the comparison record and the `model/<profile>` branch. Every later step reads it from there.

## 0. Notice

`module11 drift check --stage prod --external` reads the lifecycle of each `models.yaml` base model (`GetFoundationModel`). `ACTIVE` is the normal state. `LEGACY` is a warning: the model is on its way out and the migration should start. The 15-minute drift checker reports deployed state only; run `--external` when you want the outside probes (model lifecycle, the built-in evaluator list, external tool surfaces).

## 1. Calibrate the current model

```
module11 calibrate --manifest prod-current --n 8
```

Eight full-suite runs at three repeats on prod `stable` give the baseline and the noise floor that every comparison below uses. Skip this step if the gate baseline (`thresholds.yaml` `baselineManifest`) already names the release prod serves.

## 2. Compare the candidate on the suite

```
module11 model compare --candidate <inference-profile> --stage dev
module11 model compare show
```

The command creates (or refreshes) the bot branch `model/<profile>` from `main`, with every agent's `modelId` and `foundationModelId` in `models.yaml` set to the candidate. It then measures that branch through the audited experiment path on dev (freeze, `m11-seal-experiment`, the dev `candidate` endpoint, no user exposure), runs the full suite against it at the staging profile's repeats, finishes the experiment (dev back on its `/current`, freeze cleared) and compares each evaluator and category with the calibrated baseline.

The record classifies the change:

| Classification | What it means | What to do |
|---|---|---|
| `broad` | Two or more categories moved beyond the calibrated noise in the same direction | An instruction-style difference. Revise the prompts on the branch |
| `narrow` | One category moved | Usually a tool description or output format. Fix that artifact on the branch |
| `mixed` | Categories moved in different directions | Read the worst scenarios the record lists |
| `none` | Nothing moved beyond noise | Go to step 5 |

`model compare` redeploys dev's `candidate` endpoint, so nothing else should use dev while it runs.

## 3. Revise on the branch

Edit the prompts or tool descriptions on `model/<profile>`, then run `model compare` again. Each change is measured the same way.

## 4. Recalibrate

Once the branch is ready, calibrate again if the baseline you compare against has changed (step 1).

## 5. Shadow on prod

```
module11 shadow experiment --source model/<profile> --stage prod --hours 24 --name model-migration
module11 traffic --stage prod --until-terminal --rate <sessions>/h --hours 24
module11 shadow wait <experiment id>
module11 shadow experiment --finish <experiment id>
module11 shadow report model-migration
```

Every prod session is copied to the branch's candidate, which answers in shadow (mutating tools suppressed) while users get `stable`. After `--hours`, the controller runs two batch evaluations, one over the candidate's shadow sessions and one over the paired stable sessions, and computes paired score deltas. The verdict is `pass` unless an evaluator's mean delta is below the margin with significance. `--finish` restores prod: abort without a latch, reset, `release deploy --reapply`, clear the freeze.

**Production recommendation: shadow a model change for a day or more.** `--hours 24` is the default, and the controller's `maxHours` is 30. A day covers the daily traffic pattern, and the verdict needs enough paired sessions. Even at 12 sessions an hour, 24 hours gives about 288 pairs, above the 200 scored pairs the verdict requires (it scores at most 500).

**What the demo shows.** The pre-staged experiment behind `shadow report model-migration` was a short real run: about 2 hours at 120 sessions an hour (about 240 pairs), so the laptop did not have to stay awake overnight. It ended with 241 pairs and verdict `pass`. The report prints its real duration. Treat that run as a demonstration of the mechanism, not as the evidence you would want before migrating a production model.

## 6. Release the migration

Merge the branch through an MR. A `models.yaml` change is kind `model`: five repeats at the staging gate, approval `required` (a quality reviewer and a harness owner at `production-gated`), and the `shadow-then-canary` rollout on prod, where the shadow phase runs before any user sees the candidate. A failed verdict rolls back without a latch, so the branch can be fixed and re-proposed.

## Where things are recorded

| Record | Location |
|---|---|
| Comparison | `s3://<release-bucket>/evaluations/model-compare/<id>.json` and `module11/.local/model-compare/` |
| Experiment | `module11/.local/experiments/<id>.json`; the control table's audit events; the release ledger (`kind=experiment`) |
| Batch evaluations | named `m11_<stage>_shadow_<rollout>_c1` and `_s1`, listed in the experiment record |
