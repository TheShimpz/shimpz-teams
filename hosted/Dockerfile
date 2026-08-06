# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=skip=SecretsUsedInArgOrEnv ; false positive: the *_TOKEN_GID/DOCKER_GID ARGs are numeric group IDs, never secrets
#
# team — the only container holding /var/run/docker.sock, dedicated to Team lifecycle.
# `shimpz-brain` never mounts the socket; the
# authenticated admin panel calls Team's restricted, allowlisted, audited API instead. This
# process's own UID is unprivileged (10001) — defense-in-depth against trivial filesystem tampering,
# not a claim that socket access itself is contained (holding the socket is host-root-equivalent).
# Pinned by digest; bump deliberately.
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6
ARG SOURCE_DATE_EPOCH=0

ARG DEBIAN_SNAPSHOT=20260731T000000Z
RUN set -eux; \
    . /etc/os-release; \
    archive_keyring="$(find /usr/share/keyrings -maxdepth 1 -type f -name 'debian-archive-keyring.*' -print -quit)"; \
    test -n "$archive_keyring"; \
    rm -f /etc/apt/sources.list; \
    find /etc/apt/sources.list.d -maxdepth 1 -type f -delete; \
    printf '%s\n' \
        "deb [signed-by=${archive_keyring}] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} ${VERSION_CODENAME} main" \
        "deb [signed-by=${archive_keyring}] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} ${VERSION_CODENAME}-updates main" \
        "deb [signed-by=${archive_keyring}] https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} ${VERSION_CODENAME}-security main" \
        > /etc/apt/sources.list.d/debian-snapshot.list; \
    printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99shimpz-snapshot; \
    test "$(grep -Fc "https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" /etc/apt/sources.list.d/debian-snapshot.list)" -eq 2; \
    test "$(grep -Fc "https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" /etc/apt/sources.list.d/debian-snapshot.list)" -eq 1

ARG UV_VERSION=0.12.1
# The hash pins the immutable versioned installer served by astral.sh.
ARG UV_INSTALL_SHA256=d3f5412d38c99f9d024901843bf98206f0d2c6dbe64df40d0b740e2751ca62c1
ARG COSIGN_VERSION=3.1.2
ARG COSIGN_AMD64_SHA256=f7622ed3cf22e55e1ae6377c080979ff77a22da9981c11df222a2e444991e7cf
ARG COSIGN_ARM64_SHA256=90e7ae0b5dfd60f20816b52c012addf7fc055ebcc7bea4ce81c428ca8518c302
ARG TARGETARCH
# Must match the GID that owns /var/run/docker.sock on the host (stat -c '%g' /var/run/docker.sock).
ARG DOCKER_GID=989
# Fixed GID for Team's token group — the caller (admin, uid 1000) joins it to read the token.
# Distinct from every other sidecar's (10002–10009) so no token is readable via another's group.
ARG SHIMPZ_TEAM_TOKEN_GID=10010
# The PostgreSQL Service token group — Team joins it read-only to request a scoped database.
# MUST match the Service image's SHIMPZ_POSTGRESQL_SERVICE_TOKEN_GID so the shared 0440 token is readable.
ARG SHIMPZ_POSTGRESQL_SERVICE_TOKEN_GID=10004
ARG SHIMPZ_INTEGRATION_SECRETS_UNSEAL_TOKEN_GID=10012
ARG SHIMPZ_ACCOUNT_MODEL_PROVIDER_RESOLVE_TOKEN_GID=10013
ARG SHIMPZ_ACCOUNT_TEAM_AUTHORITY_TOKEN_GID=10019
# Controller-owned token shared read-only with the isolated Brain runtime.
ARG SHIMPZ_BRAIN_RUNTIME_TOKEN_GID=10016
ARG SHIMPZ_ASSISTANT_EGRESS_POLICY_GID=10017

# Downloaded + hash-checked BEFORE execution — never `curl | sh`.
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" -o /tmp/uv-install.sh && \
    echo "${UV_INSTALL_SHA256}  /tmp/uv-install.sh" | sha256sum -c - && \
    env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh && \
    rm -f /tmp/uv-install.sh && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/lib/apt/periodic/* /var/cache/apt/* /var/cache/fontconfig/* \
        /var/cache/ldconfig/aux-cache /var/cache/man/* /var/log/apt/* \
        /var/log/alternatives.log /var/log/dpkg.log /root/.cache/uv

