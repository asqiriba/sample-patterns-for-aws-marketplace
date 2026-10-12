# Portkey commands

Run from `agentic-ai/` after [cloud deployment](../deployment/README.md).

```bash
export MODULE10_DEPLOYMENT_DIR="$PWD/module10/.local/workshop"
```

Start and retrieve the console token:

```bash
.venv/bin/python -m module10.gateway.lifecycle start \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
.venv/bin/python -m module10.gateway.lifecycle console-login \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
```

Open the printed HTTPS URL, accept the self-signed certificate warning, and enter
the console token. After a restart, use the newly printed URL.

Check status, extend for two hours, or stop:

```bash
.venv/bin/python -m module10.gateway.lifecycle status \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
.venv/bin/python -m module10.gateway.lifecycle extend \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json" --hours 2
.venv/bin/python -m module10.gateway.lifecycle stop \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json"
```

Verify HTTPS, console authentication, and a real model fallback:

```bash
.venv/bin/python -m module10.gateway.verify \
  --outputs "$MODULE10_DEPLOYMENT_DIR/portkey-outputs.json" \
  --report "$MODULE10_DEPLOYMENT_DIR/portkey-verification.json" --model
```

## Network exposure

The Portkey task's security group allows `0.0.0.0/0` on port 8443. The
AgentCore Runtime runs in `PUBLIC` network mode and reaches Portkey from
AWS-owned addresses that cannot be predicted, so a CIDR allowlist would break
cloud mode. These controls compensate:

- TLS on every connection.
- A bearer service token, checked in `hosted.mjs` before any request reaches a
  model provider. It is a random 48-character value that the gateway stack
  stores in Secrets Manager and injects into the task. The console uses a
  separate token.
- An auto-stop schedule: two hours by default, at most 12 hours with `--hours`.
- A new public task IP on every start.

For production, run the agent in a VPC-mode AgentCore Runtime and reach a
private gateway instead.

[Costs and cleanup](../deployment/OPERATIONS.md) · [Local demo](../README.md#local)
