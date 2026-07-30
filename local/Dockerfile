# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=skip=SecretsUsedInArgOrEnv ; the Cloudflare Assistant image is a public OCI digest, not a credential
#
# Separate local-only Team controller. It does not copy the hosted app.py/runsc/PG control
# plane; only the narrow LangGraph runtime protocol and Controller-owned Power loop are shared.
FROM ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 AS uv
ARG SOURCE_DATE_EPOCH=0

FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS dependencies
ARG SOURCE_DATE_EPOCH=0
ARG COSIGN_VERSION=3.0.6
ARG COSIGN_AMD64_SHA256=c956e5dfcac53d52bcf058360d579472f0c1d2d9b69f55209e256fe7783f4c74
ARG COSIGN_ARM64_SHA256=bedac92e8c3729864e13d4a17048007cfafa79d5deca993a43a90ffe018ef2b8
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

FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS runtime
ARG SOURCE_DATE_EPOCH=0
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv

COPY local/__init__.py local/app.py local/healthcheck.py ./local/
COPY local/install/__init__.py local/install/developers.py local/install/registry.py \
    local/install/runtime.py ./local/install/
COPY assistant_human/__init__.py assistant_human/assistant_account_challenges.py \
    assistant_human/assistant_account_flow.py \
    assistant_human/assistant_chat.py assistant_human/assistant_genesis.py \
    assistant_human/assistant_manifest.py assistant_human/assistant_registry.py \
    assistant_human/challenge_store.py assistant_human/oauth_account_service.py \
    assistant_human/oauth_account_store.py assistant_human/oauth_broker_client.py \
    assistant_human/oauth_http_client.py assistant_human/oauth_pkce_challenges.py \
    assistant_human/oauth_providers.py assistant_human/private_state.py ./assistant_human/
COPY chat/__init__.py chat/orchestrator.py chat/turn.py ./chat/
COPY controller_runtime/__init__.py controller_runtime/brain_runtime_client.py \
    controller_runtime/brain_runtime_token_store.py \
    controller_runtime/local_chat_continuations.py \
    controller_runtime/local_chat_continuation_store.py \
    controller_runtime/local_token_store.py ./controller_runtime/
COPY container_policy/__init__.py container_policy/local.py container_policy/network.py ./container_policy/
COPY core/__init__.py core/strict_json.py ./core/
COPY core/http/__init__.py core/http/stdlib.py core/http/strict.py ./core/http/
COPY egress/__init__.py egress/policy.py ./egress/
COPY http_boundary/__init__.py http_boundary/local.py ./http_boundary/
COPY inference/__init__.py inference/config.py inference/model_catalog.json ./inference/
COPY install/__init__.py install/artifact_trust.py install/bindings.py install/contract.py \
    install/registry_auth.py ./install/
COPY protocol/install/v1/definitions.schema.json protocol/install/v1/resolve-response.schema.json \
    protocol/install/v1/schema_validator.py protocol/install/v1/verify.py \
    ./protocol/install/v1/
COPY local_support/__init__.py local_support/assistant_api.py local_support/assistant_lifecycle.py \
    local_support/assistant_resources.py local_support/assistant_rpc.py local_support/audit.py \
    local_support/chat_api.py local_support/chat_execution.py local_support/chat_pause.py \
    local_support/chat_private.py local_support/chat_resume.py local_support/chat_segment.py \
    local_support/chat_state.py local_support/chat_types.py local_support/egress.py local_support/errors.py \
    local_support/http.py local_support/labels.py local_support/team_lifecycle.py local_support/validation.py \
    ./local_support/
COPY power/__init__.py power/execution.py power/journal.py ./power/
COPY storage/__init__.py storage/files.py ./storage/

RUN groupadd --gid 10001 shimpzlocal && \
    groupadd --gid 10010 shimpzteam-token && \
    groupadd --gid 10016 shimpzbrain-runtime-token && \
    groupadd --gid 10017 shimpzapp-egress-policy && \
    useradd --uid 10001 --gid 10001 --groups 10010,10016,10017 --no-create-home --shell /usr/sbin/nologin shimpzlocal && \
    mkdir -p /run/shimpz-local /run/shimpz-brain-runtime /var/log/shimpz-local /var/lib/shimpz-local/storage \
        /var/lib/shimpz-local/inference /var/lib/shimpz-local/power-journal /var/lib/shimpz-local/app-egress \
        /var/lib/shimpz-local/assistant-accounts/state /var/lib/shimpz-local/assistant-accounts/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key && \
    mkdir -p /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chown shimpzlocal:shimpzteam-token /run/shimpz-local && chmod 2750 /run/shimpz-local && \
    chown shimpzlocal:shimpzbrain-runtime-token /run/shimpz-brain-runtime && \
    chmod 0750 /run/shimpz-brain-runtime && \
    chown shimpzlocal:shimpzlocal /var/log/shimpz-local /var/lib/shimpz-local/storage \
        /var/lib/shimpz-local/inference /var/lib/shimpz-local/power-journal \
        /var/lib/shimpz-local/assistant-accounts/state /var/lib/shimpz-local/assistant-accounts/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key && \
    chown shimpzlocal:shimpzlocal /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chown shimpzlocal:shimpzapp-egress-policy /var/lib/shimpz-local/app-egress && \
    chmod 0770 /var/lib/shimpz-local/app-egress && \
    chmod 0700 /var/log/shimpz-local /var/lib/shimpz-local/storage /var/lib/shimpz-local/inference \
        /var/lib/shimpz-local/power-journal \
        /var/lib/shimpz-local/assistant-accounts/state /var/lib/shimpz-local/assistant-accounts/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key \
        /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chmod 0555 /app/local/healthcheck.py

LABEL org.opencontainers.image.source="https://github.com/TheShimpz/shimpz-teams" \
      org.shimpz.component="team-local" \
      org.shimpz.local.profile="single-owner-local-v1"

USER 10001:10001
EXPOSE 7077
HEALTHCHECK --interval=10s --timeout=4s --start-period=10s --retries=3 \
    CMD ["/opt/venv/bin/python", "-m", "local.healthcheck"]
ENTRYPOINT ["/opt/venv/bin/python", "-m", "local.app"]
