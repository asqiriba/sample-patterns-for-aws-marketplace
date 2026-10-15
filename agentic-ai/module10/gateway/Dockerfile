FROM public.ecr.aws/docker/library/node:24.14.0-bookworm-slim AS build
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY module10/gateway /app/module10/gateway
COPY module10/urls.py /app/module10/urls.py
RUN python3 module10/gateway/setup.py
RUN cd module10/gateway && npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM public.ecr.aws/docker/library/node:24.14.0-bookworm-slim
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends python3 python3-venv openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir boto3==1.43.6
COPY --from=build --chown=node:node /app/module10 /app/module10
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER node
EXPOSE 8443
CMD ["python", "-m", "module10.gateway.bootstrap"]
