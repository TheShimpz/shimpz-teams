# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# check=skip=SecretsUsedInArgOrEnv ; false positive: the *_TOKEN_GID/DOCKER_GID ARGs are numeric group IDs, never secrets
#
# team — the only container holding /var/run/docker.sock, dedicated to Team lifecycle.
# `shimpz-brain` never mounts the socket; the
# authenticated admin panel calls Team's restricted, allowlisted, audited API instead. This
# process's own UID is unprivileged (10001) — defense-in-depth against trivial filesystem tampering,
# not a claim that socket access itself is contained (holding the socket is host-root-equivalent).
# Pinned by digest; bump deliberately.
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1
ARG SOURCE_DATE_EPOCH=0

ARG DEBIAN_SNAPSHOT=20260623T000000Z
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

ARG UV_VERSION=0.11.25
# The hash pins the immutable versioned installer served by astral.sh.
ARG UV_INSTALL_SHA256=ca2de1bca2913ba30ce88658b6d90a663c627ecac378803aa58084a9adb35a46
ARG COSIGN_VERSION=3.0.6
ARG COSIGN_AMD64_SHA256=c956e5dfcac53d52bcf058360d579472f0c1d2d9b69f55209e256fe7783f4c74
ARG COSIGN_ARM64_SHA256=bedac92e8c3729864e13d4a17048007cfafa79d5deca993a43a90ffe018ef2b8
ARG TARGETARCH
# Must match the GID that owns /var/run/docker.sock on the host (stat -c '%g' /var/run/docker.sock).
ARG DOCKER_GID=989
# Fixed GID for Team's token group — the caller (admin, uid 1000) joins it to read the token.
# Distinct from every other sidecar's (10002–10009) so no token is readable via another's group.
ARG SHIMPZ_TEAM_TOKEN_GID=10010
# The PostgreSQL Service token group — Team joins it read-only to request a scoped database.
# MUST match the Service image's SHIMPZ_POSTGRESQL_SERVICE_TOKEN_GID so the shared 0440 token is readable.
ARG SHIMPZ_POSTGRESQL_SERVICE_TOKEN_GID=10004
ARG SHIMPZ_BRAINCRED_UNSEAL_TOKEN_GID=10012
ARG SHIMPZ_ACCOUNTS_BRAIN_RESOLVE_TOKEN_GID=10013
# Controller-owned token shared read-only with the isolated Brain runtime.
ARG SHIMPZ_BRAIN_RUNTIME_TOKEN_GID=10016
ARG SHIMPZ_APP_EGRESS_POLICY_GID=10017

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
    && groupadd -g "${SHIMPZ_BRAINCRED_UNSEAL_TOKEN_GID}" shimpzbraincred-unseal-token \
    && groupadd -g "${SHIMPZ_ACCOUNTS_BRAIN_RESOLVE_TOKEN_GID}" shimpzbrain-resolve \
    && groupadd -g "${SHIMPZ_BRAIN_RUNTIME_TOKEN_GID}" shimpzbrain-runtime-token \
    && groupadd -g "${SHIMPZ_APP_EGRESS_POLICY_GID}" shimpzapp-egress-policy \
    && useradd -u 10001 -g dockersock \
        -G shimpzteam-token,shimpzpostgresql-token,shimpzbraincred-unseal-token,shimpzbrain-resolve,shimpzbrain-runtime-token,shimpzapp-egress-policy \
        -M -s /usr/sbin/nologin shimpzteam

WORKDIR /app
# BuildKit is required: read-only bind mounts keep dependency metadata out of every image layer while the
# frozen lock binds Docker SDK's complete transitive graph and artifact hashes.
RUN --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml,ro \
    --mount=type=bind,source=uv.lock,target=/app/uv.lock,ro \
    UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-install-project --no-dev --python 3.14 && \
    rm -rf /root/.cache/uv

COPY hosted/__init__.py hosted/app.py hosted/healthcheck.py ./hosted/
COPY validate.py manifests.py audit.py ./
COPY assistant_human/__init__.py assistant_human/assistant_account_challenges.py \
     assistant_human/assistant_account_flow.py \
     assistant_human/assistant_chat.py assistant_human/assistant_genesis.py \
     assistant_human/assistant_manifest.py assistant_human/assistant_registry.py \
     assistant_human/challenge_store.py assistant_human/hosted_assistants.py \
     assistant_human/hosted_chat_api.py assistant_human/hosted_chat_segment.py \
     assistant_human/oauth_account_service.py \
     assistant_human/oauth_account_store.py assistant_human/oauth_broker_client.py \
     assistant_human/oauth_http_client.py assistant_human/oauth_pkce_challenges.py \
     assistant_human/oauth_providers.py assistant_human/private_state.py \
     ./assistant_human/
