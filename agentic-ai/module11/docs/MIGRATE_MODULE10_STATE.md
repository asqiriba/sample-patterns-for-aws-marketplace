<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->
# Move Module 10's Pulumi state to the Module 11 S3 backend

Module 11 releases Module 10's DevOps Companion, so its Pulumi programs read
Module 10's stacks (`organization/module10-exposure/workshop` and
`organization/module10-portkey/workshop`) through stack references. Stack
references only resolve inside one backend. If you deployed Module 10 with its
documented local file backend and passphrase secrets, move both stacks into the
Module 11 S3 backend once, before the first Module 11 deploy.

Fresh setups do not need this page: create the backend first (step 0), then
deploy Module 10 with `PULUMI_BACKEND_URL` set to the S3 URL and
`--secrets-provider "$M11_SECRETS_PROVIDER"` on `stack init`.

The migration changes no AWS resource. It copies state, re-encrypts its secrets
with AWS KMS, and then applies the one Module 10 maintenance update that the
preview shows (step 8). The file backend and every backup stay on disk, so you
can always go back.

## What you need

- Run everything from `agentic-ai/`, with the AWS profile and Region you used
  for Module 10 and the Pulumi CLI you used to deploy it.
- Module 10's deployment directory: `$MODULE10_DEPLOYMENT_DIR` (by default
  `module10/.local/workshop`) with `state/`, `pulumi-passphrase` (or your
  passphrase), `stack.yaml`, `portkey-stack.yaml` and `pulumi-venv/`.
- Nothing else may update either Module 10 stack while you migrate.

```bash
export AWS_PROFILE=default AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
export MODULE10_DEPLOYMENT_DIR="$PWD/module10/.local/workshop"
export PULUMI_PYTHON_CMD="$MODULE10_DEPLOYMENT_DIR/pulumi-venv/bin/python"
export PULUMI_SKIP_UPDATE_CHECK=true
export VENV=<path to the Module 11 tooling venv (requirements.txt + requirements-pulumi.lock)>
export M11_EXPECTED_ACCOUNT=<your 12-digit account ID>
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export FILE_BACKEND_URL="file://$MODULE10_DEPLOYMENT_DIR/state"
export PULUMI_BACKEND_URL="s3://m11-pulumi-state-$ACCOUNT-$AWS_REGION"
export M11_SECRETS_PROVIDER="awskms://alias/m11-pulumi-secrets?region=$AWS_REGION"
```

## 0. Create the state backends

```bash
$VENV/bin/python -m module11.infrastructure.bootstrap.state_backend --dry-run
$VENV/bin/python -m module11.infrastructure.bootstrap.state_backend
```

This creates two versioned, SSE-KMS, public-access-blocked, TLS-only buckets
and two KMS keys with rotation on: the main backend above, and a separate
preview backend for `mr-*` stacks. It refuses to run in any account other than
`M11_EXPECTED_ACCOUNT` and never adopts a bucket or key that is not tagged
`Project=m11-devops-companion`. A second run reports nothing to create.

Cost: about $1 per month per KMS key plus requests, and cents per month of S3.

## 1. Back up both stacks and their configs

```bash
umask 077
BACKUP="$MODULE10_DEPLOYMENT_DIR/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP" && chmod 700 "$MODULE10_DEPLOYMENT_DIR/backups" "$BACKUP"
export PULUMI_CONFIG_PASSPHRASE_FILE="$MODULE10_DEPLOYMENT_DIR/pulumi-passphrase"
PULUMI_BACKEND_URL="$FILE_BACKEND_URL" pulumi -C module10/deployment/pulumi stack export \
  --stack organization/module10-exposure/workshop --file "$BACKUP/module10-exposure.file-backend.json"
PULUMI_BACKEND_URL="$FILE_BACKEND_URL" pulumi -C module10/gateway/pulumi stack export \
  --stack organization/module10-portkey/workshop --file "$BACKUP/module10-portkey.file-backend.json"
cp -p "$MODULE10_DEPLOYMENT_DIR/stack.yaml" "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml" "$BACKUP/"
cp -Rp "$MODULE10_DEPLOYMENT_DIR/state" "$BACKUP/state-file-backend"
```

Exports keep secrets encrypted (`--show-secrets` is off by default). Step 2
rewrites `stack.yaml` and `portkey-stack.yaml`, so the byte copies matter.

## 2. Switch the secrets provider to KMS (on the file backend)

`change-secrets-provider` only edits `Pulumi.<stack>.yaml` in the project
directory, so each config is copied in, migrated, and copied back.

```bash
for pair in deployment/pulumi:module10-exposure:stack.yaml gateway/pulumi:module10-portkey:portkey-stack.yaml; do
  IFS=: read -r dir project config <<< "$pair"
  cp -p "$MODULE10_DEPLOYMENT_DIR/$config" "module10/$dir/Pulumi.workshop.yaml"
  PULUMI_BACKEND_URL="$FILE_BACKEND_URL" pulumi -C "module10/$dir" stack change-secrets-provider \
    --stack "organization/$project/workshop" "$M11_SECRETS_PROVIDER"
  cp "module10/$dir/Pulumi.workshop.yaml" "$MODULE10_DEPLOYMENT_DIR/$config"
  rm "module10/$dir/Pulumi.workshop.yaml"
done
unset PULUMI_CONFIG_PASSPHRASE_FILE PULUMI_CONFIG_PASSPHRASE
```

