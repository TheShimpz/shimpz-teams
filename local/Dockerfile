# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=skip=SecretsUsedInArgOrEnv ; the Cloudflare Assistant image is a public OCI digest, not a credential
#
# The Team controller. Only the narrow LangGraph runtime protocol and Controller-owned Action loop are shared with the
# Brain.
FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

# Cosign is downloaded and hash-checked in its own stage, so curl and APT never reach an image layer.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS cosign
ARG COSIGN_VERSION=3.1.2
ARG COSIGN_AMD64_SHA256=f7622ed3cf22e55e1ae6377c080979ff77a22da9981c11df222a2e444991e7cf
ARG COSIGN_ARM64_SHA256=90e7ae0b5dfd60f20816b52c012addf7fc055ebcc7bea4ce81c428ca8518c302
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    case "${TARGETARCH}" in \
        amd64) cosign_sha256="${COSIGN_AMD64_SHA256}" ;; \
        arm64) cosign_sha256="${COSIGN_ARM64_SHA256}" ;; \
        *) echo "unsupported Cosign architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -LsSf "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-${TARGETARCH}" \
        -o /tmp/cosign && \
    echo "${cosign_sha256}  /tmp/cosign" | sha256sum -c -

# The dependency layer is the runtime's base and a pure function of the pinned base, uv, the lock, and Cosign
# (Shimpz ADR-0098): no ARG SOURCE_DATE_EPOCH, WORKDIR, COPY, or ADD here; uv, the lock, and Cosign arrive as
# read-only mounts on a discarded tmpfs, the uv cache is removed, and every /opt timestamp is fixed. An unchanged lock
# therefore yields the same layer bytes at every commit, with or without a build cache. The base ships no bytecode and
# the read-only runtime cannot write any, so the standard library and the environment are compiled here, hash-checked,
# with fixed timestamps; otherwise every Team start, health probe, and helper recompiles each imported module.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS dependencies
RUN --mount=type=tmpfs,target=/tmp \
    --mount=type=bind,from=uv,source=/uv,target=/tmp/uv \
    --mount=type=bind,source=pyproject.toml,target=/tmp/project/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/tmp/project/uv.lock \
    --mount=type=bind,from=cosign,source=/tmp/cosign,target=/tmp/cosign \
    cd /tmp/project && \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_CACHE_DIR=/opt/uv-cache UV_LINK_MODE=copy \
        /tmp/uv sync --frozen --no-install-project --no-dev --python 3.14 && \
    rm -rf /opt/uv-cache && \
    install -m 0755 /tmp/cosign /opt/venv/bin/cosign && \
    PYTHONDONTWRITEBYTECODE=1 /opt/venv/bin/python -m compileall -q -f --invalidation-mode checked-hash /usr/local/lib/python3.14 /opt/venv && \
    find /usr/local/lib/python3.14 \( -type d -o -name '*.pyc' \) -exec touch -h -d @0 {} + && \
    find /opt -depth -exec touch -h -d @0 {} +

