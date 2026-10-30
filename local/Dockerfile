# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# check=skip=SecretsUsedInArgOrEnv ; the Cloudflare Assistant image is a public OCI digest, not a credential
#
# The Team controller (ADR-0123): the `shimpz-team` binary on the distroless glibc base. Targets: `runtime` (the last,
# and the default) is the controller image; `fixture` adds the controller suite, its test-only runner, and the live
# artifact-trust probe and is never published.
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
    echo "${cosign_sha256}  /tmp/cosign" | sha256sum -c - && \
    chmod 0555 /tmp/cosign

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
    echo "$(pin library_sha256)  /tmp/pdfium/lib/libpdfium.so" | sha256sum -c - && \
    cp /tmp/pdfium.toml /tmp/pdfium/lib/pdfium.toml && \
    chmod 0444 /tmp/pdfium/lib/libpdfium.so /tmp/pdfium/lib/pdfium.toml

# The zone database the Routine phrase tables were generated from (2026c), taken whole from the pinned base that
# carries it; the controller reads it through TZDIR.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS tzdata
RUN test "$(head -n 1 /usr/share/zoneinfo/tzdata.zi)" = "# version 2026c"

# Every account, group, and runtime directory the controller and its Compose service rely on, at its exact owner and
# mode; the runtime copies the tree as it is. The groups are named because the controller resolves them by name, and
# the base's own accounts are kept.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS rootfs
RUN mkdir -p /rootfs/etc /rootfs/run/shimpz-local /rootfs/run/shimpz-brain-runtime \
        /rootfs/run/shimpz-local-supervisor /rootfs/var/lib/shimpz-local/assistant-egress \
        /rootfs/var/log/shimpz-local /rootfs/var/lib/shimpz-local/storage /rootfs/var/lib/shimpz-local/inference \
        /rootfs/var/lib/shimpz-local/action-journal /rootfs/var/lib/shimpz-local/publications \
        /rootfs/var/lib/shimpz-local/cosign /rootfs/var/lib/shimpz-local/assistant-integrations/state \
        /rootfs/var/lib/shimpz-local/assistant-integrations/key \
        /rootfs/var/lib/shimpz-local/assistant-stored-inputs/state \
        /rootfs/var/lib/shimpz-local/assistant-stored-inputs/key \
        /rootfs/var/lib/shimpz-local/chat-continuations/state /rootfs/var/lib/shimpz-local/chat-continuations/key \
        /rootfs/var/lib/shimpz-local/routines/state /rootfs/var/lib/shimpz-local/routines/key \
        /rootfs/var/lib/shimpz-local/browser-sign-ins/state /rootfs/var/lib/shimpz-local/browser-sign-ins/key && \
    printf '%s\n' 'root:x:0:0:root:/root:/sbin/nologin' 'nobody:x:65534:65534:nobody:/nonexistent:/sbin/nologin' \
        'nonroot:x:65532:65532:nonroot:/home/nonroot:/sbin/nologin' \
        'shimpzlocal:x:10001:10001::/nonexistent:/usr/sbin/nologin' > /rootfs/etc/passwd && \
    printf '%s\n' 'root:x:0:' 'nobody:x:65534:' 'tty:x:5:' 'staff:x:50:' 'nonroot:x:65532:' 'shimpzlocal:x:10001:' \
        'shimpzteam-token:x:10010:shimpzlocal' 'shimpzbrain-runtime-token:x:10016:shimpzlocal' \
        'shimpzassistant-egress-policy:x:10017:shimpzlocal' 'shimpzsupervisor-key:x:10021:shimpzlocal' \
        'shimpzaccount-egress-token:x:10022:shimpzlocal' > /rootfs/etc/group && \
    chmod 0644 /rootfs/etc/passwd /rootfs/etc/group && \
    chown 10001:10001 /rootfs/var/log/shimpz-local /rootfs/var/lib/shimpz-local/storage \
        /rootfs/var/lib/shimpz-local/inference /rootfs/var/lib/shimpz-local/action-journal \
        /rootfs/var/lib/shimpz-local/publications /rootfs/var/lib/shimpz-local/cosign \
        /rootfs/var/lib/shimpz-local/assistant-integrations/state \
        /rootfs/var/lib/shimpz-local/assistant-integrations/key \
        /rootfs/var/lib/shimpz-local/assistant-stored-inputs/state \
        /rootfs/var/lib/shimpz-local/assistant-stored-inputs/key \
        /rootfs/var/lib/shimpz-local/chat-continuations/state /rootfs/var/lib/shimpz-local/chat-continuations/key \
        /rootfs/var/lib/shimpz-local/routines/state /rootfs/var/lib/shimpz-local/routines/key \
        /rootfs/var/lib/shimpz-local/browser-sign-ins/state /rootfs/var/lib/shimpz-local/browser-sign-ins/key && \
    chmod 0700 /rootfs/var/log/shimpz-local /rootfs/var/lib/shimpz-local/storage \
        /rootfs/var/lib/shimpz-local/inference /rootfs/var/lib/shimpz-local/action-journal \
        /rootfs/var/lib/shimpz-local/publications /rootfs/var/lib/shimpz-local/cosign \
        /rootfs/var/lib/shimpz-local/assistant-integrations/state \
        /rootfs/var/lib/shimpz-local/assistant-integrations/key \
        /rootfs/var/lib/shimpz-local/assistant-stored-inputs/state \
        /rootfs/var/lib/shimpz-local/assistant-stored-inputs/key \
        /rootfs/var/lib/shimpz-local/chat-continuations/state /rootfs/var/lib/shimpz-local/chat-continuations/key \
        /rootfs/var/lib/shimpz-local/routines/state /rootfs/var/lib/shimpz-local/routines/key \
        /rootfs/var/lib/shimpz-local/browser-sign-ins/state /rootfs/var/lib/shimpz-local/browser-sign-ins/key && \
    chown 10001:10017 /rootfs/var/lib/shimpz-local/assistant-egress && \
    chmod 0750 /rootfs/var/lib/shimpz-local/assistant-egress && \
    chown 10001:10010 /rootfs/run/shimpz-local && \
    chmod 2750 /rootfs/run/shimpz-local && \
    chown 10001:10016 /rootfs/run/shimpz-brain-runtime && \
    chmod 0750 /rootfs/run/shimpz-brain-runtime && \
    chown 0:10021 /rootfs/run/shimpz-local-supervisor && \
    chmod 2770 /rootfs/run/shimpz-local-supervisor && \
    find /rootfs -depth -exec touch -h -d @0 {} +

