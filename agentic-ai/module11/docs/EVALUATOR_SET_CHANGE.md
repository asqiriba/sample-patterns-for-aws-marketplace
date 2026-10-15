<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Changing the evaluator set

A gate's statistical rows compare a release with a baseline measured under the same evaluator set. Change the ruler
and the old baseline no longer applies, so an evaluator-set change is its own release event with its own record.

## What counts as a change

Any edit to `harness/evaluation/evaluators.yaml`: adding, removing or reconfiguring an evaluator, changing its level
(session or trace), or a new pinned fingerprint of the service's built-in evaluator list. Bump `version` in
`evaluators.yaml` and `evaluatorSetVersion` in `thresholds.yaml` together; check C-rules refuse a mismatch.

## The procedure

```bash
module11 evalset show                                  # the current set and the change records
module11 evalset rebaseline --from <old> --to <new>    # sets and clears its own planned freeze
module11 calibrate --manifest prod-current --n 8       # the new set's baseline and noise floor
module11 baseline refresh                              # the bot MR that points thresholds.yaml at it
```

`evalset rebaseline` scores the same artifact (prod's current release) with the old and the new ruler and writes
`evalset-changes/<old>-<new>.json`: per evaluator and category, both rulers' means on the same sessions, so a reviewer
sees what the ruler change alone moved. It refuses to start under a freeze it does not own.

## Review

The change MR touches `evaluation/`, so CODEOWNERS names `quality` as well as a harness-owner or platform peer. That
is two people only when the `[Quality]` section names users (`PARTNER_SETUP.md`, "Inherited Owners"): with the group
form, an Owner inherited from the parent group can count as the quality reviewer. The release that carries it is
`required`: it also stops at ExtendedApproval, released only by the `production-gated` environment, which needs one
quality reviewer and one harness owner, both named users. Merge it only with the change record and the new
calibration in place; a gate under a new set without a baseline is INCONCLUSIVE.

## Informational evaluators

An evaluator with no threshold (for example `Builtin.Helpfulness` in set v3) is reported in every gate but gates
nothing. Adding one loosens nothing; it still needs the procedure above, because the set version changes.
