#!/usr/bin/env python3
"""
scripts/invoke_agentcore.py
============================
Invoke the Infrastructure Agent deployed on AgentCore Runtime via boto3.

Use this after `agentcore deploy` (formerly `agentcore launch`) has deployed the agent.
The agent ARN is printed during deployment and stored in .bedrock_agentcore.yaml.

USAGE
-----
  # Set your agent ARN
  export AGENTCORE_ARN="arn:aws:bedrock-agentcore:<region>:123456789012:runtime/..."

  # Single prompt
  python scripts/invoke_agentcore.py --prompt "Health check us-east-1"

  # Interactive REPL
  python scripts/invoke_agentcore.py --repl

REGION
------
The client Region is the first of: --region, AWS_REGION, AWS_DEFAULT_REGION,
the Region inside the runtime ARN, then us-east-1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import boto3

DEFAULT_REGION = "us-east-1"


def resolve_region(agent_arn: str | None, region: str | None = None) -> str:
    """Resolve the client Region: explicit value, env vars, the ARN, then us-east-1."""
    if region:
        return region
    env_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if env_region:
        return env_region
    # arn:<partition>:bedrock-agentcore:<region>:<account>:runtime/<id>
    parts = (agent_arn or "").split(":")
    if len(parts) > 3 and parts[0] == "arn" and parts[2] == "bedrock-agentcore" and parts[3]:
        return parts[3]
    return DEFAULT_REGION


def invoke(agent_arn: str, prompt: str, region: str | None = None) -> str:
    """Send a prompt to the deployed AgentCore agent and return the response."""
    client = boto3.client("bedrock-agentcore", region_name=resolve_region(agent_arn, region))

    payload = json.dumps({"prompt": prompt}).encode()
    session_id = str(uuid.uuid4())

    response = client.invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        payload=payload,
        qualifier="DEFAULT",
    )

    # Response is a streaming iterator of chunks
    chunks: list[str] = []
    for chunk in response.get("response", []):
        if isinstance(chunk, (bytes, bytearray)):
            chunks.append(chunk.decode("utf-8", errors="replace"))
        elif isinstance(chunk, dict):
            # Some response formats wrap content in a dict
            chunks.append(json.dumps(chunk))
        else:
            chunks.append(str(chunk))

    full_response = "".join(chunks)

    # Try to parse as JSON (structured response) and extract result field
    try:
        parsed = json.loads(full_response)
        return parsed.get("result", full_response)
    except json.JSONDecodeError:
        return full_response


def main() -> None:
    parser = argparse.ArgumentParser(description="Invoke AgentCore-deployed Infrastructure Agent")
    parser.add_argument("--arn", default=os.getenv("AGENTCORE_ARN"), help="AgentCore Runtime ARN")
    parser.add_argument(
        "--region",
        default=None,
        help="AWS Region (default: AWS_REGION, AWS_DEFAULT_REGION, the ARN's Region, then us-east-1)",
    )
    parser.add_argument("--prompt", "-p", help="Single prompt to send")
    parser.add_argument("--repl", action="store_true", help="Interactive mode")
    args = parser.parse_args()

    if not args.arn:
        print("Error: provide --arn or set AGENTCORE_ARN environment variable.")
        print("The ARN is printed during `agentcore deploy` and stored in .bedrock_agentcore.yaml")
        sys.exit(1)

    if args.prompt:
        print(f"\nPrompt: {args.prompt}")
        print(f"\nResponse:\n{invoke(args.arn, args.prompt, args.region)}\n")

    elif args.repl:
        print(f"\nInfrastructure Agent (AgentCore Runtime)")
        print(f"ARN: {args.arn}")
        print(f"Type 'quit' to exit.\n")
        while True:
            try:
                prompt = input("You › ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt.lower() in ("quit", "exit", "q"):
                break
            if not prompt:
                continue
            print(f"\nAgent › {invoke(args.arn, prompt, args.region)}\n")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