# The workspace as the dependency build sees it: every manifest and lock file, and a stub in place of each library
# and binary root and of every other target file a manifest may name (tests, benches, examples), so a source edit leaves the dependency layer unchanged. RUSTUP_TOOLCHAIN selects the image's own
# toolchain, which is the workspace's pinned one, so rustup never fetches the lint components rust-toolchain.toml
# names.
FROM docker.io/library/rust:1.99.0-trixie@sha256:6ff07edce8775d0f64be7aba9197229407301bddf2054d62c27b541a6238a181 AS skeleton
WORKDIR /workspace
COPY . .
RUN find . -type f -regextype posix-extended ! -regex '.*/(Cargo\.toml|Cargo\.lock)' ! -name '*.rs' -delete && \
    find . -type f -regextype posix-extended -name '*.rs' -path '*/src/*' ! -regex '.*/src/(lib|main)\.rs' -delete && \
    find . -depth -type d -empty -delete && \
    for root in $(find . -name '*.rs'); do \
        printf '//! Dependency stub.\n' > "${root}"; \
        if test "${root##*/}" != lib.rs; then printf 'fn main() {}\n' >> "${root}"; fi; \
    done

# The builder's Debian 13 (trixie) glibc matches the distroless cc-debian13 runtime. Every dependency of the
# controller is compiled here with the release profile, pinned by Cargo.lock.
FROM docker.io/library/rust:1.99.0-trixie@sha256:6ff07edce8775d0f64be7aba9197229407301bddf2054d62c27b541a6238a181 AS dependencies
ENV RUSTUP_TOOLCHAIN=1.99.0
WORKDIR /workspace
COPY --from=skeleton /workspace ./
RUN cargo build --release --locked --package team-local --bin shimpz-team

# The real sources replace the stubs; touching them makes every workspace crate newer than its stub's artifacts. No
# SOURCE_DATE_EPOCH reaches this stage's environment: build scripts track it, so it would rebuild every dependency.
FROM dependencies AS build
COPY . .
RUN find . -name '*.rs' -not -path './target/*' -exec touch {} + && \
    cargo build --release --locked --package team-local --bin shimpz-team && \
    chmod 0555 target/release/shimpz-team

# Test only: the controller suite, its runner, and the live artifact-trust probe, each built on its own so none widens
# another's features.
FROM build AS suite
RUN cargo build --release --locked --package team-local-fixture --bin shimpz-team-fixture && \
    cargo build --release --locked --package team-local-docker --bin shimpz-team-docker-suite && \
    cargo build --release --locked --package team-local-trust --bin shimpz-team-trust-probe && \
    chmod 0555 target/release/shimpz-team-fixture target/release/shimpz-team-docker-suite \
        target/release/shimpz-team-trust-probe

# The controller image: the distroless glibc base, the root-owned binaries, PDFium with its provenance, the pinned
# zone database, and the account and directory tree. No shell, interpreter, or package manager.
FROM gcr.io/distroless/cc-debian13@sha256:ed7c407fd64eb0af9dddb9456b94cee188a40a7f53cf38c9836e1e9ae14fca02 AS controller
COPY --link --from=rootfs /rootfs/ /
COPY --link --from=tzdata /usr/share/zoneinfo /usr/share/shimpz-team/zoneinfo
# Each file's mode is set where it is made: a COPY --chmod would also give the directories it creates that mode.
COPY --link --chown=0:0 --from=cosign /tmp/cosign /usr/local/bin/cosign
COPY --link --chown=0:0 --from=pdfium /tmp/pdfium/lib/libpdfium.so /tmp/pdfium/lib/pdfium.toml /usr/local/lib/
COPY --link --chown=0:0 --from=build /workspace/target/release/shimpz-team /usr/local/bin/shimpz-team
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

# Test only, never published: the controller image plus the suite, the runner the suite starts it with, and the
# live artifact-trust probe.
FROM controller AS fixture
COPY --link --chown=0:0 --from=suite /workspace/target/release/shimpz-team-fixture \
    /workspace/target/release/shimpz-team-docker-suite /workspace/target/release/shimpz-team-trust-probe \
    /usr/local/bin/

# The published controller image, the default target.
FROM controller AS runtime
