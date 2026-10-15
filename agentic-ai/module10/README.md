# Module 10

Run commands from `agentic-ai/`.

For the slide-aligned workshop, use **cloud mode**. It starts with API Gateway
agent exposure, then demonstrates LaunchDarkly, Portkey, and Logz.io.

| Section | Cloud demo | Local demo |
|---|---|---|
| 1 | API Gateway, Cognito, Lambda authorization, and request controls | LaunchDarkly capability rollout |
| 2 | LaunchDarkly capability rollout | Portkey model routing |
| 3 | Portkey model routing | Logz.io trace inspection |
| 4 | Logz.io trace inspection | — |

The command defaults to local mode. Specify `--mode cloud` to demonstrate the
AWS exposure boundary.

## Setup

Install Python 3.13, Git, and AWS CLI v2. Local mode also needs Node.js 24;
cloud deployment needs [Pulumi CLI](https://www.pulumi.com/docs/install/).
Use an AWS account with access to the configured Bedrock models in a supported US Region.

For local commands, select your own AWS CLI profile and target Region. Verify
the resolved account before creating resources:

```bash
export AWS_PROFILE=your-aws-cli-profile
export AWS_REGION=your-supported-aws-region
export AWS_DEFAULT_REGION="$AWS_REGION"
aws sts get-caller-identity
```

Do not put a profile, credentials, account ID, or a developer-specific Region in
tracked files. On AWS, omit `AWS_PROFILE` and use the workload role.

Install Python dependencies once:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install \
  -r module10/requirements-review.txt \
  -r module10/requirements-telemetry.txt 'boto3==1.43.6'
```

For the full demo, get **Logs** and **Tracing shipping tokens** from Logz.io →
Settings → Manage Accounts. Set these in each terminal:

```bash
export LOGZIO_REGION=your-logzio-region
export LOGZIO_LOGS_TOKEN=your-logs-shipping-token
export LOGZIO_TRACES_TOKEN=your-tracing-shipping-token
```

## Local

One-time setup:

```bash
.venv/bin/python module10/gateway/setup.py
```

The Module 10 tests need this step too; without it, 28 tests error with
"Install the pinned gateway first". The test commands are in
[Local checks](deployment/OPERATIONS.md#local-checks).

Run:

```bash
.venv/bin/python demos/module10_demo.py --mode local
```

Open the Portkey console link printed by the demo. The link signs you in.
Press Enter to advance. The demo installs the collector on first use and manages
its startup and shutdown automatically. An existing collector is reused and left running.

To run one section, add `--section 1`, `2`, or `3`. Use the same
`--session my-run` across sections. Sections 1 and 2 can run without Logz.io;
unset `MODULE10_OTLP_ENDPOINT` and the Logz.io variables for those standalone runs.

## Cloud

**First time:** follow [Deploy to AWS](deployment/README.md), then return here.

Set the directory used during deployment:

```bash
export MODULE10_DEPLOYMENT_DIR="$PWD/module10/.local/workshop"
```

Stack files, credentials, and reports in this directory are ignored by Git and
excluded from container builds.

Start Portkey (default: two hours):

```bash
.venv/bin/python -m module10.gateway.lifecycle start \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
```

Run the terminal demo:

```bash
.venv/bin/python demos/module10_demo.py --mode cloud \
  --outputs "$MODULE10_DEPLOYMENT_DIR/outputs.json" \
  --credentials-file "$MODULE10_DEPLOYMENT_DIR/credentials.json" \
  --session my-run
```

To present only the API Gateway opening, add `--section 1`. For the later
exercises, use `--section 2`, `3`, or `4` with the same session ID.
Section 1 runs one real investigation plus access, replay, and no-model throttle
checks. The terminal output explains the gateway controls and expected outcomes.
The throttle check is best effort: API Gateway throttling depends on token-bucket
timing, and recorded runs saw anywhere from 0 to 17 of 30 probes throttled. If a
burst shows no throttle, rerun it or point to the `verify_cloud` evidence; see
[Request-limit demonstration](deployment/OPERATIONS.md#request-limit-demonstration).

### Open the UIs

Print the application URL and login in a private terminal:

```bash
.venv/bin/python - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["MODULE10_DEPLOYMENT_DIR"])
print("URL:", json.loads((root / "outputs.json").read_text())["uiUrl"])
user = json.loads((root / "credentials.json").read_text())["users"]["platform"]
print("Username:", user["username"])
print("Password:", user["password"])
PY
```

For Portkey, open the URL printed by `start`, accept the self-signed certificate
warning, and sign in with this separate console token:

```bash
.venv/bin/python -m module10.gateway.lifecycle console-login \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
```

### Stop

Close the browser tabs, then run:

```bash
.venv/bin/python -m module10.gateway.lifecycle stop \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
```

[Extend or inspect a session](gateway/README.md) ·
[Costs and permanent cleanup](deployment/OPERATIONS.md)

## Folders

| Folder | Contents |
|---|---|
| `module10/` | Agent application and local demo support |
| `module10/deployment/` | AWS deployment and verification |
| `module10/gateway/` | Portkey gateway and lifecycle commands |
| `module10/ui/` | Browser client |
| `module10/fixtures/` | Example inputs and evidence |
