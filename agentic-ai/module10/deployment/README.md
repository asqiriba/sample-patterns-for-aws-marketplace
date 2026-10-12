# Deploy Module 10 to AWS

Complete [Setup](../README.md#setup) first, including the Logz.io variables.
Run from `agentic-ai/` with an AWS identity allowed to provision the stacks.
Deployment files stay in the ignored `module10/.local/` directory.

## 1. Prepare Pulumi

```bash
umask 077
export MODULE10_DEPLOYMENT_DIR="$PWD/module10/.local/workshop"
mkdir -p "$MODULE10_DEPLOYMENT_DIR/state"
python3.13 -m venv "$MODULE10_DEPLOYMENT_DIR/pulumi-venv"
"$MODULE10_DEPLOYMENT_DIR/pulumi-venv/bin/python" -m pip install \
  -r module10/deployment/pulumi/requirements.lock \
  -r module10/gateway/pulumi/requirements.txt
export PULUMI_PYTHON_CMD="$MODULE10_DEPLOYMENT_DIR/pulumi-venv/bin/python"
export PULUMI_BACKEND_URL="file://$MODULE10_DEPLOYMENT_DIR/state"
pulumi login "$PULUMI_BACKEND_URL"
```

Choose a passphrase when prompted. Keep it and the state directory for future updates.
Initialize each stack once:

```bash
pulumi -C module10/gateway/pulumi stack init workshop --secrets-provider passphrase
mv module10/gateway/pulumi/Pulumi.workshop.yaml "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
pulumi -C module10/deployment/pulumi stack init workshop --secrets-provider passphrase
mv module10/deployment/pulumi/Pulumi.workshop.yaml "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
```

## 2. Deploy Portkey

```bash
pulumi -C module10/gateway/pulumi config set aws:region "$AWS_REGION" \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
pulumi -C module10/gateway/pulumi up --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
pulumi -C module10/gateway/pulumi stack output --stack workshop --json \
  > "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
pulumi -C module10/gateway/pulumi stack --stack workshop --show-name \
  --fully-qualify-stack-names > "$MODULE10_DEPLOYMENT_DIR/portkey-stack-reference.txt"

.venv/bin/python -m module10.deployment.build_image \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json" --wait \
  --report "$MODULE10_DEPLOYMENT_DIR/portkey-build.json"
export MODULE10_PORTKEY_IMAGE_DIGEST="$(.venv/bin/python -c \
  'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["MODULE10_DEPLOYMENT_DIR"])/"portkey-build.json").read_text())["image_digest"])')"
pulumi -C module10/gateway/pulumi config set imageDigest "$MODULE10_PORTKEY_IMAGE_DIGEST" \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
pulumi -C module10/gateway/pulumi up --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/portkey-stack.yaml"
pulumi -C module10/gateway/pulumi stack output --stack workshop --json \
  > "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
```

## 3. Deploy the application

```bash
pulumi -C module10/deployment/pulumi config set aws:region "$AWS_REGION" \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/deployment/pulumi config set portkeyStack \
  "$(cat "$MODULE10_DEPLOYMENT_DIR/portkey-stack-reference.txt")" \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
for setting in enableEdge enableCloudIntegrations enableUI; do
  pulumi -C module10/deployment/pulumi config set "$setting" true \
    --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
done
aws apigateway get-account --query cloudwatchRoleArn --output text
```

If the last command returns `None` or an empty value, run:

```bash
pulumi -C module10/deployment/pulumi config set manageApiGatewayLogging true \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
```

Stage access logs need the API Gateway CloudWatch role, which is a single
setting per account and Region. With `manageApiGatewayLogging=true`, this stack
creates a role and sets it through `aws.apigateway.Account`, which affects every
API in the account and Region and is kept (`retain_on_delete`) when the stack is
destroyed. Leave the setting `false` if the command above already returns a role
ARN, for example one owned by another project. The stage's access logs then
depend on that role, so they stop if its owner deletes it.

Build and deploy:

```bash
pulumi -C module10/deployment/pulumi up --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/deployment/pulumi stack output --stack workshop --json \
  > "$MODULE10_DEPLOYMENT_DIR/outputs.json"
.venv/bin/python -m module10.deployment.build_image \
  --outputs "$MODULE10_DEPLOYMENT_DIR/outputs.json" --wait \
  --report "$MODULE10_DEPLOYMENT_DIR/image.json"
export MODULE10_IMAGE_DIGEST="$(.venv/bin/python -c \
  'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["MODULE10_DEPLOYMENT_DIR"])/"image.json").read_text())["image_digest"])')"
pulumi -C module10/deployment/pulumi config set imageDigest "$MODULE10_IMAGE_DIGEST" \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/deployment/pulumi up --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/deployment/pulumi stack output --stack workshop --json \
  > "$MODULE10_DEPLOYMENT_DIR/outputs.json"
```

## 4. Create logins and configure telemetry

```bash
.venv/bin/python -m module10.deployment.setup_users \
  --outputs "$MODULE10_DEPLOYMENT_DIR/outputs.json" \
  --credentials-file "$MODULE10_DEPLOYMENT_DIR/credentials.json"
.venv/bin/python -m module10.deployment.configure_telemetry \
  --outputs "$MODULE10_DEPLOYMENT_DIR/outputs.json"
pulumi -C module10/deployment/pulumi config set enableTelemetry true \
  --stack workshop --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/deployment/pulumi up --stack workshop \
  --config-file "$MODULE10_DEPLOYMENT_DIR/stack.yaml"
pulumi -C module10/deployment/pulumi stack output --stack workshop --json \
  > "$MODULE10_DEPLOYMENT_DIR/outputs.json"
```

## 5. Start and verify

```bash
.venv/bin/python -m module10.gateway.lifecycle start \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
.venv/bin/python -m module10.deployment.verify_cloud \
  --outputs "$MODULE10_DEPLOYMENT_DIR/outputs.json" \
  --credentials-file "$MODULE10_DEPLOYMENT_DIR/credentials.json" \
  --report "$MODULE10_DEPLOYMENT_DIR/verification.json"
```

Continue with [Run in the cloud](../README.md#cloud) and [Open the UIs](../README.md#open-the-uis).

For subsequent deployments, follow [Updates](OPERATIONS.md#updates).