COPY chat/__init__.py chat/orchestrator.py chat/turn.py ./chat/
COPY controller_runtime/__init__.py controller_runtime/accounts_client.py \
     controller_runtime/brain_credentials_client.py controller_runtime/brain_runtime_client.py \
     controller_runtime/brain_runtime_token_store.py controller_runtime/cleanup_state.py \
     controller_runtime/postgresql_service_client.py \
     controller_runtime/token_store.py ./controller_runtime/
COPY container_policy/__init__.py container_policy/hosted_apps.py container_policy/hosted_lifecycle.py \
     container_policy/hosted_resources.py container_policy/network.py ./container_policy/
# Keep the vendored protocol byte-complete for its independent digest re-verification.
COPY hosted/install/__init__.py hosted/install/developers_client.py hosted/install/developers_delegation.py \
     hosted/install/publication.py \
     ./hosted/install/
COPY install/__init__.py install/artifact.py install/artifact_trust.py install/bindings.py \
     install/contract.py install/registry_auth.py ./install/
COPY protocol/install/upstream.json ./protocol/install/
COPY protocol/install/v1/README.md protocol/install/v1/contract-files.sha256 \
     protocol/install/v1/controller-install-request.schema.json \
     protocol/install/v1/controller-install-response.schema.json \
     protocol/install/v1/definitions.schema.json protocol/install/v1/delegation-claims.schema.json \
     protocol/install/v1/install-authorization-receipt.schema.json \
     protocol/install/v1/install-authorization-request.schema.json \
     protocol/install/v1/resolve-response.schema.json protocol/install/v1/schema_validator.py \
     protocol/install/v1/team-list-response.schema.json protocol/install/v1/vectors.json \
     protocol/install/v1/verify.py ./protocol/install/v1/
COPY core/__init__.py core/strict_json.py ./core/
COPY core/http/__init__.py core/http/stdlib.py core/http/strict.py ./core/http/
COPY egress/__init__.py egress/policy.py ./egress/
COPY http_boundary/__init__.py http_boundary/hosted.py http_boundary/hosted_controller.py \
     http_boundary/runtime_state.py ./http_boundary/
COPY inference/__init__.py inference/config.py inference/model_catalog.json ./inference/
COPY power/__init__.py power/execution.py power/journal.py ./power/
COPY storage/__init__.py storage/files.py ./storage/

# Pre-create + own every named-volume mountpoint so the fresh (root:root) volume is writable by the
# non-root user. /run/shimpz-team gets GROUP `shimpzteam-token` so the fresh volume's
# perms already match what the token file inside needs — readable by the admin panel via that group.
RUN mkdir -p /run/shimpz-team /var/log/team /var/lib/team/postgresql-principals \
        /var/lib/team/storage \
        /var/lib/team/cleanup \
        /var/lib/team/inference \
        /var/lib/team/power-journal \
        /var/lib/team/assistant-accounts/state \
        /var/lib/team/assistant-accounts/key \
        /var/lib/team/dynamic-assistants \
        /var/lib/team/cosign \
        /app-egress-policy /run/shimpz-braincred-unseal /run/shimpz-accounts-brain-resolve \
        /run/shimpz-brain-runtime \
    && chown shimpzteam:shimpzteam-token /run/shimpz-team && chmod 750 /run/shimpz-team \
    && chown shimpzteam:shimpzbraincred-unseal-token /run/shimpz-braincred-unseal \
    && chown shimpzteam:shimpzbrain-resolve /run/shimpz-accounts-brain-resolve \
    && chmod 0750 /run/shimpz-braincred-unseal /run/shimpz-accounts-brain-resolve \
    && chown shimpzteam:shimpzbrain-runtime-token /run/shimpz-brain-runtime \
    && chmod 0750 /run/shimpz-brain-runtime \
    && chown -R shimpzteam:dockersock /var/log/team /var/lib/team \
    && chmod 0700 /var/lib/team/postgresql-principals \
        /var/lib/team/storage \
        /var/lib/team/cleanup \
        /var/lib/team/inference \
        /var/lib/team/power-journal \
        /var/lib/team/assistant-accounts/state \
        /var/lib/team/assistant-accounts/key \
        /var/lib/team/dynamic-assistants \
        /var/lib/team/cosign \
    && chown shimpzteam:shimpzapp-egress-policy /app-egress-policy \
    && chmod 0770 /app-egress-policy

USER shimpzteam
EXPOSE 7077
# The umbrella Compose deployment owns liveness and invokes the hosted.healthcheck module.
ENTRYPOINT ["/opt/venv/bin/python", "-m", "hosted.app"]