FROM dependencies AS runtime
ARG SOURCE_DATE_EPOCH=0
# Users and runtime directories come first, and every source copy is a linked layer that no other copy depends on,
# so changing one source file rebuilds only its own layer.
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
        /var/lib/shimpz-local/inference /var/lib/shimpz-local/action-journal /var/lib/shimpz-local/assistant-egress \
        /var/lib/shimpz-local/assistant-integrations/state /var/lib/shimpz-local/assistant-integrations/key \
        /var/lib/shimpz-local/assistant-stored-inputs/state /var/lib/shimpz-local/assistant-stored-inputs/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key \
        /var/lib/shimpz-local/routines/state /var/lib/shimpz-local/routines/key && \
    mkdir -p /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chown shimpzlocal:shimpzteam-token /run/shimpz-local && chmod 2750 /run/shimpz-local && \
    chown shimpzlocal:shimpzbrain-runtime-token /run/shimpz-brain-runtime && \
    chmod 0750 /run/shimpz-brain-runtime && \
    chown root:shimpzsupervisor-key /run/shimpz-local-supervisor && \
    chmod 2770 /run/shimpz-local-supervisor && \
    chown shimpzlocal:shimpzlocal /var/log/shimpz-local /var/lib/shimpz-local/storage \
        /var/lib/shimpz-local/inference /var/lib/shimpz-local/action-journal \
        /var/lib/shimpz-local/assistant-integrations/state /var/lib/shimpz-local/assistant-integrations/key \
        /var/lib/shimpz-local/assistant-stored-inputs/state /var/lib/shimpz-local/assistant-stored-inputs/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key \
        /var/lib/shimpz-local/routines/state /var/lib/shimpz-local/routines/key && \
    chown shimpzlocal:shimpzlocal /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chown shimpzlocal:shimpzassistant-egress-policy /var/lib/shimpz-local/assistant-egress && \
    chmod 0750 /var/lib/shimpz-local/assistant-egress && \
    chmod 0700 /var/log/shimpz-local /var/lib/shimpz-local/storage /var/lib/shimpz-local/inference \
        /var/lib/shimpz-local/action-journal \
        /var/lib/shimpz-local/assistant-integrations/state /var/lib/shimpz-local/assistant-integrations/key \
        /var/lib/shimpz-local/assistant-stored-inputs/state /var/lib/shimpz-local/assistant-stored-inputs/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key \
        /var/lib/shimpz-local/routines/state /var/lib/shimpz-local/routines/key \
        /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign

WORKDIR /app

COPY --link --chmod=0555 local/activity.py local/healthcheck.py ./local/
COPY --link local/__init__.py local/app.py local/audit.py local/authority.py local/composition.py local/errors.py \
    local/inference.py local/labels.py local/lifecycle.py local/names.py local/prepare.py local/token.py \
    local/validation.py ./local/
COPY --link local/install/__init__.py local/install/automatic.py local/install/collector.py local/install/developers.py local/install/inventory.py local/install/preview.py \
    local/install/registry.py local/install/runtime.py local/install/service.py local/install/snapshots.py \
    local/install/source_package.py ./local/install/
COPY --link assistant/__init__.py assistant/action_schema.py assistant/cache.py assistant/details.py \
    assistant/effect.py assistant/genesis.py assistant/language.py assistant/manifest.py assistant/spec.py \
    ./assistant/
COPY --link chat/__init__.py chat/attachments.py chat/contract.py chat/knowledge.py chat/orchestrator.py chat/progress.py \
    chat/turn.py ./chat/
COPY --link core/__init__.py core/base64url.py core/canonical_json.py core/identifier.py ./core/
COPY --link core/http/__init__.py core/http/stdlib.py core/http/strict.py ./core/http/
COPY --link egress/__init__.py egress/policy.py ./egress/
COPY --link inference/__init__.py inference/abort.py inference/client.py inference/config.py inference/errors.py \
    inference/model_catalog.json \
    inference/recovery.py inference/token.py inference/usage.py ./inference/
COPY --link integrations/__init__.py integrations/account_egress.py integrations/broker.py integrations/challenge_store.py \
    integrations/challenges.py integrations/flow.py integrations/http.py \
    integrations/pkce.py integrations/providers.py \
    integrations/service.py integrations/store.py ./integrations/
COPY --link local/assistant/__init__.py local/assistant/api.py local/assistant/egress.py \
    local/assistant/isolation.py local/assistant/lifecycle.py local/assistant/resources.py \
    local/assistant/rpc.py ./local/assistant/
COPY --link local/chat/__init__.py local/chat/api.py local/chat/attachments.py local/chat/capabilities.py \
    local/chat/continuation.py \
    local/chat/continuation_store.py local/chat/execution.py local/chat/human.py local/chat/pause.py \
    local/chat/private.py local/chat/resume.py local/chat/segment.py local/chat/service.py \
    local/chat/state.py local/chat/types.py ./local/chat/
