# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=skip=SecretsUsedInArgOrEnv ; the Cloudflare Assistant image is a public OCI digest, not a credential
#
# Separate local-only Team controller. It does not copy the hosted app.py/runsc/PG control
# plane; only the narrow LangGraph runtime protocol and Controller-owned Power loop are shared.
FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv
ARG SOURCE_DATE_EPOCH=0

FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS dependencies
ARG SOURCE_DATE_EPOCH=0
ARG COSIGN_VERSION=3.1.2
ARG COSIGN_AMD64_SHA256=f7622ed3cf22e55e1ae6377c080979ff77a22da9981c11df222a2e444991e7cf
ARG COSIGN_ARM64_SHA256=90e7ae0b5dfd60f20816b52c012addf7fc055ebcc7bea4ce81c428ca8518c302
ARG TARGETARCH
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-install-project --no-dev --python 3.14 && \
    rm -rf /root/.cache/uv
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    case "${TARGETARCH}" in \
        amd64) cosign_sha256="${COSIGN_AMD64_SHA256}" ;; \
        arm64) cosign_sha256="${COSIGN_ARM64_SHA256}" ;; \
        *) echo "unsupported Cosign architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -LsSf "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-${TARGETARCH}" \
        -o /tmp/cosign && \
    echo "${cosign_sha256}  /tmp/cosign" | sha256sum -c - && \
    install -m 0755 /tmp/cosign /opt/venv/bin/cosign

FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS runtime
ARG SOURCE_DATE_EPOCH=0
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv

COPY local/__init__.py local/app.py local/audit.py local/authority.py local/errors.py local/healthcheck.py \
    local/labels.py local/lifecycle.py local/token.py local/validation.py ./local/
COPY local/install/__init__.py local/install/automatic.py local/install/developers.py local/install/registry.py \
    local/install/runtime.py local/install/service.py ./local/install/
COPY assistant/__init__.py assistant/genesis.py assistant/manifest.py assistant/spec.py ./assistant/
COPY chat/__init__.py chat/contract.py chat/orchestrator.py chat/progress.py chat/turn.py ./chat/
COPY core/__init__.py core/strict_json.py ./core/
COPY core/container/__init__.py core/container/network.py ./core/container/
COPY core/http/__init__.py core/http/stdlib.py core/http/strict.py ./core/http/
COPY egress/__init__.py egress/policy.py ./egress/
COPY inference/__init__.py inference/client.py inference/config.py inference/model_catalog.json \
    inference/token.py ./inference/
COPY integrations/__init__.py integrations/account_egress.py integrations/broker.py integrations/challenge_store.py \
    integrations/challenges.py integrations/flow.py integrations/http.py \
    integrations/pkce.py integrations/private_state.py integrations/providers.py \
    integrations/service.py integrations/store.py ./integrations/
COPY local/assistant/__init__.py local/assistant/api.py local/assistant/egress.py \
    local/assistant/isolation.py local/assistant/lifecycle.py local/assistant/resources.py \
    local/assistant/rpc.py ./local/assistant/
COPY local/chat/__init__.py local/chat/api.py local/chat/continuation.py \
    local/chat/continuation_store.py local/chat/execution.py local/chat/pause.py \
    local/chat/private.py local/chat/resume.py local/chat/segment.py \
    local/chat/state.py local/chat/types.py ./local/chat/
COPY local/http/__init__.py local/http/dispatch.py local/http/server.py ./local/http/
COPY install/__init__.py install/artifact_trust.py install/bindings.py install/contract.py install/icons.py install/update.py \
    install/registry_auth.py ./install/
COPY protocol/install/upstream.json ./protocol/install/
COPY protocol/install/v1/README.md protocol/install/v1/contract-files.sha256 \
    protocol/install/v1/install-request.schema.json \
    protocol/install/v1/install-response.schema.json \
    protocol/install/v1/definitions.schema.json protocol/install/v1/delegation-claims.schema.json \
    protocol/install/v1/install-authorization-receipt.schema.json \
    protocol/install/v1/install-authorization-request.schema.json \
    protocol/install/v1/resolve-response.schema.json protocol/install/v1/schema_validator.py \
    protocol/install/v1/team-list-response.schema.json protocol/install/v1/vectors.json \
    protocol/install/v1/verify.py \
    ./protocol/install/v1/
COPY protocol/http/v1/progress.py protocol/http/v1/supervisor.py protocol/http/v1/websocket.py ./protocol/http/v1/
COPY power/__init__.py power/challenges.py power/execution.py power/human.py power/journal.py ./power/
COPY storage/__init__.py storage/files.py ./storage/

RUN test -x /usr/bin/timeout && \
    groupadd --gid 10001 shimpzlocal && \
    groupadd --gid 10010 shimpzteam-token && \
    groupadd --gid 10016 shimpzbrain-runtime-token && \
    groupadd --gid 10017 shimpzassistant-egress-policy && \
    groupadd --gid 10021 shimpzsupervisor-key && \
    groupadd --gid 10022 shimpzaccount-egress-token && \
    useradd --uid 10001 --gid 10001 --groups 10010,10016,10017,10021,10022 \
        --no-create-home --shell /usr/sbin/nologin shimpzlocal && \
    mkdir -p /run/shimpz-local /run/shimpz-brain-runtime /run/shimpz-local-supervisor \
        /var/log/shimpz-local /var/lib/shimpz-local/storage \
        /var/lib/shimpz-local/inference /var/lib/shimpz-local/power-journal /var/lib/shimpz-local/assistant-egress \
        /var/lib/shimpz-local/assistant-integrations/state /var/lib/shimpz-local/assistant-integrations/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key && \
    mkdir -p /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chown shimpzlocal:shimpzteam-token /run/shimpz-local && chmod 2750 /run/shimpz-local && \
    chown shimpzlocal:shimpzbrain-runtime-token /run/shimpz-brain-runtime && \
    chmod 0750 /run/shimpz-brain-runtime && \
    chown root:shimpzsupervisor-key /run/shimpz-local-supervisor && \
    chmod 2770 /run/shimpz-local-supervisor && \
    chown shimpzlocal:shimpzlocal /var/log/shimpz-local /var/lib/shimpz-local/storage \
        /var/lib/shimpz-local/inference /var/lib/shimpz-local/power-journal \
        /var/lib/shimpz-local/assistant-integrations/state /var/lib/shimpz-local/assistant-integrations/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key && \
    chown shimpzlocal:shimpzlocal /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chown shimpzlocal:shimpzassistant-egress-policy /var/lib/shimpz-local/assistant-egress && \
    chmod 0770 /var/lib/shimpz-local/assistant-egress && \
    chmod 0700 /var/log/shimpz-local /var/lib/shimpz-local/storage /var/lib/shimpz-local/inference \
        /var/lib/shimpz-local/power-journal \
        /var/lib/shimpz-local/assistant-integrations/state /var/lib/shimpz-local/assistant-integrations/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key \
        /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chmod 0555 /app/local/healthcheck.py

LABEL org.opencontainers.image.source="https://github.com/TheShimpz/shimpz-teams" \
      org.shimpz.component="team-local" \
      org.shimpz.local.profile="local-v1"

USER 10001:10001
EXPOSE 7077
HEALTHCHECK --interval=30s --timeout=4s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["/opt/venv/bin/python", "-m", "local.healthcheck"]
ENTRYPOINT ["/opt/venv/bin/python", "-m", "local.app"]
