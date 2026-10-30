# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# check=skip=SecretsUsedInArgOrEnv ; the Cloudflare Assistant image is a public OCI digest, not a credential
#
# The Team controller. Only the narrow LangGraph runtime protocol and Controller-owned Action loop are shared with the
# Brain.
FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

# Cosign is downloaded and hash-checked in its own stage, so curl and APT never reach an image layer.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS cosign
ARG COSIGN_VERSION=3.1.3
ARG COSIGN_AMD64_SHA256=4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71
ARG COSIGN_ARM64_SHA256=c5d324e091826b0d7a78eb16fef316450b4eb9aaec045611c08ba06f5e73220a
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

# ── The Rust controller (ADR-0123, plan phase R7), built beside the Python image until the swap ─────────────────────
# Targets: `rust-runtime` is the controller image; `fixture` adds the controller suite and its test-only runner and is
# never published.

# PDFium for the attachment worker: the archive and the library inside it are both hash-checked against the pins of
# prepare/pdfium.toml, which is copied beside the library as its provenance (V8- and XFA-free builds).
FROM cosign AS pdfium
ARG TARGETARCH
RUN --mount=type=bind,source=prepare/pdfium.toml,target=/tmp/pdfium.toml \
    case "${TARGETARCH}" in \
        amd64) architecture=x86_64 ;; \
        arm64) architecture=aarch64 ;; \
        *) echo "unsupported PDFium architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    pin() { python3 -c 'import sys, tomllib; print(tomllib.load(open("/tmp/pdfium.toml", "rb"))[sys.argv[1]][sys.argv[2]])' "${architecture}" "$1"; } && \
    curl -LsSf "$(pin url)" -o /tmp/pdfium.tgz && \
    echo "$(pin archive_sha256)  /tmp/pdfium.tgz" | sha256sum -c - && \
    mkdir /tmp/pdfium && tar --no-same-owner -xzf /tmp/pdfium.tgz -C /tmp/pdfium lib/libpdfium.so && \
    echo "$(pin library_sha256)  /tmp/pdfium/lib/libpdfium.so" | sha256sum -c -

# The zone database the Routine phrase tables were generated from (2026c), taken whole from the pinned base that
# carries it; the controller reads it through TZDIR.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS tzdata
RUN test "$(head -n 1 /usr/share/zoneinfo/tzdata.zi)" = "# version 2026c"

# Every account, group, and runtime directory the controller and its Compose service rely on, at its exact owner and
# mode; the runtime copies the tree as it is. The groups are named because the controller resolves them by name, and
# the base's own accounts are kept.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS rootfs
RUN mkdir -p /rootfs/etc /rootfs/run/shimpz-local /rootfs/run/shimpz-brain-runtime /rootfs/run/shimpz-local-supervisor \
        /rootfs/var/log/shimpz-local && \
    for name in storage inference action-journal publications cosign assistant-egress; do \
        mkdir -p "/rootfs/var/lib/shimpz-local/${name}"; \
    done && \
    for name in assistant-integrations assistant-stored-inputs chat-continuations routines browser-sign-ins; do \
        mkdir -p "/rootfs/var/lib/shimpz-local/${name}/state" "/rootfs/var/lib/shimpz-local/${name}/key"; \
    done && \
    printf '%s\n' 'root:x:0:0:root:/root:/sbin/nologin' 'nobody:x:65534:65534:nobody:/nonexistent:/sbin/nologin' \
        'nonroot:x:65532:65532:nonroot:/home/nonroot:/sbin/nologin' \
        'shimpzlocal:x:10001:10001::/nonexistent:/usr/sbin/nologin' > /rootfs/etc/passwd && \
    printf '%s\n' 'root:x:0:' 'nobody:x:65534:' 'tty:x:5:' 'staff:x:50:' 'nonroot:x:65532:' 'shimpzlocal:x:10001:' \
        'shimpzteam-token:x:10010:shimpzlocal' 'shimpzbrain-runtime-token:x:10016:shimpzlocal' \
        'shimpzassistant-egress-policy:x:10017:shimpzlocal' 'shimpzsupervisor-key:x:10021:shimpzlocal' \
        'shimpzaccount-egress-token:x:10022:shimpzlocal' > /rootfs/etc/group && \
    chmod 0644 /rootfs/etc/passwd /rootfs/etc/group && \
    chown -R 10001:10001 /rootfs/var/log/shimpz-local /rootfs/var/lib/shimpz-local && \
    chown 0:0 /rootfs/var/lib/shimpz-local /rootfs/var/lib/shimpz-local/assistant-integrations \
        /rootfs/var/lib/shimpz-local/assistant-stored-inputs /rootfs/var/lib/shimpz-local/chat-continuations \
        /rootfs/var/lib/shimpz-local/routines /rootfs/var/lib/shimpz-local/browser-sign-ins && \
    find /rootfs/var/log/shimpz-local /rootfs/var/lib/shimpz-local -mindepth 0 -type d -user 10001 -exec chmod 0700 {} + && \
    chown 10001:10017 /rootfs/var/lib/shimpz-local/assistant-egress && \
    chmod 0750 /rootfs/var/lib/shimpz-local/assistant-egress && \
    chown 10001:10010 /rootfs/run/shimpz-local && chmod 2750 /rootfs/run/shimpz-local && \
    chown 10001:10016 /rootfs/run/shimpz-brain-runtime && chmod 0750 /rootfs/run/shimpz-brain-runtime && \
    chown 0:10021 /rootfs/run/shimpz-local-supervisor && chmod 2770 /rootfs/run/shimpz-local-supervisor && \
    find /rootfs -depth -exec touch -h -d @0 {} +

