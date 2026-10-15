"""Build infrastructure first, then set imageDigest to deploy the agent.

AWS identity and Region come from the standard provider environment/configuration.
"""
import json
from pathlib import Path

import pulumi
import pulumi_aws as aws

config = pulumi.Config()
stack = pulumi.get_stack()
prefix = f"module10-{stack}"
region = aws.get_region().region
account = aws.get_caller_identity().account_id
partition = aws.get_partition().partition
tags = {"Project": "module10-exposure", "Stack": stack}


def policy(statements):
    return pulumi.Output.json_dumps({"Version": "2012-10-17", "Statement": statements})


def role(name, service, source_conditions=None):
    statement = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
    if source_conditions:
        statement["Condition"] = source_conditions
    return aws.iam.Role(name, assume_role_policy=json.dumps({
        "Version": "2012-10-17", "Statement": [statement],
    }), tags=tags)


bucket = aws.s3.BucketV2("artifacts", tags=tags)
aws.s3.BucketPublicAccessBlock("artifacts-private", bucket=bucket.id,
    block_public_acls=True, block_public_policy=True, ignore_public_acls=True, restrict_public_buckets=True)
aws.s3.BucketServerSideEncryptionConfigurationV2("artifacts-encryption", bucket=bucket.id,
    rules=[{"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}])
aws.s3.BucketVersioningV2("artifacts-versioning", bucket=bucket.id,
    versioning_configuration={"status": "Enabled"})
aws.s3.BucketLifecycleConfigurationV2("artifacts-retention", bucket=bucket.id,
    rules=[{"id": "demo-results", "status": "Enabled", "filter": {"prefix": "results/"},
            "expiration": {"days": 7}, "noncurrent_version_expiration": {"noncurrent_days": 7}},
           {"id": "demo-manifests", "status": "Enabled", "filter": {"prefix": "manifests/"},
            "expiration": {"days": 30}, "noncurrent_version_expiration": {"noncurrent_days": 30}}])
repository = aws.ecr.Repository("agent-image", image_tag_mutability="IMMUTABLE",
    image_scanning_configuration={"scan_on_push": True}, tags=tags)
build_logs = aws.cloudwatch.LogGroup("build-logs", retention_in_days=7, tags=tags)
build_role = role("image-builder", "codebuild.amazonaws.com")
build_permissions = aws.iam.RolePolicy("image-builder-permissions", role=build_role.id, policy=policy([
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": bucket.arn.apply(lambda a: a + "/build/*")},
    {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
     "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage",
     "ecr:GetDownloadUrlForLayer"], "Resource": repository.arn},
    {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
     "Resource": build_logs.arn.apply(lambda a: a + ":*")},
]))
buildspec = """version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password | docker login --username AWS --password-stdin "$ECR_REGISTRY"
  build:
    commands:
      - docker build --platform linux/arm64 -f module10/deployment/Dockerfile -t "$ECR_REPOSITORY:$IMAGE_TAG" .
  post_build:
    commands:
      - docker push "$ECR_REPOSITORY:$IMAGE_TAG"
"""
builder = aws.codebuild.Project("image-build", service_role=build_role.arn, build_timeout=30,
    artifacts={"type": "NO_ARTIFACTS"},
    environment={
        "compute_type": "BUILD_GENERAL1_SMALL", "type": "ARM_CONTAINER",
        "image": "aws/codebuild/amazonlinux-aarch64-standard:3.0", "privileged_mode": True,
        "environment_variables": [
            {"name": "ECR_REPOSITORY", "value": repository.repository_url},
            {"name": "ECR_REGISTRY", "value": repository.repository_url.apply(lambda u: u.split("/")[0])},
        ],
    },
    source={"type": "S3", "location": bucket.bucket.apply(lambda b: b + "/build/source.zip"), "buildspec": buildspec},
    logs_config={"cloudwatch_logs": {"group_name": build_logs.name}},
    tags=tags, opts=pulumi.ResourceOptions(depends_on=[build_permissions]))

pulumi.export("artifactBucket", bucket.bucket)
pulumi.export("repositoryUrl", repository.repository_url)
pulumi.export("repositoryName", repository.name)
pulumi.export("buildProject", builder.name)

cloud_integrations = config.get_bool("enableCloudIntegrations")
telemetry_secret = None
if cloud_integrations:
    aws.s3.BucketObjectv2("shared-flags", bucket=bucket.bucket, key="config/flags.json",
        content=(Path(__file__).resolve().parents[2] / "flags.json").read_text(),
        content_type="application/json", opts=pulumi.ResourceOptions(ignore_changes=["content"]))
    telemetry_secret = aws.secretsmanager.Secret("telemetry-shipping", recovery_window_in_days=7, tags=tags)
    pulumi.export("telemetrySecretArn", telemetry_secret.arn)

image_digest = config.get("imageDigest")
if image_digest:
    if not image_digest.startswith("sha256:") or len(image_digest) != 71:
        raise ValueError("imageDigest must be an immutable sha256 image digest")
    gateway = pulumi.StackReference(config.require("portkeyStack"))
    gateway_discovery = gateway.require_output("discoveryParameter")
    gateway_secret = gateway.require_output("serviceSecretArn")
    gateway_bucket = gateway.require_output("evidenceBucket")
    runtime_role = role("agent-gateway-execution", "bedrock-agentcore.amazonaws.com", {
        "StringEquals": {"aws:SourceAccount": account},
        "ArnLike": {"aws:SourceArn": f"arn:{partition}:bedrock-agentcore:{region}:{account}:*"},
    })
    runtime_permissions = aws.iam.RolePolicy("agent-permissions", role=runtime_role.id, policy=policy([
        {"Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], "Resource": repository.arn},
        {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
         "logs:DescribeLogStreams"], "Resource": f"arn:{partition}:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"},
        {"Effect": "Deny", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["ssm:GetParameter"],
         "Resource": [gateway.require_output("discoveryParameterArn"), gateway.require_output("sessionParameterArn")]},
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": gateway_secret},
        {"Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": gateway_bucket.apply(lambda b: f"arn:{partition}:s3:::{b}/evidence/*")},
        {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": bucket.arn.apply(lambda a: [a + "/results/*", a + "/manifests/*"])},
        {"Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": bucket.arn.apply(lambda a: a + "/config/*")},
        *([{"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": telemetry_secret.arn}]
          if telemetry_secret else []),
    ]))
    agent = aws.bedrock.AgentcoreAgentRuntime("companion",
        agent_runtime_name=prefix.replace("-", "_"),
        agent_runtime_artifact={"container_configuration": {
            "container_uri": repository.repository_url.apply(lambda u: f"{u}@{image_digest}")}},
        role_arn=runtime_role.arn,
        network_configuration={"network_mode": "PUBLIC"},
        protocol_configuration={"server_protocol": "HTTP"},
        lifecycle_configurations=[{"idle_runtime_session_timeout": 300, "max_lifetime": 900}],
        environment_variables={"MODULE10_ARTIFACT_BUCKET": bucket.bucket,
            "MODULE10_PORTKEY_DISCOVERY": gateway_discovery,
            "MODULE10_PORTKEY_SECRET": gateway_secret,
            "MODULE10_PORTKEY_EVIDENCE_BUCKET": gateway_bucket,
            "MODULE10_ARTIFACT_DIR": "/tmp/module10-results", "MODULE10_BUILD_ID": image_digest,
            "MODULE10_DEPLOYMENT_ENVIRONMENT": "module10-cloud",
            **({"MODULE10_FLAGS_KEY": "config/flags.json"} if cloud_integrations else {}),
            **({"MODULE10_TELEMETRY_SECRET": telemetry_secret.arn}
               if telemetry_secret and config.get_bool("enableTelemetry") else {})},
        tags=tags, opts=pulumi.ResourceOptions(depends_on=[runtime_permissions]))
    endpoint = aws.bedrock.AgentcoreAgentRuntimeEndpoint("demo-endpoint",
        agent_runtime_id=agent.agent_runtime_id, name="demo",
        agent_runtime_version=config.get("runtimeVersion") or agent.agent_runtime_version)
    pulumi.export("runtimeArn", agent.agent_runtime_arn)
    pulumi.export("runtimeId", agent.agent_runtime_id)
    pulumi.export("runtimeVersion", agent.agent_runtime_version)
    pulumi.export("endpointName", endpoint.name)
    pulumi.export("endpointRuntimeVersion", endpoint.agent_runtime_version)
    pulumi.export("imageDigest", image_digest)
    pulumi.export("portkeyDiscoveryParameter", gateway_discovery)
    if config.get_bool("enableEdge"):
        from edge import deploy_edge
        deploy_edge(agent, endpoint, bucket, region=region, account=account,
            partition=partition, tags=tags, role=role, policy=policy, config=config)
