# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# The Team Browser (ADR-0117): Debian-security Chromium under Xvfb with the accessibility bus, driven only through the
# pinned Cua Driver that Team reaches over `docker exec`. Team creates one container per Browser task and removes it
# when the task ends; every writable path is a noexec tmpfs it mounts, and the root is read-only.

# The in-image program (launcher, MCP entry, readiness probe, downloads tool) uses the standard library only.
FROM docker.io/library/rust:1.99.0-trixie@sha256:6ff07edce8775d0f64be7aba9197229407301bddf2054d62c27b541a6238a181 AS host
WORKDIR /src
COPY host/Cargo.toml host/Cargo.lock ./
COPY host/src ./src
RUN cargo build --release --locked --offline && install -m 0555 target/release/browser-host /browser-host

# The exact Cua Driver release, checked against its published SHA-256 and never fetched at run time.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS cua
ARG TARGETARCH
ADD --checksum=sha256:05dae2160c86bdf4855c279d30898213d7a150edb1992b7470afdd69f0df4064 \
    https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.36.0/cua-driver-rs-0.36.0-linux-x86_64-binary.tar.gz \
    /tmp/cua.tar.gz
RUN test "${TARGETARCH}" = amd64 && mkdir /cua && tar -xzf /tmp/cua.tar.gz -C /cua cua-driver && chmod 0555 /cua/cua-driver

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS runtime
# Every package comes from one fixed Debian snapshot, so a rebuild installs the same Chromium; a newer Chromium enters
# only by moving this date in a reviewed change (ADR-0117 §9). APT checks the snapshot's signed release files.
ARG DEBIAN_SNAPSHOT=20261017T000000Z
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/%s/%s %s main\n' \
        debian "${DEBIAN_SNAPSHOT}" trixie \
        debian "${DEBIAN_SNAPSHOT}" trixie-updates \
        debian-security "${DEBIAN_SNAPSHOT}" trixie-security > /etc/apt/sources.list && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        chromium xvfb at-spi2-core dbus-daemon ca-certificates fonts-liberation fonts-noto-core \
        libx11-6 libxi6 libxkbcommon0 libxext6 && \
    groupadd --gid 10031 shimpz-browser && \
    useradd --uid 10031 --gid 10031 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin shimpz-browser && \
    mkdir -p /etc/chromium/policies/managed /profile /downloads && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt /var/cache/debconf /var/log/* /etc/apt/sources.list && \
    # Only /bin/sh stays, because Xvfb runs xkbcomp through it to compile the keyboard map; no other shell, package
    # manager, or language interpreter remains.
    rm -f /bin/bash /usr/bin/bash /usr/bin/rbash /usr/bin/bashbug /usr/bin/perl /usr/bin/perl5* \
        /usr/bin/apt /usr/bin/apt-* /usr/bin/dpkg /usr/bin/dpkg-* /usr/bin/python3* && \
    test -x /bin/sh && for removed in bash perl dpkg apt-get python3; do test ! -e "/usr/bin/${removed}" || exit 1; done
COPY --chmod=0444 policy.json /etc/chromium/policies/managed/shimpz.json
COPY --from=host /browser-host /usr/local/bin/browser-host
COPY --from=cua /cua/cua-driver /opt/cua/cua-driver
USER 10031:10031
ENTRYPOINT ["/usr/local/bin/browser-host"]
CMD ["launch"]