# The workspace as the dependency build sees it: every manifest and lock file, and a stub in place of each library
# and binary root, so a source edit leaves the dependency layer unchanged. RUSTUP_TOOLCHAIN selects the image's own
# toolchain, which is the workspace's pinned one, so rustup never fetches the lint components rust-toolchain.toml
# names.
FROM docker.io/library/rust:1.99.0-trixie@sha256:6ff07edce8775d0f64be7aba9197229407301bddf2054d62c27b541a6238a181 AS rust-skeleton
WORKDIR /workspace
COPY . .
RUN find . -type f -regextype posix-extended ! -regex '.*/(Cargo\.toml|Cargo\.lock)' ! -regex '.*/src/(lib|main)\.rs' \
        -delete && \
    find . -depth -type d -empty -delete && \
    for root in $(find . -path '*/src/lib.rs' -o -path '*/src/main.rs'); do \
        printf '//! Dependency stub.\n' > "${root}"; \
        if test "${root##*/}" = main.rs; then printf 'fn main() {}\n' >> "${root}"; fi; \
    done

# The builder's Debian 13 (trixie) glibc matches the distroless cc-debian13 runtime. Every dependency of the
# controller is compiled here with the release profile, pinned by Cargo.lock.
FROM docker.io/library/rust:1.99.0-trixie@sha256:6ff07edce8775d0f64be7aba9197229407301bddf2054d62c27b541a6238a181 AS rust-dependencies
ENV RUSTUP_TOOLCHAIN=1.99.0
WORKDIR /workspace
COPY --from=rust-skeleton /workspace ./
RUN cargo build --release --locked --package team-local --bin shimpz-team

# The real sources replace the stubs; touching them makes every workspace crate newer than its stub's artifacts. No
# SOURCE_DATE_EPOCH reaches this stage's environment: build scripts track it, so it would rebuild every dependency.
FROM rust-dependencies AS rust-build
COPY . .
RUN find . -name '*.rs' -not -path './target/*' -exec touch {} + && \
    cargo build --release --locked --package team-local --bin shimpz-team

# Test only: the controller suite and its runner, each built on its own so neither widens the other's features.
FROM rust-build AS rust-fixture
RUN cargo build --release --locked --package team-local-fixture --bin shimpz-team-fixture && \
    cargo build --release --locked --package team-local-docker --bin shimpz-team-docker-suite

# The controller image: the distroless glibc base, the root-owned binaries, PDFium with its provenance, the pinned
# zone database, and the account and directory tree. No shell, interpreter, or package manager.
FROM gcr.io/distroless/cc-debian13@sha256:ed7c407fd64eb0af9dddb9456b94cee188a40a7f53cf38c9836e1e9ae14fca02 AS rust-runtime
COPY --link --from=rootfs /rootfs/ /
COPY --link --from=tzdata /usr/share/zoneinfo /usr/share/shimpz-team/zoneinfo
COPY --link --chown=0:0 --from=cosign --chmod=0555 /tmp/cosign /usr/local/bin/cosign
COPY --link --chown=0:0 --from=pdfium --chmod=0444 /tmp/pdfium/lib/libpdfium.so /usr/local/lib/libpdfium.so
COPY --link --chown=0:0 --chmod=0444 prepare/pdfium.toml /usr/local/lib/pdfium.toml
COPY --link --chown=0:0 --from=rust-build --chmod=0555 /workspace/target/release/shimpz-team /usr/local/bin/shimpz-team
ENV TZDIR=/usr/share/shimpz-team/zoneinfo
# The exact Browser image built with this Team image (ADR-0117 §9); the deploy and the publisher pass its digest.
ARG SHIMPZ_BROWSER_IMAGE=""
LABEL org.opencontainers.image.source="https://github.com/TheShimpz/shimpz-teams" \
      org.shimpz.component="team-local" \
      org.shimpz.local.profile="local-v1" \
      org.shimpz.local.browser-image="${SHIMPZ_BROWSER_IMAGE}"
