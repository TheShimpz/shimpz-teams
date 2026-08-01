# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY tests/fixtures/reference-assistant/shimpz.toml /opt/shimpz/shimpz.toml
COPY tests/fixtures/reference-assistant/shimpz-power /usr/local/bin/shimpz-power
COPY tests/fixtures/reference-assistant/shimpz.contract.json /opt/shimpz/shimpz.contract.json
RUN groupadd --gid 10001 assistant && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin assistant && \
    chmod 0444 /opt/shimpz/shimpz.toml /opt/shimpz/shimpz.contract.json && \
    chmod 0555 /usr/local/bin/shimpz-power

LABEL org.shimpz.assistant.id="shimpz-cloudflare"
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python3", "-c", "import signal; signal.pause()"]
