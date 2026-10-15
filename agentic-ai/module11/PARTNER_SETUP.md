<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Module 11 partner setup: the first seed of the release pipeline

The release pipeline (`m11-release`) reads the GitLab harness project through
AWS CodeConnections. The project's protected tags, environments, CI variables
and seed commits must exist before the pipeline's first run, and some of them
point at roles that the pipeline increment creates. Apply them in this order.
Run every command from `agentic-ai/` after `source module11/.local/env.sh`.

1. **Create the connection.** Set `createGitlabConnection: true` in the
   `m11-platform` stack config and apply it:

   ```bash
   pulumi -C module11/infrastructure/platform up --stack shared \
     --policy-pack "$PWD/module11/infrastructure/policy"
   ```

   The connection `m11-gitlab` starts PENDING. In the console, open Developer
   Tools > Settings > Connections > `m11-gitlab` > **Update pending
   connection** and authorize the GitLab group. It reads AVAILABLE afterwards.
   The policy pack needs an absolute path.

2. **Pipeline prerequisites in GitLab.** Apply only the settings the pipeline
   depends on: the protected `harness/v*` tags (creatable by the bot only), the
   `production` and `production-gated` environments, the OIDC sub-claim
   components and the CI variables.

   ```bash
   python -m module11 partners up --pipeline-only \
     --approver-users '{"harness-owners": ["<user>"], "platform": ["<user>"], "quality": ["<user>"]}'
   ```

   The approver rules use the user form while the bot cannot read the groups
   a project is shared with. The role-ARN variables hold the placeholder
   `pending-m11-platform-pipeline` until step 5.

3. **Seed the project.** The bot commits a filtered snapshot of `agentic-ai/`
   on top of the initial commit, with `.gitlab-ci.yml` last:

   ```bash
   python -m module11.gitlab.seed --group <group> --quality-users <quality-user>
   ```

   Never force-push the project. Later changes land as ordinary commits or
   merge requests.

4. **The pipeline increment.** Build and pin the build image, then set
   `enablePipeline: true`, `gitlabProjectId` and `buildImage` (the pinned
   digest) and apply `m11-platform` again. This creates the pipeline, its
   CodeBuild projects and the GitLab OIDC roles.

   ```bash
   python -m module11 pipeline build-image --pin
   pulumi -C module11/infrastructure/platform up --stack shared \
     --policy-pack "$PWD/module11/infrastructure/policy"
   ```

5. **Real role ARNs in the CI variables.** Run the step 2 command again.
   `M11_GITLAB_CI_ROLE_ARN`, `M11_GITLAB_CI_INFRA_ROLE_ARN` and
   `M11_PROMOTER_ROLE_ARN` now hold the roles from step 4.

6. **Check the CI image.** The seed's tag pipeline runs the `ci-image` job,
   which pushes `m11-ci:<M11_CI_IMAGE>` to the project's container registry.
   Its tag must match `M11_CI_IMAGE` in `.gitlab-ci.yml` (check C15).

The merge-request governance (protected `main`, code owners, push rules,
approval rules) and the Vercel project come later, from the full
`partners up` without `--pipeline-only`.

7. **The merge-request governance.** Once every change you want to push directly is on `main`, run the full apply.
   After it, `main` takes merge requests only:

   ```bash
   python -m module11 partners up \
     --approver-users '{"harness-owners": ["<user>"], "platform": ["<user>"], "quality": ["<another-user>"]}'
   ```

   It sets and reads back the merge gate (pipelines must succeed, no merge on a skipped pipeline) through the API,
   because an imported project never gets those flags from Pulumi. It also turns on web-based commit signing through the project API, so the bot's Commits-API commits show
   Verified. It registers the bot's signing key and runs the group-overlap check, which needs the groups to be
   readable by the token and otherwise asks you to check them by hand. It copies the Vercel project, the team
   slug and the bot's login and commit email into `m11-platform`'s config. Apply `m11-platform` once more so
   `mr-*` previews can read them from the shared outputs. The Vercel project exposes system environment
   variables, because `build.mjs` needs `VERCEL_BRANCH_URL`.

8. **Release queue.** Every push to `main` before a release reaches prod seals another release, and each one
   carries everything prod doesn't run yet. Approve the newest one, and stop the older ones that are still
   queued. Their tags stay.

A code-owner section whose only eligible member wrote the MR has no one left who may approve it, because author
approval is off. GitLab then shows that section as satisfied, and the MR still needs one approval from another
member. Give each code-owner group at least two people so an author never satisfies their own section.