USER 10001:10001
EXPOSE 7077
HEALTHCHECK --interval=30s --timeout=4s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["/usr/local/bin/shimpz-team", "healthcheck"]
ENTRYPOINT ["/usr/local/bin/shimpz-team"]
CMD ["serve"]

# Test only, never published: the controller image plus the suite and the runner the suite starts it with.
FROM rust-runtime AS fixture
COPY --link --chown=0:0 --from=rust-fixture --chmod=0555 /workspace/target/release/shimpz-team-fixture \
    /workspace/target/release/shimpz-team-docker-suite /usr/local/bin/

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
        /var/lib/shimpz-local/routines/state /var/lib/shimpz-local/routines/key \
        /var/lib/shimpz-local/browser-sign-ins/state /var/lib/shimpz-local/browser-sign-ins/key && \
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
        /var/lib/shimpz-local/routines/state /var/lib/shimpz-local/routines/key \
        /var/lib/shimpz-local/browser-sign-ins/state /var/lib/shimpz-local/browser-sign-ins/key && \
    chown shimpzlocal:shimpzlocal /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign && \
    chown shimpzlocal:shimpzassistant-egress-policy /var/lib/shimpz-local/assistant-egress && \
    chmod 0750 /var/lib/shimpz-local/assistant-egress && \
    chmod 0700 /var/log/shimpz-local /var/lib/shimpz-local/storage /var/lib/shimpz-local/inference \
        /var/lib/shimpz-local/action-journal \
        /var/lib/shimpz-local/assistant-integrations/state /var/lib/shimpz-local/assistant-integrations/key \
        /var/lib/shimpz-local/assistant-stored-inputs/state /var/lib/shimpz-local/assistant-stored-inputs/key \
        /var/lib/shimpz-local/chat-continuations/state /var/lib/shimpz-local/chat-continuations/key \
        /var/lib/shimpz-local/routines/state /var/lib/shimpz-local/routines/key \
        /var/lib/shimpz-local/browser-sign-ins/state /var/lib/shimpz-local/browser-sign-ins/key \
        /var/lib/shimpz-local/publications /var/lib/shimpz-local/cosign

WORKDIR /app

COPY --link --chmod=0555 local/activity.py local/healthcheck.py ./local/
COPY --link local/__init__.py local/app.py local/audit.py local/authority.py local/composition.py local/errors.py \
    local/inference.py local/labels.py local/lifecycle.py local/names.py local/prepare.py \
    local/validation.py ./local/
COPY --link local/install/__init__.py local/install/admission.py local/install/automatic.py \
    local/install/collector.py local/install/developers.py local/install/inventory.py local/install/preview.py \
    local/install/registry.py local/install/runtime.py local/install/service.py local/install/snapshots.py \
    local/install/source_package.py ./local/install/
COPY --link assistant/__init__.py assistant/action_schema.py assistant/cache.py assistant/details.py \
    assistant/effect.py assistant/genesis.py assistant/language.py assistant/manifest.py assistant/spec.py \
    ./assistant/
COPY --link chat/__init__.py chat/attachments.py chat/contract.py chat/orchestrator.py chat/progress.py \
    chat/turn.py ./chat/
COPY --link core/__init__.py core/base64url.py core/canonical_json.py core/identifier.py core/instant.py core/text.py ./core/
COPY --link core/http/__init__.py core/http/stdlib.py core/http/strict.py ./core/http/
COPY --link egress/__init__.py egress/policy.py ./egress/
COPY --link inference/__init__.py inference/abort.py inference/browser.py inference/client.py inference/config.py inference/errors.py \
    inference/credential.py inference/memory.py inference/model_catalog.json \
    inference/token.py inference/usage.py ./inference/
COPY --link integrations/__init__.py integrations/account_egress.py integrations/broker.py integrations/challenge_store.py \
    integrations/challenges.py integrations/flow.py integrations/http.py \
    integrations/pkce.py integrations/providers.py \
    integrations/service.py integrations/store.py ./integrations/
COPY --link local/assistant/__init__.py local/assistant/api.py local/assistant/egress.py \
    local/assistant/isolation.py local/assistant/lifecycle.py local/assistant/resources.py \
    local/assistant/rpc.py ./local/assistant/
COPY --link local/chat/__init__.py local/chat/api.py local/chat/attachments.py local/chat/capabilities.py \
    local/chat/continuation.py \
    local/chat/continuation_store.py local/chat/execution.py local/chat/human.py local/chat/limit.py \
    local/chat/memory.py local/chat/pause.py \
    local/chat/private.py local/chat/resume.py local/chat/segment.py local/chat/service.py \
    local/chat/state.py local/chat/types.py ./local/chat/