RUN case "${TARGETARCH}" in \
        amd64) cosign_sha256="${COSIGN_AMD64_SHA256}" ;; \
        arm64) cosign_sha256="${COSIGN_ARM64_SHA256}" ;; \
        *) echo "unsupported Cosign architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -LsSf "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-${TARGETARCH}" \
        -o /tmp/cosign \
    && echo "${cosign_sha256}  /tmp/cosign" | sha256sum -c - \
    && install -m 0755 /tmp/cosign /usr/local/bin/cosign \
    && rm -f /tmp/cosign

# Every shared capability has a distinct supplementary group.
RUN groupadd -g "${DOCKER_GID}" dockersock \
    && groupadd -g "${SHIMPZ_TEAM_TOKEN_GID}" shimpzteam-token \
    && groupadd -g "${SHIMPZ_POSTGRESQL_SERVICE_TOKEN_GID}" shimpzpostgresql-token \
    && groupadd -g "${SHIMPZ_INTEGRATION_SECRETS_UNSEAL_TOKEN_GID}" shimpzintegration-unseal-token \
    && groupadd -g "${SHIMPZ_ACCOUNT_MODEL_PROVIDER_RESOLVE_TOKEN_GID}" shimpzmodelprovider-resolve \
    && groupadd -g "${SHIMPZ_ACCOUNT_TEAM_AUTHORITY_TOKEN_GID}" shimpzaccount-team-authority \
    && groupadd -g "${SHIMPZ_BRAIN_RUNTIME_TOKEN_GID}" shimpzbrain-runtime-token \
    && groupadd -g "${SHIMPZ_ASSISTANT_EGRESS_POLICY_GID}" shimpzassistant-egress-policy \
    && useradd -u 10001 -g dockersock \
        -G shimpzteam-token,shimpzpostgresql-token,shimpzintegration-unseal-token,shimpzmodelprovider-resolve,shimpzaccount-team-authority,shimpzbrain-runtime-token,shimpzassistant-egress-policy \
        -M -s /usr/sbin/nologin shimpzteam

WORKDIR /app
# BuildKit is required: read-only bind mounts keep dependency metadata out of every image layer while the
# frozen lock binds Docker SDK's complete transitive graph and artifact hashes.
RUN --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml,ro \
    --mount=type=bind,source=uv.lock,target=/app/uv.lock,ro \
    UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-install-project --no-dev --python 3.14 && \
    rm -rf /root/.cache/uv

COPY hosted/__init__.py hosted/app.py hosted/audit.py hosted/authority.py hosted/cleanup.py \
     hosted/container.py hosted/healthcheck.py hosted/state.py hosted/token.py \
     hosted/validation.py ./hosted/
COPY assistant/__init__.py assistant/genesis.py assistant/manifest.py assistant/spec.py ./assistant/
COPY chat/__init__.py chat/contract.py chat/orchestrator.py chat/progress.py chat/turn.py ./chat/
COPY hosted/assistant/__init__.py hosted/assistant/lifecycle.py hosted/assistant/runtime.py \
     ./hosted/assistant/
COPY hosted/chat/__init__.py hosted/chat/api.py hosted/chat/segment.py ./hosted/chat/
# Keep the vendored protocol byte-complete for its independent digest re-verification.
COPY hosted/install/__init__.py hosted/install/developers_client.py hosted/install/developers_delegation.py \
     hosted/install/http.py hosted/install/publication.py \
     ./hosted/install/
COPY hosted/http/__init__.py hosted/http/routes.py hosted/http/server.py ./hosted/http/
COPY hosted/team/__init__.py hosted/team/lifecycle.py hosted/team/postgresql.py \
     hosted/team/resources.py ./hosted/team/
COPY install/__init__.py install/artifact.py install/artifact_trust.py install/bindings.py \
     install/contract.py install/icons.py install/registry_auth.py ./install/
