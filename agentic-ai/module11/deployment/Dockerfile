# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# DevOps Companion runtime image for AgentCore Runtime (ARM64, built in CodeBuild).
# The build context is EXACTLY the files in module11/harness/pipeline/closures/runtime.txt plus this
# Dockerfile and module11/requirements-runtime.txt, at their repository paths, so what is hashed
# (`runtime/code`) is what ships. Hence `COPY . /app` and no .dockerignore.
FROM public.ecr.aws/docker/library/python:3.13.12-slim-bookworm

WORKDIR /app
COPY module11/requirements-runtime.txt /tmp/requirements-runtime.txt
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -r /tmp/requirements-runtime.txt \
    && rm /tmp/requirements-runtime.txt
COPY . /app
RUN useradd --uid 10001 --create-home agent

# OTEL_TRACES_SAMPLER=always_on: every session's spans are exported, whatever the caller's
# X-Ray sampling decision. AgentCore injects the other OTEL_* settings
# (ADOT distro and configurator, propagators, exporters) but not the sampler.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OTEL_TRACES_SAMPLER=always_on

USER agent
EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "-m", "module11.runtime.app", "--agentcore"]
