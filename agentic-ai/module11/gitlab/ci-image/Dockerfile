# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# m11-ci: the prebuilt image for GitLab MR jobs (validate, preview:*, cleanup:previews), so no
# job runs pip at job time (design 4.5.7, M3). Built by the `ci-image` job from the project root
# and pushed as $CI_REGISTRY_IMAGE/m11-ci:$M11_CI_IMAGE. Tag jobs (promote:*) never use it.
FROM public.ecr.aws/docker/library/python:3.13.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY module11/requirements-ci.txt /tmp/requirements-ci.txt
RUN pip install -r /tmp/requirements-ci.txt && rm /tmp/requirements-ci.txt
COPY module11/gitlab/ci-image/start_preview.py /opt/m11/start_preview.py

RUN useradd --create-home --uid 10001 ci
USER ci
WORKDIR /builds
