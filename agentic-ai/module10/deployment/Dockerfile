FROM public.ecr.aws/docker/library/python:3.13.12-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY module10/requirements-review.txt module10/requirements-telemetry.txt module10/deployment/requirements-runtime.txt /tmp/dependencies/
RUN python -m venv /app/.venv && /app/.venv/bin/pip install --no-cache-dir -r /tmp/dependencies/requirements-runtime.txt
COPY module10 /app/module10
RUN /app/.venv/bin/python -m module10.collector install

FROM public.ecr.aws/docker/library/python:3.13.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home agent
COPY --from=build --chown=agent:agent /app /app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER agent
EXPOSE 8080
CMD ["python", "-m", "module10.cloud_app"]
