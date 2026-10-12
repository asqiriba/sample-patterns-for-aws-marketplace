# Operations

Run commands from `agentic-ai/` with your AWS credentials set.

## Costs

Approximate USD rates for US East (N. Virginia), excluding free-tier credits:

| Item | Estimate |
|---|---|
| Active Portkey task and public IPv4 | **$0.025/hour**, about **$0.05 for two hours** |
| Three secrets, 2 GB ECR images, and 1 GB S3 data | **$1.42/month**, plus logs and other retained data |
| ARM CodeBuild | **$0.03–$0.07** per 10–20 minute image build |
| AgentCore | Budget **$0.13/active vCPU-hour + $0.017/GB-hour** |
| Models, API, Lambda, telemetry, and data transfer | Additional usage charges |

Confirm current rates with [Fargate pricing](https://aws.amazon.com/fargate/pricing/),
[IPv4 pricing](https://aws.amazon.com/vpc/pricing/),
[AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/), and the
[AWS Pricing Calculator](https://calculator.aws/). Close browser tabs and stop
Portkey after use.

## Restore the deployment environment

```bash
export MODULE10_DEPLOYMENT_DIR="$PWD/module10/.local/workshop"
export PULUMI_PYTHON_CMD="$MODULE10_DEPLOYMENT_DIR/pulumi-venv/bin/python"
export PULUMI_BACKEND_URL="file://$MODULE10_DEPLOYMENT_DIR/state"
pulumi login "$PULUMI_BACKEND_URL"
```

Use the passphrase chosen at initialization.

## Updates

1. Stop Portkey using the command below.
2. For application/gateway source changes, repeat the image build, `imageDigest`,
   and `up` commands in the [deployment guide](README.md): gateway first, then
   application. Skip stack initialization and the bootstrap `up` commands.
3. Refresh both outputs files and rerun `setup_users` with the existing
   credentials file.
4. Repeat **Start and verify** from the deployment guide.

If `runtimeVersion` pins an old version, remove that override before updating:

```bash
pulumi -C module10/deployment/pulumi config rm runtimeVersion \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
```

For UI/Lambda-only changes:

```bash
pulumi -C module10/deployment/pulumi up --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
```

## Stop

```bash
.venv/bin/python -m module10.gateway.lifecycle stop \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
.venv/bin/python -m module10.gateway.lifecycle status \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
```

Confirm desired, running, and pending counts are zero.

## Network exposure

The Portkey task's security group allows `0.0.0.0/0` on port 8443. The
AgentCore Runtime runs in `PUBLIC` network mode and reaches Portkey from
AWS-owned addresses that cannot be predicted, so a CIDR allowlist would break
cloud mode. The compensating controls are TLS, a bearer service token checked in
`module10/gateway/hosted.mjs` (a random value the gateway stack keeps in Secrets
Manager), an auto-stop schedule (two hours by default, at most 12 hours), and a
public task IP that changes on every start. For production, run the agent in a
VPC-mode AgentCore Runtime with a private gateway.

## Request-limit demonstration

Section 1 of the cloud demo sends a burst of no-model probes to show the stage's
request limit. The result is best effort: API Gateway throttling depends on
token-bucket timing, so the same burst can see no 429 responses at all. The
deployment record shows 0 of 30 probes throttled in one demo run and 17 of 30 in
`verify_cloud`. The demo shows "No throttle observed in this burst" when that
happens.
Presenter tip: rerun Section 1 for another burst, or show the
`gateway_probe_throttles` count in the `verify_cloud` report.

## Log retention for AgentCore runtime logs

AgentCore Runtime creates its log groups itself, outside Pulumi, as
`/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>` (one per endpoint,
for example `-DEFAULT` and `-demo`). Their retention is "Never expire". List
them read-only, then set a retention period on each:

```bash
aws logs describe-log-groups \
  --log-group-name-prefix /aws/bedrock-agentcore/runtimes/ \
  --query 'logGroups[].[logGroupName,retentionInDays]' --output table
aws logs put-retention-policy --log-group-name <log-group-name> \
  --retention-in-days 7
```

See [AgentCore generated runtime observability data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)
for the log group format.

## Known deprecation warnings

`pulumi up` and `pulumi preview` print deprecation warnings from pulumi-aws 7
for `BucketV2`, `BucketVersioningV2`,
`BucketServerSideEncryptionConfigurationV2` and
`BucketLifecycleConfigurationV2`. They are harmless; the resources work as
deployed. Moving an existing stack to the non-V2 resources needs resource
aliases so Pulumi does not replace the buckets, so that migration is deferred.
New stacks and new code should use the non-V2 resources (`aws.s3.Bucket`,
`aws.s3.BucketVersioning` and so on).

## Dependency advisories

The upstream Portkey gateway at the revision pinned in
`module10/gateway/setup.py` has known npm advisories in its own lockfile, and no
fixed upstream revision exists yet. The source stays pinned and unmodified;
bump `REVISION` once upstream publishes a fix. The gateway's own `@aws-sdk`
pins in `module10/gateway/package.json` were refreshed in the Module 11
maintenance release to clear a critical `fast-xml-parser` advisory. A running
Portkey task picks this up only after its image is rebuilt.

## Updating an existing deployment after this release

The Module 11 maintenance release changes these deployed inputs:
`module10/server.py`, `module10/requirements.txt`, the
`module10/gateway/package.json` pins and lockfile, and one lifecycle rule in
`module10/deployment/pulumi/__main__.py`.

- Without an image rebuild, `pulumi preview` on the application stack shows one
  in-place update: `artifacts-retention` gains the `demo-manifests` rule
  (`manifests/` objects expire after 30 days). The gateway stack shows no changes.
- Rebuilding the images, as in [Updates](#updates), picks up the other changes
  and produces new digests. The application stack then shows an AgentCore
  runtime version update, and the gateway stack shows a new ECS task definition
  revision with an ECS service update.

The current images keep working without a rebuild.

## Permanent cleanup

Stop Portkey and restore the deployment environment above. Save any evidence you
want to keep before deleting data.

1. Read `artifactBucket` and `repositoryName` from each stack's outputs file.
2. In S3, **Empty** both artifact buckets, including all versions and delete
   markers in the versioned agent bucket.
3. In ECR, delete all images in both repositories. Leave the empty buckets and
   repositories for Pulumi to delete.
4. Destroy the application stack, then the gateway stack:

```bash
pulumi -C module10/deployment/pulumi preview --destroy --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/deployment/pulumi destroy --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/gateway/pulumi preview --destroy --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
pulumi -C module10/gateway/pulumi destroy --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
```

Remove any manually uploaded UI-bucket objects if deletion reports it nonempty.
Check separately for Runtime-created CloudWatch log groups. Secrets Manager
deletion has a seven-day recovery window. If the stack created the regional API
Gateway logging role, check whether other APIs need it before deleting the role.
Keep Pulumi state until both destroys succeed, then remove local state and
credentials when no longer needed.

## Local checks

After [local setup](../README.md#local), including the one-time
`.venv/bin/python module10/gateway/setup.py`. The Python tests need its pinned
Portkey checkout in `module10/.cache/`; without it, 28 tests error with
"Install the pinned gateway first".

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests/test_module10_*.py tests/test_portkey_lifecycle.py -q
node --test tests/test_module10_*.mjs
node --import ./module10/.cache/portkey/node_modules/tsx/dist/loader.mjs \
  --test tests/test_portkey_hosted.mjs
```
