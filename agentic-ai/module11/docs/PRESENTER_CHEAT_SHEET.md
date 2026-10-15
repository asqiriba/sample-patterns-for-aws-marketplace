<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Module 11 presenter cheat sheet (one page)

Release numbers come from live state. `d N` and `live_check --demo` print the real ones; the talk track says
"the payments release" and "the Friday MR". Before the take, read from live state which `harness/v<n>` prod serves,
which `harness/v<n>` is the payments release (its GitLab deployment waits at Approval, its `promote:production` job
is manual), and which MRs are the Friday MR and the fallback MR. Any other release shifts these.

**Before the take.**
- T-25 (recording day), in plain words:
  1. Open the payments release's GitLab deployment (Operate > Environments > production, the blocked
     `harness/v<n>` badge).
  2. Click **Approve**.
  3. Click **Run** on that pipeline's `promote:production` job.
  4. Wait until `python -m module11 rollout show --stage prod` shows **WARMING** with **0 sessions**
     (about 3 minutes after Run; the rollout opens at the end of Production Deploy).
- T-15: `python -m module11.demo.live_check --demo --armed` is green.
- T-10: open the 6 tabs, sign in to Vercel/Cognito. `unset M11_CUT` (full), or `export M11_CUT=15` / `12`.

**Keys.**
- **Enter**: next
- **s**: skip a cuttable sub-beat
- **r**: replay this beat (a real run, under the banner)
- **B5 only:** **t** ticks the controller; **v** jumps to the verdict (only after F5, once ROLLED_BACK)

| Beat | Clock (full) | Type | Say (one line) | Click | Cut | Fallback |
|---|---|---|---|---|---|---|
| B0 | 0:00 | `d 0` | "Shipping once is easy; Module 11 is shipping it the fiftieth time without fear. Something's waiting in prod." | - | - | r |
| B1 | 0:45 | `d 1` | "If it shapes behavior, it's a file, and the infrastructure is computed from it." 1e: "Three stacks in one account today; one account per stage in yours." | - | 1c = C5 | r |
| B2 | 2:45 | `d 2` | "Git is what we intended, SSM is what's running; when they disagree we revert the resource." | - | 2d-2e = C6 | r |
| B3 | 5:00 | `d 3` | "Four words on a Friday ('Skip the runbook search.'): wrong path, and the trajectory gate blocked the merge." Then the takeaway line that matches the note (same answer, or answer scoring is noisy) | Tab 1 MR (commit Verified, code owners: reads satisfied because you are the only harness-owner and the author; say what it maps, not "approval pending"; pipeline, `m11-gate` note; if the box says "Merge conflicts": "It also conflicts with this morning's payments change, but what blocks the merge is the gate."), then tab 2 Vercel: Example (talk over the 20-40 s run) | 3c = C4 | fallback MR, then r |
| B4 | 8:00 | `d 4` | "Hard gates for deterministic checks; statistical gates against a calibrated baseline." | - | 4d = C3; 4a-4c = C8 | r |
| B5 | 9:30 | `d 5` | "The payments team's few words passed every gate. Watch the canary." **Enter = t0** | Tab 3 CodePipeline (F1), tabs 4-6 GitLab environment, package, tag (F2) | none; F1-F5 always run | t at t0+4:30; stop + `demo rearm` at t0+5:30 |
| B5 verdict | ~12:35 | `v` (after F5) | "Rollback restores configuration, not consequences." "Bake prunes, posts its note, then the pipeline turns FAILED." | Tab 3 only if the driver shows Bake FAILED | - | re-take after `demo rearm`; r last |
| B6 | 15:00 | `d 6` | "Every incident becomes a regression scenario; other people's changes become release events." | Tab 3 glance only if Bake FAILED (<= 0:05) | 6d = C1, 6e = C2, 6b-6c = C7 | r |
| B7 | 16:45 | `d 7` | "Completed DevOps agent: one manifest, one gated path, every action stamped, one-call rollback. Next: Module 12." | - | - | r, else say this line |

**Timing marks.**
- Full: 17:15. Edits: 15:15 (C1-C5) and 11:55 (C1-C8). Hard ceiling 22:00.
- **B5, relative to t0** (every take and cut level; the driver prompts `t` and the stop itself):
  - F1-F5 end at t0 + 2:35; `v` works from here.
  - Measured on dev: breach 1 at t0 + 0:38 to 1:04; ROLLING_BACK at t0 + 1:22 to 2:04; ROLLED_BACK at
    t0 + 1:28 to 2:09, so usually during F1-F5: finish F5, then `v`.
  - 5c's purge may print "waiting for k of N notes to reach Memory" for a few seconds: say the reconciliation line.
  - `t` at t0 + 4:30; stop and re-arm at t0 + 5:30.
- In the full version t0 = 9:50, so ROLLED_BACK is at about 11:20-12:00, `v` at about 12:25, `t` (only if not yet
  ROLLED_BACK) at 14:20, the stop at 15:20.
- F4 shows "a shadow run" with its real measured duration (2 h, 241 pairs, pass), never "24 h".
- Two people approve only at `production-gated` (F1's ExtendedApproval line). Never claim it for an MR.
- **Re-take B5:** `module11 demo rearm` (about 10-15 min), then `d 5`. Exit 3: a guard refused and nothing changed;
  check `AWS_PROFILE` (the operator user) and the printed reason, fix it, re-run. Exit 4 or 5: run `demo rearm`
  again. Exit 7: run `live_check --demo --armed` once the rollout opens. Exit 8: fix the red preflight items.
  Exit 2 or 6, or no rollout after exit 7: the full reset cycle (OPERATIONS.md).