In short, the first-seed order is: seed the project, `partners up --pipeline-only`, approve the CodeConnections
connection, then the full `partners up`. There is no CodeCommit step: the pipeline reads GitLab from its first run.

## Your one-time steps (O1-O6)

| # | Who | Step |
|---|---|---|
| O1 | You | Create a project access token for the harness project (Maintainer, `api` scope) and store it as `devops-companion/partners/gitlab-token`. Its bot user is the CI and bot identity everywhere this module says "bot" |
| O2 | You | Create the groups `harness-owners`, `platform` and `quality` under your GitLab group, add their members directly, and share the project with all three at Developer. `quality` must share no member with the other two, directly or through inherited group membership |
| O3 | You | Create a team-scoped Vercel token for your team and store it as `devops-companion/partners/vercel-token` |
| O4 | Presenter | Add an SSH key with Authentication & Signing usage to GitLab. In the clone you push from only (`git config --local`, never `--global`), set `gpg.format ssh`, `user.signingkey <key>.pub`, `commit.gpgsign true`, `core.sshCommand "ssh -i <key> -o IdentitiesOnly=yes"`, and a `user.email` that is a verified GitLab email linked to your Vercel account |
| O5 | You | Approve the PENDING `m11-gitlab` connection in the console (step 1 above), and install Vercel for GitLab on the group |
| O6 | You | Link GitLab under Vercel Login Connections, so a team member's commits deploy a UI preview |

## GitLab on an Ultimate trial

The build ran on a GitLab Ultimate trial with one project access token. These adaptations keep every security
property; on annual Premium or Ultimate you can undo each one:

- **One token, a notes broker.** A trial cannot create a second project token, and no preview job may read
  the bot token. The `m11-gitlab-notes` Lambda alone reads it, on behalf of the preview, gate, bake and experiment
  roles, and offers fixed operations only: the marked `m11-gate` MR note, a commit note, job, deployment and approval
  reads, a commit read and a commit archive export. With a second token, point `notesCredentialSecret` at it.
- **Groups are yours to create.** A project token cannot create or list groups. `partners up` resolves group
  IDs from the project's `shared_with_groups` and stops with a clear message if one is missing.
- **The bot is the token's bot user.** `partners up` generates the bot's SSH signing key into Secrets Manager
  and registers it; if GitLab refuses, bot commits rely on GitLab web-commit signing through the Commits API, which
  shows them as Verified.
- **The seed is API commits.** `seed.py` lands the filtered snapshot as Commits-API commits on top of the
  project's first commit. No force-push, no history rewrite.
- **Inherited Owners.** Group Owners inherit membership of every subgroup and project. GitLab's documentation says
  code-owner groups count direct members only, yet in this build an Owner inherited from the top-level group was
  accepted for the `quality` code-owner section: one person's approval let an evaluation change merge. So:
  - name the quality reviewers as users: `seed.py --quality-users` (or `M11_QUALITY_USERS`) renders the
    `[Quality]` section with them, and `partners up` always writes the `production-gated` quality rule by user;
  - set `M11_APPROVER_USERS`: the bot merges its own MRs only when the approvals the design requires are there
    (a quality reviewer who is not a harness-owner or on platform, the author never counting), not on GitLab's
    approved flag alone;
  - keep the quality reviewers out of the parent group's Owners. The overlap check reports direct and inherited
    overlap.

  Until the `[Quality]` section names users, treat MR-level quality separation as not enforced. Two-person approval
  at `production-gated` is enforced either way, because its rules name users.

## Vercel Pro trial

A Vercel Pro team (Hobby is for non-commercial personal use) builds `module10/ui` once per merge request from
GitLab and serves it on a branch alias. Only commits by team members deploy a preview, so bot-authored MRs get the
API gate and a "UI preview skipped" note. The preview's `/config.json` points at the MR's own `mr-<iid>` harness
stack, and its Cognito redirect is registered per MR.

## Rotating the tokens

1. Create the new token (GitLab project access token, or Vercel team token) with the same scope.
2. Store it without putting it in your shell history:
   `read -rs TOKEN && aws secretsmanager put-secret-value --secret-id devops-companion/partners/gitlab-token
   --secret-string "$TOKEN"; unset TOKEN` (or `vercel-token`). Every reader fetches the secret by ARN at run time,
   so nothing else changes.
3. Run `python -m module11.demo.live_check --demo` (items 8 and 9 read through the new token), then revoke the old
   token in GitLab or Vercel.
