# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ECR repositories, the release and pipeline-artifact buckets, and the m11-image build.

Buckets use the non-V2 S3 resources (the BucketV2 family is deprecated), SSE-KMS with the AWS
managed key `aws/s3` (S3 uses it on behalf of any principal in the account), versioning, a
TLS-only policy and `force_destroy=True` so teardown can remove them. Cost notes: storage of a
few hundred MB of zips and images; the image build is an ARM small CodeBuild instance billed per
build minute (about 4 minutes per image).
"""
from __future__ import annotations

import json
from pathlib import Path

import pulumi
import pulumi_aws as aws

from module11.release import naming

IMAGE_PROJECT = "m11-image"
LOG_RETENTION_DAYS = 30
BUILD_IMAGE = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
BUILD_IMAGE_REPOSITORY = "m11/build-image"
BUILDSPECS = Path(__file__).resolve().parents[2] / "pipeline" / "buildspecs"


def buildspec(name: str) -> str:
    """The inline buildspec of a project, from module11/pipeline/buildspecs/<name>.yml (a Platform path)."""
    return (BUILDSPECS / f"{name}.yml").read_text(encoding="utf-8")


# The Image stage's code: build the ARM image from builds/<runtime/code hex>.zip
# and push it as <tag>. Immutable tags make a rebuild of the same context a no-op.
IMAGE_BUILDSPEC = buildspec("image")


def _bucket(name: str, logical: str, tags: dict, *, lifecycle: list | None = None) -> aws.s3.Bucket:
    bucket = aws.s3.Bucket(logical, bucket=name, force_destroy=True, tags=tags)
    aws.s3.BucketPublicAccessBlock(f"{logical}-private", bucket=bucket.id, block_public_acls=True,
                                   block_public_policy=True, ignore_public_acls=True, restrict_public_buckets=True)
    aws.s3.BucketServerSideEncryptionConfiguration(f"{logical}-encryption", bucket=bucket.id, rules=[{
        "apply_server_side_encryption_by_default": {"sse_algorithm": "aws:kms"}, "bucket_key_enabled": True}])
    aws.s3.BucketVersioning(f"{logical}-versioning", bucket=bucket.id,
                            versioning_configuration={"status": "Enabled"})
    aws.s3.BucketPolicy(f"{logical}-tls", bucket=bucket.id, policy=bucket.arn.apply(lambda arn: json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Sid": "TlsOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, f"{arn}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]})))
    if lifecycle:
        aws.s3.BucketLifecycleConfiguration(f"{logical}-lifecycle", bucket=bucket.id, rules=lifecycle)
    return bucket


def repositories(tags: dict) -> tuple[aws.ecr.Repository, aws.ecr.Repository]:
    image = aws.ecr.Repository("image-repository", name=naming.IMAGE_REPOSITORY, image_tag_mutability="IMMUTABLE",
                               image_scanning_configuration={"scan_on_push": True}, force_delete=True, tags=tags)
    preview = aws.ecr.Repository("preview-image-repository", name=naming.PREVIEW_IMAGE_REPOSITORY,
                                 image_tag_mutability="IMMUTABLE", image_scanning_configuration={"scan_on_push": True},
                                 force_delete=True, tags=tags)
    aws.ecr.LifecyclePolicy("preview-image-expiry", repository=preview.name, policy=json.dumps({"rules": [{
        "rulePriority": 1, "description": "MR images expire after 7 days",
        "selection": {"tagStatus": "any", "countType": "sinceImagePushed", "countUnit": "days", "countNumber": 7},
        "action": {"type": "expire"}}]}))
    return image, preview


def buckets(account: str, region: str, tags: dict) -> tuple[aws.s3.Bucket, aws.s3.Bucket]:
    release = _bucket(naming.RELEASE_BUCKET.format(account=account, region=region), "release-bucket", tags)
    pipeline = _bucket(naming.PIPELINE_BUCKET.format(account=account, region=region), "pipeline-artifacts", tags,
                       lifecycle=[{"id": "artifacts", "status": "Enabled", "filter": {"prefix": "artifacts/"},
                                   "expiration": {"days": 30}, "noncurrent_version_expiration": {"noncurrent_days": 7}}])
    return release, pipeline


def build_image_repository(tags: dict) -> aws.ecr.Repository:
    """m11/build-image: the prebuilt CodeBuild image (lever 4), pinned by digest in `buildImage`."""
    return aws.ecr.Repository("build-image-repository", name=BUILD_IMAGE_REPOSITORY, image_tag_mutability="IMMUTABLE",
                              image_scanning_configuration={"scan_on_push": True}, force_delete=True, tags=tags)


def image_build(*, role_arn: pulumi.Input[str], repository: aws.ecr.Repository, release_bucket: aws.s3.Bucket,
                log_group: aws.cloudwatch.LogGroup, tags: dict, depends_on: list, pipeline: bool = False,
                cache_location: pulumi.Input[str] | None = None) -> aws.codebuild.Project:
    """m11-image: privileged ARM build that turns a sealed context zip into an image. With the pipeline its
    source type is CODEPIPELINE (an action project); operator paths start it with a NO_SOURCE override
    and the same buildspec."""
    source_type = "CODEPIPELINE" if pipeline else "NO_SOURCE"
    return aws.codebuild.Project("image-build", name=IMAGE_PROJECT, service_role=role_arn, build_timeout=30,
        artifacts={"type": source_type if pipeline else "NO_ARTIFACTS"},
        environment={"compute_type": "BUILD_GENERAL1_SMALL", "type": "ARM_CONTAINER", "image": BUILD_IMAGE,
                     "privileged_mode": True, "environment_variables": [
                         {"name": "RELEASE_BUCKET", "value": release_bucket.bucket},
                         {"name": "REPOSITORY_URI", "value": repository.repository_url},
                         {"name": "REPOSITORY_NAME", "value": repository.name}]},
        source={"type": source_type, "buildspec": IMAGE_BUILDSPEC},
        cache={"type": "S3", "location": cache_location} if cache_location else None,
        logs_config={"cloudwatch_logs": {"group_name": log_group.name}},
        tags=tags, opts=pulumi.ResourceOptions(depends_on=depends_on))


# ---------------------------------------------------------------- pipeline: the other CodeBuild projects
# name -> (buildspec, timeout minutes, privileged, source type)
PROJECT_SPECS = {
    "m11-analyze": ("analyze", 45, False, "CODEPIPELINE"),
    "m11-seal": ("seal", 45, False, "CODEPIPELINE"),
    "m11-deploy": ("deploy", 60, False, "CODEPIPELINE"),
    "m11-gate": ("gate", 180, False, "CODEPIPELINE"),
    "m11-bake": ("bake", 2160, False, "CODEPIPELINE"),       # the CodeBuild maximum
    "m11-preview": ("preview", 60, False, "NO_SOURCE"),
    "m11-preview-image": ("preview-image", 30, True, "NO_SOURCE"),
    "m11-preview-infra": ("preview-infra", 60, False, "NO_SOURCE"),
    "m11-bot": ("bot", 20, False, "NO_SOURCE"),
    "m11-seal-experiment": ("seal-experiment", 45, False, "NO_SOURCE"),
}
UNTRUSTED = ("m11-preview-image", "m11-preview-infra")


def project(name: str, *, role_arn: pulumi.Input[str], image: pulumi.Input[str], pipeline_bucket: aws.s3.Bucket,
            env: list[dict], log_group: aws.cloudwatch.LogGroup, tags: dict, depends_on: list,
            standard_image: bool = False) -> aws.codebuild.Project:
    spec, timeout, privileged, source_type = PROJECT_SPECS[name]
    cache = {"type": "NO_CACHE"} if name in UNTRUSTED else \
        {"type": "S3", "location": pipeline_bucket.bucket.apply(lambda b, n=name: f"{b}/cache/{n}")}
    environment = {"compute_type": "BUILD_GENERAL1_SMALL", "type": "ARM_CONTAINER",
                   "image": BUILD_IMAGE if standard_image else image, "privileged_mode": privileged,
                   "environment_variables": env}
    if not standard_image:
        environment["image_pull_credentials_type"] = "SERVICE_ROLE"
    return aws.codebuild.Project(name, name=name, service_role=role_arn, build_timeout=timeout,
        artifacts={"type": "CODEPIPELINE" if source_type == "CODEPIPELINE" else "NO_ARTIFACTS"},
        environment=environment, source={"type": source_type, "buildspec": buildspec(spec)}, cache=cache,
        logs_config={"cloudwatch_logs": {"group_name": log_group.name}},
        tags=tags, opts=pulumi.ResourceOptions(depends_on=depends_on))
