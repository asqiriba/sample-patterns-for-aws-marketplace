<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Incident runbook: a release that misbehaves in production

Rollback restores configuration, not consequences. This runbook covers both.

## 1. Contain

- **A canary is running:** the controller rolls back on two consecutive breaches. To stop it now:
  `module11 rollout abort --stage prod --reason "<incident>"` (latches the behavior digest).
- **One capability is the problem:** `module11 flags kill <flag> --team <team> --stage prod` switches it off for one
  team with no release; `module11 flags restore --stage prod` restores the declared flags.
- **Stop new releases:** `module11 freeze set emergency --reason "<incident>"`.

## 2. Confirm the state

```bash
module11 rollout show --stage prod      # ROLLED_BACK, the reason, the latch
module11 status --all                   # every endpoint READY, live == deployed
module11 drift check --stage prod
```

## 3. Reconcile side effects

Every side effect a session writes is stamped with its manifest version, variant and session. List and purge the
rolled-back release's:

```bash
module11 side-effects --stage prod --release <release>
module11 memory purge --stage prod --release <release> --dry-run
module11 memory purge --stage prod --release <release>
```

The purge waits until no candidate session can still write (the traffic driver finished, or the rollout's terminal
time plus the adapter deadline) and reconciles against the side-effect objects in S3, because Memory indexes writes a
little after they land. It exits 5 with nothing deleted if it cannot get a ready time.

## 4. Close the loop

- Turn the failing session into a regression scenario: `module11 scenario add --from-session <session> --category
  regressions` (redacted; opens a bot MR). The next release meets it at the gate.
- Fix forward through a merge request, or revert. Clear the latch only to re-deploy the same bytes on purpose.
- Lift the freeze: `module11 freeze set none`.
- Record what happened in the release ledger's audit trail (every step above writes one).