COPY --link local/routine/__init__.py local/routine/card.py local/routine/compiled.py local/routine/contracts.py \
    local/routine/diagnostics.py local/routine/human.py local/routine/incident.py local/routine/lifecycle.py \
    local/routine/manage.py local/routine/notices.py local/routine/proposal.py local/routine/protection.py \
    local/routine/recorder.py local/routine/recovery.py local/routine/run.py local/routine/state.py \
    local/routine/store.py local/routine/watchdog.py ./local/routine/
COPY --link routine/__init__.py routine/claim.py routine/compose.py routine/cursor.py routine/definition.py \
    routine/hold.py routine/pin.py routine/plan.py routine/provenance.py routine/record.py \
    routine/recording.py routine/rerun.py routine/runs.py routine/schedule.py routine/starts.py routine/trace.py \
    ./routine/
COPY --link local/http/__init__.py local/http/audit.py local/http/deadline.py local/http/dispatch.py local/http/inference.py \
    local/http/routine.py local/http/server.py local/http/stream.py ./local/http/
COPY --link install/__init__.py install/artifact_trust.py install/bindings.py install/contract.py install/icons.py install/lock.py \
    install/registry_auth.py install/update.py ./install/
COPY --link protocol/install/upstream.json ./protocol/install/
COPY --link protocol/install/v1/README.md protocol/install/v1/contract-files.sha256 \
    protocol/install/v1/definitions.schema.json \
    protocol/install/v1/resolve-response.schema.json protocol/install/v1/schema_validator.py \
    protocol/install/v1/vectors.json protocol/install/v1/verify.py \
    ./protocol/install/v1/
COPY --link protocol/action/v1/schema.py ./protocol/action/v1/
COPY --link protocol/http/v1/identifiers.py protocol/http/v1/payload.py protocol/http/v1/phrase.py \
    protocol/http/v1/progress.py protocol/http/v1/purpose.py protocol/http/v1/routine.py \
    protocol/http/v1/routine_context.py protocol/http/v1/routine_notice.py protocol/http/v1/routine_proposal.py \
    protocol/http/v1/routine_run.py protocol/http/v1/strict_json.py protocol/http/v1/supervisor.py \
    protocol/http/v1/turn.py ./protocol/http/v1/
COPY --link protocol/assistant/v1/validators/action_effect.py protocol/assistant/v1/validators/failure.py \
    protocol/assistant/v1/validators/human_request.py protocol/assistant/v1/validators/input_file.py \
    protocol/assistant/v1/validators/message_catalog.py \
    ./protocol/assistant/v1/validators/
COPY --link action/__init__.py action/challenges.py action/dispatch.py action/execution.py action/failure.py action/frames.py \
    action/files.py action/human.py action/journal.py action/provider.py action/result.py \
    action/stored_input.py ./action/
COPY --link storage/__init__.py storage/files.py storage/private_state.py ./storage/
COPY --link prepare/__init__.py prepare/detect.py prepare/helper.py prepare/image.py prepare/limits.py prepare/pdf.py \
    prepare/service.py prepare/worker.py ./prepare/
# The application is compiled like its environment, after its last copy.
RUN PYTHONDONTWRITEBYTECODE=1 /opt/venv/bin/python -m compileall -q -f --invalidation-mode checked-hash /app

LABEL org.opencontainers.image.source="https://github.com/TheShimpz/shimpz-teams" \
      org.shimpz.component="team-local" \
      org.shimpz.local.profile="local-v1"

USER 10001:10001
EXPOSE 7077
HEALTHCHECK --interval=30s --timeout=4s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["/opt/venv/bin/python", "-m", "local.healthcheck"]
ENTRYPOINT ["/opt/venv/bin/python", "-m", "local.app"]
