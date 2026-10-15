# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The stage's edge: REST API, Module 10's TOKEN authorizer, the m11 adapter and
router, Module 10's control Lambda, usage plans, keys, throttling and (prod, mr-*) a browser client in
Module 10's user pool (declared here, taught in Module 8/10).

Every Lambda runs the sealed edge zip `releaseInputs.edge` (the files in closures/edge.txt, Module
10's .mjs packaged unchanged), so nothing under module11/edge or module10/deployment/lambda is read
from the checkout at `pulumi up` time. POST /planning and /resilience are not deployed:
flags are code. API Gateway access logs are enabled only when the account's API Gateway CloudWatch
role exists, because another project owns that account-level setting (R9).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_aws as aws

from module11.release import naming

from . import config_model as CM
from .base import XRAY, Stage, lambda_logs_statement

TEAMS = ("platform", "payments")
STAGE_NAME = "api"
MAX_INTEGRATION_MS = 900_000


@dataclass
class Edge:
    api: aws.apigateway.RestApi
    stage: aws.apigateway.Stage
    adapter: aws.lambda_.Function
    adapter_role: aws.iam.Role
    control: aws.lambda_.Function
    authorizer: aws.lambda_.Function
    browser_client: aws.cognito.UserPoolClient | None


def _function(stage: Stage, short: str, handler: str, *, edge_key: str, env: pulumi.Input[dict],
              statements: pulumi.Input[list], timeout: int = 10, memory: int = 256):
    name = naming.function(stage.name, short)
    role = stage.role(f"{short}-role", naming.role(stage.name, short), "lambda.amazonaws.com",
                      extra_tags={"M11Role": "edge"})
    logs = stage.log_group(f"{short}-logs", f"/aws/lambda/{name}")
    policy = stage.role_policy(short, naming.role(stage.name, short), pulumi.Output.from_input(statements).apply(
        lambda s: [lambda_logs_statement(stage, name), {**XRAY, "Sid": "Traces"}, *s]),
        opts=pulumi.ResourceOptions(depends_on=[role]))
    fn = aws.lambda_.Function(short, name=name, role=role.arn, runtime="nodejs24.x", architectures=["arm64"],
                              handler=handler, s3_bucket=stage.release_bucket, s3_key=edge_key, timeout=timeout,
                              memory_size=memory, environment={"variables": env}, publish=True,
                              tracing_config={"mode": "Active"}, tags=stage.tags,
                              opts=pulumi.ResourceOptions(depends_on=[policy, logs]))
    return fn, role


def browser_client(stage: Stage, pool_id: pulumi.Input[str]) -> aws.cognito.UserPoolClient:
    """A browser client in Module 10's pool with its own managed-login style (M14; v2 domain)."""
    origin = stage.settings.uiOrigin
    client = aws.cognito.UserPoolClient(
        "browser-client", name=f"m11-{stage.name}-browser", user_pool_id=pool_id, generate_secret=False,
        allowed_oauth_flows_user_pool_client=True, allowed_oauth_flows=["code"],
        allowed_oauth_scopes=["openid", "module10/invoke"], supported_identity_providers=["COGNITO"],
        callback_urls=[f"{origin}/"], logout_urls=[f"{origin}/"], explicit_auth_flows=["ALLOW_REFRESH_TOKEN_AUTH"],
        prevent_user_existence_errors="ENABLED", enable_token_revocation=True, access_token_validity=30,
        id_token_validity=30, token_validity_units={"access_token": "minutes", "id_token": "minutes"})
    aws.cognito.ManagedLoginBranding("browser-login-style", user_pool_id=pool_id, client_id=client.id,
                                     use_cognito_provided_values=True)
    return client


def deploy(stage: Stage, *, inputs: CM.ReleaseInputs, runtime_arn: pulumi.Output, tables: dict,
           module10: dict, account_logging: bool, shadow_queue: aws.sqs.Queue | None = None) -> Edge:
    s = stage.settings
    edge_key = inputs.edge["s3Key"]
    origin = s.uiOrigin
    pool_id = module10["userPoolId"]
    client = browser_client(stage, pool_id) if (s.stage == "prod" or s.is_mr) and origin else None
    api = aws.apigateway.RestApi("api", name=f"m11-{s.stage}-api", api_key_source="AUTHORIZER",
                                 description=f"DevOps Companion edge ({s.stage})",
                                 endpoint_configuration={"types": "REGIONAL"}, tags=stage.tags)
    keys = {t: aws.apigateway.ApiKey(f"key-{t}", name=f"m11-{s.stage}-{t}", enabled=True, tags=stage.tags) for t in TEAMS}
    ab = stage.artifacts_bucket
    ui = {"UI_ORIGIN": origin} if origin else {}
    tables_arn = {k: t.arn for k, t in tables.items()}
    adapter, adapter_role = _function(
        stage, "adapter", "adapter.handler", edge_key=edge_key, timeout=CM.adapter_timeout(inputs), memory=512,
        env={"TABLE_NAME": tables["requests"].name, "OUTCOMES_TABLE": tables["outcomes"].name,
             "CONTROL_TABLE": tables["control"].name, "ARTIFACT_BUCKET": ab, "RUNTIME_ARN": runtime_arn,
             "SESSION_STAGE": stage.key, "TEAM_CONCURRENCY": str(s.teamConcurrency), **ui,
             **({"SHADOW_QUEUE_URL": shadow_queue.url} if shadow_queue is not None else {})},
        statements=pulumi.Output.all(tables_arn["requests"], tables_arn["outcomes"], tables_arn["control"],
                                     runtime_arn, shadow_queue.arn if shadow_queue is not None else None).apply(lambda v: [
            *([{"Sid": "ShadowQueue", "Effect": "Allow", "Action": "sqs:SendMessage", "Resource": v[4]}] if v[4] else []),
            {"Sid": "Requests", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem",
                                                              "dynamodb:UpdateItem", "dynamodb:DeleteItem"], "Resource": v[0]},
            {"Sid": "Outcomes", "Effect": "Allow", "Action": "dynamodb:PutItem", "Resource": v[1]},
            {"Sid": "Control", "Effect": "Allow", "Action": "dynamodb:GetItem", "Resource": v[2]},
            {"Sid": "Results", "Effect": "Allow", "Action": "s3:GetObject", "Resource": stage.s3(ab, "results/*")},
            {"Sid": "Runtime", "Effect": "Allow",
             "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
             "Resource": [v[3]] + [f"{v[3]}/runtime-endpoint/{e}" for e in s.endpoints]}]))
    control, _ = _function(
        stage, "control", "control.handler", edge_key=edge_key,
        env={"TABLE_NAME": tables["requests"].name, "ARTIFACT_BUCKET": ab, **ui},
        statements=tables_arn["requests"].apply(lambda arn: [
            {"Sid": "Requests", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem",
                                                              "dynamodb:UpdateItem", "dynamodb:DeleteItem"], "Resource": arn},
            {"Sid": "Objects", "Effect": "Allow", "Action": "s3:GetObject",
             "Resource": [stage.s3(ab, "results/*"), stage.s3(ab, "config/flags.json")]}]))
    client_ids = [module10["userPoolClientId"]] + ([client.id] if client else [])
    authorizer, _ = _function(
        stage, "authorizer", "authorizer.handler", edge_key=edge_key,
        env={"TABLE_NAME": module10["stateTable"],
             "ISSUER": pulumi.Output.from_input(pool_id).apply(lambda p: f"https://cognito-idp.{stage.region}.amazonaws.com/{p}"),
             "CLIENT_ID": module10["userPoolClientId"], "CLIENT_IDS": pulumi.Output.json_dumps(client_ids),
             "USAGE_KEYS": pulumi.Output.json_dumps({t: k.value for t, k in keys.items()})},
        statements=pulumi.Output.from_input(module10["stateTable"]).apply(lambda t: [
            {"Sid": "Module10Profiles", "Effect": "Allow", "Action": "dynamodb:GetItem",
             "Resource": stage.arn("dynamodb", f"table/{t}")}]))
    token = aws.apigateway.Authorizer("caller-identity", name=f"m11-{s.stage}-caller", rest_api=api.id, type="TOKEN",
                                      authorizer_uri=authorizer.invoke_arn, authorizer_result_ttl_in_seconds=0,
                                      identity_source="method.request.header.Authorization")
    aws.lambda_.Permission("authorizer-invoke", action="lambda:InvokeFunction", function=authorizer.name,
                           principal="apigateway.amazonaws.com", source_account=stage.account,
                           source_arn=api.execution_arn.apply(lambda a: f"{a}/authorizers/*"))
    for logical, fn in (("adapter-invoke", adapter), ("control-invoke", control)):
        aws.lambda_.Permission(logical, action="lambda:InvokeFunction", function=fn.name,
                               principal="apigateway.amazonaws.com", source_account=stage.account,
                               source_arn=api.execution_arn.apply(lambda a: f"{a}/*"))
    validator = aws.apigateway.RequestValidator("json-body", rest_api=api.id, name="json-body", validate_request_body=True)
    model = aws.apigateway.Model("invocation-schema", rest_api=api.id, name="InvocationRequest",
                                 content_type="application/json", schema=json.dumps({
                                     "$schema": "http://json-schema.org/draft-04/schema#", "type": "object",
                                     "additionalProperties": False, "required": ["change_summary"],
                                     "properties": {
                                         "change_summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                                         "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"},
                                         "conversation_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,64}$"}}}))
    cors = ({"Access-Control-Allow-Origin": f"'{origin}'", "Access-Control-Allow-Headers": "'Authorization,Content-Type'",
             "Access-Control-Allow-Methods": "'GET,POST,OPTIONS'"} if origin else {})
    parts: list[pulumi.Resource] = []

    def preflight(name: str, resource: aws.apigateway.Resource) -> None:
        method = aws.apigateway.Method(f"{name}-options", rest_api=api.id, resource_id=resource.id, http_method="OPTIONS",
                                       authorization="NONE")
        integration = aws.apigateway.Integration(f"{name}-options", rest_api=api.id, resource_id=resource.id,
                                                 http_method=method.http_method, type="MOCK",
                                                 request_templates={"application/json": '{"statusCode":200}'})
        response = aws.apigateway.MethodResponse(f"{name}-options", rest_api=api.id, resource_id=resource.id,
                                                 http_method=method.http_method, status_code="200",
                                                 response_parameters={f"method.response.header.{k}": True for k in cors})
        reply = aws.apigateway.IntegrationResponse(f"{name}-options", rest_api=api.id, resource_id=resource.id,
                                                   http_method=method.http_method, status_code=response.status_code,
                                                   response_parameters={f"method.response.header.{k}": v for k, v in cors.items()},
                                                   opts=pulumi.ResourceOptions(depends_on=[integration]))
        parts.extend([method, integration, response, reply])

    def route(name: str, resource: aws.apigateway.Resource, method_name: str, fn: aws.lambda_.Function, *,
              streaming: bool = False, metered: bool = True, validate: bool = False) -> None:
        method = aws.apigateway.Method(name, rest_api=api.id, resource_id=resource.id, http_method=method_name,
                                       authorization="CUSTOM", authorizer_id=token.id, api_key_required=metered,
                                       request_validator_id=validator.id if validate else None,
                                       request_models={"application/json": model.name} if validate else None)
        if streaming:
            uri = fn.arn.apply(lambda a: f"arn:{stage.partition}:apigateway:{stage.region}:lambda:path/2021-11-15/"
                                         f"functions/{a}/response-streaming-invocations")
        else:
            uri = fn.invoke_arn
        timeout_ms = min(MAX_INTEGRATION_MS, (CM.adapter_timeout(inputs) + 30) * 1000) if streaming else 29000
        integration = aws.apigateway.Integration(name, rest_api=api.id, resource_id=resource.id,
                                                 http_method=method.http_method, integration_http_method="POST",
                                                 type="AWS_PROXY", uri=uri,
                                                 response_transfer_mode="STREAM" if streaming else "BUFFERED",
                                                 timeout_milliseconds=timeout_ms)
        parts.extend([method, integration])

    def resource(name: str, path: str, parent=None) -> aws.apigateway.Resource:
        res = aws.apigateway.Resource(name, rest_api=api.id, parent_id=parent or api.root_resource_id, path_part=path)
        preflight(name, res)
        parts.append(res)
        return res

    invoke = resource("invoke", "invoke")
    route("invoke", invoke, "POST", adapter, streaming=True, validate=True)
    route("me", resource("me", "me"), "GET", control)
    route("planning", resource("planning", "planning"), "GET", control, metered=False)
    route("resilience", resource("resilience", "resilience"), "GET", control, metered=False)
    requests = aws.apigateway.Resource("requests", rest_api=api.id, parent_id=api.root_resource_id, path_part="requests")
    parts.append(requests)
    route("result", resource("result", "{id}", requests.id), "GET", control)
    if cors:
        for kind in ("DEFAULT_4XX", "DEFAULT_5XX"):
            parts.append(aws.apigateway.Response(f"cors-{kind.lower()}", rest_api_id=api.id, response_type=kind,
                                                 response_templates={"application/json": '{"message":$context.error.messageString,"type":"$context.error.responseType"}'},
                                                 response_parameters={f"gatewayresponse.header.{k}": v for k, v in cors.items()}))
    definition = hashlib.sha256(Path(__file__).read_bytes() + edge_key.encode() + str(origin).encode()).hexdigest()
    deployment = aws.apigateway.Deployment("api-deployment", rest_api=api.id,
                                           triggers={"definition": definition},
                                           opts=pulumi.ResourceOptions(depends_on=parts))
    access = {}
    if account_logging:
        group = stage.log_group("api-access-logs", f"/aws/apigateway/m11-{s.stage}-access")
        access = {"access_log_settings": {"destination_arn": group.arn, "format": json.dumps({
            "request_id": "$context.requestId", "status": "$context.status", "team": "$context.authorizer.team",
            "method": "$context.httpMethod", "path": "$context.resourcePath",
            "latency_ms": "$context.responseLatency"})}}
    api_stage = aws.apigateway.Stage("api-stage", rest_api=api.id, deployment=deployment.id, stage_name=STAGE_NAME,
                                     xray_tracing_enabled=True, tags=stage.tags, **access)
    aws.apigateway.MethodSettings("api-throttle", rest_api=api.id, stage_name=api_stage.stage_name, method_path="*/*",
                                  settings={"metrics_enabled": True,
                                            "throttling_rate_limit": CM.STAGE_THROTTLE["rateLimit"],
                                            "throttling_burst_limit": CM.STAGE_THROTTLE["burstLimit"]})
    plan = s.usagePlan
    for team, key in keys.items():
        usage = aws.apigateway.UsagePlan(f"usage-{team}", name=f"m11-{s.stage}-{team}",
                                         api_stages=[{"api_id": api.id, "stage": api_stage.stage_name}],
                                         throttle_settings={"rate_limit": plan["rateLimit"], "burst_limit": plan["burstLimit"]},
                                         quota_settings={"limit": plan["limit"], "period": plan["period"]}, tags=stage.tags)
        aws.apigateway.UsagePlanKey(f"usage-{team}-key", key_id=key.id, key_type="API_KEY", usage_plan_id=usage.id)
    return Edge(api, api_stage, adapter, adapter_role, control, authorizer, client)
