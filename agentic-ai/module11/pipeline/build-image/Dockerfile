# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# m11/build-image: the prebuilt CodeBuild image (lever 4). It bakes in
# Python 3.13.12, the Pulumi CLI 3.267.0, the provider plugins pinned in requirements-pulumi.lock
# and a venv with requirements.txt + requirements-pulumi.lock, so no timed build runs an install
# step. Built by the m11-image project (`python -m module11 pipeline build-image`) and pinned by
# digest in m11-platform config `buildImage`. The base is the runtime image's base, so the Python
# patch version is exact. ARM64 (CodeBuild ARM_CONTAINER).
FROM public.ecr.aws/docker/library/python:3.13.12-slim-bookworm

ARG PULUMI_VERSION=3.267.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PULUMI_SKIP_UPDATE_CHECK=1 \
    PULUMI_HOME=/opt/m11/pulumi-home \
    PATH=/opt/m11/venv/bin:/opt/m11/pulumi:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl unzip zip \
    && rm -rf /var/lib/apt/lists/*

# Pulumi CLI (pinned release tarball for linux-arm64).
RUN mkdir -p /opt/m11 \
    && curl -fsSL "https://get.pulumi.com/releases/sdk/pulumi-v${PULUMI_VERSION}-linux-arm64.tar.gz" \
       | tar -xz -C /opt/m11 \
    && /opt/m11/pulumi/pulumi version

# The tooling venv: the same pins as the operator's .venvs/module11.
COPY module11/requirements.txt module11/requirements-pulumi.lock /tmp/req/
RUN python -m venv /opt/m11/venv \
    && /opt/m11/venv/bin/pip install pip==25.2 \
    && /opt/m11/venv/bin/pip install -r /tmp/req/requirements.txt -r /tmp/req/requirements-pulumi.lock \
    && rm -rf /tmp/req

# Provider plugins, matching the SDK pins in requirements-pulumi.lock.
RUN pulumi plugin install resource aws v7.46.0 \
    && pulumi plugin install resource aws-native v1.81.0 \
    && pulumi plugin install resource gitlab v10.3.1 \
    && pulumi plugin install resource vercel v5.4.1 --server github://api.github.com/pulumiverse \
    && chmod -R a+rX /opt/m11

WORKDIR /codebuild