COPY protocol/install/upstream.json ./protocol/install/
COPY protocol/install/v1/README.md protocol/install/v1/contract-files.sha256 \
     protocol/install/v1/install-request.schema.json \
     protocol/install/v1/install-response.schema.json \
     protocol/install/v1/definitions.schema.json protocol/install/v1/delegation-claims.schema.json \
     protocol/install/v1/install-authorization-receipt.schema.json \
     protocol/install/v1/install-authorization-request.schema.json \
     protocol/install/v1/resolve-response.schema.json protocol/install/v1/schema_validator.py \
     protocol/install/v1/team-list-response.schema.json protocol/install/v1/vectors.json \
     protocol/install/v1/verify.py ./protocol/install/v1/
COPY protocol/account/authority/upstream.json ./protocol/account/authority/
COPY protocol/account/authority/v1/README.md protocol/account/authority/v1/contract-files.sha256 \
     protocol/account/authority/v1/evaluation-request.schema.json \
     protocol/account/authority/v1/evaluation-response.schema.json \
     protocol/account/authority/v1/vectors.json protocol/account/authority/v1/verify.py \
     ./protocol/account/authority/v1/
COPY protocol/http/v1/payload.py ./protocol/http/v1/
COPY core/__init__.py core/strict_json.py ./core/
COPY core/container/__init__.py core/container/network.py ./core/container/
COPY core/http/__init__.py core/http/stdlib.py core/http/strict.py ./core/http/
COPY egress/__init__.py egress/policy.py ./egress/
COPY inference/__init__.py inference/client.py inference/config.py inference/integration_secrets.py \
     inference/model_catalog.json inference/token.py ./inference/
COPY integrations/__init__.py integrations/account_egress.py integrations/broker.py integrations/challenge_store.py \
     integrations/challenges.py integrations/flow.py integrations/http.py \
     integrations/pkce.py integrations/private_state.py integrations/providers.py \
     integrations/service.py integrations/store.py ./integrations/
COPY power/__init__.py power/challenges.py power/execution.py power/human.py power/journal.py ./power/
COPY storage/__init__.py storage/files.py ./storage/

# Pre-create + own every named-volume mountpoint so the fresh (root:root) volume is writable by the
# non-root user. /run/shimpz-team gets GROUP `shimpzteam-token` so the fresh volume's
# perms already match what the token file inside needs — readable by the admin panel via that group.
RUN mkdir -p /run/shimpz-team /var/log/team /var/lib/team/postgresql-principals \
        /var/lib/team/storage \
        /var/lib/team/cleanup \
        /var/lib/team/inference \
        /var/lib/team/power-journal \
        /var/lib/team/assistant-integrations/state \
        /var/lib/team/assistant-integrations/key \
        /var/lib/team/dynamic-assistants \
        /var/lib/team/cosign \
        /assistant-egress-policy /run/shimpz-integration-secrets-unseal /run/shimpz-account-model-provider-resolve \
        /run/shimpz-account-team-authority \
        /run/shimpz-brain-runtime \
    && chown shimpzteam:shimpzteam-token /run/shimpz-team && chmod 750 /run/shimpz-team \
    && chown shimpzteam:shimpzintegration-unseal-token /run/shimpz-integration-secrets-unseal \
    && chown shimpzteam:shimpzmodelprovider-resolve /run/shimpz-account-model-provider-resolve \
    && chown shimpzteam:shimpzaccount-team-authority /run/shimpz-account-team-authority \
    && chmod 0750 /run/shimpz-integration-secrets-unseal /run/shimpz-account-model-provider-resolve \
        /run/shimpz-account-team-authority \
    && chown shimpzteam:shimpzbrain-runtime-token /run/shimpz-brain-runtime \
    && chmod 0750 /run/shimpz-brain-runtime \
    && chown -R shimpzteam:dockersock /var/log/team /var/lib/team \
    && chmod 0700 /var/lib/team/postgresql-principals \
        /var/lib/team/storage \
        /var/lib/team/cleanup \
        /var/lib/team/inference \
        /var/lib/team/power-journal \
        /var/lib/team/assistant-integrations/state \
        /var/lib/team/assistant-integrations/key \
        /var/lib/team/dynamic-assistants \
        /var/lib/team/cosign \
    && chown shimpzteam:shimpzassistant-egress-policy /assistant-egress-policy \
    && chmod 0770 /assistant-egress-policy

USER shimpzteam
EXPOSE 7077
# The umbrella Compose deployment owns liveness and invokes the hosted.healthcheck module.
ENTRYPOINT ["/opt/venv/bin/python", "-m", "hosted.app"]
