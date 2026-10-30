# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# The controller suite's reference Assistant: a deterministic Action executable under the published runtime layout.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY shimpz.toml /opt/shimpz/shimpz.toml
COPY shimpz-action /usr/local/bin/shimpz-action
COPY shimpz.contract.json /opt/shimpz/shimpz.contract.json
COPY shimpz.pack.json /opt/shimpz/shimpz.pack.json
RUN groupadd --gid 10001 assistant && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin assistant && \
    chmod 0444 /opt/shimpz/shimpz.toml /opt/shimpz/shimpz.contract.json /opt/shimpz/shimpz.pack.json && \
    chmod 0555 /usr/local/bin/shimpz-action

LABEL org.shimpz.assistant.id="shimpz-cloudflare"
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python3", "-c", "import signal; signal.pause()"]