Both configs now carry `secretsprovider: awskms://…` and an `encryptedkey`
(a KMS-wrapped data key, not a secret by itself). No passphrase is needed from
here on.

## 3. Export again

```bash
for pair in deployment/pulumi:module10-exposure gateway/pulumi:module10-portkey; do
  IFS=: read -r dir project <<< "$pair"
  PULUMI_BACKEND_URL="$FILE_BACKEND_URL" pulumi -C "module10/$dir" stack export \
    --stack "organization/$project/workshop" --file "$BACKUP/$project.kms.json"
done
```

## 4. Log in to the S3 backend

```bash
pulumi login "$PULUMI_BACKEND_URL"
```

## 5 and 6. Create the stacks there and import

The migrated config is put in place first, so `stack init` reuses its
encrypted key instead of creating a new one. Import the Portkey stack first;
the application stack references it.

```bash
for pair in gateway/pulumi:module10-portkey:portkey-stack.yaml deployment/pulumi:module10-exposure:stack.yaml; do
  IFS=: read -r dir project config <<< "$pair"
  cp -p "$MODULE10_DEPLOYMENT_DIR/$config" "module10/$dir/Pulumi.workshop.yaml"
  pulumi -C "module10/$dir" stack init "organization/$project/workshop" \
    --secrets-provider "$M11_SECRETS_PROVIDER" --no-select
  pulumi -C "module10/$dir" stack import --stack "organization/$project/workshop" \
    --file "$BACKUP/$project.kms.json"
  cmp "module10/$dir/Pulumi.workshop.yaml" "$MODULE10_DEPLOYMENT_DIR/$config" && \
    rm "module10/$dir/Pulumi.workshop.yaml"
done
pulumi stack ls -C module10/deployment/pulumi --all   # both stacks, same resource counts as the file backend
```

## 7. Preview against the maintenance plan

```bash
pulumi -C module10/gateway/pulumi preview --diff --stack organization/module10-portkey/workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
pulumi -C module10/deployment/pulumi preview --diff --stack organization/module10-exposure/workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
```

Expected:

- `module10-portkey`: no changes.
- `module10-exposure`: exactly one in-place update,
  `~ aws:s3/bucketLifecycleConfigurationV2:BucketLifecycleConfigurationV2 artifacts-retention`,
  which adds the `demo-manifests` rule (prefix `manifests/`, 30-day expiration
  and noncurrent expiration). This is the maintained Module 10 code's retention
  fix. If your Module 10 deployment already has it, both previews show no
  changes; skip step 8.

**Anything else stops the migration.** Do not run `up`. Keep using the file
backend (see "Roll back") and investigate the difference first.

## 8. Apply that one update

It changes a live Module 10 resource (the artifact bucket's lifecycle rules) in
place and touches nothing else.

```bash
pulumi -C module10/deployment/pulumi up --yes --skip-preview --stack organization/module10-exposure/workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
```

No Module 10 image is rebuilt; the running images keep working.

## 9. Preview again

Re-run both commands from step 7. Both must report no changes.

## Afterwards

- Use `PULUMI_BACKEND_URL=s3://m11-pulumi-state-<account>-<region>` (and no
  passphrase variables) for every later Module 10 command. A copy of your
  session helper with only that line changed is the simplest way.
- Keep the file backend and the backups. The file backend is frozen at its
  pre-update state; the S3 backend is now the source of truth, so do not run
  `up` against the file backend.
- Module 10's teardown is unchanged, except that it runs against the S3
  backend.

## Roll back

Before step 8, the S3 copies can simply be abandoned:

```bash
cp -p "$BACKUP/stack.yaml" "$BACKUP/portkey-stack.yaml" "$MODULE10_DEPLOYMENT_DIR/"
export PULUMI_CONFIG_PASSPHRASE_FILE="$MODULE10_DEPLOYMENT_DIR/pulumi-passphrase"
for pair in deployment/pulumi:module10-exposure gateway/pulumi:module10-portkey; do
  IFS=: read -r dir project <<< "$pair"
  PULUMI_BACKEND_URL="$FILE_BACKEND_URL" pulumi -C "module10/$dir" stack import \
    --stack "organization/$project/workshop" --file "$BACKUP/$project.file-backend.json"
done
pulumi -C module10/deployment/pulumi stack rm organization/module10-exposure/workshop --preserve-config --yes
pulumi -C module10/gateway/pulumi stack rm organization/module10-portkey/workshop --preserve-config --yes
pulumi login "$FILE_BACKEND_URL"
```

The two `stack rm` commands remove only the S3 copies of the state (they
refuse while a stack holds resources; add `--force` only for these S3 copies,
never for the file backend). If `state/` itself was damaged, restore
`$BACKUP/state-file-backend` over it instead of importing.

After step 8, the lifecycle rule exists in AWS but not in the file backend's
state. Rolling back is the same, followed by one `pulumi up` from the file
backend, which reapplies the same rule and changes nothing in AWS.