COPY --link local/routine/__init__.py local/routine/card.py local/routine/replay.py local/routine/contracts.py \
    local/routine/diagnostics.py local/routine/human.py local/routine/incident.py local/routine/lifecycle.py \
    local/routine/manage.py local/routine/notices.py local/routine/proposal.py local/routine/protection.py \
    local/routine/recorder.py local/routine/recovery.py local/routine/refine.py local/routine/run.py \
    local/routine/state.py local/routine/store.py local/routine/watchdog.py ./local/routine/
COPY --link routine/__init__.py routine/claim.py routine/compose.py routine/cursor.py routine/dates.py \
    routine/definition.py routine/hold.py routine/pin.py routine/plan.py routine/provenance.py routine/record.py \
    routine/recording.py routine/reference.py routine/refine.py routine/rerun.py routine/runs.py routine/schedule.py \
    routine/starts.py routine/trace.py ./routine/
COPY --link local/http/__init__.py local/http/audit.py local/http/browser.py local/http/chat.py local/http/deadline.py \
    local/http/dispatch.py local/http/inference.py local/http/routine.py local/http/server.py local/http/stream.py local/http/token.py ./local/http/
COPY --link install/__init__.py install/artifact_trust.py install/bindings.py install/contract.py install/icons.py install/lock.py \
    install/update.py ./install/
COPY --link protocol/install/v1/definitions.schema.json protocol/install/v1/resolve-response.schema.json \
    ./protocol/install/v1/
COPY --link protocol/action/v1/pattern.py protocol/action/v1/schema.py ./protocol/action/v1/
COPY --link protocol/http/v1/challenge.py protocol/http/v1/content.py protocol/http/v1/identifiers.py \
    protocol/http/v1/payload.py protocol/http/v1/phrase.py protocol/http/v1/preview.py protocol/http/v1/progress.py \
    protocol/http/v1/purpose.py protocol/http/v1/routine.py protocol/http/v1/routine_context.py protocol/http/v1/routine_notice.py \
    protocol/http/v1/routine_proposal.py protocol/http/v1/routine_run.py protocol/http/v1/strict_json.py \
    protocol/http/v1/supervisor.py protocol/http/v1/turn.py protocol/http/v1/browser.py protocol/http/v1/browser_view.py \
    protocol/http/v1/sign_in.py protocol/http/v1/websocket.py ./protocol/http/v1/
COPY --link protocol/assistant/v1/validators/action_effect.py protocol/assistant/v1/validators/failure.py \
    protocol/assistant/v1/validators/human_request.py protocol/assistant/v1/validators/input_file.py \
    protocol/assistant/v1/validators/message_catalog.py protocol/assistant/v1/validators/route.py \
    ./protocol/assistant/v1/validators/
COPY --link action/__init__.py action/challenges.py action/dispatch.py action/execution.py action/failure.py \
    action/frames.py action/files.py action/human.py action/journal.py action/provider.py action/result.py \
    action/stored_input.py ./action/
COPY --link storage/__init__.py storage/files.py storage/private_state.py ./storage/
COPY --link browser/__init__.py browser/cua.py browser/ingest.py browser/read.py browser/screen.py browser/task.py \
    browser/thread.py ./browser/
# The seccomp profile Team gives every Browser container (ADR-0117 §3.2).
COPY --link browser/image/seccomp.json ./browser/image/
COPY --link local/browser/__init__.py local/browser/capsule.py local/browser/channel.py local/browser/credentials.py \
    local/browser/release.py local/browser/runtime.py local/browser/service.py local/browser/sign_in.py \
    local/browser/sign_ins.py local/browser/tunnel.py local/browser/view.py ./local/browser/
COPY --link prepare/__init__.py prepare/detect.py prepare/helper.py prepare/image.py prepare/limits.py prepare/page.py \
    prepare/pdf.py prepare/service.py prepare/worker.py ./prepare/
# The application is compiled like its environment, after its last copy.
RUN PYTHONDONTWRITEBYTECODE=1 /opt/venv/bin/python -m compileall -q -f --invalidation-mode checked-hash /app

# The exact Browser image built with this Team image (ADR-0117 §9); the deploy and the publisher pass its digest.
ARG SHIMPZ_BROWSER_IMAGE=""
LABEL org.opencontainers.image.source="https://github.com/TheShimpz/shimpz-teams" \
      org.shimpz.component="team-local" \
      org.shimpz.local.profile="local-v1" \
      org.shimpz.local.browser-image="${SHIMPZ_BROWSER_IMAGE}"

USER 10001:10001
EXPOSE 7077
HEALTHCHECK --interval=30s --timeout=4s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["/opt/venv/bin/python", "-m", "local.healthcheck"]
ENTRYPOINT ["/opt/venv/bin/python", "-m", "local.app"]
